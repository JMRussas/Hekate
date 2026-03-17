#  Tests for services/task_lifecycle.py

import json
from unittest.mock import AsyncMock

import pytest

from backend.models.enums import TaskStatus
from backend.models.schemas import WaveReassessmentContext
from backend.services.task_lifecycle import collect_wave_reassessment_context

@pytest.mark.asyncio
async def test_collect_wave_reassessment_context():
    """Verify that collect_wave_reassessment_context gathers all required data."""
    # 1. Setup Mock DB
    mock_db = AsyncMock()

    project_id = "proj_athena_test"
    wave_number = 1
    
    # Mock data
    mock_task_rows = [
        {
            "id": "task_1",
            "title": "Task One",
            "status": TaskStatus.COMPLETED,
            "output_text": "Completed successfully.",
            "error": None,
        },
        {
            "id": "task_2",
            "title": "Task Two",
            "status": TaskStatus.FAILED,
            "output_text": "Failed during execution.",
            "error": "ValueError: Something went wrong",
        },
    ]

    mock_knowledge_rows = [
        {"content": "This is a key finding from a previous task."},
        {"content": "Another important piece of knowledge."},
    ]

    mock_observation_rows = [
        {
            "message": "Task stuck",
            "details_json": json.dumps({"task_id": "task_0", "duration": 300}),
        }
    ]
    
    original_plan_json = json.dumps({"project": "Test Project", "waves": [[{"task": "Task One"}]]})
    mock_plan_row = {"plan_json": original_plan_json}

    # Configure mock returns
    mock_db.fetchall.side_effect = [
        mock_task_rows,
        mock_knowledge_rows,
        mock_observation_rows,
    ]
    mock_db.fetchone.return_value = mock_plan_row

    # 2. Execute the function
    context = await collect_wave_reassessment_context(
        db=mock_db, project_id=project_id, wave_number=wave_number
    )

    # 3. Assertions
    assert context is not None
    assert isinstance(context, WaveReassessmentContext)
    assert context.project_id == project_id
    assert context.wave_number == wave_number

    # Assert task outcomes
    assert len(context.task_outcomes) == 2
    assert context.task_outcomes[0].task_id == "task_1"
    assert context.task_outcomes[0].status == TaskStatus.COMPLETED
    assert context.task_outcomes[0].output_summary == "Completed successfully."
    assert context.task_outcomes[1].task_id == "task_2"
    assert context.task_outcomes[1].status == TaskStatus.FAILED
    assert context.task_outcomes[1].error == "ValueError: Something went wrong"

    # Assert knowledge findings
    assert len(context.knowledge_findings) == 2
    assert context.knowledge_findings[0] == "This is a key finding from a previous task."

    # Assert sentinel observations
    assert len(context.sentinel_observations) == 1
    assert "Task stuck" in context.sentinel_observations[0]
    assert '"task_id": "task_0"' in context.sentinel_observations[0]
    
    # Assert original plan
    assert context.original_plan == json.loads(original_plan_json)

    # Verify DB calls
    assert mock_db.fetchall.call_count == 3
    assert mock_db.fetchone.call_count == 1
    
    # Check the DB queries made
    calls = mock_db.fetchall.call_args_list
    assert "FROM tasks" in calls[0].args[0]
    assert calls[0].args[1] == (project_id, wave_number)
    
    assert "FROM project_knowledge" in calls[1].args[0]
    assert calls[1].args[1] == (project_id,)

    assert "FROM sentinel_observations" in calls[2].args[0]
    assert calls[2].args[1] == (project_id,)
    
    plan_call = mock_db.fetchone.call_args
    assert "FROM plans" in plan_call.args[0]
    assert plan_call.args[1] == (project_id,)

