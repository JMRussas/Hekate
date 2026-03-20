#  MCP Server Spawner
#
#  Lightweight stdio MCP client for Odin to spawn and manage ephemeral
#  MCP server subprocesses during task execution.
#
#  Depends on: nothing (standalone)
#  Used by:    odin_tools.py

import asyncio
import json
import logging

logger = logging.getLogger("orchestration.mcp_spawner")


class McpSession:
    """A single MCP server subprocess session via stdio.

    Supports both request/response and server-initiated notifications.
    Register handlers via on_notification() to react to events like
    tools/list_changed, resources/updated, logging/message, or custom
    god-specific notifications.
    """

    def __init__(self, proc: asyncio.subprocess.Process, server_name: str):
        self._proc = proc
        self._name = server_name
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._capabilities: dict = {}
        self._tools: list[dict] = []
        # Notification handlers: method -> list of async callbacks
        self._notification_handlers: dict[str, list] = {}
        # Wildcard handlers — called for every notification
        self._wildcard_handlers: list = []

    @property
    def tools(self) -> list[dict]:
        return self._tools

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> dict:
        return self._capabilities

    def on_notification(self, method: str, handler):
        """Register an async callback for a specific notification method.

        The handler receives (method: str, params: dict).
        Use method="*" for a wildcard that fires on every notification.

        Example:
            session.on_notification("notifications/tools/list_changed", my_handler)
            session.on_notification("*", log_all_notifications)
        """
        if method == "*":
            self._wildcard_handlers.append(handler)
        else:
            self._notification_handlers.setdefault(method, []).append(handler)

    async def _dispatch_notification(self, method: str, params: dict):
        """Route a server-initiated notification to registered handlers."""
        # Specific handlers first
        for handler in self._notification_handlers.get(method, []):
            try:
                await handler(method, params)
            except Exception as e:
                logger.warning(
                    "Notification handler error for '%s' on '%s': %s",
                    method, self._name, e,
                )
        # Wildcard handlers
        for handler in self._wildcard_handlers:
            try:
                await handler(method, params)
            except Exception as e:
                logger.warning(
                    "Wildcard notification handler error on '%s': %s",
                    self._name, e,
                )

    async def _handle_tools_changed(self, method: str, params: dict):
        """Built-in handler: refresh tool list when server signals changes."""
        try:
            tools_resp = await self._request("tools/list", {}, timeout=10.0)
            old_count = len(self._tools)
            self._tools = tools_resp.get("tools", [])
            logger.info(
                "MCP session '%s' tools refreshed: %d -> %d",
                self._name, old_count, len(self._tools),
            )
        except Exception as e:
            logger.warning(
                "Failed to refresh tools for '%s' after list_changed: %s",
                self._name, e,
            )

    async def _handle_log_message(self, method: str, params: dict):
        """Built-in handler: forward server log messages to Python logging."""
        level = params.get("level", "info")
        data = params.get("data", "")
        msg = f"[{self._name}] {data}"
        if level == "error":
            logger.error(msg)
        elif level == "warning":
            logger.warning(msg)
        elif level == "debug":
            logger.debug(msg)
        else:
            logger.info(msg)

    async def initialize(self, timeout: float = 10.0):
        """Send MCP initialize request and read response."""
        self._reader_task = asyncio.create_task(self._read_loop())

        resp = await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "odin", "version": "1.0"},
        }, timeout=timeout)

        self._capabilities = resp.get("capabilities", {})

        # Send initialized notification
        await self._notify("notifications/initialized", {})

        # List tools if supported
        if self._capabilities.get("tools"):
            tools_resp = await self._request("tools/list", {}, timeout=timeout)
            self._tools = tools_resp.get("tools", [])

        # Register built-in notification handlers
        self.on_notification(
            "notifications/tools/list_changed", self._handle_tools_changed,
        )
        self.on_notification(
            "notifications/resources/list_changed", self._handle_tools_changed,
        )
        self.on_notification("notifications/message", self._handle_log_message)

        logger.info(
            "MCP session '%s' initialized (%d tools, notifications enabled)",
            self._name, len(self._tools),
        )
        return self._capabilities

    async def call_tool(
        self, name: str, arguments: dict, timeout: float = 60.0
    ) -> str:
        """Call a tool on the MCP server."""
        resp = await self._request("tools/call", {
            "name": name,
            "arguments": arguments,
        }, timeout=timeout)

        # Extract text from content blocks
        content = resp.get("content", [])
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts) if parts else json.dumps(resp)

    async def close(self):
        """Terminate the subprocess and clean up."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
        logger.info("MCP session '%s' closed", self._name)

    async def _request(
        self, method: str, params: dict, timeout: float = 10.0
    ) -> dict:
        self._request_id += 1
        rid = self._request_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = future

        data = json.dumps(msg) + "\n"
        self._proc.stdin.write(data.encode())
        await self._proc.stdin.drain()

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(
                f"MCP request '{method}' timed out after {timeout}s"
            )

        if "error" in result:
            raise RuntimeError(f"MCP error: {result['error']}")
        return result.get("result", {})

    async def _notify(self, method: str, params: dict):
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        data = json.dumps(msg) + "\n"
        self._proc.stdin.write(data.encode())
        await self._proc.stdin.drain()

    async def _read_loop(self):
        """Read JSON-RPC messages from stdout.

        Handles both:
        - Responses (have 'id') — resolve pending request futures
        - Notifications (no 'id', have 'method') — dispatch to handlers
        """
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rid = msg.get("id")
                if rid is not None and rid in self._pending:
                    # Response to a request we sent
                    self._pending.pop(rid).set_result(msg)
                elif "method" in msg and rid is None:
                    # Server-initiated notification
                    method = msg["method"]
                    params = msg.get("params", {})
                    asyncio.create_task(
                        self._dispatch_notification(method, params)
                    )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("MCP reader error for '%s': %s", self._name, e)


class McpSpawner:
    """Manages ephemeral MCP server subprocesses for Odin."""

    def __init__(self):
        self._sessions: dict[str, McpSession] = {}

    async def spawn(
        self,
        name: str,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float = 10.0,
    ) -> McpSession:
        """Spawn an MCP server subprocess and initialize the session."""
        if name in self._sessions:
            await self._sessions[name].close()

        import os
        full_env = {**os.environ, **(env or {})}

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=full_env,
        )

        session = McpSession(proc, name)
        await session.initialize(timeout=timeout)
        self._sessions[name] = session
        return session

    async def get_session(self, name: str) -> McpSession | None:
        return self._sessions.get(name)

    async def close_session(self, name: str):
        session = self._sessions.pop(name, None)
        if session:
            await session.close()

    async def close_all(self):
        for name in list(self._sessions):
            await self.close_session(name)
        logger.info("All MCP sessions closed")

    @property
    def active_sessions(self) -> list[str]:
        return list(self._sessions.keys())
