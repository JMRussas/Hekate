#  Orchestration Engine - Sentinel Route Tests
#
#  Tests for sentinel REST endpoints and SSE stream.
#  Mocks SystemSentinel and auth dependencies via DI container overrides.
#
#  Depends on: conftest.py (app_client, authed_client)
#  Used by:    CI

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from dependency_injector import providers

from backend.services.sentinel.models import (
    HealthState,
    HealthTrend,
    SentinelMessage,
    SentinelObservation,
    Severity,
)
from backend.services.sentinel.plan_sentinel import (
    InterventionAction,
    InterventionTier,
    PlanState,
    _CATEGORY_TO_INTERVENTION,
)


def _make_observation(
    observation_id="obs_001",
    category="task_stuck",
    message="Task stuck for 5 minutes",
    severity=Severity.WARNING,
    project_id="proj_001",
    task_id="t1",
    details=None,
    timestamp=None,
):
    return SentinelObservation(
        observation_id=observation_id,
        category=category,
        message=message,
        severity=severity,
        project_id=project_id,
        task_id=task_id,
        details=details or {},
        timestamp=timestamp or datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_mock_plan_sentinel(
    running=True,
    events_processed=10,
    current_wave=1,
    task_statuses=None,
    failure_counts=None,
    observation_history=None,
    handled_interventions=None,
):
    ps = MagicMock()
    ps.running = running
    ps.state = PlanState(
        events_processed=events_processed,
        current_wave=current_wave,
        task_statuses=task_statuses or {"t1": "running", "t2": "pending"},
        failure_counts=failure_counts or {"t1": 1},
        handled_interventions=handled_interventions or set(),
    )
    ps._observation_history = observation_history or []
    ps._execute_auto_intervention = AsyncMock()
    return ps


def _make_mock_sentinel(
    running=True,
    trends=None,
    plan_sentinels=None,
):
    """Build a MagicMock that quacks like SystemSentinel."""
    mock = MagicMock()
    mock.running = running
    mock.get_all_trends.return_value = trends or {}
    mock.plan_sentinels = plan_sentinels or {}
    mock._bus = MagicMock()
    return mock


@pytest.fixture
async def sentinel_client(app_client):
    """app_client with sentinel DI override and auth header."""
    from backend.app import container

    # Register + login
    resp = await app_client.post("/api/auth/register", json={
        "email": "sentinel@example.com",
        "password": "testpass123",
        "display_name": "Sentinel User",
    })
    assert resp.status_code == 201

    resp = await app_client.post("/api/auth/login", json={
        "email": "sentinel@example.com",
        "password": "testpass123",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    app_client.headers["Authorization"] = f"Bearer {token}"

    mock_sentinel = _make_mock_sentinel()
    container.system_sentinel.override(providers.Object(mock_sentinel))

    try:
        yield app_client, mock_sentinel
    finally:
        container.system_sentinel.reset_override()


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------

class TestGetStatus:
    async def test_status_empty(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        resp = await client.get("/api/sentinel/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert data["health_trends"] == {}
        assert data["plan_sentinels"] == {}
        assert data["active_plan_sentinel_count"] == 0

    async def test_status_with_trends_and_plan_sentinels(self, sentinel_client):
        client, mock_sentinel = sentinel_client

        trend = HealthTrend(resource_id="ollama")
        trend.state = HealthState.DEGRADED
        trend.previous_state = HealthState.HEALTHY
        mock_sentinel.get_all_trends.return_value = {"ollama": trend}

        ps = _make_mock_plan_sentinel()
        mock_sentinel.plan_sentinels = {"proj_001": ps}

        resp = await client.get("/api/sentinel/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert "ollama" in data["health_trends"]
        assert data["health_trends"]["ollama"]["state"] == "degraded"
        assert data["health_trends"]["ollama"]["previous_state"] == "healthy"
        assert "proj_001" in data["plan_sentinels"]
        assert data["plan_sentinels"]["proj_001"]["running"] is True
        assert data["plan_sentinels"]["proj_001"]["events_processed"] == 10
        assert data["active_plan_sentinel_count"] == 1

    async def test_status_requires_auth(self, app_client):
        resp = await app_client.get("/api/sentinel/status")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /observations
# ---------------------------------------------------------------------------

class TestListObservations:
    async def test_observations_empty(self, sentinel_client):
        client, _ = sentinel_client
        resp = await client.get("/api/sentinel/observations")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_observations_returns_all(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs1 = _make_observation(observation_id="obs_1", project_id="p1")
        obs2 = _make_observation(observation_id="obs_2", project_id="p2")
        ps = _make_mock_plan_sentinel(observation_history=[obs1, obs2])
        mock_sentinel.plan_sentinels = {"p1": ps}

        resp = await client.get("/api/sentinel/observations")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    async def test_observations_filter_by_project(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs1 = _make_observation(observation_id="obs_1", project_id="p1")
        obs2 = _make_observation(observation_id="obs_2", project_id="p2")
        ps = _make_mock_plan_sentinel(observation_history=[obs1, obs2])
        mock_sentinel.plan_sentinels = {"p1": ps}

        resp = await client.get("/api/sentinel/observations?project_id=p1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["project_id"] == "p1"

    async def test_observations_filter_by_category(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs1 = _make_observation(observation_id="obs_1", category="task_stuck")
        obs2 = _make_observation(observation_id="obs_2", category="cascade_failure")
        ps = _make_mock_plan_sentinel(observation_history=[obs1, obs2])
        mock_sentinel.plan_sentinels = {"p1": ps}

        resp = await client.get("/api/sentinel/observations?category=cascade_failure")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["category"] == "cascade_failure"

    async def test_observations_filter_by_severity(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs1 = _make_observation(observation_id="obs_1", severity=Severity.INFO)
        obs2 = _make_observation(observation_id="obs_2", severity=Severity.CRITICAL)
        ps = _make_mock_plan_sentinel(observation_history=[obs1, obs2])
        mock_sentinel.plan_sentinels = {"p1": ps}

        resp = await client.get("/api/sentinel/observations?severity=critical")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["severity"] == "critical"

    async def test_observations_invalid_severity(self, sentinel_client):
        client, _ = sentinel_client
        resp = await client.get("/api/sentinel/observations?severity=extreme")
        assert resp.status_code == 400

    async def test_observations_limit(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        observations = [
            _make_observation(
                observation_id=f"obs_{i}",
                timestamp=datetime(2026, 3, 15, 12, i, 0, tzinfo=timezone.utc),
            )
            for i in range(10)
        ]
        ps = _make_mock_plan_sentinel(observation_history=observations)
        mock_sentinel.plan_sentinels = {"p1": ps}

        resp = await client.get("/api/sentinel/observations?limit=3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3

    async def test_observations_sorted_descending(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs_early = _make_observation(
            observation_id="early",
            timestamp=datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc),
        )
        obs_late = _make_observation(
            observation_id="late",
            timestamp=datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc),
        )
        ps = _make_mock_plan_sentinel(observation_history=[obs_early, obs_late])
        mock_sentinel.plan_sentinels = {"p1": ps}

        resp = await client.get("/api/sentinel/observations")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["observation_id"] == "late"
        assert data[1]["observation_id"] == "early"

    async def test_observations_requires_auth(self, app_client):
        resp = await app_client.get("/api/sentinel/observations")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /interventions
# ---------------------------------------------------------------------------

class TestListInterventions:
    async def test_interventions_empty(self, sentinel_client):
        client, _ = sentinel_client
        resp = await client.get("/api/sentinel/interventions")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_interventions_returns_mapped_observations(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        # task_stuck maps to an intervention
        obs = _make_observation(category="task_stuck", task_id="t1")
        ps = _make_mock_plan_sentinel(observation_history=[obs])
        mock_sentinel.plan_sentinels = {"proj_001": ps}

        resp = await client.get("/api/sentinel/interventions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["action"] == "release_claim"
        assert data[0]["tier"] == "auto"
        assert data[0]["status"] == "pending"

    async def test_interventions_excludes_non_intervention_observations(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        # budget_warning has no intervention mapping
        obs = _make_observation(category="budget_warning")
        ps = _make_mock_plan_sentinel(observation_history=[obs])
        mock_sentinel.plan_sentinels = {"proj_001": ps}

        resp = await client.get("/api/sentinel/interventions")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_interventions_shows_executed_status(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs = _make_observation(category="task_stuck", task_id="t1")
        # Mark this intervention as already handled
        handled = {("release_claim", "t1")}
        ps = _make_mock_plan_sentinel(
            observation_history=[obs],
            handled_interventions=handled,
        )
        mock_sentinel.plan_sentinels = {"proj_001": ps}

        resp = await client.get("/api/sentinel/interventions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["status"] == "executed"

    async def test_interventions_filter_by_status(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs_pending = _make_observation(observation_id="obs_p", category="task_stuck", task_id="t1")
        obs_executed = _make_observation(observation_id="obs_e", category="task_stuck", task_id="t2")
        handled = {("release_claim", "t2")}
        ps = _make_mock_plan_sentinel(
            observation_history=[obs_pending, obs_executed],
            handled_interventions=handled,
        )
        mock_sentinel.plan_sentinels = {"proj_001": ps}

        resp = await client.get("/api/sentinel/interventions?status=pending")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "obs_p"

    async def test_interventions_filter_by_project(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs1 = _make_observation(observation_id="o1", category="task_stuck", project_id="p1", task_id="t1")
        obs2 = _make_observation(observation_id="o2", category="task_stuck", project_id="p2", task_id="t2")
        ps1 = _make_mock_plan_sentinel(observation_history=[obs1])
        ps2 = _make_mock_plan_sentinel(observation_history=[obs2])
        mock_sentinel.plan_sentinels = {"p1": ps1, "p2": ps2}

        resp = await client.get("/api/sentinel/interventions?project_id=p2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["project_id"] == "p2"

    async def test_interventions_requires_auth(self, app_client):
        resp = await app_client.get("/api/sentinel/interventions")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /interventions/{id}/approve
# ---------------------------------------------------------------------------

class TestApproveIntervention:
    async def test_approve_success(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs = _make_observation(
            observation_id="int_001",
            category="task_stuck",
            task_id="t1",
        )
        ps = _make_mock_plan_sentinel(observation_history=[obs])
        mock_sentinel.plan_sentinels = {"proj_001": ps}

        resp = await client.post("/api/sentinel/interventions/int_001/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "int_001"
        assert data["action"] == "release_claim"
        assert data["status"] == "approved"
        assert data["project_id"] == "proj_001"
        ps._execute_auto_intervention.assert_awaited_once()

    async def test_approve_not_found(self, sentinel_client):
        client, _ = sentinel_client
        resp = await client.post("/api/sentinel/interventions/nonexistent/approve")
        assert resp.status_code == 404

    async def test_approve_non_intervention_observation(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        # budget_warning doesn't map to intervention
        obs = _make_observation(
            observation_id="obs_budget",
            category="budget_warning",
        )
        ps = _make_mock_plan_sentinel(observation_history=[obs])
        mock_sentinel.plan_sentinels = {"proj_001": ps}

        resp = await client.post("/api/sentinel/interventions/obs_budget/approve")
        assert resp.status_code == 400

    async def test_approve_requires_auth(self, app_client):
        resp = await app_client.post("/api/sentinel/interventions/x/approve")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /interventions/{id}/reject
# ---------------------------------------------------------------------------

class TestRejectIntervention:
    async def test_reject_success(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs = _make_observation(
            observation_id="int_002",
            category="cascade_failure",
            task_id="t2",
        )
        ps = _make_mock_plan_sentinel(observation_history=[obs])
        mock_sentinel.plan_sentinels = {"proj_001": ps}

        resp = await client.post("/api/sentinel/interventions/int_002/reject")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "int_002"
        assert data["action"] == "skip_task"
        assert data["status"] == "rejected"
        # Verify it was marked as handled
        assert ("skip_task", "t2") in ps.state.handled_interventions

    async def test_reject_not_found(self, sentinel_client):
        client, _ = sentinel_client
        resp = await client.post("/api/sentinel/interventions/nonexistent/reject")
        assert resp.status_code == 404

    async def test_reject_non_intervention_observation(self, sentinel_client):
        client, mock_sentinel = sentinel_client
        obs = _make_observation(
            observation_id="obs_budget",
            category="budget_warning",
        )
        ps = _make_mock_plan_sentinel(observation_history=[obs])
        mock_sentinel.plan_sentinels = {"proj_001": ps}

        resp = await client.post("/api/sentinel/interventions/obs_budget/reject")
        assert resp.status_code == 400

    async def test_reject_requires_auth(self, app_client):
        resp = await app_client.post("/api/sentinel/interventions/x/reject")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /decisions — decision audit trail
# ---------------------------------------------------------------------------

async def _seed_decision(client, decision_id, project_id, command, reasoning, confidence, outcome=None, details=None, ts=None):
    """Insert a decision row directly into the test DB."""
    import time as _time
    from backend.app import container
    db = container.db()
    # Ensure project exists for FK
    now = _time.time()
    await db.execute_write(
        "INSERT OR IGNORE INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, 'Test', 'test', 'draft', ?, ?)",
        (project_id, now, now),
    )
    await db.execute_write(
        """INSERT INTO sentinel_decisions
           (id, project_id, timestamp, command, reasoning, confidence, outcome, details_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            decision_id, project_id, ts or _time.time(), command,
            reasoning, confidence, outcome,
            json.dumps(details) if details else None,
        ),
    )


class TestListDecisions:
    async def test_decisions_empty(self, sentinel_client):
        client, _ = sentinel_client
        resp = await client.get("/api/sentinel/decisions")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_decisions_returns_all(self, sentinel_client):
        client, _ = sentinel_client
        await _seed_decision(client, "d1", "p1", "retry_task", "stuck", 0.8)
        await _seed_decision(client, "d2", "p2", "skip_task", "cascade", 0.6)

        resp = await client.get("/api/sentinel/decisions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    async def test_decisions_filter_by_project(self, sentinel_client):
        client, _ = sentinel_client
        await _seed_decision(client, "d1", "p1", "retry_task", "r1", 0.8)
        await _seed_decision(client, "d2", "p2", "retry_task", "r2", 0.7)

        resp = await client.get("/api/sentinel/decisions?project_id=p1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["project_id"] == "p1"

    async def test_decisions_filter_by_command(self, sentinel_client):
        client, _ = sentinel_client
        await _seed_decision(client, "d1", "p1", "retry_task", "r1", 0.8)
        await _seed_decision(client, "d2", "p1", "skip_task", "r2", 0.6)

        resp = await client.get("/api/sentinel/decisions?project_id=p1&command=skip_task")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["command"] == "skip_task"

    async def test_decisions_command_only_filter(self, sentinel_client):
        """command filter without project_id uses query_similar_decisions."""
        client, _ = sentinel_client
        await _seed_decision(client, "d1", "p1", "retry_task", "r1", 0.8)
        await _seed_decision(client, "d2", "p2", "retry_task", "r2", 0.7)
        await _seed_decision(client, "d3", "p1", "skip_task", "r3", 0.6)

        resp = await client.get("/api/sentinel/decisions?command=retry_task")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(d["command"] == "retry_task" for d in data)

    async def test_decisions_limit(self, sentinel_client):
        client, _ = sentinel_client
        import time as _time
        base = _time.time()
        for i in range(10):
            await _seed_decision(client, f"d{i}", "p1", "retry_task", f"r{i}", 0.5, ts=base + i)

        resp = await client.get("/api/sentinel/decisions?limit=3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3

    async def test_decisions_sorted_newest_first(self, sentinel_client):
        client, _ = sentinel_client
        import time as _time
        base = _time.time()
        await _seed_decision(client, "old", "p1", "retry_task", "old", 0.5, ts=base)
        await _seed_decision(client, "new", "p1", "retry_task", "new", 0.5, ts=base + 100)

        resp = await client.get("/api/sentinel/decisions")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["decision_id"] == "new"
        assert data[1]["decision_id"] == "old"

    async def test_decisions_response_format(self, sentinel_client):
        client, _ = sentinel_client
        await _seed_decision(
            client, "d1", "p1", "retry_task", "5-whys analysis",
            0.85, outcome="retried", details={"depth": 3},
        )

        resp = await client.get("/api/sentinel/decisions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        record = data[0]
        assert record["decision_id"] == "d1"
        assert record["project_id"] == "p1"
        assert record["command"] == "retry_task"
        assert record["reasoning"] == "5-whys analysis"
        assert record["confidence"] == 0.85
        assert record["outcome"] == "retried"
        assert record["details"] == {"depth": 3}
        assert "timestamp" in record  # ISO format string

    async def test_decisions_requires_auth(self, app_client):
        resp = await app_client.get("/api/sentinel/decisions")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /events — SSE stream
# ---------------------------------------------------------------------------

class TestSentinelSSE:
    async def test_sse_stream_returns_event_stream(self, sentinel_client):
        """Verify the SSE endpoint returns the right content type and headers."""
        client, mock_sentinel = sentinel_client
        from backend.middleware.auth import get_user_from_sse_token
        from backend.app import app

        # Mock the bus subscriber to yield one message then stop
        mock_sub = MagicMock()
        mock_queue = asyncio.Queue()
        msg = SentinelMessage(
            topic="resource_alert",
            source="system_sentinel",
            payload={"resource": "ollama", "state": "degraded"},
        )
        await mock_queue.put(msg)
        await mock_queue.put(None)  # Sentinel to stop
        mock_sub._queue = mock_queue
        mock_sub.unsubscribe = MagicMock()
        mock_sentinel._bus.subscribe.return_value = mock_sub

        async def mock_sse_auth():
            return {"id": "test-user", "role": "user"}

        app.dependency_overrides[get_user_from_sse_token] = mock_sse_auth
        try:
            async with client.stream("GET", "/api/sentinel/events") as response:
                assert response.status_code == 200
                assert "text/event-stream" in response.headers.get("content-type", "")

                chunks = []
                async for line in response.aiter_lines():
                    chunks.append(line)
                    if len(chunks) >= 2:
                        break

            # Verify we got an SSE event
            combined = "\n".join(chunks)
            assert "event: health_update" in combined
            assert "ollama" in combined
        finally:
            app.dependency_overrides.pop(get_user_from_sse_token, None)

    async def test_sse_keepalive(self, sentinel_client):
        """Verify keepalive is sent on timeout."""
        client, mock_sentinel = sentinel_client

        mock_sub = MagicMock()
        mock_queue = asyncio.Queue()
        mock_sub._queue = mock_queue
        mock_sub.unsubscribe = MagicMock()
        mock_sentinel._bus.subscribe.return_value = mock_sub

        # Patch the timeout in the generator to be very short
        from backend.routes.sentinel import _sentinel_event_generator
        from backend.middleware.auth import get_user_from_sse_token

        async def fast_generator(sentinel):
            sub = sentinel._bus.subscribe()
            try:
                try:
                    msg = await asyncio.wait_for(sub._queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    return
            finally:
                sub.unsubscribe()

        async def mock_sse_auth():
            return {"id": "test-user", "role": "user"}

        from backend.app import app
        app.dependency_overrides[get_user_from_sse_token] = mock_sse_auth
        try:
            with patch("backend.routes.sentinel._sentinel_event_generator", fast_generator):
                async with client.stream("GET", "/api/sentinel/events") as response:
                    assert response.status_code == 200
                    chunks = []
                    async for line in response.aiter_lines():
                        chunks.append(line)
                        if chunks:
                            break

                combined = "\n".join(chunks)
                assert "keepalive" in combined
        finally:
            app.dependency_overrides.pop(get_user_from_sse_token, None)

    async def test_sse_topic_mapping(self, sentinel_client):
        """Verify SentinelBus topics map to correct SSE event names."""
        client, mock_sentinel = sentinel_client
        from backend.middleware.auth import get_user_from_sse_token
        from backend.app import app

        mock_sub = MagicMock()
        mock_queue = asyncio.Queue()

        # Send messages with different topics
        topics = [
            ("resource_alert", "health_update"),
            ("sentinel_heartbeat", "health_update"),
            ("stall_notification", "plan_observation"),
            ("contention_advisory", "plan_observation"),
            ("intervention_proposal", "intervention_proposal"),
        ]
        for topic, _ in topics:
            msg = SentinelMessage(topic=topic, source="test", payload={"test": True})
            await mock_queue.put(msg)
        await mock_queue.put(None)

        mock_sub._queue = mock_queue
        mock_sub.unsubscribe = MagicMock()
        mock_sentinel._bus.subscribe.return_value = mock_sub

        async def mock_sse_auth():
            return {"id": "test-user", "role": "user"}

        app.dependency_overrides[get_user_from_sse_token] = mock_sse_auth
        try:
            async with client.stream("GET", "/api/sentinel/events") as response:
                lines = []
                async for line in response.aiter_lines():
                    lines.append(line)

            combined = "\n".join(lines)
            for topic, expected_event in topics:
                assert f"event: {expected_event}" in combined
        finally:
            app.dependency_overrides.pop(get_user_from_sse_token, None)
