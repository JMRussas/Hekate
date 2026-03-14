#  Orchestration Engine - Telemetry Flow Integration Tests
#
#  End-to-end verification that task completion triggers the telemetry feedback
#  flow: execute_task / complete_task_external → push_execution_outcome →
#  context store HTTP calls with correct payloads.
#
#  These sit above the unit tests in test_telemetry_feedback.py. The unit tests
#  verify push_execution_outcome in isolation; these tests verify that
#  task_lifecycle.py correctly invokes it at the right lifecycle moments with
#  the right data.
#
#  Depends on: services/task_lifecycle.py, services/telemetry_feedback.py
#  Used by:    CI pipeline

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Module paths for patching
# ---------------------------------------------------------------------------

_TELEMETRY_MOD = "backend.services.telemetry_feedback"
_LIFECYCLE_MOD = "backend.services.task_lifecycle"

# Fake service URLs used in patches — no real services are contacted
_CS_URL = "http://cs-integ-test:5102"
_OLLAMA_URL = "http://ollama-integ-test:11434"
_EMBED_MODEL = "nomic-embed-text"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(data: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def _make_telemetry_client(
    *,
    node_id: str = "test-outcome-node",
    embedding: list[float] | None = None,
) -> tuple[AsyncMock, list, list, list, list]:
    """Build a mock AsyncClient that records all telemetry HTTP calls.

    Returns: (client, node_posts, embed_posts, edge_posts, put_calls)
    """
    node_posts: list[dict] = []
    embed_posts: list[dict] = []
    edge_posts: list[dict] = []
    put_calls: list[dict] = []

    async def _post(url, *, json, **kwargs):
        url_str = str(url)
        if "/children" in url_str:
            node_posts.append(json)
            return _ok_response({"id": node_id})
        elif "/api/embeddings" in url_str:
            embed_posts.append(json)
            return _ok_response({"embedding": embedding or [0.1, 0.2, 0.3]})
        elif "/api/graph/edges" in url_str:
            edge_posts.append(json)
            return _ok_response({})
        return _ok_response({})

    async def _put(url, *, json, **kwargs):
        put_calls.append({"url": str(url), "body": json})
        return _ok_response({})

    client = AsyncMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(side_effect=_post)
    client.put = AsyncMock(side_effect=_put)
    client.aclose = AsyncMock()
    return client, node_posts, embed_posts, edge_posts, put_calls


def _agent_result(
    *,
    output: str = "Task completed successfully.",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cost_usd: float = 0.0,
    model_used: str = "qwen2.5-coder:14b",
) -> dict:
    """Canned success result from a mocked agent runner."""
    return {
        "output": output,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": cost_usd,
        "model_used": model_used,
    }


def _telemetry_patches(embed_outcomes: bool = False):
    """patch.multiple that enables telemetry pointing at the fake CS URL."""
    return patch.multiple(
        _TELEMETRY_MOD,
        TELEMETRY_FEEDBACK_ENABLED=True,
        TELEMETRY_FEEDBACK_URL=_CS_URL,
        TELEMETRY_FEEDBACK_EMBED_OUTCOMES=embed_outcomes,
        OLLAMA_HOSTS={"local": _OLLAMA_URL},
        OLLAMA_EMBED_MODEL=_EMBED_MODEL,
    )


def _lifecycle_patches():
    """Disable optional lifecycle steps (enrichment, verification, knowledge)
    that would add noise or require additional mocking in these tests."""
    return patch.multiple(
        _LIFECYCLE_MOD,
        CONTEXT_ENRICHMENT_ENABLED=False,
        VERIFICATION_ENABLED=False,
        KNOWLEDGE_EXTRACTION_ENABLED=False,
        DIAGNOSTIC_RAG_ENABLED=False,
    )


def _make_budget() -> AsyncMock:
    budget = AsyncMock()
    budget.record_spend = AsyncMock()
    budget.release_reservation = AsyncMock()
    budget.release_reservation_project = AsyncMock()
    return budget


def _make_progress() -> AsyncMock:
    progress = AsyncMock()
    progress.push_event = AsyncMock()
    return progress


async def _seed_task(
    db,
    *,
    task_id: str = "task-telem-1",
    project_id: str = "proj-telem-1",
    model_tier: str = "ollama",
    task_type: str = "code",
):
    """Insert project → plan → task rows and return the task DB row."""
    now = time.time()
    await db.execute_write(
        "INSERT OR IGNORE INTO projects "
        "(id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, 'Telemetry Test', 'verify telemetry', 'active', ?, ?)",
        (project_id, now, now),
    )
    plan_id = f"plan-{project_id}"
    await db.execute_write(
        "INSERT OR IGNORE INTO plans "
        "(id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
        (plan_id, project_id, now),
    )
    await db.execute_write(
        "INSERT OR IGNORE INTO tasks "
        "(id, project_id, plan_id, title, description, task_type, priority, "
        "status, model_tier, wave, retry_count, max_retries, created_at, updated_at) "
        "VALUES (?, ?, ?, 'Telemetry Task', 'Build something useful', ?, "
        "50, 'pending', ?, 0, 0, 3, ?, ?)",
        (task_id, project_id, plan_id, task_type, model_tier, now, now),
    )
    return await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))


async def _run_task(task_row, db, *, http_client):
    """Invoke execute_task with a real DB and a controlled HTTP client."""
    from backend.services.task_lifecycle import execute_task

    await execute_task(
        task_row=task_row,
        db=db,
        budget=_make_budget(),
        progress=_make_progress(),
        tool_registry=MagicMock(),
        http_client=http_client,
        client=None,
        semaphore=asyncio.Semaphore(1),
        dispatched=set(),
        retry_after={},
    )


# ---------------------------------------------------------------------------
# TestSuccessfulTaskTelemetry
# ---------------------------------------------------------------------------


class TestSuccessfulTaskTelemetry:
    """execute_task success → outcome node created with status=completed."""

    @pytest.mark.asyncio
    async def test_outcome_node_created_on_success(self, tmp_db):
        """A POST to /api/node/{project_id}/children is made after success."""
        task_row = await _seed_task(tmp_db, task_id="t-success-1", project_id="p-success-1")
        client, node_posts, _, _, _ = _make_telemetry_client()

        with _telemetry_patches(), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result()
                await _run_task(task_row, tmp_db, http_client=client)

        assert len(node_posts) == 1
        node = node_posts[0]
        assert node["type"] == "execution_outcome"
        assert node["attributes"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_outcome_node_url_targets_correct_project(self, tmp_db):
        """Node POST URL uses /api/node/{project_id}/children."""
        project_id = "p-url-check"
        task_row = await _seed_task(tmp_db, task_id="t-url-1", project_id=project_id)
        captured_urls: list[str] = []

        async def _spy_post(url, **kwargs):
            captured_urls.append(str(url))
            return _ok_response({"id": "n1"})

        spy_client = AsyncMock(spec=httpx.AsyncClient)
        spy_client.post = AsyncMock(side_effect=_spy_post)
        spy_client.put = AsyncMock(return_value=_ok_response({}))
        spy_client.aclose = AsyncMock()

        with _telemetry_patches(), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result()
                await _run_task(task_row, tmp_db, http_client=spy_client)

        node_urls = [u for u in captured_urls if "/children" in u]
        assert len(node_urls) == 1
        assert f"/api/node/{project_id}/children" in node_urls[0]

    @pytest.mark.asyncio
    async def test_outcome_token_counts_match_agent_result(self, tmp_db):
        """prompt/completion_tokens in the node payload match the agent result."""
        task_row = await _seed_task(tmp_db, task_id="t-tokens-1")
        client, node_posts, _, _, _ = _make_telemetry_client()

        with _telemetry_patches(), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result(prompt_tokens=300, completion_tokens=150)
                await _run_task(task_row, tmp_db, http_client=client)

        attrs = node_posts[0]["attributes"]
        assert attrs["prompt_tokens"] == 300
        assert attrs["completion_tokens"] == 150
        assert attrs["total_tokens"] == 450

    @pytest.mark.asyncio
    async def test_outcome_attributes_include_provider_and_model(self, tmp_db):
        """provider (from tier) and model (from result) appear in node attributes."""
        task_row = await _seed_task(tmp_db, task_id="t-provider-1", model_tier="ollama")
        client, node_posts, _, _, _ = _make_telemetry_client()

        with _telemetry_patches(), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result(model_used="qwen2.5-coder:14b")
                await _run_task(task_row, tmp_db, http_client=client)

        attrs = node_posts[0]["attributes"]
        assert attrs["provider"] == "ollama"
        assert attrs["model"] == "qwen2.5-coder:14b"
        assert attrs["task_type"] == "code"

    @pytest.mark.asyncio
    async def test_task_db_status_is_completed(self, tmp_db):
        """Task row in DB is marked 'completed' regardless of telemetry activity."""
        task_id = "t-db-complete-1"
        task_row = await _seed_task(tmp_db, task_id=task_id)
        client, _, _, _, _ = _make_telemetry_client()

        with _telemetry_patches(), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result()
                await _run_task(task_row, tmp_db, http_client=client)

        row = await tmp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# TestFailedTaskTelemetry
# ---------------------------------------------------------------------------


class TestFailedTaskTelemetry:
    """execute_task failure → outcome node created with status=failed."""

    @pytest.mark.asyncio
    async def test_exception_sends_failed_telemetry(self, tmp_db):
        """Non-transient exception → POST to context store with status=failed."""
        task_row = await _seed_task(tmp_db, task_id="t-fail-1")
        client, node_posts, _, _, _ = _make_telemetry_client()

        with _telemetry_patches(), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.side_effect = RuntimeError("agent crashed unexpectedly")
                await _run_task(task_row, tmp_db, http_client=client)

        assert len(node_posts) == 1
        assert node_posts[0]["attributes"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_failed_task_db_status_is_failed(self, tmp_db):
        """Task DB row is FAILED even though telemetry was triggered."""
        task_id = "t-fail-db-1"
        task_row = await _seed_task(tmp_db, task_id=task_id)
        client, _, _, _, _ = _make_telemetry_client()

        with _telemetry_patches(), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.side_effect = ValueError("unexpected failure")
                await _run_task(task_row, tmp_db, http_client=client)

        row = await tmp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert row["status"] == "failed"

    @pytest.mark.asyncio
    async def test_failed_telemetry_includes_task_metadata(self, tmp_db):
        """Node payload for a failed task still includes task_id, task_type."""
        project_id = "p-fail-meta"
        task_id = "t-fail-meta-1"
        task_row = await _seed_task(
            tmp_db, task_id=task_id, project_id=project_id, task_type="research"
        )
        client, node_posts, _, _, _ = _make_telemetry_client()

        with _telemetry_patches(), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.side_effect = RuntimeError("boom")
                await _run_task(task_row, tmp_db, http_client=client)

        attrs = node_posts[0]["attributes"]
        assert attrs["task_id"] == task_id
        assert attrs["project_id"] == project_id
        assert attrs["task_type"] == "research"


# ---------------------------------------------------------------------------
# TestTelemetryEdgesAndEmbeddings
# ---------------------------------------------------------------------------


class TestTelemetryEdgesAndEmbeddings:
    """Graph edges and embeddings are sent as part of the successful task flow."""

    @pytest.mark.asyncio
    async def test_two_graph_edges_created_on_success(self, tmp_db):
        """Exactly two graph edge POSTs — executed_by and has_type."""
        task_row = await _seed_task(tmp_db, task_id="t-edges-1")
        client, _, _, edge_posts, _ = _make_telemetry_client()

        with _telemetry_patches(embed_outcomes=False), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result()
                await _run_task(task_row, tmp_db, http_client=client)

        assert len(edge_posts) == 2
        relations = {e["relation"] for e in edge_posts}
        assert relations == {"executed_by", "has_type"}

    @pytest.mark.asyncio
    async def test_edge_from_id_is_outcome_node_id(self, tmp_db):
        """Both edges use the outcome node's ID as from_id."""
        node_id = "outcome-edge-node"
        task_row = await _seed_task(tmp_db, task_id="t-edges-2")
        client, _, _, edge_posts, _ = _make_telemetry_client(node_id=node_id)

        with _telemetry_patches(embed_outcomes=False), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result()
                await _run_task(task_row, tmp_db, http_client=client)

        assert all(e["from_id"] == node_id for e in edge_posts)

    @pytest.mark.asyncio
    async def test_embedding_generated_and_stored_when_enabled(self, tmp_db):
        """With embed_outcomes=True: Ollama called and PUT stores the vector."""
        embedding_vec = [0.4, 0.5, 0.6]
        node_id = "embed-outcome-node"
        task_row = await _seed_task(tmp_db, task_id="t-embed-1")
        client, _, embed_posts, _, put_calls = _make_telemetry_client(
            node_id=node_id, embedding=embedding_vec
        )

        with _telemetry_patches(embed_outcomes=True), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result()
                await _run_task(task_row, tmp_db, http_client=client)

        # Embedding was generated via Ollama
        assert len(embed_posts) == 1
        assert embed_posts[0]["model"] == _EMBED_MODEL

        # Embedding was stored via PUT on the outcome node
        assert len(put_calls) == 1
        assert node_id in put_calls[0]["url"]
        assert put_calls[0]["body"]["embedding"] == embedding_vec

    @pytest.mark.asyncio
    async def test_no_embedding_calls_when_disabled(self, tmp_db):
        """With embed_outcomes=False: no Ollama call and no PUT."""
        task_row = await _seed_task(tmp_db, task_id="t-embed-off")
        client, _, embed_posts, _, put_calls = _make_telemetry_client()

        with _telemetry_patches(embed_outcomes=False), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result()
                await _run_task(task_row, tmp_db, http_client=client)

        assert len(embed_posts) == 0
        assert len(put_calls) == 0


# ---------------------------------------------------------------------------
# TestContextStoreOffline
# ---------------------------------------------------------------------------


class TestContextStoreOffline:
    """Task lifecycle is unaffected when the context store is unreachable."""

    @pytest.mark.asyncio
    async def test_task_completes_when_context_store_is_down(self, tmp_db):
        """ConnectError on the telemetry POST does not fail the task."""
        task_id = "t-cs-down-1"
        task_row = await _seed_task(tmp_db, task_id=task_id)

        offline_client = AsyncMock(spec=httpx.AsyncClient)
        offline_client.post = AsyncMock(side_effect=httpx.ConnectError("store offline"))
        offline_client.put = AsyncMock(side_effect=httpx.ConnectError("store offline"))
        offline_client.aclose = AsyncMock()

        with _telemetry_patches(), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result()
                # Must not raise even though context store is offline
                await _run_task(task_row, tmp_db, http_client=offline_client)

        row = await tmp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert row["status"] == "completed"

    @pytest.mark.asyncio
    async def test_failed_task_still_fails_when_context_store_is_down(self, tmp_db):
        """Context store being offline doesn't mask a genuine task failure."""
        task_id = "t-cs-down-fail"
        task_row = await _seed_task(tmp_db, task_id=task_id)

        offline_client = AsyncMock(spec=httpx.AsyncClient)
        offline_client.post = AsyncMock(side_effect=httpx.ConnectError("store offline"))
        offline_client.put = AsyncMock(side_effect=httpx.ConnectError("store offline"))
        offline_client.aclose = AsyncMock()

        with _telemetry_patches(), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.side_effect = RuntimeError("agent crashed")
                await _run_task(task_row, tmp_db, http_client=offline_client)

        row = await tmp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# TestTelemetryDisabledInLifecycle
# ---------------------------------------------------------------------------


class TestTelemetryDisabledInLifecycle:
    """With telemetry disabled, no context store HTTP calls are made."""

    @pytest.mark.asyncio
    async def test_no_context_store_calls_when_disabled(self, tmp_db):
        """TELEMETRY_FEEDBACK_ENABLED=False → zero POSTs to the context store URL."""
        task_row = await _seed_task(tmp_db, task_id="t-disabled-1")
        call_log: list[str] = []

        async def _spy_post(url, **kwargs):
            call_log.append(str(url))
            return _ok_response({})

        spy_client = AsyncMock(spec=httpx.AsyncClient)
        spy_client.post = AsyncMock(side_effect=_spy_post)
        spy_client.put = AsyncMock(return_value=_ok_response({}))
        spy_client.aclose = AsyncMock()

        with (
            patch(f"{_TELEMETRY_MOD}.TELEMETRY_FEEDBACK_ENABLED", False),
            patch(f"{_TELEMETRY_MOD}.TELEMETRY_FEEDBACK_URL", _CS_URL),
            _lifecycle_patches(),
        ):
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result()
                await _run_task(task_row, tmp_db, http_client=spy_client)

        cs_calls = [u for u in call_log if _CS_URL in u]
        assert cs_calls == []

    @pytest.mark.asyncio
    async def test_task_still_completes_when_telemetry_disabled(self, tmp_db):
        """Disabled telemetry doesn't break normal task completion."""
        task_id = "t-disabled-complete"
        task_row = await _seed_task(tmp_db, task_id=task_id)

        with patch(f"{_TELEMETRY_MOD}.TELEMETRY_FEEDBACK_ENABLED", False), _lifecycle_patches():
            with patch(f"{_LIFECYCLE_MOD}.run_ollama_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = _agent_result()
                await _run_task(task_row, tmp_db, http_client=AsyncMock(spec=httpx.AsyncClient))

        row = await tmp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# TestCompleteTaskExternalTelemetry
# ---------------------------------------------------------------------------


class TestCompleteTaskExternalTelemetry:
    """complete_task_external → push_execution_outcome is called correctly."""

    @pytest.mark.asyncio
    async def test_external_completion_triggers_telemetry(self, tmp_db):
        """push_execution_outcome is called after external task submission."""
        from backend.services.task_lifecycle import complete_task_external

        project_id = "p-ext-1"
        task_id = "t-ext-1"
        task_row = await _seed_task(tmp_db, task_id=task_id, project_id=project_id)

        captured_kwargs: dict = {}

        async def _spy_push(**kwargs):
            captured_kwargs.update(kwargs)
            return "ext-outcome-node"

        with patch(
            f"{_LIFECYCLE_MOD}.push_execution_outcome",
            new=AsyncMock(side_effect=_spy_push),
        ), patch(f"{_LIFECYCLE_MOD}.KNOWLEDGE_EXTRACTION_ENABLED", False):
            await complete_task_external(
                task_id=task_id,
                task_row=task_row,
                project_id=project_id,
                output_text="External agent completed the task",
                model_used="claude-sonnet-4-6",
                prompt_tokens=200,
                completion_tokens=100,
                db=tmp_db,
                budget=_make_budget(),
                progress=_make_progress(),
            )

        assert captured_kwargs["task_id"] == task_id
        assert captured_kwargs["project_id"] == project_id
        assert captured_kwargs["status"] == "completed"
        assert captured_kwargs["prompt_tokens"] == 200
        assert captured_kwargs["completion_tokens"] == 100
        assert captured_kwargs["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_external_provider_inferred_from_model_name(self, tmp_db):
        """Provider is inferred from model name (claude → anthropic, gemini → gemini)."""
        from backend.services.task_lifecycle import complete_task_external

        project_id = "p-ext-provider"
        task_id = "t-ext-provider"
        task_row = await _seed_task(tmp_db, task_id=task_id, project_id=project_id)

        captured_kwargs: dict = {}

        async def _spy_push(**kwargs):
            captured_kwargs.update(kwargs)
            return None

        with patch(
            f"{_LIFECYCLE_MOD}.push_execution_outcome",
            new=AsyncMock(side_effect=_spy_push),
        ), patch(f"{_LIFECYCLE_MOD}.KNOWLEDGE_EXTRACTION_ENABLED", False):
            await complete_task_external(
                task_id=task_id,
                task_row=task_row,
                project_id=project_id,
                output_text="Done",
                model_used="claude-haiku-4-5-20251001",
                prompt_tokens=50,
                completion_tokens=25,
                db=tmp_db,
                budget=_make_budget(),
                progress=_make_progress(),
            )

        assert captured_kwargs["provider"] == "anthropic"

    @pytest.mark.asyncio
    async def test_external_completion_task_db_is_completed(self, tmp_db):
        """Task DB row is COMPLETED after complete_task_external."""
        from backend.services.task_lifecycle import complete_task_external

        task_id = "t-ext-db"
        task_row = await _seed_task(tmp_db, task_id=task_id, project_id="p-ext-db")

        with patch(
            f"{_LIFECYCLE_MOD}.push_execution_outcome", new=AsyncMock(return_value=None)
        ), patch(f"{_LIFECYCLE_MOD}.KNOWLEDGE_EXTRACTION_ENABLED", False):
            await complete_task_external(
                task_id=task_id,
                task_row=task_row,
                project_id="p-ext-db",
                output_text="Done",
                model_used="gemini-pro",
                prompt_tokens=50,
                completion_tokens=25,
                db=tmp_db,
                budget=_make_budget(),
                progress=_make_progress(),
            )

        row = await tmp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert row["status"] == "completed"
