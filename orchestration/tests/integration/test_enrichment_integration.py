#  Orchestration Engine - Context Enrichment Integration Tests
#
#  Verifies the full path from config reading through HTTP client call to
#  prompt injection and usage_log metadata writing.  All HTTP traffic is
#  intercepted at the httpx level — no real context store required.
#
#  Depends on: backend/services/enrichment_service.py,
#              backend/services/context_store_client.py,
#              backend/services/task_lifecycle.py, tests/conftest.py
#  Used by:    pytest

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.services.context_store_client import ContextStoreClient, ContextStoreResult
from backend.services.enrichment_service import EnrichmentService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_http_mock(status: int = 200, body=None):
    """Return a mock httpx.AsyncClient whose post() returns a canned response."""
    response = MagicMock()
    response.status_code = status
    if status >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=response
        )
    else:
        response.raise_for_status = MagicMock()
    response.json.return_value = body or []

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    # Must return False so exceptions raised inside the context manager are NOT
    # suppressed — a truthy __aexit__ return value swallows the exception and
    # lets execution continue past the `async with` block without `data` set.
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _patch_http(mock_client):
    """Context manager that intercepts httpx.AsyncClient construction."""
    return patch("httpx.AsyncClient", return_value=mock_client)


def _enabled_cfg(max_tokens: int = 2000, include_prior_outcomes: bool = True):
    """side_effect function for patching cfg with enrichment enabled."""
    def _cfg(key, default=None):
        mapping = {
            "context_enrichment.enabled": True,
            "context_enrichment.max_context_tokens": max_tokens,
            "context_enrichment.include_prior_outcomes": include_prior_outcomes,
            "context_enrichment.context_store_url": "http://localhost:5102",
        }
        return mapping.get(key, default)
    return _cfg


def _disabled_cfg():
    """side_effect function for patching cfg with enrichment disabled."""
    def _cfg(key, default=None):
        if key == "context_enrichment.enabled":
            return False
        return default
    return _cfg


# ---------------------------------------------------------------------------
# Test suite 1: ContextStoreClient HTTP behaviour
# ---------------------------------------------------------------------------

class TestContextStoreClientHttp:
    """Integration tests for ContextStoreClient — real class, mocked httpx."""

    async def test_success_list_body(self):
        nodes = [
            {"type": "gotcha", "title": "NullRef", "content": "Check for null"},
            {"type": "convention", "title": "Style", "content": "Use snake_case"},
        ]
        mock_client = _make_http_mock(200, nodes)
        with _patch_http(mock_client):
            client = ContextStoreClient(base_url="http://localhost:5102")
            result = await client.preview("implement auth module")

        assert result is not None
        assert len(result.nodes) == 2
        assert result.latency_ms >= 0

    async def test_success_dict_body_nodes_key(self):
        body = {"nodes": [{"type": "gotcha", "content": "watch for X"}], "total": 1}
        mock_client = _make_http_mock(200, body)
        with _patch_http(mock_client):
            client = ContextStoreClient(base_url="http://localhost:5102")
            result = await client.preview("query")

        assert result is not None
        assert len(result.nodes) == 1

    async def test_success_dict_body_results_key(self):
        body = {"results": [{"type": "convention", "content": "use DI"}]}
        mock_client = _make_http_mock(200, body)
        with _patch_http(mock_client):
            client = ContextStoreClient(base_url="http://localhost:5102")
            result = await client.preview("query")

        assert result is not None
        assert len(result.nodes) == 1

    async def test_request_payload_shape(self):
        """Client must POST {query, maxNodes} to /api/preview."""
        mock_client = _make_http_mock(200, [])
        with _patch_http(mock_client):
            client = ContextStoreClient(base_url="http://localhost:5102")
            await client.preview("auth module", max_nodes=10)

        call_args = mock_client.post.call_args
        assert call_args.kwargs["json"]["query"] == "auth module"
        assert call_args.kwargs["json"]["maxNodes"] == 10
        assert "http://localhost:5102/api/preview" in call_args.args[0]

    async def test_connect_error_returns_none(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with _patch_http(mock_client):
            client = ContextStoreClient(base_url="http://localhost:5102")
            result = await client.preview("query")

        assert result is None

    async def test_timeout_returns_none(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with _patch_http(mock_client):
            client = ContextStoreClient(base_url="http://localhost:5102")
            result = await client.preview("query")

        assert result is None

    async def test_http_500_returns_none(self):
        mock_client = _make_http_mock(500)
        with _patch_http(mock_client):
            client = ContextStoreClient(base_url="http://localhost:5102")
            result = await client.preview("query")

        assert result is None

    async def test_http_404_returns_none(self):
        mock_client = _make_http_mock(404)
        with _patch_http(mock_client):
            client = ContextStoreClient(base_url="http://localhost:5102")
            result = await client.preview("query")

        assert result is None

    async def test_empty_list_response_yields_empty_nodes(self):
        mock_client = _make_http_mock(200, [])
        with _patch_http(mock_client):
            client = ContextStoreClient(base_url="http://localhost:5102")
            result = await client.preview("query")

        assert result is not None
        assert result.nodes == []

    async def test_non_dict_nodes_filtered_out(self):
        """Non-dict entries in the list must be silently dropped."""
        body = [
            {"type": "gotcha", "content": "real node"},
            "not a dict",
            42,
            None,
        ]
        mock_client = _make_http_mock(200, body)
        with _patch_http(mock_client):
            client = ContextStoreClient(base_url="http://localhost:5102")
            result = await client.preview("query")

        assert result is not None
        assert len(result.nodes) == 1


# ---------------------------------------------------------------------------
# Test suite 2: EnrichmentService + ContextStoreClient end-to-end
# ---------------------------------------------------------------------------

class TestEnrichmentServiceIntegration:
    """EnrichmentService wired to a real ContextStoreClient (httpx mocked)."""

    def _make_service(self, http_mock, max_tokens=2000, include_prior_outcomes=True):
        """Build an EnrichmentService with the real client, mocked config+http."""
        with patch(
            "backend.services.enrichment_service.cfg",
            side_effect=_enabled_cfg(max_tokens, include_prior_outcomes),
        ), patch(
            "backend.services.context_store_client.cfg",
            side_effect=_enabled_cfg(),
        ), _patch_http(http_mock):
            svc = EnrichmentService()
        return svc, http_mock

    async def test_successful_enrichment_produces_xml(self):
        nodes = [
            {"type": "gotcha", "title": "Deadlock risk", "content": "use async locks"},
            {"type": "convention", "title": "Naming", "content": "PascalCase classes"},
        ]
        http_mock = _make_http_mock(200, nodes)
        svc, _ = self._make_service(http_mock)

        with _patch_http(http_mock):
            result = await svc.enrich("implement session manager")

        assert result is not None
        assert "<relevant_gotchas>" in result.xml_block
        assert "Deadlock risk" in result.xml_block
        assert "<project_conventions>" in result.xml_block
        assert result.tokens_used > 0
        assert result.node_count == 2
        assert result.latency_ms >= 0

    async def test_disabled_config_returns_none_without_http_call(self):
        http_mock = _make_http_mock(200, [{"type": "gotcha", "content": "x"}])
        with patch(
            "backend.services.enrichment_service.cfg",
            side_effect=_disabled_cfg(),
        ), patch(
            "backend.services.context_store_client.cfg",
            side_effect=_disabled_cfg(),
        ), _patch_http(http_mock):
            svc = EnrichmentService()
            result = await svc.enrich("any query")

        assert result is None
        http_mock.post.assert_not_called()

    async def test_unreachable_store_returns_none(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        svc, _ = self._make_service(mock_client)
        with _patch_http(mock_client):
            result = await svc.enrich("query")

        assert result is None

    async def test_timeout_returns_none(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        svc, _ = self._make_service(mock_client)
        with _patch_http(mock_client):
            result = await svc.enrich("query")

        assert result is None

    async def test_empty_node_list_returns_none(self):
        http_mock = _make_http_mock(200, [])
        svc, _ = self._make_service(http_mock)

        with _patch_http(http_mock):
            result = await svc.enrich("query")

        assert result is None

    async def test_all_unknown_types_with_outcomes_disabled_returns_none(self):
        """Nodes of unknown type are dropped when include_prior_outcomes=False."""
        nodes = [{"type": "mystery_type", "content": "something"}]
        http_mock = _make_http_mock(200, nodes)
        svc, _ = self._make_service(http_mock, include_prior_outcomes=False)

        with _patch_http(http_mock):
            result = await svc.enrich("query")

        assert result is None

    async def test_token_budget_limits_output(self):
        """With a tiny budget, the XML block stays within bounds."""
        # Generate many nodes so without a budget they'd overflow
        nodes = [
            {"type": "gotcha", "title": f"Gotcha {i}", "content": "x" * 100}
            for i in range(20)
        ]
        http_mock = _make_http_mock(200, nodes)
        svc, _ = self._make_service(http_mock, max_tokens=50)  # ~200 chars

        with _patch_http(http_mock):
            result = await svc.enrich("query")

        if result is not None:
            # Token estimate should be near or under budget
            assert result.tokens_used <= 60  # small tolerance over estimate

    async def test_file_context_nodes_go_to_correct_section(self):
        nodes = [
            {"filePath": "src/auth.py", "title": "Auth module", "content": "JWT handling"},
            {"type": "gotcha", "content": "expire tokens promptly"},
        ]
        http_mock = _make_http_mock(200, nodes)
        svc, _ = self._make_service(http_mock)

        with _patch_http(http_mock):
            result = await svc.enrich("auth implementation")

        assert result is not None
        assert "<affected_file_context>" in result.xml_block
        assert "src/auth.py" in result.xml_block
        assert "<relevant_gotchas>" in result.xml_block


# ---------------------------------------------------------------------------
# Test suite 3: Task lifecycle enrichment path + usage_log metadata
# ---------------------------------------------------------------------------

class TestTaskLifecycleEnrichment:
    """Tests the enrichment block inside execute_task and its usage_log write."""

    async def _seed_task(self, db, task_id="task_enrich_001", project_id="proj_enrich_001"):
        """Insert a minimal task row with Ollama tier for quick mock."""
        now = time.time()
        await db.execute_write(
            "INSERT OR IGNORE INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, 'Enrich Test', 'test', 'draft', ?, ?)",
            (project_id, now, now),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
            (f"plan_{project_id}", project_id, now),
        )
        await db.execute_write(
            "INSERT INTO tasks "
            "(id, project_id, plan_id, title, description, task_type, priority, "
            "status, model_tier, wave, retry_count, max_retries, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Enrich Task', 'implement OAuth flow', 'code', 50, "
            "'pending', 'ollama', 0, 0, 5, ?, ?)",
            (task_id, project_id, f"plan_{project_id}", now, now),
        )
        return task_id, project_id

    def _make_progress(self):
        progress = AsyncMock()
        progress.push_event = AsyncMock()
        return progress

    def _make_budget(self):
        budget = AsyncMock()
        budget.release_reservation = AsyncMock()
        budget.release_reservation_project = AsyncMock()
        return budget

    async def _run_execute_task(self, db, task_id, project_id, enrichment_mock=None):
        """Call execute_task with Ollama tier + all non-enrichment things mocked."""
        from backend.services.task_lifecycle import execute_task

        task_row = await db.fetchone(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )

        ollama_result = {
            "output": "Done",
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "cost_usd": 0.0,
            "model_used": "qwen2.5-coder:14b",
        }

        semaphore = asyncio.Semaphore(1)
        dispatched = set()
        retry_after = {}
        progress = self._make_progress()
        budget = self._make_budget()

        # EnrichmentService is imported locally inside execute_task, so patch it
        # at the source module — the local `from ... import EnrichmentService`
        # resolves to this target at runtime.
        enrichment_patch = (
            patch(
                "backend.services.enrichment_service.EnrichmentService",
                return_value=enrichment_mock,
            )
            if enrichment_mock is not None
            else None
        )

        patches = [
            patch("backend.services.task_lifecycle.run_ollama_task",
                  new_callable=AsyncMock, return_value=ollama_result),
            patch("backend.services.task_lifecycle.VERIFICATION_ENABLED", False),
            patch("backend.services.task_lifecycle.KNOWLEDGE_EXTRACTION_ENABLED", False),
            patch("backend.services.task_lifecycle.CHECKPOINT_ON_RETRY_EXHAUSTED", False),
            patch("backend.services.task_lifecycle.DIAGNOSTIC_RAG_ENABLED", False),
            patch("backend.services.task_lifecycle.push_execution_outcome",
                  new_callable=AsyncMock),
        ]
        if enrichment_patch:
            patches.append(enrichment_patch)

        stack = [p.start() for p in patches]
        try:
            await execute_task(
                task_row=task_row,
                est_cost=0.0,
                db=db,
                budget=budget,
                progress=progress,
                tool_registry=MagicMock(),
                http_client=None,
                client=None,
                semaphore=semaphore,
                dispatched=dispatched,
                retry_after=retry_after,
            )
        finally:
            for p in patches:
                p.stop()

    async def test_enrichment_enabled_writes_nonzero_metadata(self, tmp_db):
        """When enrichment succeeds, usage_log gets populated token/node/latency cols."""
        from backend.services.enrichment_service import EnrichmentResult

        task_id, project_id = await self._seed_task(tmp_db)

        enrichment_svc = AsyncMock()
        enrichment_svc.enrich = AsyncMock(
            return_value=EnrichmentResult(
                xml_block="<relevant_gotchas>\nUse transactions\n</relevant_gotchas>",
                tokens_used=42,
                node_count=3,
                latency_ms=17.5,
            )
        )

        with patch("backend.services.task_lifecycle.CONTEXT_ENRICHMENT_ENABLED", True):
            await self._run_execute_task(tmp_db, task_id, project_id, enrichment_svc)

        row = await tmp_db.fetchone(
            "SELECT context_tokens_injected, source_node_count, enrichment_latency_ms "
            "FROM usage_log WHERE task_id = ? AND purpose = 'context_enrichment'",
            (task_id,),
        )
        assert row is not None
        assert row["context_tokens_injected"] == 42
        assert row["source_node_count"] == 3
        assert abs(row["enrichment_latency_ms"] - 17.5) < 0.1

    async def test_enrichment_enabled_injects_xml_into_description(self, tmp_db):
        """The XML block is appended to description before the agent call."""
        from backend.services.enrichment_service import EnrichmentResult

        task_id, project_id = await self._seed_task(tmp_db)

        xml_block = "<relevant_gotchas>\nExpire tokens promptly\n</relevant_gotchas>"
        enrichment_svc = AsyncMock()
        enrichment_svc.enrich = AsyncMock(
            return_value=EnrichmentResult(
                xml_block=xml_block,
                tokens_used=15,
                node_count=1,
                latency_ms=5.0,
            )
        )

        dispatched_descriptions = []

        async def capture_ollama(task_row, http_client, budget):
            dispatched_descriptions.append(task_row["description"])
            return {
                "output": "ok", "prompt_tokens": 0,
                "completion_tokens": 0, "cost_usd": 0.0,
                "model_used": "qwen2.5",
            }

        # Re-run without the helper so we can use capture_ollama directly
        task_row = await tmp_db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        semaphore = asyncio.Semaphore(1)
        from backend.services.task_lifecycle import execute_task

        with patch("backend.services.task_lifecycle.CONTEXT_ENRICHMENT_ENABLED", True), \
             patch("backend.services.task_lifecycle.run_ollama_task", side_effect=capture_ollama), \
             patch("backend.services.enrichment_service.EnrichmentService",
                   return_value=enrichment_svc), \
             patch("backend.services.task_lifecycle.VERIFICATION_ENABLED", False), \
             patch("backend.services.task_lifecycle.KNOWLEDGE_EXTRACTION_ENABLED", False), \
             patch("backend.services.task_lifecycle.CHECKPOINT_ON_RETRY_EXHAUSTED", False), \
             patch("backend.services.task_lifecycle.DIAGNOSTIC_RAG_ENABLED", False), \
             patch("backend.services.task_lifecycle.push_execution_outcome",
                   new_callable=AsyncMock):
            await execute_task(
                task_row=task_row,
                est_cost=0.0,
                db=tmp_db,
                budget=self._make_budget(),
                progress=self._make_progress(),
                tool_registry=MagicMock(),
                http_client=None,
                client=None,
                semaphore=semaphore,
                dispatched=set(),
                retry_after={},
            )

        assert len(dispatched_descriptions) == 1
        desc = dispatched_descriptions[0]
        assert "implement OAuth flow" in desc
        assert xml_block in desc
        assert desc.index("implement OAuth flow") < desc.index("<relevant_gotchas>")

    async def test_enrichment_disabled_writes_zero_metadata(self, tmp_db):
        """When CONTEXT_ENRICHMENT_ENABLED=False, usage_log row has all zeros."""
        task_id, project_id = await self._seed_task(tmp_db, "task_dis", "proj_dis")

        with patch("backend.services.task_lifecycle.CONTEXT_ENRICHMENT_ENABLED", False):
            await self._run_execute_task(tmp_db, task_id, project_id)

        row = await tmp_db.fetchone(
            "SELECT context_tokens_injected, source_node_count, enrichment_latency_ms "
            "FROM usage_log WHERE task_id = ? AND purpose = 'context_enrichment'",
            (task_id,),
        )
        assert row is not None
        assert row["context_tokens_injected"] == 0
        assert row["source_node_count"] == 0
        assert row["enrichment_latency_ms"] == 0.0

    async def test_store_unreachable_writes_zero_metadata(self, tmp_db):
        """When enrichment is enabled but store returns None, zeros are logged."""
        task_id, project_id = await self._seed_task(tmp_db, "task_degr", "proj_degr")

        enrichment_svc = AsyncMock()
        enrichment_svc.enrich = AsyncMock(return_value=None)

        with patch("backend.services.task_lifecycle.CONTEXT_ENRICHMENT_ENABLED", True):
            await self._run_execute_task(tmp_db, task_id, project_id, enrichment_svc)

        row = await tmp_db.fetchone(
            "SELECT context_tokens_injected, source_node_count, enrichment_latency_ms "
            "FROM usage_log WHERE task_id = ? AND purpose = 'context_enrichment'",
            (task_id,),
        )
        assert row is not None
        assert row["context_tokens_injected"] == 0
        assert row["source_node_count"] == 0
        assert row["enrichment_latency_ms"] == 0.0

    async def test_store_unreachable_description_unchanged(self, tmp_db):
        """Degraded enrichment leaves the task description unmodified."""
        task_id, project_id = await self._seed_task(tmp_db, "task_degr2", "proj_degr2")

        enrichment_svc = AsyncMock()
        enrichment_svc.enrich = AsyncMock(return_value=None)
        dispatched_descriptions = []

        async def capture_ollama(task_row, http_client, budget):
            dispatched_descriptions.append(task_row["description"])
            return {
                "output": "ok", "prompt_tokens": 0,
                "completion_tokens": 0, "cost_usd": 0.0,
                "model_used": "qwen2.5",
            }

        task_row = await tmp_db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        semaphore = asyncio.Semaphore(1)
        from backend.services.task_lifecycle import execute_task

        with patch("backend.services.task_lifecycle.CONTEXT_ENRICHMENT_ENABLED", True), \
             patch("backend.services.task_lifecycle.run_ollama_task", side_effect=capture_ollama), \
             patch("backend.services.enrichment_service.EnrichmentService",
                   return_value=enrichment_svc), \
             patch("backend.services.task_lifecycle.VERIFICATION_ENABLED", False), \
             patch("backend.services.task_lifecycle.KNOWLEDGE_EXTRACTION_ENABLED", False), \
             patch("backend.services.task_lifecycle.CHECKPOINT_ON_RETRY_EXHAUSTED", False), \
             patch("backend.services.task_lifecycle.DIAGNOSTIC_RAG_ENABLED", False), \
             patch("backend.services.task_lifecycle.push_execution_outcome",
                   new_callable=AsyncMock):
            await execute_task(
                task_row=task_row,
                est_cost=0.0,
                db=tmp_db,
                budget=self._make_budget(),
                progress=self._make_progress(),
                tool_registry=MagicMock(),
                http_client=None,
                client=None,
                semaphore=semaphore,
                dispatched=set(),
                retry_after={},
            )

        assert len(dispatched_descriptions) == 1
        # No XML tags injected
        assert "<" not in dispatched_descriptions[0]
        assert dispatched_descriptions[0] == "implement OAuth flow"

    async def test_usage_log_row_always_written(self, tmp_db):
        """A usage_log row with purpose='context_enrichment' is written on every dispatch."""
        task_id, project_id = await self._seed_task(tmp_db, "task_always", "proj_always")

        with patch("backend.services.task_lifecycle.CONTEXT_ENRICHMENT_ENABLED", False):
            await self._run_execute_task(tmp_db, task_id, project_id)

        rows = await tmp_db.fetchall(
            "SELECT * FROM usage_log WHERE task_id = ? AND purpose = 'context_enrichment'",
            (task_id,),
        )
        assert len(rows) == 1
        assert rows[0]["provider"] == "context_store"
        assert rows[0]["model"] == "preview"
        assert rows[0]["cost_usd"] == 0.0
