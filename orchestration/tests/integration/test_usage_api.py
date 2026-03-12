#  Orchestration Engine - Usage API Integration Tests
#
#  Budget status, usage summary, and daily/project breakdowns.
#
#  Depends on: backend/routes/usage.py, tests/conftest.py
#  Used by:    pytest




class TestUsageSummary:
    async def test_empty_summary(self, authed_client):
        resp = await authed_client.get("/api/usage/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_cost_usd"] == 0.0
        assert data["api_call_count"] == 0


class TestBudgetStatus:
    async def test_budget_returns_limits(self, authed_client):
        resp = await authed_client.get("/api/usage/budget")
        assert resp.status_code == 200
        data = resp.json()
        assert "daily_limit_usd" in data
        assert "monthly_limit_usd" in data
        assert data["daily_pct"] == 0.0


class TestDailyUsage:
    async def test_daily_empty(self, authed_client):
        resp = await authed_client.get("/api/usage/daily")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_daily_custom_days_param(self, authed_client):
        resp = await authed_client.get("/api/usage/daily?days=7")
        assert resp.status_code == 200


class TestByProjectUsage:
    async def test_by_project_empty(self, authed_client):
        resp = await authed_client.get("/api/usage/by-project")
        assert resp.status_code == 200
        assert resp.json() == []
