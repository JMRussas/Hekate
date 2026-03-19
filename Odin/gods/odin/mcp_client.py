"""MCP Client Manager for Odin.

Spawns and manages stdio MCP server subprocesses. Odin uses this to
dynamically discover and call tools from any registered MCP server
during task execution.

Architecture:
  - MCPServerRegistry: knows what MCP servers are available (from .mcp.json
    files, god.json mcp_servers entries, or programmatic registration)
  - MCPServerHandle: a running MCP server with an active ClientSession
  - MCPClientManager: lifecycle manager — spawn, discover, call, teardown,
    idle reap
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, Implementation
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, TextContent, Tool

logger = logging.getLogger("odin.mcp_client")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class MCPServerConfig:
    """Configuration for a stdio MCP server that Odin can spawn."""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Handle — a running MCP server
# ---------------------------------------------------------------------------

@dataclass
class MCPServerHandle:
    """A running MCP server process with an active session."""
    name: str
    session: ClientSession
    tools: list[Tool]
    spawned_at: float
    last_used_at: float
    _exit_stack: AsyncExitStack

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]

    def touch(self):
        self.last_used_at = time.monotonic()


# ---------------------------------------------------------------------------
# Registry — what MCP servers are available
# ---------------------------------------------------------------------------

class MCPServerRegistry:
    """Registry of available MCP servers, loaded from config files."""

    def __init__(self):
        self._servers: dict[str, MCPServerConfig] = {}

    def register(self, config: MCPServerConfig):
        self._servers[config.name] = config
        logger.info(
            "Registered MCP server: %s (%s %s)",
            config.name, config.command, " ".join(config.args),
        )

    def unregister(self, name: str):
        self._servers.pop(name, None)

    def get(self, name: str) -> MCPServerConfig | None:
        return self._servers.get(name)

    def all(self) -> dict[str, MCPServerConfig]:
        return dict(self._servers)

    def names(self) -> list[str]:
        return list(self._servers.keys())

    def load_from_mcp_json(self, path: str | Path):
        """Load server configs from a .mcp.json file.

        Resolves relative args paths against the config file's directory.
        """
        path = Path(path)
        if not path.exists():
            logger.warning("MCP config not found: %s", path)
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read MCP config %s: %s", path, e)
            return

        config_dir = path.parent
        servers = data.get("mcpServers", {})
        for name, cfg in servers.items():
            command = cfg.get("command", "")
            args = cfg.get("args", [])
            env = cfg.get("env") or None
            cwd = cfg.get("cwd")

            # Resolve relative paths in args against the config directory
            resolved_args = []
            for arg in args:
                candidate = config_dir / arg
                if candidate.exists():
                    resolved_args.append(str(candidate.resolve()))
                else:
                    resolved_args.append(arg)

            self.register(MCPServerConfig(
                name=name,
                command=command,
                args=resolved_args,
                env=env,
                cwd=cwd or str(config_dir),
            ))

    def load_from_gods_dir(self, gods_dir: str | Path):
        """Load MCP server configs from god.json files with mcp_servers field."""
        gods_dir = Path(gods_dir)
        if not gods_dir.exists():
            return
        for god_json in gods_dir.glob("*/god.json"):
            try:
                data = json.loads(god_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for srv in data.get("mcp_servers", []):
                name = srv.get("name")
                if not name:
                    continue
                self.register(MCPServerConfig(
                    name=name,
                    command=srv.get("command", "python"),
                    args=srv.get("args", []),
                    env=srv.get("env"),
                    cwd=srv.get("cwd", str(god_json.parent)),
                    tags=srv.get("tags", []),
                ))


# ---------------------------------------------------------------------------
# Client Manager — spawn, call, teardown
# ---------------------------------------------------------------------------

class MCPClientManager:
    """Manages spawning and lifecycle of stdio MCP server subprocesses.

    Odin uses this to:
      - Spawn MCP servers on demand for task execution
      - Discover tools provided by each server
      - Call tools across servers during multi-tool tasks
      - Tear down servers when tasks complete or idle timeout is exceeded
    """

    def __init__(
        self,
        registry: MCPServerRegistry,
        idle_timeout: float = 300.0,
        reap_interval: float = 30.0,
    ):
        self._registry = registry
        self._handles: dict[str, MCPServerHandle] = {}
        self._idle_timeout = idle_timeout
        self._reap_interval = reap_interval
        self._reaper_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def registry(self) -> MCPServerRegistry:
        return self._registry

    @property
    def active_servers(self) -> list[str]:
        return list(self._handles.keys())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start the idle reaper background task."""
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(
                self._idle_reaper(), name="odin-mcp-reaper"
            )
            logger.info(
                "MCP client manager started (idle_timeout=%.0fs, servers=%s)",
                self._idle_timeout, self._registry.names(),
            )

    async def stop(self):
        """Stop the reaper and tear down all servers."""
        if self._reaper_task:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None
        await self.teardown_all()
        logger.info("MCP client manager stopped")

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    async def spawn(self, server_name: str) -> MCPServerHandle:
        """Spawn an MCP server or return existing handle if already running."""
        async with self._lock:
            if server_name in self._handles:
                handle = self._handles[server_name]
                handle.touch()
                return handle

            config = self._registry.get(server_name)
            if config is None:
                raise ValueError(
                    f"Unknown MCP server: {server_name}. "
                    f"Available: {self._registry.names()}"
                )
            return await self._spawn_locked(config)

    async def _spawn_locked(self, config: MCPServerConfig) -> MCPServerHandle:
        """Spawn process, initialize session, discover tools. Caller holds _lock."""
        logger.info(
            "Spawning MCP server: %s (%s %s)",
            config.name, config.command, " ".join(config.args),
        )

        params = StdioServerParameters(
            command=config.command,
            args=config.args,
            env=config.env,
            cwd=config.cwd,
        )

        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(params)
            )
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    client_info=Implementation(name="odin", version="0.1.0"),
                )
            )

            # Initialize handshake
            init_result = await session.initialize()
            logger.info(
                "MCP server %s initialized (protocol=%s, server=%s/%s)",
                config.name,
                init_result.protocolVersion,
                init_result.serverInfo.name,
                init_result.serverInfo.version,
            )

            # Discover tools
            tools_result = await session.list_tools()
            tools = tools_result.tools
            logger.info(
                "MCP server %s provides %d tools: %s",
                config.name, len(tools), [t.name for t in tools],
            )

            now = time.monotonic()
            handle = MCPServerHandle(
                name=config.name,
                session=session,
                tools=tools,
                spawned_at=now,
                last_used_at=now,
                _exit_stack=stack,
            )
            self._handles[config.name] = handle
            return handle

        except Exception:
            await stack.aclose()
            raise

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    async def list_tools(self, server_name: str) -> list[Tool]:
        """List tools from a server (spawns it if not running)."""
        handle = await self.spawn(server_name)
        handle.touch()
        return list(handle.tools)

    async def list_all_tools(self) -> dict[str, list[Tool]]:
        """List tools from all registered servers (spawns each)."""
        result: dict[str, list[Tool]] = {}
        for name in self._registry.names():
            try:
                result[name] = await self.list_tools(name)
            except Exception as e:
                logger.warning("Failed to list tools from %s: %s", name, e)
        return result

    def get_cached_tools(self, server_name: str) -> list[Tool] | None:
        """Get tools from cache without spawning. Returns None if not running."""
        handle = self._handles.get(server_name)
        return list(handle.tools) if handle else None

    # ------------------------------------------------------------------
    # Tool calling
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Call a tool on a server (spawns it if not running)."""
        handle = await self.spawn(server_name)
        handle.touch()
        logger.debug(
            "Calling %s.%s with args=%s", server_name, tool_name, arguments
        )
        try:
            result = await handle.session.call_tool(tool_name, arguments)
            logger.debug(
                "Tool %s.%s returned (isError=%s)",
                server_name, tool_name, result.isError,
            )
            return result
        except Exception as e:
            logger.error("Tool call %s.%s failed: %s", server_name, tool_name, e)
            # Server may have crashed — tear it down so next call respawns
            await self._teardown_handle(server_name)
            raise

    async def call_tool_text(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Call a tool and return its text content as a string."""
        result = await self.call_tool(server_name, tool_name, arguments)
        return extract_text(result)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def teardown(self, server_name: str):
        """Tear down a specific MCP server."""
        async with self._lock:
            await self._teardown_handle(server_name)

    async def _teardown_handle(self, server_name: str):
        """Tear down a handle. Does not require _lock (caller manages)."""
        handle = self._handles.pop(server_name, None)
        if handle is None:
            return
        logger.info(
            "Tearing down MCP server: %s (uptime=%.1fs)",
            server_name, time.monotonic() - handle.spawned_at,
        )
        try:
            await handle._exit_stack.aclose()
        except Exception as e:
            logger.warning("Error tearing down %s: %s", server_name, e)

    async def teardown_all(self):
        """Tear down all running MCP servers."""
        async with self._lock:
            names = list(self._handles.keys())
            for name in names:
                await self._teardown_handle(name)

    async def teardown_servers(self, server_names: list[str]):
        """Tear down a specific set of MCP servers."""
        async with self._lock:
            for name in server_names:
                await self._teardown_handle(name)

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    async def _idle_reaper(self):
        """Background task that tears down servers idle longer than timeout."""
        while True:
            try:
                await asyncio.sleep(self._reap_interval)
                now = time.monotonic()
                async with self._lock:
                    to_reap = [
                        name
                        for name, handle in self._handles.items()
                        if (now - handle.last_used_at) > self._idle_timeout
                    ]
                    for name in to_reap:
                        logger.info("Reaping idle MCP server: %s", name)
                        await self._teardown_handle(name)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Idle reaper error: %s", e)

    # ------------------------------------------------------------------
    # Task dispatch helpers
    # ------------------------------------------------------------------

    def select_servers_for_task(self, task: dict[str, Any]) -> list[str]:
        """Determine which MCP servers a task needs based on its metadata.

        Selection logic:
          1. Explicit: task has 'mcp_servers' field listing server names
          2. Tag-based: task has 'tags' matched against server config tags
          3. Tool-based: task has 'required_tools' matched against cached tool names

        Returns list of server names to spawn for this task.
        """
        # 1. Explicit server list
        explicit = task.get("mcp_servers")
        if explicit:
            available = set(self._registry.names())
            return [s for s in explicit if s in available]

        selected: set[str] = set()

        # 2. Tag-based matching
        task_tags = set(task.get("tags", []))
        if task_tags:
            for name, config in self._registry.all().items():
                if task_tags & set(config.tags):
                    selected.add(name)

        # 3. Tool-based matching (only checks cached — won't spawn)
        required_tools = task.get("required_tools", [])
        if required_tools:
            needed = set(required_tools)
            for name, handle in self._handles.items():
                if needed & set(handle.tool_names):
                    selected.add(name)
            # Also check registry configs that have tool lists in tags
            # (servers not yet spawned won't have cached tools)

        return list(selected)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return MCP client manager status for health endpoints."""
        now = time.monotonic()
        return {
            "active_servers": {
                name: {
                    "tools": handle.tool_names,
                    "uptime_s": round(now - handle.spawned_at, 1),
                    "idle_s": round(now - handle.last_used_at, 1),
                }
                for name, handle in self._handles.items()
            },
            "registered_servers": self._registry.names(),
            "idle_timeout_s": self._idle_timeout,
        }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def extract_text(result: CallToolResult) -> str:
    """Extract text content from a CallToolResult."""
    parts = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
    return "\n".join(parts)
