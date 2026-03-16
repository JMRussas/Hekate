#  Orchestration Engine - Sentinel Intervention DB Tests
#
#  Tests for durable intervention persistence and the DB-backed
#  intervention API routes (GET, approve, reject).
#
#  Depends on: conftest.py (app_client, tmp_db)
#  Used by:    CI

import json
import time
import uuid

import pytest
from dependency_injector import providers
from unittest.mock import AsyncMock, MagicMock

from backend.services.sentinel.models import Severity, SentinelObservation
from backend.services.sentinel.plan_sentinel import (
    InterventionAction,
    InterventionTier,
    PlanState,
)
from tests.conftest import create_test_project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _intervention_id():
    return str(uuid.uuid4())


async def _insert_intervention(db, *, id=None, project_id="proj1", task_id="t1",
                                category="intervention_proposal", severity="warning",
                                message="Proposed release_claim: task stuck",
                                details=None, created_at=None):
    """Insert a sentinel_observations row for an intervention."""
    row_id = id or _intervention_id()
    details = details or {
        "action": "release_claim",
        "tier": "supervised",
        "reasoning": "Task stuck for 5 minutes",
        "observation_id": "obs_001",
        "task_id": task_id,
    }
    await db.execute_write(
        "INSERT OR IGNORE INTO sentinel_observations "
        "(id, project_id, task_id, category, severity, message, details_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, project_id, task_id, category, severity, message,
         json.dumps(details), created_at or time.time()),
    )
    return row_id


def _make_mock_plan_sentinel():
    ps = MagicMock()
    ps.running = True
    ps.state = PlanState(
        events_processed=5,
        current_wave=1,
        task_statuses={"t1": "running"},
        failure_counts={},
        handled_interventions=set(),
    )
    ps._execute_auto_intervention = AsyncMock()
    return ps


def _make_mock_sentinel(plan_sentinels=None):
    mock = MagicMock()
    mock.running = True
    mock.get_all_trends.return_value = {}
    mock.plan_sentinels = plan_sentinels or {}
    mock._bus = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# Fixture: authed client with sentinel DI override
# ---------------------------------------------------------------------------

@pytest.fixture
async def intervention_client(app_client):
    """app_client with auth + mock sentinel + real DB for intervention tests."""
    from backend.app import container

    resp = await app_client.post("/api/auth/register", json={
        "email": "intv@example.com",
        "password": "testpass123",
        "display_name": "Intervention User",
    })
    assert resp.status_code == 201

    resp = await app_client.post("/api/auth/login", json={
        "email": "intv@example.com",
        "password": "testpass123",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    app_client.headers["Authorization"] = f"Bearer {token}"

    mock_sentinel = _make_mock_sentinel()
    container.system_sentinel.override(providers.Object(mock_sentinel))

    # Get the DB from the container so we can seed it
    db = container.db()

    # Create FK-safe project
    await create_test_project(db, "proj1")

    try:
        yield app_client, mock_sentinel, db
    finally:
        container.system_sentinel.reset_override()


# ---------------------------------------------------------------------------
# GET /interventions — DB-backed queries
# ---------------------------------------------------------------------------

class TestListInterventionsDB:
    async def test_empty(self, intervention_client):
        client, _, _ = intervention_client
        resp = await client.get("/api/sentinel/interventions")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_proposals(self, intervention_client):
        client, _, db = intervention_client
        iid = await _insert_intervention(db, project_id="proj1")

        resp = await client.get("/api/sentinel/interventions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == iid
        assert data[0]["action"] == "release_claim"
        assert data[0]["tier"] == "supervised"
        assert data[0]["status"] == "pending"
        assert data[0]["category"] == "intervention_proposal"

    async def test_returns_results(self, intervention_client):
        client, _, db = intervention_client
        iid = await _insert_intervention(
            db,
            category="intervention_result",
            message="Auto release_claim: succeeded — claim released",
            details={
                "action": "release_claim",
                "tier": "auto",
                "reasoning": "claim released",
                "success": True,
                "observation_id": "obs_001",
                "task_id": "t1",
            },
        )

        resp = await client.get("/api/sentinel/interventions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == iid
        assert data[0]["status"] == "executed"
        assert data[0]["tier"] == "auto"
        assert data[0]["details"]["success"] is True

    async def test_excludes_non_intervention_categories(self, intervention_client):
        client, _, db = intervention_client
        # Insert a regular observation — should NOT appear in interventions
        await db.execute_write(
            "INSERT INTO sentinel_observations "
            "(id, project_id, task_id, category, severity, message, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_intervention_id(), "proj1", "t1", "task_stuck", "warning",
             "Task stuck", "{}", time.time()),
        )
        await _insert_intervention(db)

        resp = await client.get("/api/sentinel/interventions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["category"] in ("intervention_proposal", "intervention_result")

    async def test_filter_by_status(self, intervention_client):
        client, _, db = intervention_client
        # One pending proposal
        await _insert_intervention(db, id="pending_1")
        # One approved proposal
        await _insert_intervention(
            db, id="approved_1",
            details={
                "action": "release_claim",
                "tier": "supervised",
                "reasoning": "stuck",
                "status": "approved",
                "observation_id": "obs_002",
                "task_id": "t1",
            },
        )

        resp = await client.get("/api/sentinel/interventions?status=pending")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "pending_1"

        resp = await client.get("/api/sentinel/interventions?status=approved")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "approved_1"

    async def test_filter_by_project(self, intervention_client):
        client, _, db = intervention_client
        await create_test_project(db, "proj2")
        await _insert_intervention(db, id="p1_intv", project_id="proj1")
        await _insert_intervention(db, id="p2_intv", project_id="proj2")

        resp = await client.get("/api/sentinel/interventions?project_id=proj1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["project_id"] == "proj1"

    async def test_respects_limit(self, intervention_client):
        client, _, db = intervention_client
        for i in range(5):
            await _insert_intervention(
                db, id=f"intv_{i}",
                created_at=time.time() + i,
            )

        resp = await client.get("/api/sentinel/interventions?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_ordered_descending(self, intervention_client):
        client, _, db = intervention_client
        t = time.time()
        await _insert_intervention(db, id="early", created_at=t - 100)
        await _insert_intervention(db, id="late", created_at=t + 100)

        resp = await client.get("/api/sentinel/interventions")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["id"] == "late"
        assert data[1]["id"] == "early"

    async def test_requires_auth(self, app_client):
        resp = await app_client.get("/api/sentinel/interventions")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /interventions/{id}/approve — DB-backed
# ---------------------------------------------------------------------------

class TestApproveInterventionDB:
    async def test_approve_updates_db(self, intervention_client):
        client, mock_sentinel, db = intervention_client
        iid = await _insert_intervention(db)

        resp = await client.post(f"/api/sentinel/interventions/{iid}/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == iid
        assert data["status"] == "approved"
        assert data["action"] == "release_claim"

        # Verify DB was updated
        row = await db.fetchone(
            "SELECT details_json FROM sentinel_observations WHERE id = ?", (iid,)
        )
        details = json.loads(row["details_json"])
        assert details["status"] == "approved"
        assert "approved_by" in details
        assert "approved_at" in details

    async def test_approve_executes_on_running_sentinel(self, intervention_client):
        client, mock_sentinel, db = intervention_client
        ps = _make_mock_plan_sentinel()
        mock_sentinel.plan_sentinels = {"proj1": ps}

        iid = await _insert_intervention(db)

        resp = await client.post(f"/api/sentinel/interventions/{iid}/approve")
        assert resp.status_code == 200
        ps._execute_auto_intervention.assert_awaited_once()

    async def test_approve_without_running_sentinel(self, intervention_client):
        """Approve should succeed even if plan sentinel is no longer running."""
        client, mock_sentinel, db = intervention_client
        mock_sentinel.plan_sentinels = {}  # No running sentinels

        iid = await _insert_intervention(db)

        resp = await client.post(f"/api/sentinel/interventions/{iid}/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    async def test_approve_not_found(self, intervention_client):
        client, _, _ = intervention_client
        resp = await client.post("/api/sentinel/interventions/nonexistent/approve")
        assert resp.status_code == 404

    async def test_approve_already_approved(self, intervention_client):
        client, _, db = intervention_client
        iid = await _insert_intervention(
            db,
            details={
                "action": "release_claim",
                "tier": "supervised",
                "reasoning": "stuck",
                "status": "approved",
                "approved_by": "someone",
                "observation_id": "obs_001",
                "task_id": "t1",
            },
        )

        resp = await client.post(f"/api/sentinel/interventions/{iid}/approve")
        assert resp.status_code == 400
        assert "already approved" in resp.json()["detail"].lower()

    async def test_approve_already_rejected(self, intervention_client):
        client, _, db = intervention_client
        iid = await _insert_intervention(
            db,
            details={
                "action": "release_claim",
                "tier": "supervised",
                "reasoning": "stuck",
                "status": "rejected",
                "observation_id": "obs_001",
                "task_id": "t1",
            },
        )

        resp = await client.post(f"/api/sentinel/interventions/{iid}/approve")
        assert resp.status_code == 400
        assert "already rejected" in resp.json()["detail"].lower()

    async def test_approve_intervention_result_not_found(self, intervention_client):
        """Cannot approve an intervention_result row (only proposals)."""
        client, _, db = intervention_client
        iid = await _insert_intervention(db, category="intervention_result")

        resp = await client.post(f"/api/sentinel/interventions/{iid}/approve")
        assert resp.status_code == 404

    async def test_approve_requires_auth(self, app_client):
        resp = await app_client.post("/api/sentinel/interventions/x/approve")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /interventions/{id}/reject — DB-backed
# ---------------------------------------------------------------------------

class TestRejectInterventionDB:
    async def test_reject_updates_db(self, intervention_client):
        client, _, db = intervention_client
        iid = await _insert_intervention(db)

        resp = await client.post(f"/api/sentinel/interventions/{iid}/reject")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == iid
        assert data["status"] == "rejected"
        assert data["action"] == "release_claim"

        # Verify DB was updated
        row = await db.fetchone(
            "SELECT details_json FROM sentinel_observations WHERE id = ?", (iid,)
        )
        details = json.loads(row["details_json"])
        assert details["status"] == "rejected"
        assert "rejected_by" in details
        assert "rejected_at" in details

    async def test_reject_marks_handled_in_sentinel(self, intervention_client):
        client, mock_sentinel, db = intervention_client
        ps = _make_mock_plan_sentinel()
        mock_sentinel.plan_sentinels = {"proj1": ps}

        iid = await _insert_intervention(db)

        resp = await client.post(f"/api/sentinel/interventions/{iid}/reject")
        assert resp.status_code == 200
        assert ("release_claim", "t1") in ps.state.handled_interventions

    async def test_reject_without_running_sentinel(self, intervention_client):
        client, mock_sentinel, db = intervention_client
        mock_sentinel.plan_sentinels = {}

        iid = await _insert_intervention(db)

        resp = await client.post(f"/api/sentinel/interventions/{iid}/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    async def test_reject_not_found(self, intervention_client):
        client, _, _ = intervention_client
        resp = await client.post("/api/sentinel/interventions/nonexistent/reject")
        assert resp.status_code == 404

    async def test_reject_already_approved(self, intervention_client):
        client, _, db = intervention_client
        iid = await _insert_intervention(
            db,
            details={
                "action": "release_claim",
                "tier": "supervised",
                "reasoning": "stuck",
                "status": "approved",
                "observation_id": "obs_001",
                "task_id": "t1",
            },
        )

        resp = await client.post(f"/api/sentinel/interventions/{iid}/reject")
        assert resp.status_code == 400

    async def test_reject_already_rejected(self, intervention_client):
        client, _, db = intervention_client
        iid = await _insert_intervention(
            db,
            details={
                "action": "release_claim",
                "tier": "supervised",
                "reasoning": "stuck",
                "status": "rejected",
                "observation_id": "obs_001",
                "task_id": "t1",
            },
        )

        resp = await client.post(f"/api/sentinel/interventions/{iid}/reject")
        assert resp.status_code == 400

    async def test_reject_requires_auth(self, app_client):
        resp = await app_client.post("/api/sentinel/interventions/x/reject")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Persistence from PlanSentinel methods (unit tests)
# ---------------------------------------------------------------------------

class TestInterventionPersistence:
    """Test that _publish_intervention_proposal and _execute_auto_intervention
    persist to the sentinel_observations table."""

    async def test_publish_proposal_persists(self, tmp_db):
        """_publish_intervention_proposal writes an intervention_proposal row."""
        from backend.services.sentinel.plan_sentinel import PlanSentinel
        from backend.services.sentinel.bus import SentinelBus

        await create_test_project(tmp_db, "proj_persist")

        bus = SentinelBus()
        ps = PlanSentinel.__new__(PlanSentinel)
        ps._project_id = "proj_persist"
        ps._db = tmp_db
        ps._bus = bus
        ps._decision_logger = None
        ps._state = PlanState()
        ps._state.handled_interventions = set()

        obs = SentinelObservation(
            observation_id="obs_test_1",
            category="task_stuck",
            message="Task stuck for 5 minutes",
            severity=Severity.WARNING,
            project_id="proj_persist",
            task_id="t_stuck",
            details={"wave": 1},
        )

        await ps._publish_intervention_proposal(
            InterventionAction.RELEASE_CLAIM, obs, reasoning_result=None,
        )

        rows = await tmp_db.fetchall(
            "SELECT * FROM sentinel_observations WHERE category = 'intervention_proposal' AND project_id = ?",
            ("proj_persist",),
        )
        assert len(rows) == 1
        details = json.loads(rows[0]["details_json"])
        assert details["action"] == "release_claim"
        assert details["tier"] == "supervised"
        assert details["observation_id"] == "obs_test_1"
        assert rows[0]["task_id"] == "t_stuck"

    async def test_execute_auto_persists(self, tmp_db):
        """_execute_auto_intervention writes an intervention_result row."""
        from backend.services.sentinel.plan_sentinel import PlanSentinel
        from backend.services.sentinel.bus import SentinelBus
        from backend.services.sentinel.intervention_executor import InterventionResult

        await create_test_project(tmp_db, "proj_auto")

        bus = SentinelBus()
        mock_executor = AsyncMock()
        mock_executor.release_claim = AsyncMock(
            return_value=InterventionResult(
                action="release_claim", success=True, detail="claim released",
                task_id="t_auto",
            )
        )

        ps = PlanSentinel.__new__(PlanSentinel)
        ps._project_id = "proj_auto"
        ps._db = tmp_db
        ps._bus = bus
        ps._decision_logger = None
        ps._executor = mock_executor
        ps._state = PlanState()
        ps._state.handled_interventions = set()
        ps._state.retry_counts = {}

        obs = SentinelObservation(
            observation_id="obs_auto_1",
            category="task_stuck",
            message="Task stuck for 5 minutes",
            severity=Severity.WARNING,
            project_id="proj_auto",
            task_id="t_auto",
            details={},
        )

        await ps._execute_auto_intervention(
            InterventionAction.RELEASE_CLAIM, obs, reasoning_result=None,
        )

        rows = await tmp_db.fetchall(
            "SELECT * FROM sentinel_observations WHERE category = 'intervention_result' AND project_id = ?",
            ("proj_auto",),
        )
        assert len(rows) == 1
        details = json.loads(rows[0]["details_json"])
        assert details["action"] == "release_claim"
        assert details["tier"] == "auto"
        assert details["success"] is True
        assert rows[0]["task_id"] == "t_auto"
