# Tests for services/sentinel/plan_sentinel.py

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.sentinel.models import ProjectWorldModel, SentinelObservation, TaskWorldState
from backend.services.sentinel.plan_sentinel import PlanSentinel

@pytest.fixture
def mock_bus():
    """Pytest fixture for a mock SentinelBus."""
    return AsyncMock()

@pytest.fixture
def mock_db():
    """Pytest fixture for a mock database connection."""
    return AsyncMock()

@pytest.mark.asyncio
async def test_wave_reassessment_is_triggered(mock_bus, mock_db, mocker):
    """
    Verify that when a wave completes, the _trigger_wave_reassessment method
    is called.
    """
    project_id = "proj_athena_trigger_test"
    sentinel = PlanSentinel(project_id=project_id, bus=mock_bus, db=mock_db)

    # 1. Mock the trigger method to spy on it
    trigger_spy = mocker.patch.object(sentinel, "_trigger_wave_reassessment", new_callable=AsyncMock)

    # 2. Setup the world model to simulate a completed wave
    # All tasks in the current wave (wave 1) are in a terminal state
    sentinel._world_model.current_wave = 1
    sentinel._world_model.tasks = {
        "task-1": TaskWorldState(id="task-1", wave=1, status="completed"),
        "task-2": TaskWorldState(id="task-2", wave=1, status="failed"),
        "task-3": TaskWorldState(id="task-3", wave=2, status="pending"), # Should be ignored
    }
    
    # 3. Run the state detection logic
    await sentinel._evaluate_state_detection_rules()

    # 4. Assert that the trigger was called
    trigger_spy.assert_called_once()
    
    # 5. Assert the details of the call
    call_args = trigger_spy.call_args
    obs = call_args.args[0]
    assert isinstance(obs, SentinelObservation)
    assert obs.category == "wave_complete"
    assert obs.details["wave"] == 1
    assert obs.project_id == project_id

    # 6. Verify that it's NOT called again (deduplication)
    await sentinel._evaluate_state_detection_rules()
    trigger_spy.assert_called_once() # Should still be 1

@pytest.mark.asyncio
async def test_trigger_wave_reassessment_calls_collector(mock_bus, mock_db, mocker):
    """
    Verify that _trigger_wave_reassessment correctly calls the
    collect_wave_reassessment_context function.
    """
    project_id = "proj_athena_collector_call"
    sentinel = PlanSentinel(project_id=project_id, bus=mock_bus, db=mock_db)

    # 1. Mock the collector function
    mock_collector = mocker.patch(
        "backend.services.task_lifecycle.collect_wave_reassessment_context",
        new_callable=AsyncMock
    )
    mock_collector.return_value = MagicMock() # Just needs to return something truthy
    
    # 2. Create a sample wave_complete observation
    obs = SentinelObservation(
        category="wave_complete",
        message="Wave 1 complete",
        project_id=project_id,
        details={"wave": 1, "completed": 1, "failed": 1, "total": 2}
    )

    # 3. Call the trigger method directly
    await sentinel._trigger_wave_reassessment(obs)

    # 4. Assert that the collector was called with the correct arguments
    mock_collector.assert_called_once_with(
        db=mock_db,
        project_id=project_id,
        wave_number=1,
    )

