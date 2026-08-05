#  Orchestration Engine - Prometheus Metrics Collector
#
#  Stateless collector that queries existing DB tables and populates
#  a fresh prometheus_client CollectorRegistry on each scrape.
#  Uses a custom registry to avoid conflicts with the default global one.
#
#  Depends on: backend/db/connection.py
#  Used by:    container.py, routes/metrics.py

from __future__ import annotations

import logging

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)

logger = logging.getLogger(__name__)


class PrometheusCollector:
    """Owns all Prometheus metric definitions and collection logic.

    Each call to ``collect()`` creates a fresh registry, queries the DB,
    and populates metrics from current state.  This avoids stale gauge
    values — gauges always reflect the latest snapshot.
    """

    def __init__(self, db):
        self._db = db

    # ------------------------------------------------------------------
    # Registry + metric factory
    # ------------------------------------------------------------------

    def _build_registry(self):
        """Create a fresh registry with all metric families defined."""
        reg = CollectorRegistry(auto_describe=False)

        metrics = {}

        # --- Counters ---
        metrics["tasks_total"] = Counter(
            "tasks_total",
            "Total tasks by status and provider",
            ["status", "provider"],
            registry=reg,
        )
        metrics["retries_total"] = Counter(
            "retries_total",
            "Total retry events",
            registry=reg,
        )
        metrics["checkpoints_total"] = Counter(
            "checkpoints_total",
            "Total checkpoint events",
            registry=reg,
        )
        metrics["compensation_total"] = Counter(
            "compensation_total",
            "Total compensation events",
            registry=reg,
        )

        # --- Gauges ---
        metrics["tasks_in_progress"] = Gauge(
            "tasks_in_progress",
            "Tasks currently executing",
            ["provider"],
            registry=reg,
        )
        metrics["active_projects"] = Gauge(
            "active_projects",
            "Projects in executing or planning status",
            registry=reg,
        )
        metrics["queue_depth"] = Gauge(
            "queue_depth",
            "Tasks in pending status",
            registry=reg,
        )

        # --- Histograms ---
        metrics["task_duration_seconds"] = Histogram(
            "task_duration_seconds",
            "Task duration from created to completed",
            ["task_type", "provider"],
            buckets=(5, 15, 30, 60, 120, 300, 600, 1800, 3600),
            registry=reg,
        )
        metrics["retry_delay_seconds"] = Histogram(
            "retry_delay_seconds",
            "Delay between retry events",
            buckets=(1, 5, 10, 30, 60, 120, 300),
            registry=reg,
        )

        # --- LLM-specific ---
        metrics["llm_calls_total"] = Counter(
            "llm_calls_total",
            "Total LLM API calls",
            ["provider", "model", "purpose"],
            registry=reg,
        )
        metrics["llm_tokens_total"] = Counter(
            "llm_tokens_total",
            "Total LLM tokens consumed",
            ["provider", "token_type"],
            registry=reg,
        )
        metrics["llm_cost_usd_total"] = Counter(
            "llm_cost_usd_total",
            "Total LLM cost in USD",
            ["provider"],
            registry=reg,
        )
        metrics["llm_latency_seconds"] = Histogram(
            "llm_latency_seconds",
            "LLM call latency",
            ["provider"],
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
            registry=reg,
        )

        return reg, metrics

    # ------------------------------------------------------------------
    # collect() — main entry point
    # ------------------------------------------------------------------

    async def collect(self) -> CollectorRegistry:
        """Query DB tables and return a populated registry."""
        reg, m = self._build_registry()

        try:
            await self._collect_tasks(m)
            await self._collect_usage(m)
            await self._collect_projects(m)
            await self._collect_relay_events(m)
        except Exception:
            logger.exception("prometheus_collector: error during collect()")

        return reg

    # ------------------------------------------------------------------
    # Per-table collectors
    # ------------------------------------------------------------------

    async def _collect_tasks(self, m: dict) -> None:
        """Populate task counters, gauges, and duration histograms."""
        # Task counts by status + provider
        rows = await self._db.fetchall(
            "SELECT status, COALESCE(model_tier, 'unknown') AS provider, COUNT(*) AS cnt "
            "FROM tasks GROUP BY status, provider",
            (),
        )
        for row in rows:
            status, provider, cnt = row["status"], row["provider"], row["cnt"]
            m["tasks_total"].labels(status=status, provider=provider).inc(cnt)

        # In-progress gauge
        rows = await self._db.fetchall(
            "SELECT COALESCE(model_tier, 'unknown') AS provider, COUNT(*) AS cnt "
            "FROM tasks WHERE status = 'executing' GROUP BY provider",
            (),
        )
        for row in rows:
            m["tasks_in_progress"].labels(provider=row["provider"]).set(row["cnt"])

        # Queue depth
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE status = 'pending'",
            (),
        )
        if row:
            m["queue_depth"].set(row["cnt"])

        # Duration histogram (completed tasks only)
        rows = await self._db.fetchall(
            "SELECT task_type, COALESCE(model_tier, 'unknown') AS provider, "
            "  (completed_at - created_at) AS duration "
            "FROM tasks "
            "WHERE status = 'completed' AND completed_at IS NOT NULL AND created_at IS NOT NULL",
            (),
        )
        for row in rows:
            duration = row["duration"]
            if duration is not None and duration >= 0:
                m["task_duration_seconds"].labels(
                    task_type=row["task_type"], provider=row["provider"]
                ).observe(duration)

    async def _collect_usage(self, m: dict) -> None:
        """Populate LLM counters from usage_log."""
        # Call counts by provider/model/purpose
        rows = await self._db.fetchall(
            "SELECT provider, model, COALESCE(purpose, '') AS purpose, COUNT(*) AS cnt "
            "FROM usage_log GROUP BY provider, model, purpose",
            (),
        )
        for row in rows:
            m["llm_calls_total"].labels(
                provider=row["provider"], model=row["model"], purpose=row["purpose"]
            ).inc(row["cnt"])

        # Token totals by provider
        rows = await self._db.fetchall(
            "SELECT provider, "
            "  SUM(prompt_tokens) AS prompt_sum, "
            "  SUM(completion_tokens) AS completion_sum "
            "FROM usage_log GROUP BY provider",
            (),
        )
        for row in rows:
            p = row["provider"]
            prompt = row["prompt_sum"] or 0
            completion = row["completion_sum"] or 0
            m["llm_tokens_total"].labels(provider=p, token_type="prompt").inc(prompt)
            m["llm_tokens_total"].labels(provider=p, token_type="completion").inc(completion)

        # Cost totals by provider
        rows = await self._db.fetchall(
            "SELECT provider, SUM(cost_usd) AS total_cost FROM usage_log GROUP BY provider",
            (),
        )
        for row in rows:
            cost = row["total_cost"] or 0.0
            m["llm_cost_usd_total"].labels(provider=row["provider"]).inc(cost)

    async def _collect_projects(self, m: dict) -> None:
        """Populate active_projects gauge."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS cnt FROM projects WHERE status IN ('executing', 'planning')",
            (),
        )
        if row:
            m["active_projects"].set(row["cnt"])

    async def _collect_relay_events(self, m: dict) -> None:
        """Populate retry/checkpoint/compensation counters from god_relay_events."""
        rows = await self._db.fetchall(
            "SELECT event_type, COUNT(*) AS cnt FROM god_relay_events GROUP BY event_type",
            (),
        )
        # Map event types to metrics.  The relay uses event_type strings
        # like 'task_retry', 'checkpoint_created', 'compensation_triggered'.
        retry_keywords = ("retry",)
        checkpoint_keywords = ("checkpoint",)
        compensation_keywords = ("compensation", "rollback")

        for row in rows:
            et, cnt = row["event_type"], row["cnt"]
            et_lower = et.lower()

            if any(kw in et_lower for kw in retry_keywords):
                m["retries_total"].inc(cnt)
            elif any(kw in et_lower for kw in checkpoint_keywords):
                m["checkpoints_total"].inc(cnt)
            elif any(kw in et_lower for kw in compensation_keywords):
                m["compensation_total"].inc(cnt)

        # Retry delay histogram — compute delay between consecutive retry
        # events per task (from payload JSON is fragile; use created_at gaps).
        # Only meaningful if we have retry pairs, skip if table is empty.
        retry_rows = await self._db.fetchall(
            "SELECT created_at FROM god_relay_events "
            "WHERE event_type LIKE '%retry%' ORDER BY created_at",
            (),
        )
        prev_ts = None
        for row in retry_rows:
            ts = row["created_at"]
            if prev_ts is not None and ts > prev_ts:
                m["retry_delay_seconds"].observe(ts - prev_ts)
            prev_ts = ts
