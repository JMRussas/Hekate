#  Orchestration Engine - Metrics Routes Integration Tests
#
#  Tests for daily metrics and error metrics endpoints.
#
#  Depends on: backend/routes/metrics.py, tests/conftest.py
#  Used by:    pytest

import json
import time
from datetime import date, timedelta


async def _get_authed_client(app_client, tmp_db):
    """Register a user (first = admin) and return the authed client."""
    resp = await app_client.post("/api/auth/register", json={
        "email": "metrics@example.com",
        "password": "metricspass123",
        "display_name": "Metrics User",
    })
    assert resp.status_code == 201

    resp = await app_client.post("/api/auth/login", json={
        "email": "metrics@example.com",
        "password": "metricspass123",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    app_client.headers["Authorization"] = f"Bearer {token}"
    return app_client


async def _seed_daily_metrics(tmp_db, days_ago_entries=None):
    """Insert sample daily_metrics rows.

    Args:
        days_ago_entries: list of (days_ago, metric_name, metric_value, details_dict|None)
            Defaults to a standard set covering today and recent days.
    """
    if days_ago_entries is None:
        today = date.today()
        days_ago_entries = [
            (0, "tasks_completed", 5.0, {"project": "alpha"}),
            (0, "tasks_failed", 1.0, None),
            (0, "error_timeout", 3.0, {"service": "hermes"}),
            (1, "tasks_completed", 8.0, {"project": "beta"}),
            (1, "error_auth", 2.0, {"endpoint": "/api/login"}),
            (3, "tasks_completed", 12.0, None),
            (10, "tasks_completed", 4.0, None),
            (10, "error_crash", 1.0, {"stack": "traceback..."}),
        ]

    now = time.time()
    today = date.today()
    for days_ago, name, value, details in days_ago_entries:
        d = (today - timedelta(days=days_ago)).isoformat()
        details_json = json.dumps(details) if details else None
        await tmp_db.execute_write(
            "INSERT INTO daily_metrics (date, metric_name, metric_value, details_json, extracted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (d, name, value, details_json, now),
        )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestMetricsAuth:
    async def test_daily_requires_auth(self, app_client, tmp_db):
        resp = await app_client.get("/api/metrics/daily")
        assert resp.status_code == 401

    async def test_errors_requires_auth(self, app_client, tmp_db):
        resp = await app_client.get("/api/metrics/errors")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/metrics/daily
# ---------------------------------------------------------------------------

class TestDailyMetrics:
    async def test_empty_db(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        resp = await client.get("/api/metrics/daily")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_grouped_by_date(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        await _seed_daily_metrics(tmp_db)

        resp = await client.get("/api/metrics/daily?days=7")
        assert resp.status_code == 200
        data = resp.json()

        assert isinstance(data, list)
        # Should include today (3 metrics), yesterday (2), 3 days ago (1)
        # but NOT 10 days ago (outside 7-day window)
        dates = [entry["date"] for entry in data]
        today_str = date.today().isoformat()
        assert today_str in dates

        old_date = (date.today() - timedelta(days=10)).isoformat()
        assert old_date not in dates

        # Check grouping structure
        for entry in data:
            assert "date" in entry
            assert "metrics" in entry
            assert isinstance(entry["metrics"], list)
            for m in entry["metrics"]:
                assert "name" in m
                assert "value" in m
                assert "details" in m

    async def test_details_parsed_as_json(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        await _seed_daily_metrics(tmp_db)

        resp = await client.get("/api/metrics/daily?days=7")
        data = resp.json()

        today_entry = next(e for e in data if e["date"] == date.today().isoformat())
        metrics_by_name = {m["name"]: m for m in today_entry["metrics"]}

        # tasks_completed has details
        assert metrics_by_name["tasks_completed"]["details"] == {"project": "alpha"}
        # tasks_failed has no details
        assert metrics_by_name["tasks_failed"]["details"] is None

    async def test_days_param_filters(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        await _seed_daily_metrics(tmp_db)

        # days=1 should only return today's metrics
        resp = await client.get("/api/metrics/daily?days=1")
        assert resp.status_code == 200
        data = resp.json()
        dates = [e["date"] for e in data]
        today_str = date.today().isoformat()
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()
        # "today" is within 1 day window, yesterday may or may not depending
        # on the >= comparison with date arithmetic
        for d in dates:
            assert d >= (date.today() - timedelta(days=1)).isoformat()

    async def test_days_param_wide_window(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        await _seed_daily_metrics(tmp_db)

        # days=90 should include everything
        resp = await client.get("/api/metrics/daily?days=90")
        assert resp.status_code == 200
        data = resp.json()
        dates = [e["date"] for e in data]
        old_date = (date.today() - timedelta(days=10)).isoformat()
        assert old_date in dates

    async def test_ordered_by_date_then_metric(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        await _seed_daily_metrics(tmp_db)

        resp = await client.get("/api/metrics/daily?days=90")
        data = resp.json()
        dates = [e["date"] for e in data]
        assert dates == sorted(dates)

    async def test_days_validation_below_min(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        resp = await client.get("/api/metrics/daily?days=0")
        assert resp.status_code == 422

    async def test_days_validation_above_max(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        resp = await client.get("/api/metrics/daily?days=91")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/metrics/errors
# ---------------------------------------------------------------------------

class TestErrorMetrics:
    async def test_empty_db(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        resp = await client.get("/api/metrics/errors")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_only_error_metrics(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        await _seed_daily_metrics(tmp_db)

        resp = await client.get("/api/metrics/errors?days=90")
        assert resp.status_code == 200
        data = resp.json()

        assert isinstance(data, list)
        assert len(data) > 0
        # All returned metrics should start with 'error'
        for entry in data:
            assert entry["metric_name"].startswith("error")
            assert "date" in entry
            assert "value" in entry
            assert "details" in entry

    async def test_error_details_parsed(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        await _seed_daily_metrics(tmp_db)

        resp = await client.get("/api/metrics/errors?days=90")
        data = resp.json()

        by_name = {e["metric_name"]: e for e in data if e["date"] == date.today().isoformat()}
        assert by_name["error_timeout"]["details"] == {"service": "hermes"}

    async def test_days_filters_errors(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        await _seed_daily_metrics(tmp_db)

        # days=1 should NOT include the error_crash from 10 days ago
        resp = await client.get("/api/metrics/errors?days=1")
        data = resp.json()
        metric_names = [e["metric_name"] for e in data]
        assert "error_crash" not in metric_names

    async def test_ordered_by_date_desc(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        await _seed_daily_metrics(tmp_db)

        resp = await client.get("/api/metrics/errors?days=90")
        data = resp.json()
        dates = [e["date"] for e in data]
        assert dates == sorted(dates, reverse=True)

    async def test_days_validation_below_min(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        resp = await client.get("/api/metrics/errors?days=0")
        assert resp.status_code == 422

    async def test_days_validation_above_max(self, app_client, tmp_db):
        client = await _get_authed_client(app_client, tmp_db)
        resp = await client.get("/api/metrics/errors?days=91")
        assert resp.status_code == 422

    async def test_default_days_is_1(self, app_client, tmp_db):
        """Default days=1 should exclude metrics from 3+ days ago."""
        client = await _get_authed_client(app_client, tmp_db)
        await _seed_daily_metrics(tmp_db)

        resp = await client.get("/api/metrics/errors")
        data = resp.json()
        # Should not include error_crash (10 days ago) or error_auth (1 day ago might be borderline)
        for entry in data:
            entry_date = date.fromisoformat(entry["date"])
            assert (date.today() - entry_date).days <= 1
