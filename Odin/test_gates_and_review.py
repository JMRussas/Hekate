"""Tests for gates, review, narration, and the gated pipeline flow."""

import json
import time

import pytest

from gods.pipeline import Pipeline, Event, Emit, GateResult
from gods.gates import (
    check_plan_created, check_code_written, check_verification_ran,
    check_test_written_first, compose_gates,
)


# ---------------------------------------------------------------------------
# Gate basics
# ---------------------------------------------------------------------------

class TestGateResult:
    def test_passed(self):
        r = GateResult(True, "all good")
        assert r.passed
        assert r.reason == "all good"

    def test_failed(self):
        r = GateResult(False, "missing output", {"task_id": "t1"})
        assert not r.passed
        assert r.details["task_id"] == "t1"


# ---------------------------------------------------------------------------
# Pipeline with gates
# ---------------------------------------------------------------------------

class TestPipelineGates:
    @pytest.mark.asyncio
    async def test_gate_passes_allows_emits(self, sqlite_db):
        async def handler(event, db):
            return [Emit("output", {"v": 1}, source="h")]

        async def gate(event, emits, db):
            return GateResult(True, "looks good")

        p = Pipeline(sqlite_db)
        p.register("input", handler, gate=gate)

        await p.emit("input", {}, source="test")
        p._last_seen_id = 0
        await p.tick()

        # Check that output was emitted (gate_passed too)
        rows = await sqlite_db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type IN ('output', 'gate_passed') ORDER BY created_at"
        )
        types = [r[0] for r in rows]
        assert "output" in types
        assert "gate_passed" in types

    @pytest.mark.asyncio
    async def test_gate_fails_blocks_emits(self, sqlite_db):
        async def handler(event, db):
            return [Emit("should_not_appear", {}, source="h")]

        async def gate(event, emits, db):
            return GateResult(False, "output is empty")

        p = Pipeline(sqlite_db)
        p.register("input", handler, gate=gate)

        await p.emit("input", {}, source="test")
        p._last_seen_id = 0
        await p.tick()

        # Check output was NOT emitted
        rows = await sqlite_db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = 'should_not_appear'"
        )
        assert len(rows) == 0

        # But gate_failed was
        rows = await sqlite_db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = 'gate_failed'"
        )
        assert len(rows) == 1
        payload = json.loads(rows[0][1])
        assert "output is empty" in payload["reason"]

    @pytest.mark.asyncio
    async def test_gate_retry_with_feedback(self, sqlite_db):
        """Gate fails, handler retries with feedback, gate passes on retry."""
        attempts = []

        async def handler(event, db):
            attempt = event.payload.get("_gate_attempt", 1)
            attempts.append(attempt)
            if attempt >= 2:
                return [Emit("output", {"fixed": True}, source="h")]
            return [Emit("output", {"fixed": False}, source="h")]

        async def gate(event, emits, db):
            fixed = emits[0].payload.get("fixed", False) if emits else False
            if fixed:
                return GateResult(True, "fixed")
            return GateResult(False, "not fixed yet")

        p = Pipeline(sqlite_db)
        p.register("input", handler, gate=gate, max_retries=2)

        await p.emit("input", {}, source="test")
        p._last_seen_id = 0
        await p.tick()

        assert len(attempts) == 2  # first attempt failed, second passed
        assert attempts[0] == 1
        assert attempts[1] == 2

        # Output should be emitted (second attempt passed gate)
        rows = await sqlite_db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = 'output'"
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_gate_exhausted_after_max_retries(self, sqlite_db):
        async def handler(event, db):
            return [Emit("output", {}, source="h")]

        async def gate(event, emits, db):
            return GateResult(False, "always fails")

        p = Pipeline(sqlite_db)
        p.register("input", handler, gate=gate, max_retries=2)

        await p.emit("input", {}, source="test")
        p._last_seen_id = 0
        await p.tick()

        # gate_exhausted should be emitted
        rows = await sqlite_db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = 'gate_exhausted'"
        )
        assert len(rows) == 1

        # output should NOT be emitted
        rows = await sqlite_db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = 'output'"
        )
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_no_gate_passes_through(self, sqlite_db):
        """Handler with no gate — emits go straight through."""
        async def handler(event, db):
            return [Emit("output", {}, source="h")]

        p = Pipeline(sqlite_db)
        p.register("input", handler)  # no gate

        await p.emit("input", {}, source="test")
        p._last_seen_id = 0
        await p.tick()

        rows = await sqlite_db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = 'output'"
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Compose gates
# ---------------------------------------------------------------------------

class TestComposeGates:
    @pytest.mark.asyncio
    async def test_all_pass(self):
        async def gate_a(e, emits, db):
            return GateResult(True, "a ok")

        async def gate_b(e, emits, db):
            return GateResult(True, "b ok")

        composed = compose_gates(gate_a, gate_b)
        r = await composed(None, [], None)
        assert r.passed

    @pytest.mark.asyncio
    async def test_first_fails_short_circuits(self):
        async def gate_fail(e, emits, db):
            return GateResult(False, "a failed")

        async def gate_pass(e, emits, db):
            return GateResult(True, "b ok")

        composed = compose_gates(gate_fail, gate_pass)
        r = await composed(None, [], None)
        assert not r.passed
        assert "a failed" in r.reason


# ---------------------------------------------------------------------------
# TDD gate
# ---------------------------------------------------------------------------

class TestTDDGate:
    @pytest.mark.asyncio
    async def test_red_phase_test_fails(self):
        emits = [Emit("worker_event", {
            "tdd_phase": "test_written",
            "test_fails": True,
        })]
        r = await check_test_written_first(None, emits, None)
        assert r.passed

    @pytest.mark.asyncio
    async def test_red_phase_test_doesnt_fail(self):
        emits = [Emit("worker_event", {
            "tdd_phase": "test_written",
            "test_fails": False,
        })]
        r = await check_test_written_first(None, emits, None)
        assert not r.passed
        assert "doesn't fail" in r.reason

    @pytest.mark.asyncio
    async def test_green_phase_passes(self):
        emits = [Emit("worker_event", {
            "tdd_phase": "implementation",
            "test_passes": True,
        })]
        r = await check_test_written_first(None, emits, None)
        assert r.passed

    @pytest.mark.asyncio
    async def test_green_phase_still_fails(self):
        emits = [Emit("worker_event", {
            "tdd_phase": "implementation",
            "test_passes": False,
        })]
        r = await check_test_written_first(None, emits, None)
        assert not r.passed

    @pytest.mark.asyncio
    async def test_no_tdd_phase_skips(self):
        emits = [Emit("worker_event", {"status": "completed"})]
        r = await check_test_written_first(None, emits, None)
        assert r.passed  # skip if not in TDD mode


# ---------------------------------------------------------------------------
# Narration
# ---------------------------------------------------------------------------

class TestNarration:
    @pytest.mark.asyncio
    async def test_narration_callback_called(self, sqlite_db):
        narrations = []

        async def on_narrate(source, event_type, message):
            narrations.append((source, message))

        p = Pipeline(sqlite_db, on_narration=on_narrate)
        await p.narrate("hermes", "Reading existing test file...")
        await p.narrate("hermes", "Writing failing test...")

        assert len(narrations) == 2
        assert narrations[0] == ("hermes", "Reading existing test file...")
        assert narrations[1] == ("hermes", "Writing failing test...")

    @pytest.mark.asyncio
    async def test_narration_persisted_to_relay(self, sqlite_db):
        p = Pipeline(sqlite_db)
        await p.narrate("athena", "Generating plan with L2 rigor...")

        rows = await sqlite_db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = 'narration'"
        )
        assert len(rows) == 1
        payload = json.loads(rows[0][1])
        assert payload["source"] == "athena"
        assert "L2 rigor" in payload["message"]

    @pytest.mark.asyncio
    async def test_narration_callback_failure_doesnt_block(self, sqlite_db):
        async def bad_callback(source, event_type, message):
            raise RuntimeError("SSE push failed")

        p = Pipeline(sqlite_db, on_narration=bad_callback)
        # Should not raise
        await p.narrate("hermes", "This should not crash")


# ---------------------------------------------------------------------------
# Full gated flow simulation
# ---------------------------------------------------------------------------

class TestGatedFlow:
    @pytest.mark.asyncio
    async def test_plan_generate_review_dispatch(self, sqlite_db):
        """Simulate: generate plan → gate checks it → review → dispatch."""
        log = []

        async def plan_handler(event, db):
            log.append("plan:generate")
            feedback = event.payload.get("_gate_feedback")
            if feedback:
                log.append(f"plan:retry with feedback: {feedback[:30]}")
            return [Emit("plan_generated", {
                "plan_id": "plan-1",
                "project_id": "p1",
            }, source="athena")]

        async def plan_gate(event, emits, db):
            # Simulate: first attempt has gaps, second passes
            attempt = event.payload.get("_gate_attempt", 1)
            if attempt <= 1:
                return GateResult(False, "Missing error handling tasks")
            return GateResult(True, "Plan looks complete")

        async def dispatch_handler(event, db):
            log.append("odin:dispatch")
            return [Emit("dispatch_command", {"task_id": "t1"}, source="odin")]

        p = Pipeline(sqlite_db)
        p.register("project_created", plan_handler, name="athena",
                    gate=plan_gate, max_retries=2)
        p.register("plan_generated", dispatch_handler, name="odin")

        await p.emit("project_created", {"project_id": "p1"}, source="api")
        p._last_seen_id = 0

        # Multiple ticks: plan generates → gate fails → retries → gate passes → dispatch
        for _ in range(5):
            await p.tick()

        assert "plan:generate" in log
        assert any("retry with feedback" in l for l in log)
        assert "odin:dispatch" in log

    @pytest.mark.asyncio
    async def test_execution_with_tdd_gate(self, sqlite_db):
        """Simulate: execute with TDD gate enforcing test-first."""
        log = []

        async def execute_handler(event, db):
            attempt = event.payload.get("_gate_attempt", 1)
            if attempt == 1:
                # First attempt: forgot to write test first
                log.append("hermes:execute (no test)")
                return [Emit("worker_event", {
                    "task_id": "t1",
                    "status": "completed",
                    "tdd_phase": "implementation",
                    "test_passes": False,
                }, source="hermes")]
            else:
                # Second attempt: wrote test first
                log.append("hermes:execute (with test)")
                return [Emit("worker_event", {
                    "task_id": "t1",
                    "status": "completed",
                    "tdd_phase": "implementation",
                    "test_passes": True,
                }, source="hermes")]

        p = Pipeline(sqlite_db)
        p.register("dispatch_command", execute_handler, name="hermes",
                    gate=check_test_written_first, max_retries=2)

        await p.emit("dispatch_command", {"task_id": "t1"}, source="odin")
        p._last_seen_id = 0
        await p.tick()

        assert "hermes:execute (no test)" in log
        assert "hermes:execute (with test)" in log

    @pytest.mark.asyncio
    async def test_introspection_shows_gates(self, sqlite_db):
        async def h(e, db): return None
        async def g(e, emits, db): return GateResult(True, "ok")

        p = Pipeline(sqlite_db)
        p.register("x", h, gate=g, max_retries=3)

        info = p.handlers[0]
        assert info["has_gate"] is True
        assert info["max_retries"] == 3
