#  Tests for services/task_lifecycle.py

import json
from unittest.mock import AsyncMock, call

import pytest

from backend.models.enums import TaskStatus
from backend.models.schemas import (
    KnowledgeFinding,
    SentinelObservationSummary,
    WaveReassessmentContext,
)
from backend.services.task_lifecycle import collect_wave_reassessment_context


@pytest.mark.asyncio
async def test_collect_wave_reassessment_context():
    """Verify that collect_wave_reassessment_context gathers all required data."""
    mock_db = AsyncMock()

    project_id = "proj_athena_test"
    wave_number = 1

    # Mock task rows (now includes cost/timing/model_tier from JOIN with usage_log)
    mock_task_rows = [
        {
            "id": "task_1",
            "title": "Task One",
            "status": TaskStatus.COMPLETED,
            "output_text": "Completed successfully.",
            "error": None,
            "model_tier": "claude_code",
            "started_at": 1000.0,
            "completed_at": 1060.0,
            "task_cost": 0.05,
        },
        {
            "id": "task_2",
            "title": "Task Two",
            "status": TaskStatus.FAILED,
            "output_text": "Failed during execution.",
            "error": "ValueError: Something went wrong",
            "model_tier": "gemini_cli",
            "started_at": 1000.0,
            "completed_at": 1030.0,
            "task_cost": 0.01,
        },
    ]

    mock_knowledge_rows = [
        {
            "id": "k1",
            "content": "This is a key finding from a previous task.",
            "category": "discovery",
            "confidence": "high",
            "rationale": "Directly observed in execution",
            "source_task_title": "Task Zero",
        },
        {
            "id": "k2",
            "content": "Another important piece of knowledge.",
            "category": "gotcha",
            "confidence": "medium",
            "rationale": "",
            "source_task_title": "",
        },
    ]

    mock_observation_rows = [
        {
            "id": "obs1",
            "rule": "task_stuck",
            "severity": "warning",
            "summary": "Task stuck",
            "details_json": json.dumps({"task_id": "task_0", "duration": 300}),
        }
    ]

    original_plan_json = json.dumps(
        {"project": "Test Project", "waves": [[{"task": "Task One"}]]}
    )
    mock_plan_row = {"plan_json": original_plan_json}

    # fetchone is called twice: terminal check, then plan query
    mock_db.fetchone.side_effect = [
        {"cnt": 0},  # all terminal
        mock_plan_row,
    ]
    mock_db.fetchall.side_effect = [
        mock_task_rows,
        mock_knowledge_rows,
        mock_observation_rows,
    ]

    context = await collect_wave_reassessment_context(
        db=mock_db, project_id=project_id, wave_number=wave_number
    )

    # --- Assertions ---
    assert context is not None
    assert isinstance(context, WaveReassessmentContext)
    assert context.project_id == project_id
    assert context.wave_number == wave_number
    assert context.all_tasks_terminal is True

    # Task outcomes
    assert len(context.task_outcomes) == 2
    assert context.task_outcomes[0].task_id == "task_1"
    assert context.task_outcomes[0].status == TaskStatus.COMPLETED
    assert context.task_outcomes[0].output_summary == "Completed successfully."
    assert context.task_outcomes[0].model_tier == "claude_code"
    assert context.task_outcomes[0].cost_usd == 0.05
    assert context.task_outcomes[0].duration_seconds == 60.0
    assert context.task_outcomes[1].task_id == "task_2"
    assert context.task_outcomes[1].status == TaskStatus.FAILED
    assert context.task_outcomes[1].error == "ValueError: Something went wrong"
    assert context.task_outcomes[1].model_tier == "gemini_cli"
    assert context.task_outcomes[1].cost_usd == 0.01
    assert context.task_outcomes[1].duration_seconds == 30.0

    # Wave cost
    assert context.wave_cost_usd == 0.06

    # Knowledge findings (now structured with rationale/confidence/id)
    assert len(context.knowledge_findings) == 2
    assert isinstance(context.knowledge_findings[0], KnowledgeFinding)
    assert context.knowledge_findings[0].id == "k1"
    assert context.knowledge_findings[0].content == "This is a key finding from a previous task."
    assert context.knowledge_findings[0].category == "discovery"
    assert context.knowledge_findings[0].confidence == "high"
    assert context.knowledge_findings[0].rationale == "Directly observed in execution"
    assert context.knowledge_findings[0].source_task_title == "Task Zero"
    assert context.knowledge_findings[1].id == "k2"
    assert context.knowledge_findings[1].category == "gotcha"
    assert context.knowledge_findings[1].confidence == "medium"
    assert context.knowledge_findings[1].rationale == ""
    assert context.knowledge_findings[1].source_task_title == ""

    # Sentinel observations (now structured)
    assert len(context.sentinel_observations) == 1
    assert isinstance(context.sentinel_observations[0], SentinelObservationSummary)
    assert context.sentinel_observations[0].category == "task_stuck"
    assert context.sentinel_observations[0].severity == "warning"
    assert context.sentinel_observations[0].message == "Task stuck"
    assert context.sentinel_observations[0].details["task_id"] == "task_0"

    # Original plan
    assert context.original_plan == json.loads(original_plan_json)

    # Verify DB call counts
    assert mock_db.fetchone.call_count == 2
    assert mock_db.fetchall.call_count == 3


@pytest.mark.asyncio
async def test_collect_wave_reassessment_returns_none_if_non_terminal():
    """Returns None when some tasks are still running."""
    mock_db = AsyncMock()
    mock_db.fetchone.return_value = {"cnt": 2}  # 2 non-terminal tasks

    context = await collect_wave_reassessment_context(
        db=mock_db, project_id="proj_1", wave_number=0
    )

    assert context is None
    # Should not have queried tasks or knowledge
    assert mock_db.fetchall.call_count == 0


@pytest.mark.asyncio
async def test_collect_wave_reassessment_returns_none_if_no_plan():
    """Returns None when no plan exists."""
    mock_db = AsyncMock()
    mock_db.fetchone.side_effect = [
        {"cnt": 0},  # all terminal
        None,  # no plan
    ]
    mock_db.fetchall.side_effect = [
        [  # task rows
            {
                "id": "t1", "title": "T", "status": "completed",
                "output_text": "", "error": None,
                "model_tier": "ollama", "started_at": 1.0,
                "completed_at": 2.0, "task_cost": 0.0,
            }
        ],
        [],  # knowledge
        [],  # observations
    ]

    context = await collect_wave_reassessment_context(
        db=mock_db, project_id="proj_1", wave_number=0
    )

    assert context is None


@pytest.mark.asyncio
async def test_collect_wave_reassessment_returns_none_if_no_tasks():
    """Returns None when wave has no tasks."""
    mock_db = AsyncMock()
    mock_db.fetchone.return_value = {"cnt": 0}
    mock_db.fetchall.side_effect = [
        [],  # no tasks
    ]

    context = await collect_wave_reassessment_context(
        db=mock_db, project_id="proj_1", wave_number=5
    )

    assert context is None


@pytest.mark.asyncio
async def test_collect_wave_reassessment_empty_knowledge():
    """Empty knowledge rows produces empty knowledge_findings list."""
    mock_db = AsyncMock()

    mock_task_rows = [
        {
            "id": "t1", "title": "Task", "status": "completed",
            "output_text": "Done.", "error": None,
            "model_tier": "ollama", "started_at": 1.0,
            "completed_at": 2.0, "task_cost": 0.0,
        },
    ]
    mock_plan_row = {"plan_json": json.dumps({"summary": "Plan"})}

    mock_db.fetchone.side_effect = [{"cnt": 0}, mock_plan_row]
    mock_db.fetchall.side_effect = [
        mock_task_rows,
        [],  # no knowledge
        [],  # no observations
    ]

    context = await collect_wave_reassessment_context(
        db=mock_db, project_id="proj_empty", wave_number=0
    )

    assert context is not None
    assert context.knowledge_findings == []


@pytest.mark.asyncio
async def test_collect_wave_reassessment_null_knowledge_fields():
    """Null rationale/source_task_title default to empty strings."""
    mock_db = AsyncMock()

    mock_task_rows = [
        {
            "id": "t1", "title": "Task", "status": "completed",
            "output_text": "Done.", "error": None,
            "model_tier": "claude_code", "started_at": 10.0,
            "completed_at": 20.0, "task_cost": 0.01,
        },
    ]
    mock_knowledge_rows = [
        {
            "id": "k_null",
            "content": "Finding with null fields",
            "category": None,
            "confidence": None,
            "rationale": None,
            "source_task_title": None,
        },
    ]
    mock_plan_row = {"plan_json": json.dumps({"summary": "Plan"})}

    mock_db.fetchone.side_effect = [{"cnt": 0}, mock_plan_row]
    mock_db.fetchall.side_effect = [
        mock_task_rows,
        mock_knowledge_rows,
        [],  # no observations
    ]

    context = await collect_wave_reassessment_context(
        db=mock_db, project_id="proj_null", wave_number=0
    )

    assert context is not None
    assert len(context.knowledge_findings) == 1
    kf = context.knowledge_findings[0]
    assert kf.id == "k_null"
    assert kf.content == "Finding with null fields"
    assert kf.category == "discovery"  # None defaults to "discovery"
    assert kf.confidence == "medium"   # None defaults to "medium"
    assert kf.rationale == ""          # None defaults to ""
    assert kf.source_task_title == ""  # None defaults to ""
