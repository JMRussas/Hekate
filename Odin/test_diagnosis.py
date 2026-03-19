"""Tests for gods/diagnosis.py — failure diagnosis and repair loop."""

import json
import time

import pytest

from gods.diagnosis import (
    diagnose, diagnose_handler, escalation_handler,
    Diagnosis, ERROR_PATTERNS, PROVIDER_FALLBACK,
    _pick_alternative_provider, _escalate_strategy,
)
from gods.pipeline import Pipeline, Event, Emit, GateResult


# ---------------------------------------------------------------------------
# diagnose() — pattern matching
# ---------------------------------------------------------------------------

class TestDiagnose:
    def test_rate_limit_changes_provider(self):
        d = diagnose("hermes", "rate limit exceeded", 1, 3, current_provider="claude")
        assert d.strategy == "change_provider"
        assert d.confidence >= 0.7
        assert d.fix_detail.get("new_provider") is not None

    def test_syntax_error_retries_with_fix(self):
        d = diagnose("hermes", "SyntaxError: unexpected indent", 1, 3)
        assert d.strategy == "retry_with_fix"
        assert "syntax" in d.fix_detail.get("guidance", "").lower()

    def test_test_failure_retries_with_fix(self):
        d = diagnose("mimir", "test failed: AssertionError in test_auth", 1, 3)
        assert d.strategy == "retry_with_fix"
        assert "test" in d.fix_detail.get("guidance", "").lower()

    def test_empty_output_retries(self):
        d = diagnose("hermes", "Task completed with empty output", 1, 3)
        assert d.strategy == "retry_with_fix"

    def test_unknown_error_low_confidence(self):
        d = diagnose("hermes", "something completely novel happened", 1, 3)
        assert d.strategy == "retry_with_fix"
        assert d.confidence < 0.5

    def test_retries_exhausted_escalates(self):
        d = diagnose("hermes", "syntax error", 3, 3)
        assert d.strategy in ("change_provider", "escalate")

    def test_doesnt_repeat_failed_strategy(self):
        history = [{"strategy": "retry_with_fix", "reason": "syntax error", "attempt": 1}]
        d = diagnose("hermes", "syntax error again", 2, 3, history=history)
        # Should escalate from retry_with_fix to change_provider
        assert d.strategy != "retry_with_fix"

    def test_escalates_when_all_strategies_tried(self):
        history = [
            {"strategy": "retry_with_fix", "reason": "err", "attempt": 1},
            {"strategy": "change_provider", "reason": "err", "attempt": 2},
        ]
        d = diagnose("hermes", "still failing", 3, 3, history=history)
        assert d.strategy == "escalate"

    def test_why_chain_populated(self):
        d = diagnose("hermes", "rate limit", 1, 3)
        assert len(d.why_chain) > 0
        assert any("rate limit" in step.lower() for step in d.why_chain)


# ---------------------------------------------------------------------------
# Provider fallback
# ---------------------------------------------------------------------------

class TestProviderFallback:
    def test_picks_next_provider(self):
        assert _pick_alternative_provider("claude") == "gemini"
        assert _pick_alternative_provider("gemini") == "ollama"

    def test_wraps_around(self):
        assert _pick_alternative_provider("ollama") == "claude"

    def test_unknown_returns_first(self):
        assert _pick_alternative_provider("codex") == "claude"


class TestEscalateStrategy:
    def test_retry_escalates_to_change_provider(self):
        assert _escalate_strategy("retry_with_fix") == "change_provider"

    def test_change_provider_escalates_to_escalate(self):
        assert _escalate_strategy("change_provider") == "escalate"

    def test_unknown_escalates(self):
        assert _escalate_strategy("anything") == "escalate"


# ---------------------------------------------------------------------------
# diagnose_handler — pipeline integration
# ---------------------------------------------------------------------------

class TestDiagnoseHandler:
    @pytest.mark.asyncio
    async def test_emits_repair_command(self):
        event = Event("gate_failed", {
            "handler": "hermes",
            "reason": "SyntaxError in output",
            "attempt": 1,
            "max_attempts": 3,
            "provider": "claude",
        }, "hermes")

        emits = await diagnose_handler(event, None)

        assert len(emits) == 1
        assert emits[0].event_type == "repair_command"
        assert emits[0].payload["strategy"] == "retry_with_fix"
        assert emits[0].payload["handler"] == "hermes"
        assert "guidance" in emits[0].payload["fix_detail"]

    @pytest.mark.asyncio
    async def test_threads_repair_history(self):
        event = Event("gate_failed", {
            "handler": "hermes",
            "reason": "still broken",
            "attempt": 2,
            "max_attempts": 3,
            "_repair_history": [
                {"strategy": "retry_with_fix", "reason": "first fail", "attempt": 1},
            ],
        }, "hermes")

        emits = await diagnose_handler(event, None)
        history = emits[0].payload["_repair_history"]
        assert len(history) == 2  # original + new entry


# ---------------------------------------------------------------------------
# escalation_handler
# ---------------------------------------------------------------------------

class TestEscalationHandler:
    @pytest.mark.asyncio
    async def test_emits_human_intervention(self):
        event = Event("repair_command", {
            "handler": "hermes",
            "strategy": "escalate",
            "root_cause": "All strategies exhausted",
            "why_chain": ["tried everything"],
        }, "diagnosis")

        emits = await escalation_handler(event, None)

        assert len(emits) == 1
        assert emits[0].event_type == "human_intervention_needed"
        assert emits[0].severity == "error"
        assert "hermes" in emits[0].payload["handler"]


# ---------------------------------------------------------------------------
# Full diagnosis + repair loop in pipeline
# ---------------------------------------------------------------------------

class TestDiagnosisRepairLoop:
    @pytest.mark.asyncio
    async def test_gate_fail_diagnose_repair_succeed(self, sqlite_db):
        """gate fails → diagnosis → repair_command → handler retries → succeeds."""
        log = []

        async def my_handler(event, db):
            fix = event.payload.get("_fix_applied")
            if fix:
                log.append(f"handler: running with fix ({fix})")
                return [Emit("success", {"fixed": True}, source="h")]
            log.append("handler: running (will fail gate)")
            return [Emit("success", {"fixed": False}, source="h")]

        async def my_gate(event, emits, db):
            if emits and emits[0].payload.get("fixed"):
                return GateResult(True, "fixed")
            return GateResult(False, "SyntaxError in output")

        async def repair_handler(event, db):
            """Responds to repair_command by re-emitting the original event with fix."""
            if event.payload.get("strategy") == "escalate":
                return None  # let escalation_handler deal with it
            strategy = event.payload.get("strategy")
            fix_detail = event.payload.get("fix_detail", {})
            log.append(f"repair: applying {strategy}")
            return [Emit("trigger", {
                "_fix_applied": strategy,
                "_guidance": fix_detail.get("guidance", ""),
            }, source="repair")]

        p = Pipeline(sqlite_db)
        p.register("trigger", my_handler, name="worker",
                    gate=my_gate, max_retries=1)  # 1 retry = gate_failed fires, then exhausted
        p.register("gate_failed", diagnose_handler, name="diagnosis")
        p.register("gate_exhausted", diagnose_handler, name="diagnosis_exhausted")
        p.register("repair_command", repair_handler, name="repair",
                    filter=lambda e: e.payload.get("strategy") != "escalate")
        p.register("repair_command", escalation_handler, name="escalation",
                    filter=lambda e: e.payload.get("strategy") == "escalate")

        await p.emit("trigger", {}, source="test")
        p._last_seen_id = 0

        # Run enough ticks for:
        # 1. handler runs → gate fails → gate_failed emitted
        # 2. diagnosis picks up gate_failed → repair_command emitted
        # 3. repair picks up repair_command → trigger emitted with fix
        # 4. handler runs again with fix → gate passes → success emitted
        for _ in range(10):
            await p.tick()

        assert "handler: running (will fail gate)" in log
        assert any("repair: applying" in l for l in log)
        assert any("handler: running with fix" in l for l in log)

        # Verify success event was emitted (may fire more than once via repair loop)
        rows = await sqlite_db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = 'success'"
        )
        assert len(rows) >= 1

    @pytest.mark.asyncio
    async def test_escalation_when_all_repairs_fail(self, sqlite_db):
        """Everything fails → escalation → human_intervention_needed."""
        async def always_fails(event, db):
            return [Emit("output", {"bad": True}, source="h")]

        async def always_fails_gate(event, emits, db):
            return GateResult(False, "completely broken")

        p = Pipeline(sqlite_db)
        p.register("trigger", always_fails, name="doomed",
                    gate=always_fails_gate, max_retries=0)
        p.register("gate_failed", diagnose_handler, name="diagnosis")
        p.register("gate_exhausted", diagnose_handler, name="diagnosis_exhausted")
        p.register("repair_command", escalation_handler, name="escalation",
                    filter=lambda e: e.payload.get("strategy") == "escalate")

        await p.emit("trigger", {}, source="test")
        p._last_seen_id = 0

        for _ in range(10):
            await p.tick()

        # Should eventually emit human_intervention_needed
        rows = await sqlite_db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = 'human_intervention_needed'"
        )
        # At least one escalation should have fired
        assert len(rows) >= 1
