#  God base class — Tier 1 self-awareness for all Hekate gods.
#
#  Every god inherits from God and gets for free:
#    - Heartbeat to god_events table (configurable interval, default 30s)
#    - Tool call tracking (success/failure counts, avg latency)
#    - Uptime reporting
#    - Dependency health checks
#
#  The db parameter accepts any object matching the orchestration Database
#  interface (execute_write, fetchone, fetchall).
#
#  SQL uses $1, $2 Postgres-style placeholders — the codebase standard.
#  The orchestration Database class (orchestration/backend/db/connection.py)
#  auto-translates $N → ? for SQLite via _pg_to_sqlite().  All SQL in the
#  codebase is written with $N and the backend handles the rest.

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Awaitable, Protocol, runtime_checkable

logger = logging.getLogger("gods")


# ---------------------------------------------------------------------------
# Database protocol — matches orchestration/backend/db/connection.Database
# ---------------------------------------------------------------------------

@runtime_checkable
class DatabaseLike(Protocol):
    async def execute_write(self, sql: str, params: tuple | list = ()) -> str: ...
    async def fetchone(self, sql: str, params: tuple | list = ()) -> Any: ...
    async def fetchall(self, sql: str, params: tuple | list = ()) -> list: ...


# ---------------------------------------------------------------------------
# God base class
# ---------------------------------------------------------------------------

class God:
    """Base class for all gods in the Hekate pantheon.

    Provides Tier 1 self-awareness automatically:
      - Periodic heartbeat events written to the ``god_events`` table
      - Tool call instrumentation (success/failure counts, latency)
      - Uptime tracking (seconds since ``start()``)
      - Named dependency health checks

    Subclasses override ``on_start`` / ``on_stop`` for god-specific init.
    """

    def __init__(
        self,
        name: str,
        db: DatabaseLike,
        *,
        heartbeat_interval: float = 30.0,
    ):
        self.name = name
        self.db = db
        self.heartbeat_interval = heartbeat_interval

        # Uptime
        self._started_at: float | None = None

        # Tool call tracking: tool_name -> {success, failure, total_ms}
        self._tool_stats: dict[str, dict[str, int | float]] = {}

        # Named dependency health checks: name -> async callable
        self._dependencies: dict[str, Callable[[], Awaitable[Any]]] = {}

        # Background
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def uptime_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return time.time() - self._started_at

    @property
    def tool_stats(self) -> dict[str, dict]:
        """Aggregated per-tool statistics."""
        out: dict[str, dict] = {}
        for tool, d in self._tool_stats.items():
            total = d["success"] + d["failure"]
            out[tool] = {
                "success": d["success"],
                "failure": d["failure"],
                "total_calls": total,
                "avg_latency_ms": round(d["total_ms"] / total, 1) if total else 0,
            }
        return out

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def register_dependency(
        self, name: str, check_fn: Callable[[], Awaitable[Any]]
    ):
        """Register a named async health-check.

        ``check_fn`` should return without error when healthy and raise
        on failure.  It is called on every heartbeat tick.
        """
        self._dependencies[name] = check_fn

    async def start(self):
        """Start background processes (heartbeat).  Idempotent."""
        if self._running:
            return
        self._running = True
        self._started_at = time.time()
        await self.on_start()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"{self.name}-heartbeat"
        )
        logger.info(
            "%s started (heartbeat every %.0fs)", self.name, self.heartbeat_interval
        )

    async def stop(self):
        """Stop background processes gracefully."""
        self._running = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self._heartbeat_task = None
        await self.on_stop()
        logger.info(
            "%s stopped (uptime %.0fs)", self.name, self.uptime_seconds
        )

    async def on_start(self):
        """Override in subclasses for god-specific initialization."""

    async def on_stop(self):
        """Override in subclasses for god-specific teardown."""

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self):
        while self._running:
            try:
                dep_health = await self._check_all_dependencies()
                await self._write_event("heartbeat", {
                    "uptime_s": round(self.uptime_seconds, 1),
                    "tool_stats": self.tool_stats,
                    "dependencies": dep_health,
                })
            except Exception:
                logger.exception("%s heartbeat failed", self.name)
            try:
                await asyncio.sleep(self.heartbeat_interval)
            except asyncio.CancelledError:
                break

    async def _check_all_dependencies(self) -> dict[str, dict]:
        results: dict[str, dict] = {}
        for dep_name, check_fn in self._dependencies.items():
            t0 = time.monotonic()
            try:
                await check_fn()
                ms = (time.monotonic() - t0) * 1000
                results[dep_name] = {"healthy": True, "latency_ms": round(ms, 1)}
            except Exception as exc:
                ms = (time.monotonic() - t0) * 1000
                results[dep_name] = {
                    "healthy": False,
                    "latency_ms": round(ms, 1),
                    "error": str(exc),
                }
        return results

    # ------------------------------------------------------------------
    # Event writing (god_events table)
    # ------------------------------------------------------------------

    async def _write_event(
        self,
        event_type: str,
        payload: dict | None = None,
        severity: str = "info",
    ):
        """Insert a row into the ``god_events`` table.

        Uses ``CURRENT_TIMESTAMP`` for ``created_at`` so it works on both
        Postgres and SQLite regardless of server-default configuration.
        """
        payload_str = json.dumps(payload, default=str) if payload else None
        await self.db.execute_write(
            "INSERT INTO god_events "
            "(god_name, event_type, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)",
            (self.name, event_type, payload_str, severity),
        )

    # ------------------------------------------------------------------
    # Tool call tracking
    # ------------------------------------------------------------------

    async def record_tool_call(
        self, tool_name: str, *, success: bool, latency_ms: float
    ):
        """Record a single tool invocation."""
        if tool_name not in self._tool_stats:
            self._tool_stats[tool_name] = {
                "success": 0, "failure": 0, "total_ms": 0.0,
            }
        entry = self._tool_stats[tool_name]
        entry["success" if success else "failure"] += 1
        entry["total_ms"] += latency_ms

        await self._write_event("tool_call", {
            "tool": tool_name,
            "success": success,
            "latency_ms": round(latency_ms, 1),
        })

    @asynccontextmanager
    async def track_tool(self, tool_name: str):
        """Context manager that times and records a tool call.

        Usage::

            async with god.track_tool("get_system_status"):
                result = await get_system_status()
        """
        t0 = time.monotonic()
        try:
            yield
            ms = (time.monotonic() - t0) * 1000
            await self.record_tool_call(tool_name, success=True, latency_ms=ms)
        except Exception:
            ms = (time.monotonic() - t0) * 1000
            await self.record_tool_call(tool_name, success=False, latency_ms=ms)
            raise

    # ------------------------------------------------------------------
    # Convenience event helpers
    # ------------------------------------------------------------------

    async def log_observation(
        self,
        summary: str,
        details: dict | None = None,
        severity: str = "info",
    ):
        await self._write_event(
            "observation", {"summary": summary, **(details or {})}, severity
        )

    async def log_decision(
        self,
        decision_type: str,
        reasoning: str,
        confidence: float,
        action: str | None = None,
    ):
        await self._write_event("decision", {
            "type": decision_type,
            "reasoning": reasoning,
            "confidence": confidence,
            "action": action,
        })

    async def log_error(self, error: str, details: dict | None = None):
        await self._write_event(
            "error", {"error": error, **(details or {})}, severity="error"
        )

    # ------------------------------------------------------------------
    # Relay integration — subscribe to cross-god events
    # ------------------------------------------------------------------

    def subscribe(self, event_types: list[str]):
        """Declare which relay event types this god consumes.

        Call in ``__init__`` or ``on_start``. The god's tick loop
        should call ``poll_relay()`` to receive new events.
        """
        self._relay_subscriptions = event_types
        self._relay_cursor: float = time.time()  # start from now
        logger.info(
            "%s subscribed to relay events: %s", self.name, event_types
        )

    async def poll_relay(self) -> list:
        """Poll for new relay events matching this god's subscriptions.

        Returns a list of Event objects. Updates the internal cursor
        so the next poll only returns newer events.

        Requires the ``god_relay_events`` table to exist.
        """
        subs = getattr(self, "_relay_subscriptions", [])
        if not subs:
            return []

        cursor = getattr(self, "_relay_cursor", time.time())

        # Import here to avoid circular import at module level
        from gods.relay import poll_events
        events, new_cursor = await poll_events(
            self.db, subs, cursor
        )
        self._relay_cursor = new_cursor
        return events

    async def emit_relay(
        self,
        event_type: str,
        payload: dict,
        severity: str = "info",
    ):
        """Write an event to the relay table for other gods to consume.

        This is the cross-god communication channel. For self-observation
        (heartbeat, tool_call), use ``_write_event`` which writes to
        ``god_events``.
        """
        payload_str = json.dumps(payload, default=str)
        await self.db.execute_write(
            "INSERT INTO god_relay_events "
            "(event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            (event_type, f"god:{self.name}", payload_str,
             severity, time.time()),
        )
