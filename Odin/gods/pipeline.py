"""Gods pipeline — event-driven function dispatch with gates.

One process, one loop. Each "god" is a handler function that takes an
event and a database, does its work, and optionally emits new events.

Every handler can have a gate — a check function that validates the
handler's output before emits go through. If the gate fails, the emits
are blocked and a gate_failed event is emitted instead. This enforces
"did this actually happen? is it right?" after every step.

The relay table (god_relay_events) is the durable log. Handlers are
called in-process. The table exists for auditability, restart recovery,
and future scale-out.

Usage:
    pipeline = Pipeline(db)

    pipeline.register("project_created", athena_plan,
        gate=check_plan_created)

    pipeline.register("dispatch_command", hermes_execute,
        gate=check_code_written)

    await pipeline.run()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger("gods.pipeline")


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

@dataclass
class Event:
    event_type: str
    payload: dict[str, Any]
    source: str
    timestamp: float = field(default_factory=time.time)
    severity: str = "info"


# ---------------------------------------------------------------------------
# Emit — what handlers produce
# ---------------------------------------------------------------------------

@dataclass
class Emit:
    event_type: str
    payload: dict[str, Any]
    source: str = ""
    severity: str = "info"


# ---------------------------------------------------------------------------
# Gate result — what gate functions return
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


# Handler: (event, db) → list[Emit] | None
Handler = Callable[[Event, Any], Awaitable[list[Emit] | None]]

# Gate: (event, emits, db) → GateResult
# Gets the original event, the handler's proposed emits, and db access
Gate = Callable[[Event, list[Emit], Any], Awaitable[GateResult]]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@dataclass
class Registration:
    event_type: str
    handler: Handler
    name: str
    filter: Callable[[Event], bool] | None = None
    gate: Gate | None = None
    max_retries: int = 0  # how many times to retry if gate fails


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Pipeline:
    """Event-driven function pipeline with gates.

    register() binds handler functions to event types.
    Gates validate handler output before emits go through.
    tick() polls → dispatches → gates → emits.
    """

    def __init__(
        self,
        db,
        *,
        tick_interval: float = 1.0,
        source_name: str = "pipeline",
        on_narration: Callable[[str, str, str], Awaitable[None]] | None = None,
    ):
        self.db = db
        self.tick_interval = tick_interval
        self.source_name = source_name
        self._handlers: list[Registration] = []
        self._cursor: float = time.time()
        self._last_seen_id: int = 0  # track last processed row ID for dedup
        self._running = False
        self._tick_count = 0
        # Narration callback: (source, event_type, message) → None
        # Called by handlers to stream real-time narration (peer coding)
        self.on_narration = on_narration

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        event_type: str,
        handler: Handler,
        *,
        name: str | None = None,
        filter: Callable[[Event], bool] | None = None,
        gate: Gate | None = None,
        max_retries: int = 0,
    ):
        """Bind a handler to an event type.

        Args:
            event_type: The event type to match
            handler: Async function (event, db) → list[Emit] | None
            name: Human label for logging
            filter: Only call if filter(event) is True
            gate: Check function — validates output before emits go through
            max_retries: Times to retry if gate fails (feeds gate feedback back)
        """
        self._handlers.append(Registration(
            event_type=event_type,
            handler=handler,
            name=name or handler.__name__,
            filter=filter,
            gate=gate,
            max_retries=max_retries,
        ))

    # ------------------------------------------------------------------
    # Narration — real-time peer coding output
    # ------------------------------------------------------------------

    async def narrate(self, source: str, message: str):
        """Emit a narration event for real-time display.

        Handlers call pipeline.narrate() to stream what they're doing.
        This goes to the on_narration callback (which pushes to SSE)
        AND to the relay table for persistence.
        """
        if self.on_narration:
            try:
                await self.on_narration(source, "narration", message)
            except Exception:
                pass  # never block on narration failure

        await self._emit(Emit(
            event_type="narration",
            payload={"source": source, "message": message},
            source=source,
        ))

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    async def run(self):
        self._running = True
        logger.info(
            "Pipeline starting (tick=%.1fs, handlers=%d)",
            self.tick_interval, len(self._handlers),
        )
        while self._running:
            try:
                await self.tick()
            except Exception:
                logger.exception("Pipeline tick failed")
            await asyncio.sleep(self.tick_interval)

    async def stop(self):
        self._running = False
        self.stop_scheduler()

    # ------------------------------------------------------------------
    # Tick scheduler — auto-inject tick events
    # ------------------------------------------------------------------

    def start_scheduler(self, interval: float = 10.0):
        """Start injecting periodic tick events into the relay table."""
        self._scheduler_running = True
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(interval),
            name="tick-scheduler",
        )

    def stop_scheduler(self):
        """Stop the tick scheduler."""
        self._scheduler_running = False
        task = getattr(self, "_scheduler_task", None)
        if task and not task.done():
            task.cancel()

    async def _scheduler_loop(self, interval: float):
        """Background loop that injects tick events."""
        try:
            while self._scheduler_running:
                await self._emit(Emit(
                    event_type="tick",
                    payload={},
                    source="scheduler",
                ))
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    async def reset(self):
        """Reset pipeline state — clear relay table, reset cursor to 0."""
        self._last_seen_id = 0
        try:
            await self.db.execute_write("DELETE FROM god_relay_events")
            await self.db.execute_write(
                "INSERT OR REPLACE INTO god_registry (name, last_seen_id, last_heartbeat) "
                "VALUES ($1, $2, $3)",
                (self.source_name, 0, time.time()),
            )
        except Exception as e:
            logger.warning("Pipeline: reset failed: %s", e)

    async def restore_cursor(self):
        """Restore _last_seen_id from god_registry table."""
        try:
            row = await self.db.fetchone(
                "SELECT last_seen_id FROM god_registry WHERE name = $1",
                (self.source_name,),
            )
            if row:
                val = row[0] if isinstance(row, (list, tuple)) else row["last_seen_id"]
                self._last_seen_id = int(val) if val else 0
                logger.info("Pipeline: restored cursor to %d", self._last_seen_id)
            else:
                logger.info("Pipeline: no saved cursor found, starting from 0")
        except Exception as e:
            logger.warning("Pipeline: cursor restore failed: %s", e)

    async def _persist_cursor(self):
        """Persist _last_seen_id to god_registry table."""
        try:
            await self.db.execute_write(
                "INSERT OR REPLACE INTO god_registry (name, last_seen_id, last_heartbeat) "
                "VALUES ($1, $2, $3)",
                (self.source_name, self._last_seen_id, time.time()),
            )
        except Exception:
            pass  # Table may not exist yet

    async def tick(self):
        self._tick_count += 1
        subscribed = list({r.event_type for r in self._handlers})
        if not subscribed:
            return

        events = await self._poll(subscribed)
        if not events:
            return

        # Deduplicate to prevent event floods:
        # - tick/project_tick: keep latest per (type, project_id)
        # - dispatch_command: keep latest per task_id
        # - worker_event with status=skipped: keep latest per task_id
        dedup_by_project = {"tick", "project_tick"}
        dedup_by_task = {"dispatch_command"}
        seen: dict[tuple, Event] = {}
        unique_events: list[Event] = []

        for event in events:
            if event.event_type in dedup_by_project:
                key = (event.event_type, event.payload.get("project_id", ""))
                seen[key] = event
            elif event.event_type in dedup_by_task:
                key = (event.event_type, event.payload.get("task_id", ""))
                seen[key] = event
            elif (event.event_type == "worker_event"
                  and event.payload.get("status") == "skipped"):
                key = ("worker_skipped", event.payload.get("task_id", ""))
                seen[key] = event
            else:
                unique_events.append(event)

        # Add deduplicated ticks back
        unique_events.extend(seen.values())

        for event in unique_events:
            await self._dispatch(event)

        # Persist cursor after processing
        await self._persist_cursor()

    # ------------------------------------------------------------------
    # Dispatch with gate enforcement
    # ------------------------------------------------------------------

    async def _dispatch(self, event: Event):
        for reg in self._handlers:
            if reg.event_type != event.event_type:
                continue
            if reg.filter and not reg.filter(event):
                continue

            logger.info("[%s] handling %s", reg.name, event.event_type)

            # Run handler (with retries if gate fails)
            emits = await self._run_with_gate(reg, event)

            if emits:
                for e in emits:
                    await self._emit(e)

    async def _run_with_gate(
        self, reg: Registration, event: Event
    ) -> list[Emit] | None:
        """Run handler, check gate, retry if needed."""
        last_gate_feedback: str | None = None

        for attempt in range(reg.max_retries + 1):
            # Inject gate feedback into event payload on retries
            run_event = event
            if last_gate_feedback and attempt > 0:
                run_event = Event(
                    event_type=event.event_type,
                    payload={
                        **event.payload,
                        "_gate_feedback": last_gate_feedback,
                        "_gate_attempt": attempt + 1,
                    },
                    source=event.source,
                    timestamp=event.timestamp,
                    severity=event.severity,
                )

            # Call handler
            try:
                t0 = time.monotonic()
                results = await reg.handler(run_event, self.db)
                ms = (time.monotonic() - t0) * 1000
            except Exception as e:
                logger.error("[%s] failed: %s", reg.name, e, exc_info=True)
                await self._emit(Emit(
                    event_type="handler_error",
                    payload={
                        "handler": reg.name,
                        "event_type": event.event_type,
                        "error": repr(e),
                        "attempt": attempt + 1,
                    },
                    source=reg.name,
                    severity="error",
                ))
                return None

            if not results:
                results = []

            # No gate → pass through
            if not reg.gate:
                logger.info("[%s] completed in %.0fms → %d emit(s)", reg.name, ms, len(results))
                return results

            # Run gate — pass the run_event (which has _gate_attempt on retries)
            try:
                gate_result = await reg.gate(run_event, results, self.db)
            except Exception as e:
                logger.error("[%s] gate crashed: %s", reg.name, e)
                gate_result = GateResult(
                    passed=False,
                    reason=f"Gate crashed: {e}",
                )

            if gate_result.passed:
                logger.info(
                    "[%s] completed in %.0fms, gate PASSED → %d emit(s)",
                    reg.name, ms, len(results),
                )
                # Emit gate_passed for audit trail
                await self._emit(Emit(
                    event_type="gate_passed",
                    payload={
                        "handler": reg.name,
                        "reason": gate_result.reason,
                        "attempt": attempt + 1,
                        **gate_result.details,
                    },
                    source=reg.name,
                ))
                return results

            # Gate failed
            logger.warning(
                "[%s] gate FAILED (attempt %d/%d): %s",
                reg.name, attempt + 1, reg.max_retries + 1, gate_result.reason,
            )
            last_gate_feedback = gate_result.reason

            await self._emit(Emit(
                event_type="gate_failed",
                payload={
                    "handler": reg.name,
                    "reason": gate_result.reason,
                    "attempt": attempt + 1,
                    "max_attempts": reg.max_retries + 1,
                    "event_type": event.event_type,
                    "original_payload": event.payload,
                    "provider": event.payload.get("provider"),
                    **gate_result.details,
                },
                source=reg.name,
                severity="warning",
            ))

        # All retries exhausted
        logger.error("[%s] gate failed after %d attempts", reg.name, reg.max_retries + 1)
        await self._emit(Emit(
            event_type="gate_exhausted",
            payload={
                "handler": reg.name,
                "event_type": event.event_type,
                "last_reason": last_gate_feedback,
                "original_payload": event.payload,
                "provider": event.payload.get("provider"),
            },
            source=reg.name,
            severity="error",
        ))
        return None

    # ------------------------------------------------------------------
    # Event I/O
    # ------------------------------------------------------------------

    async def _poll(self, event_types: list[str]) -> list[Event]:
        placeholders = ", ".join(f"${i+1}" for i in range(len(event_types)))
        id_param = f"${len(event_types) + 1}"

        # Use row ID for dedup — avoids timestamp precision issues
        sql = (
            f"SELECT id, event_type, source, payload, severity, created_at "
            f"FROM god_relay_events "
            f"WHERE event_type IN ({placeholders}) "
            f"AND id > {id_param} "
            f"ORDER BY id ASC "
            f"LIMIT 100"
        )
        rows = await self.db.fetchall(sql, [*event_types, self._last_seen_id])
        if not rows:
            return []

        events = []
        for row in rows:
            if isinstance(row, dict):
                row_id = row["id"]
                et, src, payload_str, sev, ts = (
                    row["event_type"], row["source"], row["payload"],
                    row["severity"], row["created_at"],
                )
            else:
                row_id, et, src, payload_str, sev, ts = row

            payload = {}
            if payload_str:
                try:
                    payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": str(payload_str)}

            events.append(Event(
                event_type=et, payload=payload, source=src,
                timestamp=float(ts), severity=sev or "info",
            ))
            self._last_seen_id = max(self._last_seen_id, int(row_id))

        return events

    async def _emit(self, emit: Emit):
        source = emit.source or self.source_name
        payload_str = json.dumps(emit.payload, default=str)
        ts = time.time()

        await self.db.execute_write(
            "INSERT INTO god_relay_events "
            "(event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            (emit.event_type, source, payload_str, emit.severity, ts),
        )

    async def emit(
        self,
        event_type: str,
        payload: dict,
        source: str = "",
        severity: str = "info",
    ):
        await self._emit(Emit(
            event_type=event_type,
            payload=payload,
            source=source or self.source_name,
            severity=severity,
        ))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def handlers(self) -> list[dict]:
        return [
            {
                "event_type": r.event_type,
                "name": r.name,
                "has_filter": r.filter is not None,
                "has_gate": r.gate is not None,
                "max_retries": r.max_retries,
            }
            for r in self._handlers
        ]

    @property
    def status(self) -> dict:
        return {
            "running": self._running,
            "tick_count": self._tick_count,
            "cursor": self._cursor,
            "handler_count": len(self._handlers),
            "subscribed_events": list({r.event_type for r in self._handlers}),
        }
