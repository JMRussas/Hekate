# Plan Levels (L1-L5)

Hekate plans at five levels of depth. Higher levels produce more detailed task specifications, reducing the ambiguity that CLI executors must resolve on their own. The right level depends on your language tooling, task complexity, and how much control you want over the output.

---

## Level Summary

| Level | Name | What the plan contains | When to use |
|-------|------|----------------------|-------------|
| L1 | Rough tasks | Title, task type, wave | Fast prototyping, exploration |
| L2 | Detailed specs | + description, affected files, dependencies, complexity | Standard projects, any language |
| L3 | Implementation details | + code patterns, test strategy, edge cases | Complex code, C++ projects |
| L4 | Exact changes | + method signatures, types, return values | Well-understood changes |
| L5 | Executable spec | + full code body per change | Language-tooling-assisted, simple/medium tasks |

---

## L1: Rough Tasks

The lightest plan. Each task has a title, type, and wave assignment.

```json
{
  "id": "t1",
  "title": "Add user model",
  "task_type": "code",
  "wave": 0
}
```

**Required fields:** `title`, `task_type`, `wave`

**Best for:** Quick experiments, research tasks, documentation. The executor has maximum freedom to decide implementation details.

---

## L2: Detailed Specs

Adds structured context: what files are affected, what the task depends on, and how complex it is.

```json
{
  "id": "t1",
  "title": "Add user model",
  "task_type": "code",
  "wave": 0,
  "description": "Create a User model with fields: id, email, password_hash, created_at. Include Pydantic schema for API serialization.",
  "affected_files": ["backend/models/user.py", "backend/models/schemas.py"],
  "depends_on": [],
  "complexity": "simple"
}
```

**Additional required fields:** `description`, `affected_files`, `complexity`

**Validation at L2:**

- Description must be non-empty
- `affected_files` must list at least one file
- `complexity` must be set (`simple`, `medium`, `complex`)
- Dependencies reference valid task IDs (no dangling references)
- No circular dependencies (checked via DFS)

---

## L3: Implementation Details

Adds specific guidance on how to implement: code patterns to follow, what to test, and what can go wrong.

```json
{
  "id": "t1",
  "title": "Add user model",
  "task_type": "code",
  "wave": 0,
  "description": "Create a User model with fields: id, email, password_hash, created_at.",
  "affected_files": ["backend/models/user.py"],
  "depends_on": [],
  "complexity": "simple",
  "implementation_notes": "Use SQLAlchemy declarative base. Email field should have a unique constraint. Use uuid4 for ID generation.",
  "test_strategy": "Unit test model creation, email uniqueness constraint, and serialization round-trip.",
  "edge_cases": [
    "Empty email string",
    "Email longer than 320 characters",
    "Duplicate email insertion"
  ]
}
```

**Additional required fields:** `implementation_notes`, `test_strategy`, `edge_cases`

**Best for:** Complex tasks, C++ projects (where language tooling cannot reach L5), and tasks with non-obvious failure modes.

---

## L4: Exact Changes

Specifies the concrete code changes: method signatures, parameter types, and return types. The executor knows exactly what to write -- just not the implementation body.

```json
{
  "id": "t1",
  "title": "Add user model",
  "task_type": "code",
  "wave": 0,
  "description": "Create a User model with fields.",
  "affected_files": ["backend/models/user.py"],
  "depends_on": [],
  "complexity": "simple",
  "implementation_notes": "Use SQLAlchemy declarative base.",
  "test_strategy": "Unit test model creation and serialization.",
  "edge_cases": ["Empty email", "Duplicate email"],
  "changes": [
    {
      "file": "backend/models/user.py",
      "signature": "class User(Base)",
      "type": "class",
      "description": "SQLAlchemy model with id, email, password_hash, created_at fields"
    },
    {
      "file": "backend/models/user.py",
      "signature": "def verify_password(self, password: str) -> bool",
      "type": "method",
      "description": "Compare bcrypt hash"
    }
  ]
}
```

**Additional required fields:** Each entry in `changes` must have a `signature`.

---

## L5: Executable Spec

The plan contains the full code body for each change. With language tooling, the plan can be applied directly without an LLM.

```json
{
  "changes": [
    {
      "file": "backend/models/user.py",
      "signature": "def verify_password(self, password: str) -> bool",
      "type": "method",
      "body": "return bcrypt.checkpw(password.encode(), self.password_hash.encode())"
    }
  ]
}
```

**Additional required fields:** Each entry in `changes` must have a `body`.

!!! tip "L5 is tooling-assisted"
    L5 plans are most valuable when language analysis tools (Roslyn, Jedi, TS Compiler) can validate the generated code before execution. Without tooling, L5 is still valid but loses the automatic validation benefit.

---

## Language Tooling Matrix

The available language tooling determines the maximum practical plan level:

| Language | Tooling | Service | Port | Max Level |
|----------|---------|---------|------|-----------|
| C# | Roslyn | HekateServer | 5110 | L5 |
| Python | Jedi 0.19.2 | HekatePythonWorker | 9200 | L5 |
| TypeScript | TS Compiler API | HekateTypeScriptWorker | 9202 | L5 |
| C++ | None (no Clang integration) | -- | -- | L3 |
| Other | None | -- | -- | L2 |

!!! note "Tooling availability"
    Tooling is provided by the Hekate MCP code analysis server (`HekateServer` on port 5110) and its language workers. These services must be running for tooling-assisted planning. The `suggest_target_level()` function checks for tooling availability when `target_level` is set to `auto`.

---

## How `suggest_target_level()` Works

When `target_level` is `auto`, the planner calls `suggest_target_level()` to determine the appropriate depth. The algorithm:

1. **Research/docs/analysis tasks** always get **L2** -- deep planning adds no value for non-code work.

2. **Language tooling available** (Roslyn, Jedi, or TS Compiler) and complexity is simple/medium: **L5** -- the plan becomes an executable spec.

3. **Simple tasks** (no tooling, 3 or fewer tasks): **L4** -- exact changes are practical at small scale.

4. **Medium tasks** (no tooling, 5 or fewer tasks): **L4** -- still push for exact change specs.

5. **Everything else** (complex or large): **L3** -- the LLM executor needs room to explore.

```python
# From Odin/gods/plan_levels.py
def suggest_target_level(
    task_type="code",
    complexity="medium",
    has_roslyn=False,
    has_jedi=False,
    has_ts_compiler=False,
    task_count=1,
) -> PlanLevel:
    if task_type in ("research", "documentation", "analysis"):
        return PlanLevel.L2
    has_tooling = has_roslyn or has_jedi or has_ts_compiler
    if has_tooling and complexity in ("simple", "medium"):
        return PlanLevel.L5
    if complexity == "simple" and task_count <= 3:
        return PlanLevel.L4
    if complexity == "medium" and task_count <= 5:
        return PlanLevel.L4
    return PlanLevel.L3
```

---

## Rule Validation

Each level adds validation rules on top of the previous level. The `validate_plan()` function checks:

### All levels
- Plan must have at least one task
- No duplicate task IDs

### L2+
- All dependency references point to existing task IDs
- No circular dependencies (detected via depth-first search)
- Per-task: description, affected_files, complexity required

### L3+
- Per-task: implementation_notes, test_strategy, edge_cases required

### L4+
- Per-task: `changes` list required, each entry must have a `signature`

### L5
- Per-task: each entry in `changes` must have a `body`

If validation fails, the planner receives feedback and retries at the same level. The `RuleResult` object provides specific error messages:

```python
result = validate_plan(tasks, PlanLevel.L3, requirements="...")
if not result.passed:
    print(result.reason)   # First error
    print(result.errors)   # All errors
```

---

## Choosing the Right Level

| Scenario | Recommended Level |
|----------|-------------------|
| Exploring a new codebase | L1 or L2 |
| Standard feature work | L2 (default) |
| Refactoring with many files | L3 |
| Bug fix with known root cause | L4 |
| C#/Python/TS with tooling available | L5 (auto selects this) |
| C++ projects | L3 (tooling ceiling) |
| Game dev with NoZ engine | L3-L4 |

!!! warning "Over-planning"
    L4 and L5 plans take longer to generate and can constrain the executor. For complex or exploratory tasks, L3 gives the executor enough guidance without over-specifying implementation details that may need to change during execution.
