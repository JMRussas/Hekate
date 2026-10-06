"""ClaudeConversation — persistent Claude CLI session using stream-json I/O.

One process per conversation. Uses:
  --input-format stream-json   ← send user messages as JSON lines to stdin
  --output-format stream-json  ← receive events as JSON lines from stdout

Never killed externally. Gap monitoring emits 'slow' events when output
stops for > _slow_threshold seconds, but the process is left running.

Usage:
    conv = await ClaudeConversation.create(model="claude-sonnet-4-6")
    async for event in conv.send("Plan this node"):
        handle(event)
    async for event in conv.send("Any gaps?"):
        handle(event)
    await conv.close()

Event types yielded by send():
    {"type": "token", "text": "..."}          — text chunk
    {"type": "tool_call", "name": "...", "input": {...}, "id": "..."}
    {"type": "tool_result", "name": "...", "output": "...", "id": "..."}
    {"type": "slow", "gap_s": 145.2, "elapsed_s": 312.0}
    {"type": "result", "cost_usd": 0.001, "exit_code": 0, ...}
    {"type": "done"}           — always last
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from typing import AsyncIterator

logger = logging.getLogger("llm_gateway.conversation")

# Seconds of silence before emitting a 'slow' event
_DEFAULT_SLOW_THRESHOLD = 120


class ClaudeConversation:
    """A persistent Claude CLI conversation session."""

    def __init__(
        self,
        conversation_id: str,
        process: asyncio.subprocess.Process,
        slow_threshold: int = _DEFAULT_SLOW_THRESHOLD,
    ):
        self.conversation_id = conversation_id
        self._process = process
        self._slow_threshold = slow_threshold
        self._last_activity = time.time()
        self._start_time = time.time()
        self._lock = asyncio.Lock()  # one send() at a time per session

    @classmethod
    async def create(
        cls,
        *,
        model: str | None = "claude-sonnet-4-6",
        mcp_config: str | None = None,
        allowed_tools: list[str] | None = None,
        slow_threshold: int = _DEFAULT_SLOW_THRESHOLD,
    ) -> "ClaudeConversation":
        """Spawn a new Claude CLI process and return a ClaudeConversation."""
        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise RuntimeError("claude CLI not found on PATH")

        cmd = [
            claude_bin,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--no-interactive",
        ]
        if model:
            cmd.extend(["--model", model])
        if mcp_config:
            cmd.extend(["--mcp-config", mcp_config])
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])

        # Strip CLAUDECODE to avoid nested session detection
        import os
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )

        conversation_id = uuid.uuid4().hex[:16]
        logger.info(
            "ClaudeConversation %s: started (pid=%d, model=%s)",
            conversation_id[:8], process.pid, model,
        )
        return cls(conversation_id, process, slow_threshold=slow_threshold)

    async def send(self, message: str) -> AsyncIterator[dict]:
        """Send a user message and yield events until the result event.

        Yields event dicts with 'type' key. Always ends with {"type": "done"}.
        """
        if not hasattr(self, "_lock"):
            self._lock = asyncio.Lock()
        async with self._lock:
            try:
                # Write user message to stdin as stream-json
                payload = json.dumps({"type": "user", "message": message}) + "\n"
                self._process.stdin.write(payload.encode())
                await self._process.stdin.drain()

                # Read response lines until result event
                async for event in self._read_until_result():
                    yield event

            except Exception as e:
                logger.error(
                    "ClaudeConversation %s: error during send: %s",
                    self.conversation_id[:8], e,
                )
                yield {"type": "error", "message": str(e)}

            finally:
                yield {"type": "done"}

    async def _read_until_result(self) -> AsyncIterator[dict]:
        """Read stdout lines and yield parsed events until result event."""
        last_line_at = time.time()
        self._last_activity = last_line_at

        async for raw_line in self._process.stdout:
            now = time.time()
            gap = now - last_line_at

            # Gap monitoring — never kill, just flag
            if gap >= self._slow_threshold:
                elapsed = now - self._start_time
                logger.warning(
                    "ClaudeConversation %s: silent for %.0fs (%.0fs total)",
                    self.conversation_id[:8], gap, elapsed,
                )
                yield {"type": "slow", "gap_s": gap, "elapsed_s": elapsed}

            last_line_at = now
            self._last_activity = now

            line = raw_line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # Non-JSON line — treat as raw text
                yield {"type": "token", "text": line.decode(errors="replace")}
                continue

            event_type = data.get("type", "")

            if event_type == "assistant":
                # Extract text chunks from assistant message
                msg = data.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            yield {"type": "token", "text": text}

            elif event_type == "tool_use":
                yield {
                    "type": "tool_call",
                    "name": data.get("name", ""),
                    "input": data.get("input", {}),
                    "id": data.get("id", ""),
                }

            elif event_type == "tool_result":
                output = data.get("content", data.get("output", ""))
                yield {
                    "type": "tool_result",
                    "name": data.get("tool_use_id", ""),
                    "output": output,
                    "id": data.get("tool_use_id", ""),
                }

            elif event_type == "result":
                yield {
                    "type": "result",
                    "exit_code": data.get("exit_code", 0),
                    "cost_usd": data.get("total_cost_usd", 0.0),
                    "usage": data.get("usage", {}),
                }
                return  # result marks end of this turn

            elif event_type == "system":
                # System info (model name etc) — log but don't forward
                logger.debug(
                    "ClaudeConversation %s: system event: %s",
                    self.conversation_id[:8], data,
                )

            # Other event types (e.g. "user" echo) — silently skip

    async def close(self):
        """Terminate the subprocess and mark session closed."""
        try:
            self._process.kill()
        except Exception:
            pass
        try:
            await self._process.wait()
        except Exception:
            pass
        logger.info(
            "ClaudeConversation %s: closed",
            self.conversation_id[:8],
        )
