#  Orchestration Engine - Telemetry Feedback Unit Tests
#
#  Tests for backend/services/telemetry_feedback.py
#  Covers: node payload construction, embedding integration, graph edge
#  linking, and graceful degradation when dependencies are unavailable.
#
#  Depends on: backend/services/telemetry_feedback.py
#  Used by:    CI pipeline

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_CS_URL = "http://localhost:5102"
_OLLAMA_URL = "http://localhost:11434"
_EMBED_MODEL = "nomic-embed-text"

# Patch targets — all constants live in the telemetry module's namespace
_MOD = "backend.services.telemetry_feedback"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(data: dict) -> AsyncMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def _error_response(status: int) -> AsyncMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"HTTP {status}", request=MagicMock(), response=resp
    )
    return resp


def _make_client(*, post_responses: dict | None = None, put_ok: bool = True) -> AsyncMock:
    """Build a mock AsyncClient.

    ``post_responses`` maps URL substrings to either a response object or an
    exception to raise.  Unmapped URLs get a 200 OK with an empty body.
    """
    client = AsyncMock(spec=httpx.AsyncClient)
    client.aclose = AsyncMock()

    async def _post(url, **kwargs):
        for key, value in (post_responses or {}).items():
            if key in str(url):
                if isinstance(value, BaseException):
                    raise value
                return value
        return _ok_response({})

    client.post = AsyncMock(side_effect=_post)

    if put_ok:
        client.put = AsyncMock(return_value=_ok_response({}))
    else:
        client.put = AsyncMock(side_effect=httpx.ConnectError("store down"))

    return client


def _default_kwargs(**overrides) -> dict:
    """Return a complete set of push_execution_outcome kwargs."""
    base = dict(
        task_id="task-abc",
        project_id="proj-xyz",
        task_title="Build the widget",
        task_description="Implement a new widget component",
        task_type="code",
        provider="claude_code",
        model="claude-sonnet-4-6",
        prompt_tokens=500,
        completion_tokens=200,
        duration_seconds=12.5,
        status="completed",
        verification_outcome="passed",
        complexity="medium",
    )
    base.update(overrides)
    return base


def _enabled_patches(embed_outcomes: bool = True):
    """Return a list of patch objects for the three telemetry config constants."""
    return [
        patch(f"{_MOD}.TELEMETRY_FEEDBACK_ENABLED", True),
        patch(f"{_MOD}.TELEMETRY_FEEDBACK_URL", _CS_URL),
        patch(f"{_MOD}.TELEMETRY_FEEDBACK_EMBED_OUTCOMES", embed_outcomes),
        patch(f"{_MOD}.OLLAMA_HOSTS", {"local": _OLLAMA_URL}),
        patch(f"{_MOD}.OLLAMA_EMBED_MODEL", _EMBED_MODEL),
    ]


# ---------------------------------------------------------------------------
# TestTelemetryDisabled
# ---------------------------------------------------------------------------


class TestTelemetryDisabled:
    """push_execution_outcome is a no-op when telemetry is disabled."""

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        from backend.services.telemetry_feedback import push_execution_outcome

        client = AsyncMock(spec=httpx.AsyncClient)
        with patch(f"{_MOD}.TELEMETRY_FEEDBACK_ENABLED", False):
            result = await push_execution_outcome(
                **_default_kwargs(), http_client=client
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_no_http_calls_when_disabled(self):
        from backend.services.telemetry_feedback import push_execution_outcome

        client = AsyncMock(spec=httpx.AsyncClient)
        with patch(f"{_MOD}.TELEMETRY_FEEDBACK_ENABLED", False):
            await push_execution_outcome(**_default_kwargs(), http_client=client)

        client.post.assert_not_called()
        client.put.assert_not_called()


# ---------------------------------------------------------------------------
# TestOutcomeNodeCreation
# ---------------------------------------------------------------------------


class TestOutcomeNodeCreation:
    """Verify the POST /api/node/{project_id}/children payload and response handling."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_node_id(self):
        from backend.services.telemetry_feedback import push_execution_outcome

        node_resp = _ok_response({"id": "returned-id-123"})
        embed_resp = _ok_response({"embedding": [0.1, 0.2, 0.3]})
        client = _make_client(post_responses={
            "/api/node/": node_resp,
            "/api/embeddings": embed_resp,
        })

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            result = await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert result == "returned-id-123"

    @pytest.mark.asyncio
    async def test_node_id_fallback_to_generated(self):
        """If response body has no 'id', use the locally-generated UUID fragment."""
        from backend.services.telemetry_feedback import push_execution_outcome

        node_resp = _ok_response({})  # no "id" key
        client = _make_client(post_responses={"/api/node/": node_resp})

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            result = await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert result is not None
        assert isinstance(result, str)
        assert len(result) == 16  # uuid4().hex[:16]

    @pytest.mark.asyncio
    async def test_payload_all_required_fields_present(self):
        """The POST body to /api/node/{project_id}/children has all required fields."""
        from backend.services.telemetry_feedback import push_execution_outcome

        captured_payload = {}

        async def _capture_post(url, *, json, **kwargs):
            if "/api/node/" in url and "/children" in url:
                captured_payload.update(json)
            return _ok_response({"id": "node-1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_capture_post)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        kwargs = _default_kwargs(
            task_id="t1",
            project_id="p1",
            task_title="My Task",
            task_type="code",
            provider="gemini_cli",
            model="gemini-pro",
            prompt_tokens=100,
            completion_tokens=50,
            duration_seconds=5.0,
            status="completed",
            verification_outcome="passed",
            complexity="low",
        )

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(**kwargs, http_client=client)

        attrs = captured_payload["attributes"]
        assert captured_payload["type"] == "execution_outcome"
        assert attrs["task_id"] == "t1"
        assert attrs["project_id"] == "p1"
        assert attrs["task_type"] == "code"
        assert attrs["provider"] == "gemini_cli"
        assert attrs["model"] == "gemini-pro"
        assert attrs["prompt_tokens"] == 100
        assert attrs["completion_tokens"] == 50
        assert attrs["total_tokens"] == 150
        assert attrs["duration_seconds"] == 5.0
        assert attrs["status"] == "completed"
        assert attrs["verification_outcome"] == "passed"
        assert attrs["complexity"] == "low"
        assert "recorded_at" in attrs

    @pytest.mark.asyncio
    async def test_label_truncated_to_80_chars(self):
        """Label is 'Outcome: {title[:80]}' — long titles are truncated."""
        from backend.services.telemetry_feedback import push_execution_outcome

        long_title = "X" * 120
        captured_label = {}

        async def _capture(url, *, json, **kwargs):
            if "/children" in url:
                captured_label["label"] = json["label"]
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_capture)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(
                **_default_kwargs(task_title=long_title), http_client=client
            )

        label = captured_label["label"]
        assert label == f"Outcome: {'X' * 80}"
        assert len(label) == len("Outcome: ") + 80

    @pytest.mark.asyncio
    async def test_verification_outcome_none_stored_as_none_string(self):
        """None verification_outcome is stored as 'none' string in attributes."""
        from backend.services.telemetry_feedback import push_execution_outcome

        captured_attrs = {}

        async def _capture(url, *, json, **kwargs):
            if "/children" in url:
                captured_attrs.update(json["attributes"])
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_capture)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(
                **_default_kwargs(verification_outcome=None), http_client=client
            )

        assert captured_attrs["verification_outcome"] == "none"

    @pytest.mark.asyncio
    async def test_complexity_none_stored_as_unknown(self):
        """None complexity is stored as 'unknown' string in attributes."""
        from backend.services.telemetry_feedback import push_execution_outcome

        captured_attrs = {}

        async def _capture(url, *, json, **kwargs):
            if "/children" in url:
                captured_attrs.update(json["attributes"])
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_capture)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(
                **_default_kwargs(complexity=None), http_client=client
            )

        assert captured_attrs["complexity"] == "unknown"

    @pytest.mark.asyncio
    async def test_node_url_includes_project_id(self):
        """POST URL is {base_url}/api/node/{project_id}/children."""
        from backend.services.telemetry_feedback import push_execution_outcome

        captured_url = {}

        async def _capture(url, **kwargs):
            if "/children" in str(url):
                captured_url["url"] = str(url)
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_capture)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(
                **_default_kwargs(project_id="proj-42"), http_client=client
            )

        assert captured_url["url"] == f"{_CS_URL}/api/node/proj-42/children"

    @pytest.mark.asyncio
    async def test_connect_error_returns_none(self):
        """ConnectError from context store → returns None, no exception raised."""
        from backend.services.telemetry_feedback import push_execution_outcome

        client = _make_client(post_responses={
            "/api/node/": httpx.ConnectError("store offline"),
        })

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            result = await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """TimeoutException from context store → returns None."""
        from backend.services.telemetry_feedback import push_execution_outcome

        client = _make_client(post_responses={
            "/api/node/": httpx.TimeoutException("timed out"),
        })

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            result = await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert result is None

    @pytest.mark.asyncio
    async def test_http_status_error_returns_none(self):
        """HTTP 500 from context store → returns None."""
        from backend.services.telemetry_feedback import push_execution_outcome

        client = _make_client(post_responses={
            "/api/node/": _error_response(500),
        })

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            result = await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert result is None


# ---------------------------------------------------------------------------
# TestEmbeddingIntegration
# ---------------------------------------------------------------------------


class TestEmbeddingIntegration:
    """Verify Ollama embedding generation and storage."""

    @pytest.mark.asyncio
    async def test_embedding_skipped_when_disabled(self):
        """When embed_outcomes is False, no Ollama or PUT calls are made."""
        from backend.services.telemetry_feedback import push_execution_outcome

        node_resp = _ok_response({"id": "n1"})
        client = _make_client(post_responses={"/api/node/": node_resp})

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(**_default_kwargs(), http_client=client)

        # Only the node POST call; no Ollama call
        post_urls = [str(c.args[0]) for c in client.post.call_args_list]
        assert not any("/api/embeddings" in u for u in post_urls)
        client.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_embedding_prompt_contains_required_fields(self):
        """Ollama is called with a prompt containing task_type, provider, model, status."""
        from backend.services.telemetry_feedback import push_execution_outcome

        captured_prompt = {}

        async def _capture(url, *, json, **kwargs):
            if "/api/embeddings" in str(url):
                captured_prompt["prompt"] = json["prompt"]
                captured_prompt["model"] = json["model"]
                return _ok_response({"embedding": [0.1, 0.2]})
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_capture)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        kwargs = _default_kwargs(
            task_type="code",
            provider="claude_code",
            model="claude-sonnet-4-6",
            status="completed",
            verification_outcome="passed",
            task_description="Build a thing",
        )

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=True,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(**kwargs, http_client=client)

        prompt = captured_prompt["prompt"]
        assert "code" in prompt
        assert "claude_code" in prompt
        assert "claude-sonnet-4-6" in prompt
        assert "completed" in prompt
        assert "passed" in prompt
        assert "Build a thing" in prompt
        assert captured_prompt["model"] == _EMBED_MODEL

    @pytest.mark.asyncio
    async def test_embedding_description_truncated_to_500(self):
        """Task description is truncated to 500 chars in the embedding prompt."""
        from backend.services.telemetry_feedback import push_execution_outcome

        long_description = "D" * 800
        captured_prompt = {}

        async def _capture(url, *, json, **kwargs):
            if "/api/embeddings" in str(url):
                captured_prompt["prompt"] = json["prompt"]
                return _ok_response({"embedding": [0.1]})
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_capture)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=True,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(
                **_default_kwargs(task_description=long_description),
                http_client=client,
            )

        prompt = captured_prompt["prompt"]
        # The full 800-char description must not appear — only the first 500 chars
        assert "D" * 501 not in prompt
        assert "D" * 500 in prompt

    @pytest.mark.asyncio
    async def test_embedding_stored_via_put(self):
        """Embedding vector is stored via PUT /api/node/{node_id}/attributes."""
        from backend.services.telemetry_feedback import push_execution_outcome

        embedding_vec = [0.1, 0.2, 0.3, 0.4]
        captured_put = {}

        async def _post(url, *, json, **kwargs):
            if "/api/embeddings" in str(url):
                return _ok_response({"embedding": embedding_vec})
            return _ok_response({"id": "node-xyz"})

        async def _put(url, *, json, **kwargs):
            captured_put["url"] = str(url)
            captured_put["body"] = json
            return _ok_response({})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_post)
        client.put = AsyncMock(side_effect=_put)
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=True,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert captured_put["url"] == f"{_CS_URL}/api/node/node-xyz/attributes"
        assert captured_put["body"]["embedding"] == embedding_vec

    @pytest.mark.asyncio
    async def test_embedding_failure_does_not_prevent_node_id_return(self):
        """Ollama failure is swallowed — outcome node ID is still returned."""
        from backend.services.telemetry_feedback import push_execution_outcome

        async def _post(url, *, json, **kwargs):
            if "/api/embeddings" in str(url):
                raise RuntimeError("ollama is down")
            return _ok_response({"id": "n-ok"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_post)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=True,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            result = await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert result == "n-ok"
        # PUT should not be called — no embedding to store
        client.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_embedding_list_not_stored(self):
        """If Ollama returns an empty embedding list, no PUT is made."""
        from backend.services.telemetry_feedback import push_execution_outcome

        async def _post(url, *, json, **kwargs):
            if "/api/embeddings" in str(url):
                return _ok_response({"embedding": []})
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_post)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=True,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(**_default_kwargs(), http_client=client)

        client.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_ollama_host_from_config(self):
        """Embedding POST goes to the first Ollama host in OLLAMA_HOSTS."""
        from backend.services.telemetry_feedback import push_execution_outcome

        custom_ollama = "http://192.168.1.164:11434"
        captured_url = {}

        async def _post(url, *, json, **kwargs):
            if "/api/embeddings" in str(url):
                captured_url["url"] = str(url)
                return _ok_response({"embedding": [0.5]})
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_post)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=True,
            OLLAMA_HOSTS={"server": custom_ollama},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert captured_url["url"] == f"{custom_ollama}/api/embeddings"


# ---------------------------------------------------------------------------
# TestGraphEdgeLinking
# ---------------------------------------------------------------------------


class TestGraphEdgeLinking:
    """Verify graph edge creation for provider and task_type nodes."""

    @pytest.mark.asyncio
    async def test_two_edges_created(self):
        """Exactly two edge POST calls are made to /api/graph/edges."""
        from backend.services.telemetry_feedback import push_execution_outcome

        edge_posts = []

        async def _post(url, *, json, **kwargs):
            if "/api/graph/edges" in str(url):
                edge_posts.append(json)
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_post)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert len(edge_posts) == 2

    @pytest.mark.asyncio
    async def test_provider_edge_structure(self):
        """Provider edge: from_id=outcome_id, to_id='provider:{provider}', relation='executed_by'."""
        from backend.services.telemetry_feedback import push_execution_outcome

        edge_posts = []

        async def _post(url, *, json, **kwargs):
            if "/api/graph/edges" in str(url):
                edge_posts.append(json)
            return _ok_response({"id": "outcome-99"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_post)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(
                **_default_kwargs(provider="gemini_cli", project_id="proj-1"),
                http_client=client,
            )

        provider_edge = next(e for e in edge_posts if e["relation"] == "executed_by")
        assert provider_edge["from_id"] == "outcome-99"
        assert provider_edge["to_id"] == "provider:gemini_cli"
        assert provider_edge["attributes"]["project_id"] == "proj-1"

    @pytest.mark.asyncio
    async def test_task_type_edge_structure(self):
        """Task type edge: from_id=outcome_id, to_id='task_type:{task_type}', relation='has_type'."""
        from backend.services.telemetry_feedback import push_execution_outcome

        edge_posts = []

        async def _post(url, *, json, **kwargs):
            if "/api/graph/edges" in str(url):
                edge_posts.append(json)
            return _ok_response({"id": "outcome-77"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_post)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(
                **_default_kwargs(task_type="game_code", project_id="proj-2"),
                http_client=client,
            )

        type_edge = next(e for e in edge_posts if e["relation"] == "has_type")
        assert type_edge["from_id"] == "outcome-77"
        assert type_edge["to_id"] == "task_type:game_code"
        assert type_edge["attributes"]["project_id"] == "proj-2"

    @pytest.mark.asyncio
    async def test_edge_failure_does_not_prevent_node_id_return(self):
        """Graph edge errors are swallowed — outcome node ID is still returned."""
        from backend.services.telemetry_feedback import push_execution_outcome

        async def _post(url, *, json, **kwargs):
            if "/api/graph/edges" in str(url):
                raise httpx.ConnectError("graph store down")
            return _ok_response({"id": "n-safe"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_post)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            result = await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert result == "n-safe"

    @pytest.mark.asyncio
    async def test_edge_url_uses_configured_base(self):
        """Edge POST URL is {TELEMETRY_FEEDBACK_URL}/api/graph/edges."""
        from backend.services.telemetry_feedback import push_execution_outcome

        edge_urls = []

        async def _post(url, *, json, **kwargs):
            if "/api/graph/edges" in str(url):
                edge_urls.append(str(url))
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_post)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        custom_url = "http://custom-store:5200"
        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=custom_url,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert all(u == f"{custom_url}/api/graph/edges" for u in edge_urls)


# ---------------------------------------------------------------------------
# TestGracefulDegradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """push_execution_outcome never raises — all failures return None."""

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_none(self):
        """Unexpected exception inside _push_outcome is swallowed."""
        from backend.services.telemetry_feedback import push_execution_outcome

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=RuntimeError("unexpected boom"))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            result = await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert result is None

    @pytest.mark.asyncio
    async def test_context_store_offline_full_path_returns_none(self):
        """All HTTP calls fail (ConnectError) — result is None, no exception."""
        from backend.services.telemetry_feedback import push_execution_outcome

        client = _make_client(post_responses={
            "": httpx.ConnectError("all offline"),
        })

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=True,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            result = await push_execution_outcome(**_default_kwargs(), http_client=client)

        assert result is None

    @pytest.mark.asyncio
    async def test_own_client_closed_on_exit(self):
        """When no http_client is injected, the internally-created client is closed."""
        from backend.services.telemetry_feedback import push_execution_outcome

        mock_client_instance = AsyncMock(spec=httpx.AsyncClient)
        mock_client_instance.post = AsyncMock(
            return_value=_ok_response({"id": "n1"})
        )
        mock_client_instance.put = AsyncMock(return_value=_ok_response({}))
        mock_client_instance.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            with patch.multiple(_MOD,
                TELEMETRY_FEEDBACK_ENABLED=True,
                TELEMETRY_FEEDBACK_URL=_CS_URL,
                TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
                OLLAMA_HOSTS={"local": _OLLAMA_URL},
                OLLAMA_EMBED_MODEL=_EMBED_MODEL,
            ):
                # No http_client kwarg — service creates its own
                await push_execution_outcome(**_default_kwargs())

        mock_client_instance.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_injected_client_not_closed(self):
        """When http_client is injected, the caller owns it — aclose is NOT called."""
        from backend.services.telemetry_feedback import push_execution_outcome

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_ok_response({"id": "n1"}))
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(**_default_kwargs(), http_client=client)

        client.aclose.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_task_status_stored_correctly(self):
        """Status 'failed' flows through to the node attributes unchanged."""
        from backend.services.telemetry_feedback import push_execution_outcome

        captured_attrs = {}

        async def _capture(url, *, json, **kwargs):
            if "/children" in url:
                captured_attrs.update(json["attributes"])
            return _ok_response({"id": "n1"})

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=_capture)
        client.put = AsyncMock(return_value=_ok_response({}))
        client.aclose = AsyncMock()

        with patch.multiple(_MOD,
            TELEMETRY_FEEDBACK_ENABLED=True,
            TELEMETRY_FEEDBACK_URL=_CS_URL,
            TELEMETRY_FEEDBACK_EMBED_OUTCOMES=False,
            OLLAMA_HOSTS={"local": _OLLAMA_URL},
            OLLAMA_EMBED_MODEL=_EMBED_MODEL,
        ):
            await push_execution_outcome(
                **_default_kwargs(status="failed", verification_outcome=None),
                http_client=client,
            )

        assert captured_attrs["status"] == "failed"
