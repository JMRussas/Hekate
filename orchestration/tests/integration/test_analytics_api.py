#  Orchestration Engine - Analytics API Integration Tests
#
#  Tests for admin-only analytics endpoints: cost breakdown, task outcomes, efficiency.
#
#  Depends on: backend/routes/analytics.py, tests/conftest.py
#  Used by:    pytest

import time


async def _get_admin_client(app_client, tmp_db):
    """Register an admin user (first user) and return the authed client."""
    resp = await app_client.post("/api/auth/register", json={
        "email": "admin@example.com",
        "password": "adminpass123",
        "display_name": "Admin",
    })
    assert resp.status_code == 201

    resp = await app_client.post("/api/auth/login", json={
        "email": "admin@example.com",
        "password": "adminpass123",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    app_client.headers["Authorization"] = f"Bearer {token}"
    return app_client


async def _seed_analytics_data(tmp_db):
    """Insert sample projects, tasks, usage_log, and checkpoints.

    Seed data summary:
      - 1 project (proj_analytics / "Analytics Test")
      - 7 tasks: 3 haiku (2 completed, 1 failed),
                 4 sonnet (2 completed, 1 needs_review, 1 cancelled)
      - 5 usage_log entries (for terminal tasks with API spend: t1, t2, t4, t6, t7)
      - 2 checkpoints (1 unresolved, 1 resolved)
    """
    now = time.time()
    project_id = "proj_analytics"

    await tmp_db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, 'Analytics Test', 'test reqs', 'executing', ?, ?)",
        (project_id, now, now),
    )
    await tmp_db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
        (f"plan_{project_id}", project_id, now),
    )

    # Tasks with different tiers and statuses
    # (id, tier, status, cost, wave, verif_status, retry_count, started_at, completed_at)
    tasks = [
        ("t1", "haiku", "completed", 0.005, 0, "passed", 0, now - 100, now - 50),
        ("t2", "haiku", "completed", 0.003, 1, "passed", 0, now - 90, now - 40),
        ("t3", "haiku", "failed", 0.002, 2, None, 0, now - 80, now - 30),
        ("t4", "sonnet", "completed", 0.05, 0, "gaps_found", 0, now - 70, now - 20),
        ("t5", "sonnet", "needs_review", 0.04, 0, "human_needed", 1, now - 60, None),
        ("t6", "sonnet", "completed", 0.06, 1, "passed", 0, now - 55, now - 10),
        ("t7", "sonnet", "cancelled", 0.02, 0, None, 0, now - 50, now - 5),
    ]
    for tid, tier, status, cost, wave, verif, retries, started, completed in tasks:
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, "
            "task_type, priority, status, model_tier, wave, retry_count, max_retries, "
            "cost_usd, verification_status, started_at, completed_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'test', 'code', 0, ?, ?, ?, ?, 5, ?, ?, ?, ?, ?, ?)",
            (tid, project_id, f"plan_{project_id}", f"Task {tid}",
             status, tier, wave, retries, cost, verif, started, completed, now, now),
        )

    # Usage log entries for terminal tasks with API spend
    # haiku tasks: t1=0.005, t2=0.003
    # sonnet tasks: t4=0.05, t6=0.06, t7=0.02 (cancelled but had spend)
    # Total: 0.138
    for tid, cost in [("t1", 0.005), ("t2", 0.003), ("t4", 0.05), ("t6", 0.06), ("t7", 0.02)]:
        await tmp_db.execute_write(
            "INSERT INTO usage_log (project_id, task_id, provider, model, "
            "prompt_tokens, completion_tokens, cost_usd, purpose, timestamp) "
            "VALUES (?, ?, 'anthropic', 'claude-3-haiku', 100, 50, ?, 'execution', ?)",
            (project_id, tid, cost, now),
        )

    # Checkpoints: 1 unresolved, 1 resolved
    await tmp_db.execute_write(
        "INSERT INTO checkpoints (id, project_id, task_id, checkpoint_type, summary, "
        "attempts_json, question, created_at) "
        "VALUES ('cp1', ?, 't3', 'retry_exhausted', 'Task failed', '[]', 'What now?', ?)",
        (project_id, now),
    )
    await tmp_db.execute_write(
        "INSERT INTO checkpoints (id, project_id, task_id, checkpoint_type, summary, "
        "attempts_json, question, resolved_at, created_at) "
        "VALUES ('cp2', ?, 't3', 'retry_exhausted', 'Resolved', '[]', 'Fixed', ?, ?)",
        (project_id, now, now),
    )

    return project_id


# ---------------------------------------------------------------------------
# Auth / Access Control
# ---------------------------------------------------------------------------

class TestAnalyticsAuth:
    async def test_cost_breakdown_requires_auth(self, app_client, tmp_db):
        resp = await app_client.get("/api/admin/analytics/cost-breakdown")
        assert resp.status_code == 401

    async def test_task_outcomes_requires_auth(self, app_client, tmp_db):
        resp = await app_client.get("/api/admin/analytics/task-outcomes")
        assert resp.status_code == 401

    async def test_efficiency_requires_auth(self, app_client, tmp_db):
        resp = await app_client.get("/api/admin/analytics/efficiency")
        assert resp.status_code == 401

    async def test_cost_breakdown_requires_admin(self, app_client, tmp_db):
        # Register admin first, then non-admin
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.post("/api/auth/register", json={
            "email": "user@example.com",
            "password": "userpass123",
            "display_name": "User",
        })
        assert resp.status_code == 201
        resp = await client.post("/api/auth/login", json={
            "email": "user@example.com",
            "password": "userpass123",
        })
        token = resp.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.get("/api/admin/analytics/cost-breakdown")
        assert resp.status_code == 403

    async def test_task_outcomes_requires_admin(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.post("/api/auth/register", json={
            "email": "user2@example.com",
            "password": "userpass123",
            "display_name": "User2",
        })
        assert resp.status_code == 201
        resp = await client.post("/api/auth/login", json={
            "email": "user2@example.com",
            "password": "userpass123",
        })
        client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        resp = await client.get("/api/admin/analytics/task-outcomes")
        assert resp.status_code == 403

    async def test_efficiency_requires_admin(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.post("/api/auth/register", json={
            "email": "user3@example.com",
            "password": "userpass123",
            "display_name": "User3",
        })
        assert resp.status_code == 201
        resp = await client.post("/api/auth/login", json={
            "email": "user3@example.com",
            "password": "userpass123",
        })
        client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        resp = await client.get("/api/admin/analytics/efficiency")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Cost Breakdown
# ---------------------------------------------------------------------------

class TestCostBreakdown:
    async def test_empty_db_returns_zeros(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.get("/api/admin/analytics/cost-breakdown")
        assert resp.status_code == 200
        data = resp.json()
        assert data["by_project"] == []
        assert data["by_model_tier"] == []
        assert data["daily_trend"] == []
        assert data["total_cost_usd"] == 0

    async def test_with_data(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        await _seed_analytics_data(tmp_db)

        resp = await client.get("/api/admin/analytics/cost-breakdown")
        assert resp.status_code == 200
        data = resp.json()

        # By project — all terminal tasks from usage_log
        assert len(data["by_project"]) == 1
        assert data["by_project"][0]["project_name"] == "Analytics Test"
        assert data["by_project"][0]["task_count"] == 5  # t1, t2, t4, t6, t7

        # By model tier — from usage_log joined with tasks (terminal only)
        tiers = {t["model_tier"]: t for t in data["by_model_tier"]}
        assert "haiku" in tiers
        assert "sonnet" in tiers
        # haiku: t1 + t2 usage_log (both completed, terminal) = 2 distinct tasks
        assert tiers["haiku"]["task_count"] == 2
        # sonnet: t4 + t6 + t7 usage_log (completed + cancelled, all terminal) = 3 tasks
        assert tiers["sonnet"]["task_count"] == 3
        # haiku cost: 0.005 + 0.003 = 0.008
        assert tiers["haiku"]["cost_usd"] == 0.008
        # sonnet cost: 0.05 + 0.06 + 0.02 = 0.13
        assert tiers["sonnet"]["cost_usd"] == 0.13

        # Daily trend — from usage_log (same source as by_project/by_model_tier)
        assert len(data["daily_trend"]) == 1
        assert data["daily_trend"][0]["api_calls"] == 5

        # Total cost = sum of by_project = 0.005 + 0.003 + 0.05 + 0.06 + 0.02 = 0.138
        assert data["total_cost_usd"] == 0.138

    async def test_days_param(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.get("/api/admin/analytics/cost-breakdown?days=7")
        assert resp.status_code == 200

    async def test_days_param_validation_below_min(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.get("/api/admin/analytics/cost-breakdown?days=0")
        assert resp.status_code == 422  # ge=1

    async def test_days_param_validation_above_max(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.get("/api/admin/analytics/cost-breakdown?days=91")
        assert resp.status_code == 422  # le=90

    async def test_days_filters_usage_log(self, app_client, tmp_db):
        """Usage_log entries outside the days window are excluded."""
        client = await _get_admin_client(app_client, tmp_db)
        now = time.time()
        project_id = "proj_days"

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, 'Days Test', 'test', 'executing', ?, ?)",
            (project_id, now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
            (f"plan_{project_id}", project_id, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, "
            "task_type, priority, status, model_tier, wave, retry_count, max_retries, "
            "created_at, updated_at) "
            "VALUES ('td1', ?, ?, 'T', 'test', 'code', 0, 'completed', 'haiku', 0, 0, 5, ?, ?)",
            (project_id, f"plan_{project_id}", now, now),
        )

        # Recent entry (within 7 days)
        await tmp_db.execute_write(
            "INSERT INTO usage_log (project_id, task_id, provider, model, "
            "prompt_tokens, completion_tokens, cost_usd, purpose, timestamp) "
            "VALUES (?, 'td1', 'anthropic', 'test', 100, 50, 0.01, 'execution', ?)",
            (project_id, now - 86400),  # 1 day ago
        )
        # Old entry (outside 7-day window)
        await tmp_db.execute_write(
            "INSERT INTO usage_log (project_id, task_id, provider, model, "
            "prompt_tokens, completion_tokens, cost_usd, purpose, timestamp) "
            "VALUES (?, 'td1', 'anthropic', 'test', 100, 50, 0.99, 'execution', ?)",
            (project_id, now - 86400 * 30),  # 30 days ago
        )

        # With days=7, only the recent entry should be included
        resp = await client.get("/api/admin/analytics/cost-breakdown?days=7")
        data = resp.json()
        assert data["total_cost_usd"] == 0.01
        assert len(data["by_project"]) == 1
        assert data["by_project"][0]["cost_usd"] == 0.01


# ---------------------------------------------------------------------------
# Task Outcomes
# ---------------------------------------------------------------------------

class TestTaskOutcomes:
    async def test_empty_db(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.get("/api/admin/analytics/task-outcomes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["by_tier"] == []
        assert data["verification_by_tier"] == []

    async def test_with_data(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        await _seed_analytics_data(tmp_db)

        resp = await client.get("/api/admin/analytics/task-outcomes")
        assert resp.status_code == 200
        data = resp.json()

        # By tier
        tiers = {t["model_tier"]: t for t in data["by_tier"]}
        assert "haiku" in tiers
        assert tiers["haiku"]["completed"] == 2
        assert tiers["haiku"]["failed"] == 1
        assert tiers["haiku"]["needs_review"] == 0
        assert tiers["haiku"]["total"] == 3
        assert tiers["haiku"]["success_rate"] == round(2 / 3, 4)

        assert "sonnet" in tiers
        assert tiers["sonnet"]["completed"] == 2
        assert tiers["sonnet"]["failed"] == 0
        assert tiers["sonnet"]["needs_review"] == 1
        assert tiers["sonnet"]["total"] == 4  # 2 completed + 1 needs_review + 1 cancelled
        assert tiers["sonnet"]["success_rate"] == round(2 / 4, 4)

        # Verification by tier
        verif = {v["model_tier"]: v for v in data["verification_by_tier"]}
        assert "haiku" in verif
        assert verif["haiku"]["passed"] == 2
        assert verif["haiku"]["gaps_found"] == 0
        assert verif["haiku"]["human_needed"] == 0
        assert verif["haiku"]["pass_rate"] == 1.0

        assert "sonnet" in verif
        assert verif["sonnet"]["passed"] == 1  # t6
        assert verif["sonnet"]["gaps_found"] == 1  # t4
        assert verif["sonnet"]["human_needed"] == 1  # t5

    async def test_unknown_verification_status_excluded(self, app_client, tmp_db):
        """Tasks with unknown verification_status values are excluded from known buckets."""
        client = await _get_admin_client(app_client, tmp_db)
        now = time.time()
        project_id = "proj_verif"

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, 'Verif Test', 'test', 'draft', ?, ?)",
            (project_id, now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
            (f"plan_{project_id}", project_id, now),
        )
        # Task with a hypothetical unknown verification status
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, "
            "task_type, priority, status, model_tier, wave, retry_count, max_retries, "
            "verification_status, created_at, updated_at) "
            "VALUES ('tv1', ?, ?, 'T', 'test', 'code', 0, 'completed', 'haiku', 0, 0, 5, "
            "'some_new_status', ?, ?)",
            (project_id, f"plan_{project_id}", now, now),
        )

        resp = await client.get("/api/admin/analytics/task-outcomes")
        data = resp.json()
        verif = {v["model_tier"]: v for v in data["verification_by_tier"]}
        # The unknown status creates a tier entry but doesn't increment known buckets
        if "haiku" in verif:
            assert verif["haiku"]["passed"] == 0
            assert verif["haiku"]["gaps_found"] == 0
            assert verif["haiku"]["human_needed"] == 0
            # total_verified is sum of known buckets, so 0
            assert verif["haiku"]["total_verified"] == 0


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------

class TestEfficiency:
    async def test_empty_db(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.get("/api/admin/analytics/efficiency")
        assert resp.status_code == 200
        data = resp.json()
        assert data["retries_by_tier"] == []
        assert data["checkpoint_count"] == 0
        assert data["unresolved_checkpoint_count"] == 0
        assert data["wave_throughput"] == []
        assert data["cost_efficiency"] == []

    async def test_with_data(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        await _seed_analytics_data(tmp_db)

        resp = await client.get("/api/admin/analytics/efficiency")
        assert resp.status_code == 200
        data = resp.json()

        # Retries — terminal statuses only
        retries = {r["model_tier"]: r for r in data["retries_by_tier"]}
        assert "haiku" in retries
        assert retries["haiku"]["total_tasks"] == 3  # 2 completed + 1 failed
        assert retries["haiku"]["tasks_with_retries"] == 0
        assert retries["haiku"]["total_retries"] == 0

        assert "sonnet" in retries
        assert retries["sonnet"]["total_tasks"] == 4  # 2 completed + 1 needs_review + 1 cancelled
        assert retries["sonnet"]["tasks_with_retries"] == 1  # t5 has retry_count=1
        assert retries["sonnet"]["total_retries"] == 1

        # Checkpoints
        assert data["checkpoint_count"] == 2
        assert data["unresolved_checkpoint_count"] == 1

        # Wave throughput — per-project, terminal tasks with both started_at and completed_at
        # t1: wave 0, t2: wave 1, t3: wave 2, t4: wave 0, t6: wave 1, t7: wave 0
        # (t5 has no completed_at, excluded)
        waves = data["wave_throughput"]
        assert len(waves) == 3  # waves 0, 1, 2 for proj_analytics
        assert all(w["project_id"] == "proj_analytics" for w in waves)
        assert all(w["project_name"] == "Analytics Test" for w in waves)
        wave_map = {w["wave"]: w for w in waves}
        assert wave_map[0]["task_count"] == 3  # t1 + t4 + t7
        assert wave_map[1]["task_count"] == 2  # t2 + t6
        assert wave_map[2]["task_count"] == 1  # t3
        assert wave_map[0]["avg_duration_seconds"] is not None

        # Cost efficiency — terminal statuses, costs from usage_log
        eff = {e["model_tier"]: e for e in data["cost_efficiency"]}
        assert "haiku" in eff
        assert eff["haiku"]["tasks_completed"] == 2
        assert eff["haiku"]["verification_pass_count"] == 2
        # haiku cost from usage_log: 0.005 + 0.003 = 0.008
        assert eff["haiku"]["cost_usd"] == 0.008
        assert eff["haiku"]["cost_per_pass"] == round(0.008 / 2, 6)

        assert "sonnet" in eff
        assert eff["sonnet"]["tasks_completed"] == 2
        assert eff["sonnet"]["verification_pass_count"] == 1  # only t6 passed
        # sonnet cost from usage_log: 0.05 + 0.06 + 0.02 = 0.13
        assert eff["sonnet"]["cost_usd"] == 0.13
