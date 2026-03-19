"""Tests for gods/plan_levels.py — L1-L5 planning level system with rule engine.

RED PHASE: gods/plan_levels.py does not exist yet.

Levels:
  L1: rough task breakdown (what needs doing)
  L2: detailed specs per task (how, affected files, deps)
  L3: implementation details (code patterns, edge cases, test strategy)
  L4: exact changes (specific code blocks, signatures, return values)
  L5: executable spec (tool applies directly, no LLM needed)

Rule engine validates completeness at each level before advancing.
"""

import pytest
from gods.plan_levels import (
    PlanLevel,
    TaskSpec,
    PlanConfig,
    RuleResult,
    validate_plan,
    validate_task_at_level,
    suggest_target_level,
)


# ---------------------------------------------------------------------------
# Fixtures — plan data at different levels
# ---------------------------------------------------------------------------

def _l1_task(**overrides):
    """Minimal L1 task — just title and type."""
    base = {
        "id": "t1",
        "title": "Add health endpoint",
        "task_type": "code",
        "wave": 0,
    }
    base.update(overrides)
    return TaskSpec(**base)


def _l2_task(**overrides):
    """L2 task — adds affected files, deps, description."""
    base = {
        "id": "t1",
        "title": "Add health endpoint",
        "task_type": "code",
        "wave": 0,
        "description": "Add GET /api/health/detailed that returns service versions and uptime",
        "affected_files": ["orchestration/backend/routes/health.py"],
        "depends_on": [],
        "complexity": "simple",
    }
    base.update(overrides)
    return TaskSpec(**base)


def _l3_task(**overrides):
    """L3 task — adds implementation details, test strategy."""
    base = {
        "id": "t1",
        "title": "Add health endpoint",
        "task_type": "code",
        "wave": 0,
        "description": "Add GET /api/health/detailed that returns service versions and uptime",
        "affected_files": ["orchestration/backend/routes/health.py"],
        "depends_on": [],
        "complexity": "simple",
        "implementation_notes": "Use FastAPI router, return JSON with version from importlib.metadata",
        "test_strategy": "Test endpoint returns 200 with expected keys: version, uptime, db_status",
        "edge_cases": ["DB connection down should return degraded status, not 500"],
    }
    base.update(overrides)
    return TaskSpec(**base)


def _l4_task(**overrides):
    """L4 task — adds exact changes, method signatures."""
    base = {
        "id": "t1",
        "title": "Add health endpoint",
        "task_type": "code",
        "wave": 0,
        "description": "Add GET /api/health/detailed that returns service versions and uptime",
        "affected_files": ["orchestration/backend/routes/health.py"],
        "depends_on": [],
        "complexity": "simple",
        "implementation_notes": "Use FastAPI router, return JSON with importlib.metadata",
        "test_strategy": "Test endpoint returns 200 with expected keys",
        "edge_cases": ["DB connection down returns degraded"],
        "changes": [
            {
                "file": "orchestration/backend/routes/health.py",
                "action": "add_function",
                "name": "detailed_health",
                "signature": "async def detailed_health() -> dict",
                "returns": '{"version": str, "uptime_seconds": float, "db_status": str}',
            }
        ],
    }
    base.update(overrides)
    return TaskSpec(**base)


def _l5_task(**overrides):
    """L5 task — executable spec, tool can apply directly."""
    base = {
        "id": "t1",
        "title": "Add health endpoint",
        "task_type": "code",
        "wave": 0,
        "description": "Add GET /api/health/detailed that returns service versions and uptime",
        "affected_files": ["orchestration/backend/routes/health.py"],
        "depends_on": [],
        "complexity": "simple",
        "implementation_notes": "Use FastAPI router",
        "test_strategy": "Test returns 200 with keys",
        "edge_cases": ["DB down returns degraded"],
        "changes": [
            {
                "file": "orchestration/backend/routes/health.py",
                "action": "add_function",
                "name": "detailed_health",
                "signature": "async def detailed_health() -> dict",
                "returns": '{"version": str, "uptime_seconds": float, "db_status": str}',
                "body": (
                    "import time, importlib.metadata\n"
                    "version = importlib.metadata.version('orchestration')\n"
                    "uptime = time.time() - app.state.start_time\n"
                    "return {'version': version, 'uptime_seconds': uptime, 'db_status': 'ok'}"
                ),
            }
        ],
    }
    base.update(overrides)
    return TaskSpec(**base)


# ---------------------------------------------------------------------------
# PlanLevel enum
# ---------------------------------------------------------------------------

class TestPlanLevel:
    def test_level_ordering(self):
        assert PlanLevel.L1 < PlanLevel.L2 < PlanLevel.L3 < PlanLevel.L4 < PlanLevel.L5

    def test_level_from_string(self):
        assert PlanLevel.from_str("L1") == PlanLevel.L1
        assert PlanLevel.from_str("L3") == PlanLevel.L3
        assert PlanLevel.from_str("L5") == PlanLevel.L5

    def test_level_from_string_case_insensitive(self):
        assert PlanLevel.from_str("l2") == PlanLevel.L2

    def test_level_from_string_invalid(self):
        with pytest.raises(ValueError):
            PlanLevel.from_str("L6")

    def test_level_next(self):
        assert PlanLevel.L1.next() == PlanLevel.L2
        assert PlanLevel.L4.next() == PlanLevel.L5

    def test_level_next_at_max(self):
        assert PlanLevel.L5.next() is None


# ---------------------------------------------------------------------------
# TaskSpec dataclass
# ---------------------------------------------------------------------------

class TestTaskSpec:
    def test_l1_minimal(self):
        t = _l1_task()
        assert t.id == "t1"
        assert t.title == "Add health endpoint"
        assert t.affected_files is None or t.affected_files == []

    def test_l2_has_files_and_deps(self):
        t = _l2_task()
        assert len(t.affected_files) > 0
        assert t.description is not None
        assert t.complexity is not None


# ---------------------------------------------------------------------------
# Rule engine — validate_task_at_level
# ---------------------------------------------------------------------------

class TestValidateTaskL1:
    def test_valid_l1(self):
        result = validate_task_at_level(_l1_task(), PlanLevel.L1)
        assert result.passed

    def test_l1_missing_title(self):
        result = validate_task_at_level(_l1_task(title=""), PlanLevel.L1)
        assert not result.passed
        assert "title" in result.reason.lower()

    def test_l1_missing_task_type(self):
        result = validate_task_at_level(_l1_task(task_type=""), PlanLevel.L1)
        assert not result.passed

    def test_l1_missing_wave(self):
        result = validate_task_at_level(_l1_task(wave=None), PlanLevel.L1)
        assert not result.passed


class TestValidateTaskL2:
    def test_valid_l2(self):
        result = validate_task_at_level(_l2_task(), PlanLevel.L2)
        assert result.passed

    def test_l2_missing_description(self):
        result = validate_task_at_level(_l2_task(description=""), PlanLevel.L2)
        assert not result.passed
        assert "description" in result.reason.lower()

    def test_l2_missing_affected_files(self):
        result = validate_task_at_level(_l2_task(affected_files=[]), PlanLevel.L2)
        assert not result.passed
        assert "affected_files" in result.reason.lower()

    def test_l2_missing_complexity(self):
        result = validate_task_at_level(_l2_task(complexity=None), PlanLevel.L2)
        assert not result.passed

    def test_l1_task_fails_l2(self):
        """An L1 task should fail L2 validation."""
        result = validate_task_at_level(_l1_task(), PlanLevel.L2)
        assert not result.passed


class TestValidateTaskL3:
    def test_valid_l3(self):
        result = validate_task_at_level(_l3_task(), PlanLevel.L3)
        assert result.passed

    def test_l3_missing_implementation_notes(self):
        result = validate_task_at_level(_l3_task(implementation_notes=""), PlanLevel.L3)
        assert not result.passed

    def test_l3_missing_test_strategy(self):
        result = validate_task_at_level(_l3_task(test_strategy=""), PlanLevel.L3)
        assert not result.passed

    def test_l3_missing_edge_cases(self):
        result = validate_task_at_level(_l3_task(edge_cases=[]), PlanLevel.L3)
        assert not result.passed

    def test_l2_task_fails_l3(self):
        result = validate_task_at_level(_l2_task(), PlanLevel.L3)
        assert not result.passed


class TestValidateTaskL4:
    def test_valid_l4(self):
        result = validate_task_at_level(_l4_task(), PlanLevel.L4)
        assert result.passed

    def test_l4_missing_changes(self):
        result = validate_task_at_level(_l4_task(changes=[]), PlanLevel.L4)
        assert not result.passed
        assert "changes" in result.reason.lower()

    def test_l4_change_missing_signature(self):
        bad_change = [{"file": "foo.py", "action": "add_function", "name": "bar"}]
        result = validate_task_at_level(_l4_task(changes=bad_change), PlanLevel.L4)
        assert not result.passed
        assert "signature" in result.reason.lower()

    def test_l3_task_fails_l4(self):
        result = validate_task_at_level(_l3_task(), PlanLevel.L4)
        assert not result.passed


class TestValidateTaskL5:
    def test_valid_l5(self):
        result = validate_task_at_level(_l5_task(), PlanLevel.L5)
        assert result.passed

    def test_l5_change_missing_body(self):
        changes = [{
            "file": "foo.py", "action": "add_function",
            "name": "bar", "signature": "def bar() -> None",
        }]
        result = validate_task_at_level(_l5_task(changes=changes), PlanLevel.L5)
        assert not result.passed
        assert "body" in result.reason.lower()

    def test_l4_task_fails_l5(self):
        result = validate_task_at_level(_l4_task(), PlanLevel.L5)
        assert not result.passed


# ---------------------------------------------------------------------------
# Rule engine — validate_plan (whole plan)
# ---------------------------------------------------------------------------

class TestValidatePlan:
    def test_valid_l1_plan(self):
        tasks = [_l1_task(id="t1"), _l1_task(id="t2", title="Add tests")]
        result = validate_plan(tasks, PlanLevel.L1, requirements="Add health endpoint with tests")
        assert result.passed

    def test_empty_plan_fails(self):
        result = validate_plan([], PlanLevel.L1, requirements="Do something")
        assert not result.passed
        assert "empty" in result.reason.lower() or "no tasks" in result.reason.lower()

    def test_duplicate_task_ids(self):
        tasks = [_l1_task(id="t1"), _l1_task(id="t1", title="Different")]
        result = validate_plan(tasks, PlanLevel.L1, requirements="Something")
        assert not result.passed
        assert "duplicate" in result.reason.lower()

    def test_circular_deps(self):
        t1 = _l2_task(id="t1", depends_on=["t2"])
        t2 = _l2_task(id="t2", title="Other task", depends_on=["t1"])
        result = validate_plan([t1, t2], PlanLevel.L2, requirements="Something")
        assert not result.passed
        assert "circular" in result.reason.lower()

    def test_missing_dep_target(self):
        t1 = _l2_task(id="t1", depends_on=["t99"])
        result = validate_plan([t1], PlanLevel.L2, requirements="Something")
        assert not result.passed
        assert "t99" in result.reason.lower()

    def test_invalid_wave_order(self):
        """Tasks in wave 1 that depend on nothing should be wave 0."""
        t1 = _l2_task(id="t1", wave=1, depends_on=[])
        result = validate_plan([t1], PlanLevel.L2, requirements="Something")
        # Should warn but not necessarily fail — wave ordering is advisory
        assert result.passed or "wave" in result.reason.lower()

    def test_task_too_vague_for_level(self):
        """An L1 task in an L2 plan should fail."""
        tasks = [_l2_task(id="t1"), _l1_task(id="t2", title="Do something")]
        result = validate_plan(tasks, PlanLevel.L2, requirements="Something")
        assert not result.passed

    def test_single_task_plan_valid(self):
        result = validate_plan([_l1_task()], PlanLevel.L1, requirements="Add endpoint")
        assert result.passed


# ---------------------------------------------------------------------------
# RuleResult
# ---------------------------------------------------------------------------

class TestRuleResult:
    def test_passed(self):
        r = RuleResult(passed=True, reason="OK")
        assert r.passed
        assert r.errors == []

    def test_failed_with_errors(self):
        r = RuleResult(passed=False, reason="Bad plan", errors=["no tasks", "circular deps"])
        assert not r.passed
        assert len(r.errors) == 2

    def test_merge_results(self):
        r1 = RuleResult(passed=True, reason="OK")
        r2 = RuleResult(passed=False, reason="Missing files", errors=["t1: no affected_files"])
        merged = RuleResult.merge([r1, r2])
        assert not merged.passed
        assert len(merged.errors) == 1


# ---------------------------------------------------------------------------
# PlanConfig
# ---------------------------------------------------------------------------

class TestPlanConfig:
    def test_defaults(self):
        cfg = PlanConfig()
        assert cfg.tdd is True
        assert cfg.narration is True
        assert cfg.target_level == "auto"
        assert cfg.direct_write is True

    def test_custom(self):
        cfg = PlanConfig(tdd=False, target_level="L3", max_concurrent=1)
        assert cfg.tdd is False
        assert cfg.target_level == "L3"
        assert cfg.max_concurrent == 1


# ---------------------------------------------------------------------------
# suggest_target_level — auto depth based on complexity/tooling
# ---------------------------------------------------------------------------

class TestSuggestTargetLevel:
    def test_simple_code_suggests_l4(self):
        level = suggest_target_level(
            task_type="code", complexity="simple",
            has_roslyn=False, has_jedi=False, task_count=1,
        )
        assert level == PlanLevel.L4

    def test_complex_code_suggests_l3(self):
        level = suggest_target_level(
            task_type="code", complexity="complex",
            has_roslyn=False, has_jedi=False, task_count=5,
        )
        assert level == PlanLevel.L3

    def test_csharp_with_roslyn_suggests_l5(self):
        level = suggest_target_level(
            task_type="code", complexity="simple",
            has_roslyn=True, has_jedi=False, task_count=1,
        )
        assert level == PlanLevel.L5

    def test_python_with_jedi_suggests_l5(self):
        level = suggest_target_level(
            task_type="code", complexity="simple",
            has_roslyn=False, has_jedi=True, task_count=1,
        )
        assert level == PlanLevel.L5

    def test_python_jedi_medium_suggests_l5(self):
        level = suggest_target_level(
            task_type="code", complexity="medium",
            has_roslyn=False, has_jedi=True, task_count=3,
        )
        assert level == PlanLevel.L5

    def test_python_jedi_complex_suggests_l3(self):
        """Complex tasks should stay at L3 even with tooling — LLM needs room."""
        level = suggest_target_level(
            task_type="code", complexity="complex",
            has_roslyn=False, has_jedi=True, task_count=5,
        )
        assert level == PlanLevel.L3

    def test_typescript_with_compiler_suggests_l5(self):
        level = suggest_target_level(
            task_type="code", complexity="simple",
            has_roslyn=False, has_jedi=False, has_ts_compiler=True, task_count=1,
        )
        assert level == PlanLevel.L5

    def test_typescript_medium_suggests_l5(self):
        level = suggest_target_level(
            task_type="code", complexity="medium",
            has_roslyn=False, has_jedi=False, has_ts_compiler=True, task_count=3,
        )
        assert level == PlanLevel.L5

    def test_medium_without_tooling_suggests_l4(self):
        level = suggest_target_level(
            task_type="code", complexity="medium",
            has_roslyn=False, has_jedi=False, task_count=3,
        )
        assert level == PlanLevel.L4

    def test_research_suggests_l2(self):
        level = suggest_target_level(
            task_type="research", complexity="medium",
            has_roslyn=False, has_jedi=False, task_count=3,
        )
        assert level <= PlanLevel.L2

    def test_documentation_suggests_l2(self):
        level = suggest_target_level(
            task_type="documentation", complexity="simple",
            has_roslyn=False, has_jedi=False, task_count=1,
        )
        assert level <= PlanLevel.L2
