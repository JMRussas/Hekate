"""Event relay — the gate all god/demigod communication flows through.

Every event a god or demigod produces goes through the relay. The relay
decides where each event goes: log file, database, another god, or
nowhere (rejected).

Architecture:
  - EventRelay: the core class. Configurable sinks + optional gate.
  - Sinks: where events end up (log, HTTP endpoint, file, callback)
  - Gate: optional filter that can reject, transform, or annotate events
    before they reach sinks. This is the control point.

Usage:
    relay = EventRelay.from_config({
        "sinks": [
            {"type": "log"},
            {"type": "http", "url": "http://localhost:5201/events"},
        ],
        "gate": {"max_events_per_second": 100}
    })

    await relay.emit("demigod_step", {
        "run_id": "abc123",
        "state": "check_all",
        "action": "inspect",
    }, source="demigod:health_checker")

The relay is designed to be:
  - Fire-and-forget from the caller's perspective (never blocks the run)
  - Configurable per god/demigod instance
  - Extensible with new sink types
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from gods import safe_json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger("gods.relay")

# Re-export DatabaseLike so consumers can import from relay
try:
    from gods.base import DatabaseLike
except ImportError:
    # Standalone usage — define a minimal protocol
    from typing import runtime_checkable

    @runtime_checkable
    class DatabaseLike(Protocol):  # type: ignore[no-redef]
        async def execute_write(self, sql: str, params: tuple | list = ()) -> str: ...
        async def fetchone(self, sql: str, params: tuple | list = ()) -> Any: ...
        async def fetchall(self, sql: str, params: tuple | list = ()) -> list: ...



# ---------------------------------------------------------------------------
# Event structure
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """A structured event from a god or demigod."""
    event_type: str
    payload: dict[str, Any]
    source: str          # "demigod:health_checker", "god:odin", etc.
    timestamp: float = field(default_factory=time.time)
    severity: str = "info"

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "payload": self.payload,
            "source": self.source,
            "timestamp": self.timestamp,
            "severity": self.severity,
        }


# ---------------------------------------------------------------------------
# Sink protocol — where events end up
# ---------------------------------------------------------------------------

class Sink(Protocol):
    async def write(self, event: Event) -> None: ...


class LogSink:
    """Write events to the Python logger."""

    def __init__(self, logger_name: str = "gods.events"):
        self._log = logging.getLogger(logger_name)

    async def write(self, event: Event) -> None:
        self._log.info(
            "[%s] %s: %s",
            event.source, event.event_type,
            json.dumps(event.payload, default=str)[:300],
        )


class HttpSink:
    """POST events to an HTTP endpoint (Hades, context store, etc.)."""

    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout

    async def write(self, event: Event) -> None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=event.to_dict())
                if resp.status_code >= 400:
                    logger.debug(
                        "HTTP sink %s returned %d", self.url, resp.status_code
                    )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.debug("HTTP sink %s unreachable: %s", self.url, e)


class FileSink:
    """Append events as JSON lines to a file."""

    def __init__(self, path: str):
        self.path = path

    async def write(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), default=str) + "\n"
        # Use asyncio to avoid blocking
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._append, line)

    def _append(self, line: str):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)


class CallbackSink:
    """Forward events to an async callback."""

    def __init__(self, callback):
        self._cb = callback

    async def write(self, event: Event) -> None:
        await self._cb(event)


class PostgresSink:
    """Write events to the god_relay_events table.

    Uses the DatabaseLike protocol so it works with both Postgres
    (asyncpg) and SQLite (aiosqlite) backends.
    """

    def __init__(self, db: DatabaseLike):
        self._db = db

    async def write(self, event: Event) -> None:
        payload_str = json.dumps(event.payload, default=str)
        await self._db.execute_write(
            "INSERT INTO god_relay_events "
            "(event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            (event.event_type, event.source, payload_str,
             event.severity, event.timestamp),
        )


# ---------------------------------------------------------------------------
# Gate — optional filter/transform before events reach sinks
# ---------------------------------------------------------------------------

class Gate:
    """Controls what events are allowed through.

    Can reject events (rate limiting, severity filtering),
    transform them (add metadata, redact fields),
    or route them (different sinks for different event types).
    """

    def __init__(
        self,
        max_events_per_second: float = 0,  # 0 = unlimited
        min_severity: str | None = None,
        blocked_types: set[str] | None = None,
    ):
        self.max_eps = max_events_per_second
        self.min_severity = min_severity
        self.blocked_types = blocked_types or set()
        self._last_emit = 0.0
        self._count_window = 0.0
        self._count = 0

    _SEVERITY_ORDER = {"debug": 0, "info": 1, "warning": 2, "error": 3}

    def check(self, event: Event) -> bool:
        """Return True if the event should be allowed through."""
        # Type blocking
        if event.event_type in self.blocked_types:
            return False

        # Severity filter
        if self.min_severity:
            event_level = self._SEVERITY_ORDER.get(event.severity, 1)
            min_level = self._SEVERITY_ORDER.get(self.min_severity, 0)
            if event_level < min_level:
                return False

        # Rate limiting
        if self.max_eps > 0:
            now = time.monotonic()
            if now - self._count_window > 1.0:
                self._count_window = now
                self._count = 0
            self._count += 1
            if self._count > self.max_eps:
                return False

        return True


# ---------------------------------------------------------------------------
# EventRelay — the main class
# ---------------------------------------------------------------------------

class EventRelay:
    """The central event relay. All god/demigod events flow through here.

    Events are dispatched to all sinks asynchronously. Sink failures are
    logged but never propagate to the caller — the relay is fire-and-forget
    from the event producer's perspective.
    """

    def __init__(
        self,
        sinks: list[Sink] | None = None,
        gate: Gate | None = None,
    ):
        self._sinks: list[Sink] = sinks or []
        self._gate = gate

    def add_sink(self, sink: Sink):
        self._sinks.append(sink)

    async def emit(
        self,
        event_type: str,
        payload: dict,
        source: str = "unknown",
        severity: str = "info",
    ) -> None:
        """Emit an event through the relay.

        This is fire-and-forget: sink failures are swallowed.
        If a gate is configured and rejects the event, it's dropped.
        """
        event = Event(
            event_type=event_type,
            payload=payload,
            source=source,
            severity=severity,
        )

        if self._gate and not self._gate.check(event):
            logger.debug("Gate rejected: %s from %s", event_type, source)
            return

        for sink in self._sinks:
            try:
                await sink.write(event)
            except Exception as e:
                logger.debug("Sink %s failed: %s", type(sink).__name__, e)

    def make_callback(self, source: str):
        """Create an EventCallback compatible with the demigod runtime.

        Returns an async function with signature (event_type, payload) → None
        that routes through this relay with the given source tag.
        """
        async def callback(event_type: str, payload: dict):
            await self.emit(event_type, payload, source=source)
        return callback

    @classmethod
    def from_config(cls, config: dict) -> EventRelay:
        """Build a relay from a config dict.

        Config format:
            {
                "sinks": [
                    {"type": "log"},
                    {"type": "log", "logger": "custom.logger"},
                    {"type": "http", "url": "http://..."},
                    {"type": "file", "path": "/var/log/gods.jsonl"},
                ],
                "gate": {
                    "max_events_per_second": 100,
                    "min_severity": "info",
                    "blocked_types": ["heartbeat"]
                }
            }
        """
        sinks: list[Sink] = []
        db = config.get("_db")  # injected DatabaseLike instance
        for sink_cfg in config.get("sinks", []):
            sink_type = sink_cfg.get("type", "log")
            if sink_type == "log":
                sinks.append(LogSink(sink_cfg.get("logger", "gods.events")))
            elif sink_type == "http":
                url = sink_cfg.get("url", "")
                if url:
                    sinks.append(HttpSink(url, sink_cfg.get("timeout", 5.0)))
            elif sink_type == "file":
                path = sink_cfg.get("path", "")
                if path:
                    sinks.append(FileSink(path))
            elif sink_type == "postgres":
                if db:
                    sinks.append(PostgresSink(db))
                else:
                    logger.warning("Postgres sink requested but no _db provided")

        gate = None
        gate_cfg = config.get("gate")
        if gate_cfg:
            gate = Gate(
                max_events_per_second=gate_cfg.get("max_events_per_second", 0),
                min_severity=gate_cfg.get("min_severity"),
                blocked_types=set(gate_cfg.get("blocked_types", [])),
            )

        return cls(sinks=sinks, gate=gate)

    @classmethod
    def default(cls) -> EventRelay:
        """A relay with just a log sink — minimum viable setup."""
        return cls(sinks=[LogSink()])


# ---------------------------------------------------------------------------
# Event polling — the read side for gods
# ---------------------------------------------------------------------------

async def poll_events(
    db: DatabaseLike,
    event_types: list[str],
    since: float,
    limit: int = 100,
) -> tuple[list[Event], float]:
    """Poll god_relay_events for new events since a timestamp.

    Returns (events, new_cursor) where new_cursor is the timestamp
    to use as `since` on the next poll. If no events, returns the
    original since value.

    Gods call this on their tick interval with their subscribed
    event types and their last poll timestamp.
    """
    if not event_types:
        return [], since

    # Build IN clause with positional params
    placeholders = ", ".join(f"${i+1}" for i in range(len(event_types)))
    since_param = f"${len(event_types) + 1}"
    limit_param = f"${len(event_types) + 2}"

    sql = (
        f"SELECT event_type, source, payload, severity, created_at "
        f"FROM god_relay_events "
        f"WHERE event_type IN ({placeholders}) "
        f"AND created_at > {since_param} "
        f"ORDER BY created_at ASC "
        f"LIMIT {limit_param}"
    )
    params = [*event_types, since, limit]

    rows = await db.fetchall(sql, params)
    if not rows:
        return [], since

    events = []
    new_cursor = since
    for row in rows:
        # Handle both dict-like and tuple row results
        if isinstance(row, dict):
            et, src, payload_str, sev, ts = (
                row["event_type"], row["source"], row["payload"],
                row["severity"], row["created_at"],
            )
        else:
            et, src, payload_str, sev, ts = row

        payload = {}
        if payload_str:
            try:
                payload = safe_json.loads(payload_str, {}) if isinstance(payload_str, str) else payload_str
            except (json.JSONDecodeError, TypeError):
                payload = {"raw": str(payload_str)}

        events.append(Event(
            event_type=et,
            payload=payload,
            source=src,
            timestamp=float(ts),
            severity=sev or "info",
        ))
        new_cursor = max(new_cursor, float(ts))

    return events, new_cursor
