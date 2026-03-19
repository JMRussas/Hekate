"""Plan level system and rule engine.

Validates plan completeness at each level (L1-L5) and gates transitions.
All checks are structural — no AI needed.

Levels:
  L1: rough task breakdown (title, type, wave)
  L2: detailed specs (description, affected_files, deps, complexity)
  L3: implementation details (approach, test_strategy, edge_cases)
  L4: exact changes (method_signatures, code_blocks)
  L5: executable spec (diffs — tool can apply directly)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from difflib import SequenceMatcher


# ---------------------------------------------------------------------------
# Plan levels
# ---------------------------------------------------------------------------

class PlanLevel(enum.IntEnum):
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4
    L5 = 5

    @classmethod
    def from_str(cls, s: str) -> PlanLevel | None:
        if s == "auto":
            return None
        return cls[s]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class PlanConfig:
    tdd: bool = True
    narration: bool = True
    target_level: str = "auto"  # "auto" | "L1" | "L2" | "L3" | "L4" | "L5"

    @classmethod
    def from_dict(cls, d: dict) -> PlanConfig:
        return cls(
            tdd=d.get("tdd", True),
            narration=d.get("narration", True),
            target_level=d.get("target_level", "auto"),
        )


# ---------------------------------------------------------------------------
# Rule result
# ---------------------------------------------------------------------------

@dataclass
class RuleResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def merge(self, other: RuleResult) -> RuleResult:
        return RuleResult(
            passed=self.passed and other.passed,
            errors=self.errors + other.errors,
            warnings=self.warnings + other.warnings,
        )


# ---------------------------------------------------------------------------
# Level field requirements
# ---------------------------------------------------------------------------

_LEVEL_FIELDS: dict[PlanLevel, list[str]] = {
    PlanLevel.L1: ["title", "task_type", "wave"],
    PlanLevel.L2: ["description", "affected_files", "complexity"],
    PlanLevel.L3: ["approach", "test_strategy", "edge_cases"],
    PlanLevel.L4: ["method_signatures", "code_blocks"],
    PlanLevel.L5: ["diffs"],
}


def check_level_requirements(tasks: list[dict], level: PlanLevel) -> RuleResult:
    """Check that all tasks have the required fields for the given level.
    Higher levels include all lower level requirements."""
    if not tasks:
        return RuleResult(passed=False, errors=["Plan is empty — no tasks defined"])

    errors: list[str] = []

    # Collect all required fields up to this level
    required: list[str] = []
    for lvl in PlanLevel:
        if lvl <= level:
            required.extend(_LEVEL_FIELDS[lvl])

    for i, task in enumerate(tasks):
        task_label = task.get("title", f"task[{i}]")
        for field_name in required:
            val = task.get(field_name)
            if val is None or val == "" or val == []:
                errors.append(f"Task '{task_label}': missing {field_name} (required at {level.name})")

    return RuleResult(passed=len(errors) == 0, errors=errors)


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def check_duplicates(tasks: list[dict]) -> RuleResult:
    """Check for duplicate or near-duplicate task titles."""
    errors: list[str] = []
    warnings: list[str] = []
    seen: list[tuple[str, int]] = []  # (title, wave)

    for task in tasks:
        title = task.get("title", "")
        wave = task.get("wave", -1)

        for prev_title, prev_wave in seen:
            sim = _similarity(title, prev_title)
            if sim >= 0.85:
                if wave == prev_wave:
                    errors.append(
                        f"Duplicate tasks in wave {wave}: '{title}' ≈ '{prev_title}' "
                        f"(similarity: {sim:.0%})"
                    )
                else:
                    warnings.append(
                        f"Similar tasks across waves: '{title}' (w{wave}) ≈ "
                        f"'{prev_title}' (w{prev_wave})"
                    )

        seen.append((title, wave))

    return RuleResult(passed=len(errors) == 0, errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Dependency validation
# ---------------------------------------------------------------------------

def check_deps(tasks: list[dict]) -> RuleResult:
    """Validate dependency graph: no missing targets, no cycles, no backward wave deps."""
    errors: list[str] = []
    task_ids = {t.get("id") for t in tasks if t.get("id")}
    task_waves = {t["id"]: t.get("wave", 0) for t in tasks if t.get("id")}

    # Build adjacency for cycle detection
    adj: dict[str, list[str]] = {}
    for task in tasks:
        tid = task.get("id")
        deps = task.get("depends_on", [])
        if not tid or not deps:
            continue
        adj[tid] = deps

        for dep_id in deps:
            # Missing target
            if dep_id not in task_ids:
                errors.append(f"Task '{tid}' depends on '{dep_id}' which doesn't exist")
                continue

            # Backward wave dependency
            task_wave = task_waves.get(tid, 0)
            dep_wave = task_waves.get(dep_id, 0)
            if dep_wave > task_wave:
                errors.append(
                    f"Task '{tid}' (wave {task_wave}) depends on '{dep_id}' "
                    f"(wave {dep_wave}) — dependency in a later wave"
                )

    # Cycle detection (DFS)
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _has_cycle(node: str) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for neighbor in adj.get(node, []):
            if neighbor in task_ids and _has_cycle(neighbor):
                return True
        in_stack.discard(node)
        return False

    for tid in adj:
        if _has_cycle(tid):
            errors.append(f"Circular dependency detected involving task '{tid}'")
            break  # One cycle error is enough

    return RuleResult(passed=len(errors) == 0, errors=errors)


# ---------------------------------------------------------------------------
# Requirement coverage
# ---------------------------------------------------------------------------

def check_requirement_coverage(requirements: str, tasks: list[dict]) -> RuleResult:
    """Check that requirements are covered by tasks.

    Uses keyword extraction — splits requirements into sentences/clauses
    and checks each has at least one task with overlapping terms.
    """
    if not requirements or not requirements.strip():
        return RuleResult(passed=True)

    errors: list[str] = []

    # Split requirements into clauses
    import re
    clauses = re.split(r'[.;!\n]+', requirements)
    clauses = [c.strip() for c in clauses if c.strip() and len(c.strip()) > 10]

    # Build task text corpus
    task_text = " ".join(
        (t.get("title", "") + " " + t.get("description", "")).lower()
        for t in tasks
    )

    # Check each clause has some coverage
    stopwords = {"the", "a", "an", "and", "or", "to", "for", "in", "of", "with", "is", "it", "that", "this"}
    for clause in clauses:
        words = set(re.findall(r'\b\w{3,}\b', clause.lower())) - stopwords
        if not words:
            continue

        # How many clause words appear in task text?
        matched = sum(1 for w in words if w in task_text)
        coverage = matched / len(words) if words else 1.0

        if coverage < 0.3:
            errors.append(f"Requirement may not be covered: '{clause[:80]}'")

    return RuleResult(passed=len(errors) == 0, errors=errors)


# ---------------------------------------------------------------------------
# Task scope
# ---------------------------------------------------------------------------

_SCOPE_LIMITS = {
    "simple": (1, 5),
    "medium": (2, 10),
    "complex": (3, 20),
}


def check_task_scope(tasks: list[dict], complexity: str = "medium") -> RuleResult:
    """Check that task count is reasonable for the complexity."""
    if not tasks:
        return RuleResult(passed=False, errors=["No tasks — plan is empty"])

    min_tasks, max_tasks = _SCOPE_LIMITS.get(complexity, (1, 15))
    count = len(tasks)
    errors: list[str] = []
    warnings: list[str] = []

    if count > max_tasks:
        errors.append(
            f"Too many tasks ({count}) for {complexity} complexity "
            f"(expected {min_tasks}-{max_tasks}). Plan may be over-decomposed."
        )
    elif count < min_tasks:
        warnings.append(
            f"Very few tasks ({count}) for {complexity} complexity "
            f"(expected {min_tasks}-{max_tasks})"
        )

    return RuleResult(passed=len(errors) == 0, errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Target level suggestion
# ---------------------------------------------------------------------------

def suggest_target_level(
    task_type: str = "code",
    complexity: str = "medium",
    has_roslyn: bool = False,
    has_tests: bool = True,
) -> PlanLevel:
    """Suggest the target planning depth based on task characteristics."""
    if task_type in ("research", "documentation", "analysis"):
        return PlanLevel.L2

    if has_roslyn and task_type == "code":
        return PlanLevel.L5

    if complexity == "complex":
        return PlanLevel.L3

    if complexity == "simple" and has_tests:
        return PlanLevel.L3

    return PlanLevel.L3  # default for code


# ---------------------------------------------------------------------------
# Full validation
# ---------------------------------------------------------------------------

def validate_plan(plan: dict, level: PlanLevel) -> RuleResult:
    """Run all rule checks against a plan at the given level.

    Returns a merged RuleResult with all errors and warnings.
    """
    tasks = plan.get("tasks", [])
    requirements = plan.get("requirements", "")
    complexity = plan.get("complexity", "medium")

    result = RuleResult(passed=True)

    # Level field requirements
    result = result.merge(check_level_requirements(tasks, level))

    # Structural checks (run at all levels)
    result = result.merge(check_duplicates(tasks))
    result = result.merge(check_deps(tasks))
    result = result.merge(check_requirement_coverage(requirements, tasks))
    result = result.merge(check_task_scope(tasks, complexity))

    return result
