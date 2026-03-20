"""Unit tests for sentinel world-model dataclasses and serialization."""

from datetime import datetime, timezone

from backend.services.sentinel.models import (
    DecisionRecord,
    ExecutionStrategy,
    Intervention,
    ProjectWorldModel,
    SentinelCommand,
    Severity,
    TaskWorldState,
)


# --- SentinelCommand ---

def test_sentinel_command_values():
    assert SentinelCommand.DISPATCH_TASK.value == "dispatch_task"
    assert SentinelCommand.CANCEL_TASK.value == "cancel_task"
    assert SentinelCommand.PAUSE_PROJECT.value == "pause_project"
    assert SentinelCommand.RESUME_PROJECT.value == "resume_project"
    assert SentinelCommand.ADVANCE_WAVE.value == "advance_wave"
    assert SentinelCommand.RETRY_TASK.value == "retry_task"
    assert SentinelCommand.REASSIGN_TIER.value == "reassign_tier"
    assert SentinelCommand.SKIP_TASK.value == "skip_task"


def test_sentinel_command_roundtrip():
    for cmd in SentinelCommand:
        assert SentinelCommand(cmd.value) is cmd


# --- TaskWorldState ---

def test_task_world_state_defaults():
    t = TaskWorldState(id="t1")
    assert t.status == "pending"
    assert t.wave == 0
    assert t.retry_count == 0
    assert t.started_at is None
    assert t.completed_at is None


def test_task_world_state_roundtrip():
    now = datetime.now(timezone.utc)
    t = TaskWorldState(
        id="t1",
        status="running",
        wave=2,
        model_tier="claude_code",
        retry_count=1,
        started_at=now,
        completed_at=None,
        output_summary="did stuff",
        error="",
    )
    d = t.to_dict()
    t2 = TaskWorldState.from_dict(d)
    assert t2.id == t.id
    assert t2.status == t.status
    assert t2.wave == t.wave
    assert t2.model_tier == t.model_tier
    assert t2.retry_count == t.retry_count
    assert t2.started_at == t.started_at
    assert t2.completed_at is None
    assert t2.output_summary == t.output_summary


def test_task_world_state_with_completed_at():
    now = datetime.now(timezone.utc)
    t = TaskWorldState(id="t2", status="completed", completed_at=now)
    d = t.to_dict()
    t2 = TaskWorldState.from_dict(d)
    assert t2.completed_at == now


# --- ExecutionStrategy ---

def test_execution_strategy_defaults():
    s = ExecutionStrategy()
    assert s.max_concurrent == 4
    assert s.model_preferences == {}
    assert s.retry_policy["max_retries"] == 3
    assert s.checkpoint_on_wave is True


def test_execution_strategy_roundtrip():
    s = ExecutionStrategy(
        max_concurrent=8,
        model_preferences={"code": "claude_code", "simple": "ollama"},
        retry_policy={"max_retries": 5, "backoff_seconds": 60},
        checkpoint_on_wave=False,
    )
    d = s.to_dict()
    s2 = ExecutionStrategy.from_dict(d)
    assert s2.max_concurrent == 8
    assert s2.model_preferences == s.model_preferences
    assert s2.retry_policy["max_retries"] == 5
    assert s2.checkpoint_on_wave is False


def test_execution_strategy_from_empty_dict():
    s = ExecutionStrategy.from_dict({})
    assert s.max_concurrent == 4
    assert s.checkpoint_on_wave is True


# --- DecisionRecord ---

def test_decision_record_defaults():
    r = DecisionRecord()
    assert r.project_id == ""
    assert r.command == SentinelCommand.DISPATCH_TASK
    assert r.confidence == 1.0
    assert r.outcome == ""
    assert len(r.id) == 32  # uuid hex


def test_decision_record_roundtrip():
    now = datetime.now(timezone.utc)
    r = DecisionRecord(
        id="abc123",
        project_id="proj1",
        timestamp=now,
        command=SentinelCommand.RETRY_TASK,
        reasoning="task stuck for 5 minutes",
        confidence=0.85,
        outcome="retried_successfully",
    )
    d = r.to_dict()
    assert d["command"] == "retry_task"
    assert d["confidence"] == 0.85

    r2 = DecisionRecord.from_dict(d)
    assert r2.id == "abc123"
    assert r2.command == SentinelCommand.RETRY_TASK
    assert r2.reasoning == "task stuck for 5 minutes"
    assert r2.timestamp == now
    assert r2.outcome == "retried_successfully"


# --- ProjectWorldModel ---

def test_project_world_model_defaults():
    m = ProjectWorldModel(project_id="p1")
    assert m.status == "pending"
    assert m.tasks == {}
    assert m.current_wave == 0
    assert m.completed_waves == []
    assert m.budget_spent == 0.0
    assert m.budget_limit == 0.0
    assert m.resource_health == {}
    assert m.decision_log == []
    assert isinstance(m.strategy, ExecutionStrategy)


def test_project_world_model_roundtrip():
    now = datetime.now(timezone.utc)
    model = ProjectWorldModel(
        project_id="proj42",
        status="running",
        tasks={
            "t1": TaskWorldState(id="t1", status="completed", wave=0, completed_at=now),
            "t2": TaskWorldState(id="t2", status="running", wave=1, started_at=now),
        },
        current_wave=1,
        completed_waves=[0],
        budget_spent=1.50,
        budget_limit=10.0,
        resource_health={"ollama": "healthy", "claude": "degraded"},
        timing={"started": now.isoformat()},
        decision_log=[
            DecisionRecord(
                id="d1",
                project_id="proj42",
                timestamp=now,
                command=SentinelCommand.ADVANCE_WAVE,
                reasoning="wave 0 complete",
                confidence=1.0,
            ),
        ],
        strategy=ExecutionStrategy(max_concurrent=2),
    )

    d = model.to_dict()
    assert d["project_id"] == "proj42"
    assert len(d["tasks"]) == 2
    assert d["tasks"]["t1"]["status"] == "completed"
    assert d["completed_waves"] == [0]
    assert len(d["decision_log"]) == 1
    assert d["strategy"]["max_concurrent"] == 2

    m2 = ProjectWorldModel.from_dict(d)
    assert m2.project_id == "proj42"
    assert m2.status == "running"
    assert m2.tasks["t1"].status == "completed"
    assert m2.tasks["t2"].started_at == now
    assert m2.current_wave == 1
    assert m2.completed_waves == [0]
    assert m2.budget_spent == 1.50
    assert m2.budget_limit == 10.0
    assert m2.resource_health["claude"] == "degraded"
    assert m2.decision_log[0].command == SentinelCommand.ADVANCE_WAVE
    assert m2.strategy.max_concurrent == 2


def test_project_world_model_empty_roundtrip():
    m = ProjectWorldModel(project_id="empty")
    d = m.to_dict()
    m2 = ProjectWorldModel.from_dict(d)
    assert m2.project_id == "empty"
    assert m2.tasks == {}
    assert m2.decision_log == []


# --- Severity & Intervention enums ---

def test_severity_values():
    assert Severity.INFO.value == "info"
    assert Severity.WARNING.value == "warning"
    assert Severity.CRITICAL.value == "critical"
    assert len(Severity) == 3


def test_intervention_values():
    assert Intervention.NONE.value == "none"
    assert Intervention.PAUSE.value == "pause"
    assert Intervention.THROTTLE.value == "throttle"
    assert Intervention.ESCALATE.value == "escalate"
    assert len(Intervention) == 4


# --- SentinelCommand edge cases ---

def test_sentinel_command_count():
    assert len(SentinelCommand) == 10


def test_sentinel_command_from_invalid_value():
    import pytest
    with pytest.raises(ValueError):
        SentinelCommand("nonexistent_command")


# --- TaskWorldState edge cases ---

def test_task_world_state_error_field_roundtrip():
    t = TaskWorldState(id="t-err", status="failed", error="timeout after 300s")
    d = t.to_dict()
    t2 = TaskWorldState.from_dict(d)
    assert t2.error == "timeout after 300s"
    assert t2.status == "failed"


def test_task_world_state_all_statuses():
    for status in ("pending", "running", "completed", "failed", "cancelled"):
        t = TaskWorldState(id="t1", status=status)
        d = t.to_dict()
        t2 = TaskWorldState.from_dict(d)
        assert t2.status == status


def test_task_world_state_high_retry_count():
    t = TaskWorldState(id="t1", retry_count=99)
    d = t.to_dict()
    t2 = TaskWorldState.from_dict(d)
    assert t2.retry_count == 99


def test_task_world_state_from_dict_missing_optionals():
    """from_dict handles missing optional fields with defaults."""
    minimal = {"id": "t1", "status": "pending", "wave": 0}
    t = TaskWorldState.from_dict(minimal)
    assert t.model_tier == ""
    assert t.retry_count == 0
    assert t.output_summary == ""
    assert t.error == ""
    assert t.started_at is None
    assert t.completed_at is None


def test_task_world_state_model_tier_variations():
    for tier in ("claude_code", "gemini_cli", "ollama", "haiku", ""):
        t = TaskWorldState(id="t1", model_tier=tier)
        d = t.to_dict()
        assert d["model_tier"] == tier
        t2 = TaskWorldState.from_dict(d)
        assert t2.model_tier == tier


def test_task_world_state_long_output_summary():
    summary = "x" * 10000
    t = TaskWorldState(id="t1", output_summary=summary)
    d = t.to_dict()
    t2 = TaskWorldState.from_dict(d)
    assert t2.output_summary == summary


# --- ExecutionStrategy edge cases ---

def test_execution_strategy_custom_retry_policy():
    policy = {"max_retries": 10, "backoff_seconds": 120, "custom_key": "value"}
    s = ExecutionStrategy(retry_policy=policy)
    d = s.to_dict()
    s2 = ExecutionStrategy.from_dict(d)
    assert s2.retry_policy["max_retries"] == 10
    assert s2.retry_policy["custom_key"] == "value"


def test_execution_strategy_zero_concurrent():
    s = ExecutionStrategy(max_concurrent=0)
    d = s.to_dict()
    s2 = ExecutionStrategy.from_dict(d)
    assert s2.max_concurrent == 0


def test_execution_strategy_many_model_preferences():
    prefs = {f"task_type_{i}": f"model_{i}" for i in range(20)}
    s = ExecutionStrategy(model_preferences=prefs)
    d = s.to_dict()
    s2 = ExecutionStrategy.from_dict(d)
    assert s2.model_preferences == prefs


# --- DecisionRecord edge cases ---

def test_decision_record_unique_ids():
    r1 = DecisionRecord()
    r2 = DecisionRecord()
    assert r1.id != r2.id


def test_decision_record_all_commands_roundtrip():
    for cmd in SentinelCommand:
        r = DecisionRecord(command=cmd, reasoning=f"test {cmd.value}")
        d = r.to_dict()
        r2 = DecisionRecord.from_dict(d)
        assert r2.command == cmd
        assert r2.reasoning == f"test {cmd.value}"


def test_decision_record_zero_confidence():
    r = DecisionRecord(confidence=0.0)
    d = r.to_dict()
    r2 = DecisionRecord.from_dict(d)
    assert r2.confidence == 0.0


def test_decision_record_outcome_missing_in_dict():
    """from_dict defaults outcome to empty string if missing."""
    d = {
        "id": "x",
        "project_id": "p",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": "dispatch_task",
        "reasoning": "",
        "confidence": 1.0,
    }
    r = DecisionRecord.from_dict(d)
    assert r.outcome == ""


# --- ProjectWorldModel edge cases ---

def test_project_world_model_many_tasks():
    tasks = {}
    for i in range(50):
        tasks[f"t{i}"] = TaskWorldState(
            id=f"t{i}", status="completed" if i % 2 == 0 else "failed", wave=i // 10
        )
    m = ProjectWorldModel(project_id="big", tasks=tasks)
    d = m.to_dict()
    m2 = ProjectWorldModel.from_dict(d)
    assert len(m2.tasks) == 50
    assert m2.tasks["t0"].status == "completed"
    assert m2.tasks["t1"].status == "failed"


def test_project_world_model_multiple_decision_log():
    now = datetime.now(timezone.utc)
    decisions = [
        DecisionRecord(id=f"d{i}", project_id="p1", timestamp=now, command=cmd)
        for i, cmd in enumerate(SentinelCommand)
    ]
    m = ProjectWorldModel(project_id="p1", decision_log=decisions)
    d = m.to_dict()
    m2 = ProjectWorldModel.from_dict(d)
    assert len(m2.decision_log) == len(SentinelCommand)
    for i, cmd in enumerate(SentinelCommand):
        assert m2.decision_log[i].command == cmd


def test_project_world_model_budget_edge_values():
    m = ProjectWorldModel(project_id="p1", budget_spent=999.99, budget_limit=1000.0)
    d = m.to_dict()
    m2 = ProjectWorldModel.from_dict(d)
    assert m2.budget_spent == 999.99
    assert m2.budget_limit == 1000.0


def test_project_world_model_from_dict_missing_optionals():
    """from_dict handles minimal required fields."""
    d = {"project_id": "p1", "status": "pending"}
    m = ProjectWorldModel.from_dict(d)
    assert m.tasks == {}
    assert m.current_wave == 0
    assert m.completed_waves == []
    assert m.budget_spent == 0.0
    assert m.budget_limit == 0.0
    assert m.resource_health == {}
    assert m.timing == {}
    assert m.decision_log == []
    assert isinstance(m.strategy, ExecutionStrategy)
    assert m.strategy.max_concurrent == 4


def test_project_world_model_timing_preserved():
    timing = {"started": "2026-01-01T00:00:00", "wave_0_end": "2026-01-01T01:00:00"}
    m = ProjectWorldModel(project_id="p1", timing=timing)
    d = m.to_dict()
    m2 = ProjectWorldModel.from_dict(d)
    assert m2.timing == timing


def test_project_world_model_resource_health_many():
    health = {f"resource_{i}": "healthy" if i % 2 == 0 else "degraded" for i in range(10)}
    m = ProjectWorldModel(project_id="p1", resource_health=health)
    d = m.to_dict()
    m2 = ProjectWorldModel.from_dict(d)
    assert m2.resource_health == health


def test_project_world_model_completed_waves_order():
    m = ProjectWorldModel(project_id="p1", completed_waves=[2, 0, 1])
    d = m.to_dict()
    m2 = ProjectWorldModel.from_dict(d)
    assert m2.completed_waves == [2, 0, 1]  # preserves insertion order
