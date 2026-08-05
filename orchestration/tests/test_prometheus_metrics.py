#  Orchestration Engine - Prometheus Metrics Tests
#
#  Tests for the /metrics endpoint (Prometheus exposition format).
#  Validates format, counter accuracy, gauge snapshots, histogram buckets,
#  LLM metrics, idempotent scrapes, and empty-DB behavior.
#
#  Depends on: backend/routes/prometheus.py, backend/services/prometheus_collector.py, tests/conftest.py
#  Used by:    pytest

import time

import pytest
from prometheus_client.parser import text_string_to_metric_families


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_project(db, project_id, status="draft"):
    """Insert a project row."""
    now = time.time()
    await db.execute_write(
        "INSERT OR IGNORE INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, 'Test', 'test', ?, ?, ?)",
        (project_id, status, now, now),
    )


async def _seed_plan(db, plan_id, project_id):
    """Insert a plan row (FK target for tasks)."""
    now = time.time()
    await db.execute_write(
        "INSERT OR IGNORE INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
        (plan_id, project_id, now),
    )


async def _seed_task(db, task_id, project_id, plan_id, *,
                     status="pending", model_tier="claude_code",
                     task_type="code", created_at=None, completed_at=None):
    """Insert a task row with configurable status/provider/timing."""
    now = time.time()
    created = created_at or now
    await db.execute_write(
        "INSERT INTO tasks (id, project_id, plan_id, title, description, "
        "task_type, priority, status, model_tier, wave, retry_count, max_retries, "
        "created_at, completed_at, updated_at) "
        "VALUES (?, ?, ?, 'Task', 'desc', ?, 0, ?, ?, 0, 0, 5, ?, ?, ?)",
        (task_id, project_id, plan_id, task_type, status, model_tier,
         created, completed_at, now),
    )


async def _seed_usage(db, project_id, *,
                      provider="anthropic", model="claude-3-haiku",
                      purpose="execute", prompt_tokens=100,
                      completion_tokens=50, cost_usd=0.01):
    """Insert a usage_log row."""
    now = time.time()
    await db.execute_write(
        "INSERT INTO usage_log (project_id, provider, model, purpose, "
        "prompt_tokens, completion_tokens, cost_usd, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, provider, model, purpose,
         prompt_tokens, completion_tokens, cost_usd, now),
    )


def _parse_metrics(body: str) -> dict:
    """Parse Prometheus text into {metric_name: MetricFamily}.

    The parser strips _total suffix from counter family names, so we index
    by both the parsed name and the original name with _total for convenience.
    """
    families = {}
    for family in text_string_to_metric_families(body):
        families[family.name] = family
        # Counter families have _total stripped by the parser — add alias
        if family.type == "counter" and not family.name.endswith("_total"):
            families[family.name + "_total"] = family
    return families


def _sample_value(family, labels: dict | None = None) -> float | None:
    """Extract a single sample value from a metric family matching labels."""
    labels = labels or {}
    for sample in family.samples:
        sample_labels = {k: v for k, v in sample.labels.items()
                        if k not in ("le",)}
        if all(sample_labels.get(k) == v for k, v in labels.items()):
            if sample.name.endswith("_total") or sample.name == family.name:
                return sample.value
    return None


def _bucket_value(family, labels: dict, le: str) -> float | None:
    """Extract a histogram bucket count for a given le boundary."""
    for sample in family.samples:
        if sample.name.endswith("_bucket") and sample.labels.get("le") == le:
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return None


# ---------------------------------------------------------------------------
# 1. Format test
# ---------------------------------------------------------------------------

class TestPrometheusFormat:
    async def test_content_type_and_parseable(self, app_client, tmp_db):
        """GET /metrics returns text/plain with version=0.0.4, parseable by prometheus_client."""
        resp = await app_client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "version=0.0.4" in resp.headers["content-type"]

        body = resp.text
        families = _parse_metrics(body)
        # Should have the core metric families defined in the collector
        expected_names = [
            "tasks_total", "tasks_in_progress", "active_projects",
            "queue_depth", "task_duration_seconds",
            "llm_calls_total", "llm_tokens_total", "llm_cost_usd_total",
        ]
        for name in expected_names:
            assert name in families, f"Missing metric family: {name}"


# ---------------------------------------------------------------------------
# 2. Counter accuracy
# ---------------------------------------------------------------------------

class TestCounterAccuracy:
    async def test_tasks_total_matches_seeded(self, app_client, tmp_db):
        """Seed known status/provider combos, verify tasks_total counters."""
        pid, plan = "proj_ctr", "plan_ctr"
        await _seed_project(tmp_db, pid)
        await _seed_plan(tmp_db, plan, pid)

        # 3 completed/claude_code, 2 pending/ollama, 1 failed/claude_code
        for i in range(3):
            await _seed_task(tmp_db, f"t_cc_{i}", pid, plan,
                             status="completed", model_tier="claude_code",
                             completed_at=time.time())
        for i in range(2):
            await _seed_task(tmp_db, f"t_ol_{i}", pid, plan,
                             status="pending", model_tier="ollama")
        await _seed_task(tmp_db, "t_fail", pid, plan,
                         status="failed", model_tier="claude_code")

        resp = await app_client.get("/metrics")
        assert resp.status_code == 200
        families = _parse_metrics(resp.text)

        f = families["tasks_total"]
        assert _sample_value(f, {"status": "completed", "provider": "claude_code"}) == 3.0
        assert _sample_value(f, {"status": "pending", "provider": "ollama"}) == 2.0
        assert _sample_value(f, {"status": "failed", "provider": "claude_code"}) == 1.0


# ---------------------------------------------------------------------------
# 3. Gauge accuracy
# ---------------------------------------------------------------------------

class TestGaugeAccuracy:
    async def test_tasks_in_progress_gauge(self, app_client, tmp_db):
        """Executing tasks reflected in tasks_in_progress gauge."""
        pid, plan = "proj_g", "plan_g"
        await _seed_project(tmp_db, pid)
        await _seed_plan(tmp_db, plan, pid)

        await _seed_task(tmp_db, "t_ex1", pid, plan,
                         status="executing", model_tier="claude_code")
        await _seed_task(tmp_db, "t_ex2", pid, plan,
                         status="executing", model_tier="claude_code")
        await _seed_task(tmp_db, "t_ex3", pid, plan,
                         status="executing", model_tier="ollama")
        # A pending task should NOT be in the gauge
        await _seed_task(tmp_db, "t_pend", pid, plan,
                         status="pending", model_tier="claude_code")

        resp = await app_client.get("/metrics")
        families = _parse_metrics(resp.text)

        f = families["tasks_in_progress"]
        assert _sample_value(f, {"provider": "claude_code"}) == 2.0
        assert _sample_value(f, {"provider": "ollama"}) == 1.0

    async def test_active_projects_gauge(self, app_client, tmp_db):
        """Projects in executing/planning status counted as active."""
        await _seed_project(tmp_db, "p_exec", status="executing")
        await _seed_project(tmp_db, "p_plan", status="planning")
        await _seed_project(tmp_db, "p_done", status="completed")
        await _seed_project(tmp_db, "p_draft", status="draft")

        resp = await app_client.get("/metrics")
        families = _parse_metrics(resp.text)

        f = families["active_projects"]
        val = _sample_value(f, {})
        assert val == 2.0

    async def test_queue_depth_gauge(self, app_client, tmp_db):
        """Pending tasks counted in queue_depth gauge."""
        pid, plan = "proj_qd", "plan_qd"
        await _seed_project(tmp_db, pid)
        await _seed_plan(tmp_db, plan, pid)

        for i in range(4):
            await _seed_task(tmp_db, f"t_q_{i}", pid, plan, status="pending")
        await _seed_task(tmp_db, "t_q_exec", pid, plan, status="executing")

        resp = await app_client.get("/metrics")
        families = _parse_metrics(resp.text)

        f = families["queue_depth"]
        val = _sample_value(f, {})
        assert val == 4.0


# ---------------------------------------------------------------------------
# 4. Histogram buckets
# ---------------------------------------------------------------------------

class TestHistogramBuckets:
    async def test_duration_histogram(self, app_client, tmp_db):
        """Completed tasks with known durations land in correct buckets."""
        pid, plan = "proj_h", "plan_h"
        await _seed_project(tmp_db, pid)
        await _seed_plan(tmp_db, plan, pid)

        now = time.time()
        # Task A: 10s duration → should be in le="15" bucket
        await _seed_task(tmp_db, "t_fast", pid, plan,
                         status="completed", model_tier="claude_code",
                         task_type="code",
                         created_at=now - 10, completed_at=now)
        # Task B: 45s duration → should be in le="60" bucket
        await _seed_task(tmp_db, "t_med", pid, plan,
                         status="completed", model_tier="claude_code",
                         task_type="code",
                         created_at=now - 45, completed_at=now)
        # Task C: 200s duration → should be in le="300" bucket
        await _seed_task(tmp_db, "t_slow", pid, plan,
                         status="completed", model_tier="claude_code",
                         task_type="code",
                         created_at=now - 200, completed_at=now)

        resp = await app_client.get("/metrics")
        families = _parse_metrics(resp.text)

        f = families["task_duration_seconds"]
        labels = {"task_type": "code", "provider": "claude_code"}

        # le="15" should have 1 (the 10s task)
        assert _bucket_value(f, labels, "15.0") == 1.0
        # le="60" should have 2 (10s + 45s)
        assert _bucket_value(f, labels, "60.0") == 2.0
        # le="300" should have 3 (all three)
        assert _bucket_value(f, labels, "300.0") == 3.0
        # +Inf should have all 3
        assert _bucket_value(f, labels, "+Inf") == 3.0


# ---------------------------------------------------------------------------
# 5. LLM metrics
# ---------------------------------------------------------------------------

class TestLLMMetrics:
    async def test_token_and_cost_totals(self, app_client, tmp_db):
        """usage_log entries produce correct llm_tokens_total and llm_cost_usd_total."""
        pid = "proj_llm"
        await _seed_project(tmp_db, pid)

        # Two calls from anthropic
        await _seed_usage(tmp_db, pid, provider="anthropic", model="haiku",
                          prompt_tokens=100, completion_tokens=50, cost_usd=0.01)
        await _seed_usage(tmp_db, pid, provider="anthropic", model="haiku",
                          prompt_tokens=200, completion_tokens=100, cost_usd=0.02)
        # One call from ollama
        await _seed_usage(tmp_db, pid, provider="ollama", model="qwen",
                          prompt_tokens=500, completion_tokens=300, cost_usd=0.0)

        resp = await app_client.get("/metrics")
        families = _parse_metrics(resp.text)

        # Token totals
        tok = families["llm_tokens_total"]
        assert _sample_value(tok, {"provider": "anthropic", "token_type": "prompt"}) == 300.0
        assert _sample_value(tok, {"provider": "anthropic", "token_type": "completion"}) == 150.0
        assert _sample_value(tok, {"provider": "ollama", "token_type": "prompt"}) == 500.0
        assert _sample_value(tok, {"provider": "ollama", "token_type": "completion"}) == 300.0

        # Cost totals
        cost = families["llm_cost_usd_total"]
        assert _sample_value(cost, {"provider": "anthropic"}) == pytest.approx(0.03)
        assert _sample_value(cost, {"provider": "ollama"}) == pytest.approx(0.0)

    async def test_llm_calls_total(self, app_client, tmp_db):
        """usage_log rows counted in llm_calls_total with correct labels."""
        pid = "proj_calls"
        await _seed_project(tmp_db, pid)

        await _seed_usage(tmp_db, pid, provider="anthropic", model="haiku",
                          purpose="execute")
        await _seed_usage(tmp_db, pid, provider="anthropic", model="haiku",
                          purpose="execute")
        await _seed_usage(tmp_db, pid, provider="anthropic", model="sonnet",
                          purpose="verify")

        resp = await app_client.get("/metrics")
        families = _parse_metrics(resp.text)

        f = families["llm_calls_total"]
        assert _sample_value(f, {"provider": "anthropic", "model": "haiku", "purpose": "execute"}) == 2.0
        assert _sample_value(f, {"provider": "anthropic", "model": "sonnet", "purpose": "verify"}) == 1.0


# ---------------------------------------------------------------------------
# 6. Idempotent scrape
# ---------------------------------------------------------------------------

class TestIdempotentScrape:
    async def test_two_scrapes_consistent(self, app_client, tmp_db):
        """Two consecutive /metrics calls return identical values (stateless re-query)."""
        pid, plan = "proj_idem", "plan_idem"
        await _seed_project(tmp_db, pid, status="executing")
        await _seed_plan(tmp_db, plan, pid)
        await _seed_task(tmp_db, "t_idem", pid, plan,
                         status="completed", model_tier="claude_code",
                         completed_at=time.time())
        await _seed_usage(tmp_db, pid, provider="anthropic", model="haiku",
                          prompt_tokens=100, completion_tokens=50, cost_usd=0.005)

        resp1 = await app_client.get("/metrics")
        resp2 = await app_client.get("/metrics")

        assert resp1.status_code == 200
        assert resp2.status_code == 200

        fam1 = _parse_metrics(resp1.text)
        fam2 = _parse_metrics(resp2.text)

        # Compare key counter/gauge values
        for name in ["tasks_total", "active_projects", "llm_tokens_total", "llm_cost_usd_total"]:
            samples1 = {(s.name, tuple(sorted(s.labels.items()))): s.value
                        for s in fam1[name].samples}
            samples2 = {(s.name, tuple(sorted(s.labels.items()))): s.value
                        for s in fam2[name].samples}
            assert samples1 == samples2, f"Mismatch on {name}"


# ---------------------------------------------------------------------------
# 7. Empty DB
# ---------------------------------------------------------------------------

class TestEmptyDB:
    async def test_empty_db_valid_format(self, app_client, tmp_db):
        """Empty database returns valid Prometheus format with zero-valued metrics."""
        resp = await app_client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

        body = resp.text
        families = _parse_metrics(body)

        # Core families should still be declared
        assert "tasks_total" in families
        assert "active_projects" in families
        assert "llm_tokens_total" in families

        # Gauges should be zero
        ap = families["active_projects"]
        val = _sample_value(ap, {})
        assert val == 0.0

        qd = families["queue_depth"]
        val = _sample_value(qd, {})
        assert val == 0.0
