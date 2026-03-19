"""Plan level system (L1-L5) with rule engine.

Levels:
  L1: rough task breakdown (what needs doing)
  L2: detailed specs (how, affected files, deps, complexity)
  L3: implementation details (code patterns, edge cases, test strategy)
  L4: exact changes (specific code blocks, method signatures, return values)
  L5: executable spec (tool applies directly, no LLM needed)

Rule engine validates completeness at each level before advancing.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# PlanLevel enum
# ---------------------------------------------------------------------------

class PlanLevel(enum.IntEnum):
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4
    L5 = 5

    @classmethod
    def from_str(cls, s: str) -> PlanLevel:
        key = s.upper()
        if key not in ("L1", "L2", "L3", "L4", "L5"):
            raise ValueError(f"Invalid plan level: {s}")
        return cls[key]

    def next(self) -> PlanLevel | None:
        try:
            return PlanLevel(self.value + 1)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# TaskSpec — plan task at any level
# ---------------------------------------------------------------------------

@dataclass
class TaskSpec:
    id: str
    title: str
    task_type: str = "code"
    wave: int | None = 0

    # L2+
    description: str | None = None
    affected_files: list[str] | None = None
    depends_on: list[str] | None = None
    complexity: str | None = None

    # L3+
    implementation_notes: str | None = None
    test_strategy: str | None = None
    edge_cases: list[str] | None = None

    # L4+
    changes: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# RuleResult
# ---------------------------------------------------------------------------

@dataclass
class RuleResult:
    passed: bool
    reason: str
    errors: list[str] = field(default_factory=list)

    @classmethod
    def merge(cls, results: list[RuleResult]) -> RuleResult:
        all_errors = []
        for r in results:
            all_errors.extend(r.errors)
        all_passed = all(r.passed for r in results)
        if all_passed:
            return cls(passed=True, reason="All checks passed", errors=[])
        reasons = [r.reason for r in results if not r.passed]
        return cls(
            passed=False,
            reason="; ".join(reasons),
            errors=all_errors,
        )


# ---------------------------------------------------------------------------
# PlanConfig
# ---------------------------------------------------------------------------

@dataclass
class PlanConfig:
    tdd: bool = True
    narration: bool = True
    target_level: str = "auto"
    direct_write: bool = True
    max_concurrent: int = 2


# ---------------------------------------------------------------------------
# validate_task_at_level — per-task rule checks
# ---------------------------------------------------------------------------

def validate_task_at_level(task: TaskSpec, level: PlanLevel) -> RuleResult:
    """Validate a single task meets the requirements for the given level."""
    errors: list[str] = []

    # L1: must have title, task_type, wave
    if not task.title or not task.title.strip():
        errors.append(f"Task {task.id}: missing title")
    if not task.task_type or not task.task_type.strip():
        errors.append(f"Task {task.id}: missing task_type")
    if task.wave is None:
        errors.append(f"Task {task.id}: missing wave")

    if level >= PlanLevel.L2:
        if not task.description or not task.description.strip():
            errors.append(f"Task {task.id}: missing description (required at L2+)")
        if not task.affected_files:
            errors.append(f"Task {task.id}: missing affected_files (required at L2+)")
        if task.complexity is None:
            errors.append(f"Task {task.id}: missing complexity (required at L2+)")

    if level >= PlanLevel.L3:
        if not task.implementation_notes or not task.implementation_notes.strip():
            errors.append(f"Task {task.id}: missing implementation_notes (required at L3+)")
        if not task.test_strategy or not task.test_strategy.strip():
            errors.append(f"Task {task.id}: missing test_strategy (required at L3+)")
        if not task.edge_cases:
            errors.append(f"Task {task.id}: missing edge_cases (required at L3+)")

    if level >= PlanLevel.L4:
        if not task.changes:
            errors.append(f"Task {task.id}: missing changes (required at L4+)")
        else:
            for i, change in enumerate(task.changes):
                if "signature" not in change or not change.get("signature"):
                    errors.append(
                        f"Task {task.id}: change[{i}] missing signature (required at L4+)"
                    )

    if level >= PlanLevel.L5:
        if not task.changes:
            errors.append(f"Task {task.id}: missing changes (required at L5)")
        else:
            for i, change in enumerate(task.changes):
                if "body" not in change or not change.get("body"):
                    errors.append(
                        f"Task {task.id}: change[{i}] missing body (required at L5)"
                    )

    if errors:
        return RuleResult(passed=False, reason=errors[0], errors=errors)
    return RuleResult(passed=True, reason="OK")


# ---------------------------------------------------------------------------
# validate_plan — whole-plan rule checks
# ---------------------------------------------------------------------------

def validate_plan(
    tasks: list[TaskSpec],
    level: PlanLevel,
    requirements: str = "",
) -> RuleResult:
    """Validate an entire plan at the given level."""
    errors: list[str] = []

    # Empty plan
    if not tasks:
        return RuleResult(
            passed=False,
            reason="Empty plan: no tasks defined",
            errors=["No tasks in plan"],
        )

    # Duplicate IDs
    ids = [t.id for t in tasks]
    seen = set()
    for tid in ids:
        if tid in seen:
            errors.append(f"Duplicate task ID: {tid}")
        seen.add(tid)

    if errors:
        return RuleResult(passed=False, reason=errors[0], errors=errors)

    # Dependency validation (L2+)
    if level >= PlanLevel.L2:
        id_set = set(ids)
        # Check for missing dep targets
        for task in tasks:
            for dep in (task.depends_on or []):
                if dep not in id_set:
                    errors.append(f"Task {task.id} depends on {dep} which doesn't exist")

        # Check for circular deps (simple DFS)
        if not errors:
            adj: dict[str, list[str]] = {t.id: list(t.depends_on or []) for t in tasks}
            visited: set[str] = set()
            in_stack: set[str] = set()

            def _has_cycle(node: str) -> bool:
                if node in in_stack:
                    return True
                if node in visited:
                    return False
                visited.add(node)
                in_stack.add(node)
                for dep in adj.get(node, []):
                    if _has_cycle(dep):
                        return True
                in_stack.discard(node)
                return False

            for tid in ids:
                if _has_cycle(tid):
                    errors.append(f"Circular dependency detected involving {tid}")
                    break

    if errors:
        return RuleResult(passed=False, reason=errors[0], errors=errors)

    # Per-task validation
    task_results = [validate_task_at_level(t, level) for t in tasks]
    merged = RuleResult.merge(task_results)
    if not merged.passed:
        return merged

    return RuleResult(passed=True, reason="Plan is valid at " + level.name)


# ---------------------------------------------------------------------------
# suggest_target_level — auto depth based on complexity/tooling
# ---------------------------------------------------------------------------

def suggest_target_level(
    task_type: str = "code",
    complexity: str = "medium",
    has_roslyn: bool = False,
    has_jedi: bool = False,
    task_count: int = 1,
) -> PlanLevel:
    """Suggest the target planning depth based on task characteristics.

    Language tooling (Roslyn for C#, Jedi for Python) enables L5 —
    the plan becomes an executable spec that a tool applies directly.
    """
    # Research and docs don't need deep planning
    if task_type in ("research", "documentation", "analysis"):
        return PlanLevel.L2

    # Language tooling available — can go to L5
    has_tooling = has_roslyn or has_jedi
    if has_tooling and complexity in ("simple", "medium"):
        return PlanLevel.L5

    # Known patterns, simple — L4 (exact changes are practical)
    if complexity == "simple" and task_count <= 3:
        return PlanLevel.L4

    # Medium code without tooling — L4 (still push for exact changes)
    if complexity == "medium" and task_count <= 5:
        return PlanLevel.L4

    # Complex or large — L3 (LLM needs room to explore)
    return PlanLevel.L3
