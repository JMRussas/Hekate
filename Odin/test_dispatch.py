"""Tests for gods/odin/dispatch.py — model selection, retry strategy, wave parallelism."""

import asyncio
import json

import pytest

from gods.odin.dispatch import (
    DispatchCommand,
    FailureDiagnosis,
    build_dispatch_commands,
    compute_wave_parallelism,
    diagnose_failure,
    select_provider,
)


# ---------------------------------------------------------------------------
# FakeDB for async queries
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self, fetchall_results=None):
        self._fetchall_results = fetchall_results or []
        self._fetchall_index = 0

    async def execute_write(self, sql, params=()):
        return "OK"

    async def fetchone(self, sql, params=()):
        return None

    async def fetchall(self, sql, params=()):
        if self._fetchall_index < len(self._fetchall_results):
            result = self._fetchall_results[self._fetchall_index]
            self._fetchall_index += 1
            return result
        return []


# ---------------------------------------------------------------------------
# select_provider tests
# ---------------------------------------------------------------------------

class TestSelectProvider:
    def test_default_code_medium_picks_claude(self):
        provider, reason = select_provider(
            "code", "medium",
            {"claude_code": True, "gemini_cli": True, "ollama": True},
        )
        assert provider == "claude_code"
        assert "claude_code" in reason

    def test_research_simple_picks_gemini(self):
        provider, _ = select_provider(
            "research", "simple",
            {"claude_code": True, "gemini_cli": True, "ollama": True},
        )
        assert provider == "gemini_cli"

    def test_asset_picks_ollama(self):
        provider, _ = select_provider(
            "asset", "simple",
            {"claude_code": True, "gemini_cli": True, "ollama": True},
        )
        assert provider == "ollama"

    def test_falls_back_when_recommended_unavailable(self):
        provider, reason = select_provider(
            "code", "medium",
            {"claude_code": False, "gemini_cli": True, "ollama": True},
        )
        assert provider == "gemini_cli"
        assert "fallback" in reason

    def test_skips_previously_failed_provider(self):
        provider, _ = select_provider(
            "code", "medium",
            {"claude_code": True, "gemini_cli": True, "ollama": True},
            failure_history=[{"model_tier": "claude_code", "error": "rate limit"}],
        )
        assert provider == "gemini_cli"

    def test_budget_tight_prefers_ollama(self):
        provider, reason = select_provider(
            "code", "simple",
            {"claude_code": True, "gemini_cli": True, "ollama": True},
            budget_remaining=0.5,
        )
        assert provider == "ollama"
        assert "budget" in reason.lower()

    def test_all_providers_unavailable_forces_ollama(self):
        provider, reason = select_provider(
            "code", "complex",
            {"claude_code": False, "gemini_cli": False, "ollama": False},
        )
        assert provider == "ollama"
        assert "forced" in reason.lower() or "fallback" in reason.lower()

    def test_unknown_task_type_defaults_to_claude(self):
        provider, _ = select_provider(
            "mystery_type", "medium",
            {"claude_code": True, "gemini_cli": True, "ollama": True},
        )
        assert provider == "claude_code"

    def test_exhausted_alternatives_retries_original(self):
        """When all alternatives failed, re-select the original if available."""
        provider, reason = select_provider(
            "code", "medium",
            {"claude_code": True, "gemini_cli": True, "ollama": True},
            failure_history=[
                {"model_tier": "claude_code", "error": "err1"},
                {"model_tier": "gemini_cli", "error": "err2"},
                {"model_tier": "ollama", "error": "err3"},
            ],
        )
        # Should pick the first available since all have failed
        assert provider in ("claude_code", "gemini_cli", "ollama")
        assert "exhausted" in reason.lower()


# ---------------------------------------------------------------------------
# diagnose_failure tests
# ---------------------------------------------------------------------------

class TestDiagnoseFailure:
    def test_retries_exhausted_returns_skip(self):
        diag = diagnose_failure(
            task_id="abc123",
            error="some error",
            retry_count=3,
            max_retries=3,
            model_tier="claude_code",
            available_providers={"claude_code": True},
        )
        assert diag.fix_type == "skip"
        assert diag.confidence >= 0.8
        assert "exhausted" in diag.root_cause.lower()

    def test_rate_limit_reassigns_tier(self):
        diag = diagnose_failure(
            task_id="abc123",
            error="Error: rate limit exceeded",
            retry_count=0,
            max_retries=3,
            model_tier="claude_code",
            available_providers={"claude_code": True, "gemini_cli": True, "ollama": True},
        )
        assert diag.fix_type == "reassign_tier"
        assert diag.new_tier is not None
        assert diag.new_tier != "claude_code"

    def test_syntax_error_modifies_prompt(self):
        diag = diagnose_failure(
            task_id="abc123",
            error="SyntaxError: unexpected indent at line 42",
            retry_count=0,
            max_retries=3,
            model_tier="claude_code",
            available_providers={"claude_code": True},
        )
        assert diag.fix_type == "modify_prompt"
        assert diag.prompt_guidance is not None
        assert "syntax" in diag.prompt_guidance.lower()

    def test_timeout_retries_as_is(self):
        diag = diagnose_failure(
            task_id="abc123",
            error="execution timed out after 600s",
            retry_count=0,
            max_retries=3,
            model_tier="claude_code",
            available_providers={"claude_code": True},
        )
        assert diag.fix_type == "retry_as_is"

    def test_unknown_error_low_confidence(self):
        diag = diagnose_failure(
            task_id="abc123",
            error="Something completely unexpected happened",
            retry_count=0,
            max_retries=3,
            model_tier="claude_code",
            available_providers={"claude_code": True},
        )
        assert diag.fix_type == "retry_as_is"
        assert diag.confidence <= 0.5

    def test_reassign_with_no_alternatives_falls_back_to_retry(self):
        diag = diagnose_failure(
            task_id="abc123",
            error="HTTP 429 too many requests",
            retry_count=0,
            max_retries=3,
            model_tier="claude_code",
            available_providers={
                "claude_code": True, "gemini_cli": False, "ollama": False,
            },
        )
        # Only claude_code available — no alternative to reassign to
        assert diag.fix_type == "retry_as_is"
        assert diag.confidence < 0.8

    def test_repeated_failures_escalate(self):
        diag = diagnose_failure(
            task_id="abc123",
            error="generic error",
            retry_count=1,
            max_retries=3,
            model_tier="claude_code",
            available_providers={"claude_code": True, "gemini_cli": True},
            recent_decisions=[
                {"task_id": "abc123", "action_taken": "retry"},
                {"task_id": "abc123", "action_taken": "retry"},
            ],
        )
        assert diag.fix_type in ("reassign_tier", "modify_prompt")

    def test_why_chain_is_populated(self):
        diag = diagnose_failure(
            task_id="abc123",
            error="ModuleNotFoundError: No module named 'foo'",
            retry_count=0,
            max_retries=3,
            model_tier="claude_code",
            available_providers={"claude_code": True},
        )
        assert len(diag.why_chain) >= 1
        assert any("ModuleNotFoundError" in step for step in diag.why_chain)

    def test_to_dict_includes_all_fields(self):
        diag = FailureDiagnosis(
            task_id="abc",
            fix_type="reassign_tier",
            confidence=0.8,
            root_cause="rate limit",
            reasoning="test",
            new_tier="gemini_cli",
            why_chain=["step1"],
        )
        d = diag.to_dict()
        assert d["task_id"] == "abc"
        assert d["fix_type"] == "reassign_tier"
        assert d["new_tier"] == "gemini_cli"
        assert d["why_chain"] == ["step1"]


# ---------------------------------------------------------------------------
# compute_wave_parallelism tests
# ---------------------------------------------------------------------------

class TestWaveParallelism:
    def test_no_open_slots(self):
        assert compute_wave_parallelism(
            ready_task_count=5, running_task_count=4,
            available_providers={"claude_code": True},
            max_concurrent=4,
        ) == 0

    def test_respects_max_concurrent(self):
        result = compute_wave_parallelism(
            ready_task_count=10, running_task_count=0,
            available_providers={"claude_code": True, "gemini_cli": True, "ollama": True},
            max_concurrent=4,
        )
        assert result <= 4

    def test_no_providers_returns_zero(self):
        assert compute_wave_parallelism(
            ready_task_count=5, running_task_count=0,
            available_providers={"claude_code": False, "gemini_cli": False, "ollama": False},
            max_concurrent=4,
        ) == 0

    def test_budget_constrained_caps_at_one(self):
        result = compute_wave_parallelism(
            ready_task_count=5, running_task_count=0,
            available_providers={"claude_code": True, "gemini_cli": True},
            max_concurrent=4,
            budget_remaining=2.0,
        )
        assert result == 1

    def test_fewer_ready_than_slots(self):
        result = compute_wave_parallelism(
            ready_task_count=2, running_task_count=0,
            available_providers={"claude_code": True, "gemini_cli": True, "ollama": True},
            max_concurrent=4,
        )
        assert result == 2

    def test_single_provider_scales_down(self):
        result_full = compute_wave_parallelism(
            ready_task_count=10, running_task_count=0,
            available_providers={"claude_code": True, "gemini_cli": True, "ollama": True},
            max_concurrent=4,
        )
        result_single = compute_wave_parallelism(
            ready_task_count=10, running_task_count=0,
            available_providers={"claude_code": True, "gemini_cli": False, "ollama": False},
            max_concurrent=4,
        )
        assert result_single <= result_full


# ---------------------------------------------------------------------------
# DispatchCommand tests
# ---------------------------------------------------------------------------

class TestDispatchCommand:
    def test_to_dict(self):
        cmd = DispatchCommand(
            task_id="t1", project_id="p1",
            provider="claude_code", model=None,
            priority=1, timeout=300, reason="test",
        )
        d = cmd.to_dict()
        assert d["task_id"] == "t1"
        assert d["provider"] == "claude_code"
        assert d["model"] is None
        assert d["reason"] == "test"


# ---------------------------------------------------------------------------
# build_dispatch_commands integration test (with fake DB)
# ---------------------------------------------------------------------------

class TestBuildDispatchCommands:
    @pytest.mark.asyncio
    async def test_no_projects_returns_empty(self):
        commands, diagnoses = await build_dispatch_commands(
            world={"projects": []},
            db=FakeDB(),
            llm_gateway_url="http://localhost:9999",
        )
        assert commands == []
        assert diagnoses == []

    @pytest.mark.asyncio
    async def test_non_executing_project_skipped(self):
        world = {
            "projects": [{
                "id": "p1", "name": "test", "status": "draft",
                "task_counts": {}, "current_wave": 0, "anomalies": None,
            }],
            "recent_decisions": [],
        }
        commands, diagnoses = await build_dispatch_commands(
            world=world, db=FakeDB(),
            llm_gateway_url="http://localhost:9999",
        )
        assert commands == []

    @pytest.mark.asyncio
    async def test_diagnoses_failed_tasks(self):
        world = {
            "projects": [{
                "id": "p1", "name": "test", "status": "executing",
                "task_counts": {"failed": 1},
                "current_wave": 0,
                "anomalies": [{
                    "task_id": "t1", "title": "broken task",
                    "status": "failed",
                    "error": "SyntaxError: invalid syntax",
                    "retry_count": 0, "max_retries": 3,
                    "model_tier": "claude_code",
                    "running_seconds": 0,
                }],
            }],
            "recent_decisions": [],
        }
        # No ready tasks from DB
        commands, diagnoses = await build_dispatch_commands(
            world=world, db=FakeDB(),
            llm_gateway_url="http://localhost:9999",
        )
        assert len(diagnoses) == 1
        assert diagnoses[0].task_id == "t1"
        assert diagnoses[0].fix_type == "modify_prompt"
