# Testing

How to run and write tests for the Hekate platform.

---

## Test Suite Structure

Tests are organized by component:

| Directory | Component | Framework |
|-----------|-----------|-----------|
| `Odin/tests/` | Gods pipeline (handlers, gates, integration) | pytest |
| `orchestration/frontend/` | Dashboard UI | Vitest (if configured) |
| `context-store/Api.Tests/` | Context Store API | xUnit (.NET) |

The gods pipeline tests in `Odin/tests/` are the primary test suite for the execution engine.

---

## Running Tests

### Gods Pipeline (Python)

```bash
# Run all pipeline tests
cd Odin && python -m pytest tests/ -v

# Run a specific test file
python -m pytest Odin/tests/test_integration.py -v

# Run with output visible
python -m pytest Odin/tests/ -v -s

# Run tests matching a pattern
python -m pytest Odin/tests/ -k "test_athena" -v
```

### Orchestration Frontend

```bash
cd orchestration/frontend && npm test
```

### Context Store (.NET)

```bash
dotnet test context-store/Api.Tests/
```

---

## Key Test Files

### `Odin/tests/test_integration.py`

End-to-end integration tests for the gods pipeline. Tests the full lifecycle: project creation through planning, dispatch, execution, verification, and completion. Mocks the LLM Gateway and CLI executors.

### `Odin/tests/test_reliability.py`

Reliability tests for edge cases: zero-task projects, worktree isolation, process cleanup, and plan preservation.

### `Odin/tests/test_failure_modes.py`

Tests for failure scenarios: executor crashes, verification failures, retry exhaustion, and cascading cancellation.

### `Odin/tests/test_athena_node_tree.py`

Tests for the Athena planning handler, specifically the node tree decomposition and leveled planning (L1-L5).

### `Odin/tests/test_mimir_agent.py`

Tests for the Mimir verification agent, including verdict handling (passed, gaps_found, human_needed) and feedback propagation.

### `Odin/tests/test_planning_agent.py`

Tests for the planning agent's interaction with the LLM Gateway and plan structure validation.

### `Odin/tests/test_log_extractor.py`

Tests for log parsing and extraction utilities used in pipeline diagnostics.

### `Odin/tests/test_log_rotation.py`

Tests for log file rotation to prevent unbounded log growth.

---

## Writing New Tests

### Handler Tests

Test individual god handlers by mocking events and checking emitted events.

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

async def test_odin_dispatches_pending_tasks():
    """Test that Odin dispatches pending tasks with satisfied dependencies."""
    # 1. Set up mock database with a project and pending tasks
    db = AsyncMock()
    db.fetchall.return_value = [
        {"id": "task-1", "status": "pending", "wave": 0, "project_id": "proj-1"}
    ]

    # 2. Create the handler with mocked dependencies
    handler = OdinHandler(db=db, config=test_config)

    # 3. Fire the event
    event = {"type": "wave_ready", "project_id": "proj-1", "wave": 0}
    emitted = await handler.handle(event)

    # 4. Assert the handler emitted the right events
    assert any(e["type"] == "task_dispatched" for e in emitted)
    assert emitted[0]["task_id"] == "task-1"
```

Key patterns:
- Mock the database (`AsyncMock` for async DB methods)
- Mock external services (LLM Gateway, CLI executors)
- Fire an event and collect emitted events
- Assert on the emitted event types and payloads

### Gate Tests

Test pipeline gates that control event flow between handlers.

```python
async def test_budget_gate_blocks_over_budget():
    """Test that the budget gate blocks dispatch when over budget."""
    gate = BudgetGate(max_budget_usd=10.0)

    # Simulate a project that has spent $11
    event = {
        "type": "task_dispatched",
        "project_id": "proj-1",
        "budget_used_usd": 11.0,
    }

    result = await gate.check(event)
    assert result.blocked is True
    assert "budget" in result.reason.lower()
```

### Integration Tests

Full pipeline integration tests use the actual event loop with mocked external services.

```python
async def test_full_pipeline_e2e():
    """Test complete project lifecycle through the pipeline."""
    # 1. Create an in-memory database
    db = await create_test_db()

    # 2. Insert a project with requirements
    project_id = await insert_test_project(db, "Test Project", "Build a hello world app")

    # 3. Create the pipeline with mocked LLM and CLI
    pipeline = create_test_pipeline(
        db=db,
        llm_mock=mock_llm_gateway,
        cli_mock=mock_claude_code,
    )

    # 4. Start the project and run the pipeline
    await pipeline.emit({"type": "project_start", "project_id": project_id})
    await pipeline.run_until_idle(timeout=30)

    # 5. Assert project completed
    project = await db.fetchone("SELECT status FROM projects WHERE id = $1", (project_id,))
    assert project["status"] == "completed"
```

### Test Fixtures

Common fixtures for pipeline tests:

```python
@pytest.fixture
async def test_db():
    """Create an in-memory SQLite database with schema."""
    db = await create_test_db()
    yield db
    await db.close()

@pytest.fixture
def mock_llm():
    """Mock LLM Gateway that returns canned plans."""
    mock = AsyncMock()
    mock.generate.return_value = {
        "plan": {"tasks": [{"title": "Task 1", "task_type": "code"}]},
        "model_used": "test-model",
        "cost_usd": 0.01,
    }
    return mock

@pytest.fixture
def mock_executor():
    """Mock CLI executor that always succeeds."""
    mock = AsyncMock()
    mock.execute.return_value = {
        "output_text": "Done. Created src/main.py",
        "exit_code": 0,
    }
    return mock
```

---

## Test Configuration

### pytest.ini / pyproject.toml

Tests use standard pytest configuration. Key settings:

```ini
[pytest]
asyncio_mode = auto
testpaths = Odin/tests
python_files = test_*.py
python_functions = test_*
```

### Running with Coverage

```bash
python -m pytest Odin/tests/ --cov=Odin/gods --cov-report=term-missing -v
```

---

## Testing Tips

1. **Mock external services, not internal logic.** Mock the LLM Gateway HTTP calls and CLI subprocesses, but let the actual handler logic run.

2. **Use the event-driven pattern.** The pipeline is event-driven -- tests should emit events and check resulting events, not call handler methods directly (unless unit testing a specific handler).

3. **Test failure paths.** The pipeline has retry logic, verification feedback loops, and escalation to human review. Test these paths, not just the happy path.

4. **Database state is the source of truth.** After running pipeline events, assert on database state (project status, task status, verification status) rather than return values.

5. **Timeouts matter.** Pipeline integration tests should use `run_until_idle` with a reasonable timeout rather than sleeping. The pipeline processes events as fast as possible in test mode.
