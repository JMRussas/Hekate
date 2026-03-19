"""Tests for gods/plan_rules.py — rule engine for plan level validation.

RED PHASE: gods/plan_rules.py does not exist yet.

The rule engine validates plan completeness at each level (L1-L5) and
gates level transitions. Rules are structural checks — no AI needed.

Plan levels:
  L1: rough task breakdown (title, type, wave)
  L2: detailed specs (description, affected_files, deps, complexity)
  L3: implementation details (approach, patterns, test_strategy, edge_cases)
  L4: exact changes (method_signatures, code_blocks, line_targets)
  L5: executable spec (full diffs, tool can apply directly)

Config:
  tdd: bool (default True) — require TDD phase
  narration: bool (default True) — model talks through steps
  target_level: "auto"|"L1"|"L2"|"L3"|"L4"|"L5" (default "auto")
"""

import pytest

# RED: these don't exist yet
from gods.plan_rules import (
    PlanLevel,
    PlanConfig,
    RuleResult,
    validate_plan,
    check_level_requirements,
    check_duplicates,
    check_deps,
    check_requirement_coverage,
    check_task_scope,
    suggest_target_level,
)


# ---------------------------------------------------------------------------
# Plan level schemas
# ---------------------------------------------------------------------------

class TestPlanLevel:
    def test_levels_ordered(self):
        assert PlanLevel.L1 < PlanLevel.L2 < PlanLevel.L3 < PlanLevel.L4 < PlanLevel.L5

    def test_level_from_string(self):
        assert PlanLevel.from_str("L1") == PlanLevel.L1
        assert PlanLevel.from_str("L3") == PlanLevel.L3
        assert PlanLevel.from_str("auto") is None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestPlanConfig:
    def test_defaults(self):
        cfg = PlanConfig()
        assert cfg.tdd is True
        assert cfg.narration is True
        assert cfg.target_level == "auto"

    def test_override(self):
        cfg = PlanConfig(tdd=False, narration=False, target_level="L3")
        assert cfg.tdd is False
        assert cfg.target_level == "L3"

    def test_from_dict(self):
        cfg = PlanConfig.from_dict({"tdd": False, "target_level": "L4"})
        assert cfg.tdd is False
        assert cfg.target_level == "L4"
        assert cfg.narration is True  # default preserved


# ---------------------------------------------------------------------------
# Rule results
# ---------------------------------------------------------------------------

class TestRuleResult:
    def test_passed(self):
        r = RuleResult(passed=True)
        assert r.passed
        assert r.errors == []

    def test_failed_with_errors(self):
        r = RuleResult(passed=False, errors=["missing title", "no wave"])
        assert not r.passed
        assert len(r.errors) == 2

    def test_warnings_dont_fail(self):
        r = RuleResult(passed=True, warnings=["task might be too large"])
        assert r.passed
        assert len(r.warnings) == 1


# ---------------------------------------------------------------------------
# L1 rules: every task needs title, type, wave
# ---------------------------------------------------------------------------

class TestL1Rules:
    def test_valid_l1_plan(self):
        tasks = [
            {"title": "Add endpoint", "task_type": "code", "wave": 0},
            {"title": "Write test", "task_type": "code", "wave": 1},
        ]
        result = check_level_requirements(tasks, PlanLevel.L1)
        assert result.passed

    def test_missing_title(self):
        tasks = [{"task_type": "code", "wave": 0}]
        result = check_level_requirements(tasks, PlanLevel.L1)
        assert not result.passed
        assert any("title" in e for e in result.errors)

    def test_missing_wave(self):
        tasks = [{"title": "Do thing", "task_type": "code"}]
        result = check_level_requirements(tasks, PlanLevel.L1)
        assert not result.passed
        assert any("wave" in e for e in result.errors)

    def test_empty_plan_fails(self):
        result = check_level_requirements([], PlanLevel.L1)
        assert not result.passed
        assert any("empty" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# L2 rules: L1 + description, affected_files, deps, complexity
# ---------------------------------------------------------------------------

class TestL2Rules:
    def test_valid_l2_plan(self):
        tasks = [
            {
                "title": "Add endpoint",
                "task_type": "code",
                "wave": 0,
                "description": "Add GET /api/health/detailed returning JSON",
                "affected_files": ["orchestration/backend/routes/health.py"],
                "complexity": "simple",
            },
        ]
        result = check_level_requirements(tasks, PlanLevel.L2)
        assert result.passed

    def test_missing_description_at_l2(self):
        tasks = [
            {"title": "Add endpoint", "task_type": "code", "wave": 0,
             "affected_files": ["foo.py"], "complexity": "simple"},
        ]
        result = check_level_requirements(tasks, PlanLevel.L2)
        assert not result.passed
        assert any("description" in e for e in result.errors)

    def test_missing_affected_files_at_l2(self):
        tasks = [
            {"title": "Add endpoint", "task_type": "code", "wave": 0,
             "description": "Do something", "complexity": "simple"},
        ]
        result = check_level_requirements(tasks, PlanLevel.L2)
        assert not result.passed
        assert any("affected_files" in e for e in result.errors)

    def test_l1_fields_still_required(self):
        """L2 must also pass L1 rules."""
        tasks = [
            {"description": "Do something", "affected_files": ["foo.py"],
             "complexity": "simple"},
        ]
        result = check_level_requirements(tasks, PlanLevel.L2)
        assert not result.passed


# ---------------------------------------------------------------------------
# L3 rules: L2 + approach, patterns, test_strategy, edge_cases
# ---------------------------------------------------------------------------

class TestL3Rules:
    def test_valid_l3_plan(self):
        tasks = [
            {
                "title": "Add endpoint", "task_type": "code", "wave": 0,
                "description": "Add GET /api/health/detailed",
                "affected_files": ["routes/health.py"],
                "complexity": "simple",
                "approach": "Add a new route handler using FastAPI dependency injection",
                "test_strategy": "Unit test with TestClient, mock DB",
                "edge_cases": ["DB connection timeout", "missing VERSION constant"],
            },
        ]
        result = check_level_requirements(tasks, PlanLevel.L3)
        assert result.passed

    def test_missing_approach_at_l3(self):
        tasks = [
            {"title": "X", "task_type": "code", "wave": 0,
             "description": "Y", "affected_files": ["f.py"], "complexity": "simple",
             "test_strategy": "unit", "edge_cases": ["none"]},
        ]
        result = check_level_requirements(tasks, PlanLevel.L3)
        assert not result.passed
        assert any("approach" in e for e in result.errors)

    def test_missing_test_strategy_at_l3(self):
        tasks = [
            {"title": "X", "task_type": "code", "wave": 0,
             "description": "Y", "affected_files": ["f.py"], "complexity": "simple",
             "approach": "Do it", "edge_cases": ["none"]},
        ]
        result = check_level_requirements(tasks, PlanLevel.L3)
        assert not result.passed
        assert any("test_strategy" in e for e in result.errors)


# ---------------------------------------------------------------------------
# L4 rules: L3 + method_signatures, code_blocks, line_targets
# ---------------------------------------------------------------------------

class TestL4Rules:
    def test_valid_l4_plan(self):
        tasks = [
            {
                "title": "Add endpoint", "task_type": "code", "wave": 0,
                "description": "Add GET /api/health/detailed",
                "affected_files": ["routes/health.py"],
                "complexity": "simple",
                "approach": "FastAPI route handler",
                "test_strategy": "TestClient",
                "edge_cases": ["timeout"],
                "method_signatures": [
                    {"file": "routes/health.py", "name": "health_detailed",
                     "params": [], "returns": "dict"},
                ],
                "code_blocks": [
                    {"file": "routes/health.py", "action": "add",
                     "after_line": 42, "code": "@router.get('/health/detailed')"},
                ],
            },
        ]
        result = check_level_requirements(tasks, PlanLevel.L4)
        assert result.passed

    def test_missing_method_signatures_at_l4(self):
        tasks = [
            {"title": "X", "task_type": "code", "wave": 0,
             "description": "Y", "affected_files": ["f.py"], "complexity": "simple",
             "approach": "Z", "test_strategy": "T", "edge_cases": ["E"],
             "code_blocks": [{"file": "f.py", "action": "add", "code": "x"}]},
        ]
        result = check_level_requirements(tasks, PlanLevel.L4)
        assert not result.passed


# ---------------------------------------------------------------------------
# L5 rules: L4 + full executable diffs
# ---------------------------------------------------------------------------

class TestL5Rules:
    def test_valid_l5_plan(self):
        tasks = [
            {
                "title": "Add endpoint", "task_type": "code", "wave": 0,
                "description": "Add GET /api/health/detailed",
                "affected_files": ["routes/health.py"],
                "complexity": "simple",
                "approach": "FastAPI route handler",
                "test_strategy": "TestClient",
                "edge_cases": ["timeout"],
                "method_signatures": [{"file": "routes/health.py", "name": "f", "params": [], "returns": "dict"}],
                "code_blocks": [{"file": "routes/health.py", "action": "add", "code": "x"}],
                "diffs": [
                    {"file": "routes/health.py",
                     "hunks": [{"old_start": 40, "old_lines": 0, "new_start": 40, "new_lines": 5,
                                "content": "+@router.get('/health/detailed')\n+async def health_detailed():\n+    return {'status': 'ok'}"}]},
                ],
            },
        ]
        result = check_level_requirements(tasks, PlanLevel.L5)
        assert result.passed

    def test_missing_diffs_at_l5(self):
        tasks = [
            {"title": "X", "task_type": "code", "wave": 0,
             "description": "Y", "affected_files": ["f.py"], "complexity": "simple",
             "approach": "Z", "test_strategy": "T", "edge_cases": ["E"],
             "method_signatures": [{"file": "f.py", "name": "f", "params": [], "returns": "str"}],
             "code_blocks": [{"file": "f.py", "action": "add", "code": "x"}]},
        ]
        result = check_level_requirements(tasks, PlanLevel.L5)
        assert not result.passed


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

class TestDuplicates:
    def test_no_duplicates(self):
        tasks = [
            {"title": "Add endpoint", "wave": 0},
            {"title": "Write test", "wave": 1},
        ]
        result = check_duplicates(tasks)
        assert result.passed

    def test_exact_duplicates(self):
        tasks = [
            {"title": "Implement health endpoint", "wave": 0},
            {"title": "Implement health endpoint", "wave": 0},
        ]
        result = check_duplicates(tasks)
        assert not result.passed
        assert any("duplicate" in e.lower() for e in result.errors)

    def test_fuzzy_duplicates(self):
        """Titles that are substantially similar should be caught."""
        tasks = [
            {"title": "Implement /api/health/detailed Endpoint", "wave": 0},
            {"title": "Implement /api/health/detailed endpoint", "wave": 0},
        ]
        result = check_duplicates(tasks)
        assert not result.passed

    def test_similar_but_different_waves_ok(self):
        """Same work in different waves might be intentional (unlikely but allowed)."""
        tasks = [
            {"title": "Run integration tests", "wave": 1},
            {"title": "Run integration tests", "wave": 3},
        ]
        result = check_duplicates(tasks)
        # Still flagged as warning, not error
        assert result.passed
        assert len(result.warnings) > 0


# ---------------------------------------------------------------------------
# Dependency validation
# ---------------------------------------------------------------------------

class TestDeps:
    def test_valid_deps(self):
        tasks = [
            {"id": "t1", "title": "A", "wave": 0, "depends_on": []},
            {"id": "t2", "title": "B", "wave": 1, "depends_on": ["t1"]},
        ]
        result = check_deps(tasks)
        assert result.passed

    def test_missing_dep_target(self):
        tasks = [
            {"id": "t1", "title": "A", "wave": 0, "depends_on": []},
            {"id": "t2", "title": "B", "wave": 1, "depends_on": ["t99"]},
        ]
        result = check_deps(tasks)
        assert not result.passed
        assert any("t99" in e for e in result.errors)

    def test_circular_deps(self):
        tasks = [
            {"id": "t1", "title": "A", "wave": 0, "depends_on": ["t2"]},
            {"id": "t2", "title": "B", "wave": 0, "depends_on": ["t1"]},
        ]
        result = check_deps(tasks)
        assert not result.passed
        assert any("circular" in e.lower() for e in result.errors)

    def test_dep_in_later_wave_warns(self):
        """Depending on a task in a later wave is suspicious."""
        tasks = [
            {"id": "t1", "title": "A", "wave": 1, "depends_on": ["t2"]},
            {"id": "t2", "title": "B", "wave": 2, "depends_on": []},
        ]
        result = check_deps(tasks)
        assert not result.passed
        assert any("wave" in e.lower() for e in result.errors)

    def test_no_deps_field_ok(self):
        """Tasks without deps field are fine."""
        tasks = [
            {"id": "t1", "title": "A", "wave": 0},
        ]
        result = check_deps(tasks)
        assert result.passed


# ---------------------------------------------------------------------------
# Requirement coverage
# ---------------------------------------------------------------------------

class TestRequirementCoverage:
    def test_all_covered(self):
        requirements = "Add a /api/health endpoint. Write tests for it."
        tasks = [
            {"title": "Add /api/health endpoint", "description": "Implement the health endpoint"},
            {"title": "Write tests for health endpoint", "description": "Test the endpoint"},
        ]
        result = check_requirement_coverage(requirements, tasks)
        assert result.passed

    def test_missing_coverage(self):
        requirements = "Add a health endpoint. Add a metrics endpoint. Write tests."
        tasks = [
            {"title": "Add health endpoint", "description": "Health check"},
        ]
        result = check_requirement_coverage(requirements, tasks)
        assert not result.passed
        # Should identify what's missing
        assert any("metrics" in e.lower() or "test" in e.lower() for e in result.errors)

    def test_empty_requirements_passes(self):
        result = check_requirement_coverage("", [{"title": "Do thing"}])
        assert result.passed


# ---------------------------------------------------------------------------
# Task scope check
# ---------------------------------------------------------------------------

class TestTaskScope:
    def test_reasonable_count(self):
        tasks = [{"title": f"Task {i}", "wave": 0} for i in range(3)]
        result = check_task_scope(tasks, complexity="simple")
        assert result.passed

    def test_too_many_for_simple(self):
        tasks = [{"title": f"Task {i}", "wave": 0} for i in range(15)]
        result = check_task_scope(tasks, complexity="simple")
        assert not result.passed
        assert any("too many" in e.lower() for e in result.errors)

    def test_complex_allows_more(self):
        tasks = [{"title": f"Task {i}", "wave": 0} for i in range(12)]
        result = check_task_scope(tasks, complexity="complex")
        assert result.passed

    def test_zero_tasks_fails(self):
        result = check_task_scope([], complexity="simple")
        assert not result.passed


# ---------------------------------------------------------------------------
# Target level suggestion
# ---------------------------------------------------------------------------

class TestSuggestLevel:
    def test_simple_code_suggests_l3(self):
        level = suggest_target_level(
            task_type="code", complexity="simple",
            has_roslyn=False, has_tests=True,
        )
        assert level == PlanLevel.L3

    def test_csharp_with_roslyn_suggests_l5(self):
        level = suggest_target_level(
            task_type="code", complexity="medium",
            has_roslyn=True, has_tests=True,
        )
        assert level == PlanLevel.L5

    def test_research_suggests_l2(self):
        level = suggest_target_level(
            task_type="research", complexity="medium",
            has_roslyn=False, has_tests=False,
        )
        assert level == PlanLevel.L2

    def test_complex_novel_suggests_l3(self):
        level = suggest_target_level(
            task_type="code", complexity="complex",
            has_roslyn=False, has_tests=True,
        )
        assert level == PlanLevel.L3


# ---------------------------------------------------------------------------
# Full validate_plan (runs all checks at a given level)
# ---------------------------------------------------------------------------

class TestValidatePlan:
    def test_valid_l1_plan_passes_all(self):
        plan = {
            "requirements": "Add health endpoint",
            "tasks": [
                {"id": "t1", "title": "Add endpoint", "task_type": "code", "wave": 0},
                {"id": "t2", "title": "Write test", "task_type": "code", "wave": 1,
                 "depends_on": ["t1"]},
            ],
            "complexity": "simple",
        }
        result = validate_plan(plan, PlanLevel.L1)
        assert result.passed

    def test_invalid_plan_collects_all_errors(self):
        plan = {
            "requirements": "Add health endpoint. Add metrics. Write tests.",
            "tasks": [
                {"id": "t1", "title": "Add endpoint", "task_type": "code"},  # missing wave
                {"id": "t1", "title": "Add endpoint", "task_type": "code"},  # duplicate
            ],
            "complexity": "simple",
        }
        result = validate_plan(plan, PlanLevel.L1)
        assert not result.passed
        # Should have errors from multiple rules
        assert len(result.errors) >= 2

    def test_validate_at_higher_level_checks_lower_too(self):
        """Validating at L2 should also check L1 requirements."""
        plan = {
            "requirements": "",
            "tasks": [
                {"id": "t1", "task_type": "code", "wave": 0,
                 "description": "Do thing", "affected_files": ["f.py"],
                 "complexity": "simple"},
                # Missing title — L1 violation
            ],
            "complexity": "simple",
        }
        result = validate_plan(plan, PlanLevel.L2)
        assert not result.passed
        assert any("title" in e for e in result.errors)
