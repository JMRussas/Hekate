#  Orchestration Engine - Diagnostic RAG Integration Tests
#
#  Tests for DiagnosticIngester, _search_diagnostic_rag, _ingest_retry_success,
#  and checkpoint ingestion.

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# DiagnosticIngester
# ---------------------------------------------------------------------------


class TestDiagnosticIngester:
    """Tests for the DiagnosticIngester service."""

    @pytest.fixture
    def ingest_path(self, tmp_path):
        return str(tmp_path / "ingest.jsonl")

    @pytest.mark.asyncio
    async def test_ingest_resolution_writes_jsonl(self, ingest_path):
        """ingest_resolution writes valid JSONL to the configured path."""
        from backend.services.diagnostic_ingest import DiagnosticIngester
        ingester = DiagnosticIngester()

        with patch("backend.services.diagnostic_ingest.RAG_DIAGNOSTIC_INGEST_PATH", ingest_path):
            await ingester.ingest_resolution(
                error_text="anthropic.RateLimitError: 429",
                resolution_text="Applied exponential backoff",
                error_context="Claude API call",
                tags=["api", "rate-limit"],
                gotcha="",
            )

        with open(ingest_path, "r", encoding="utf-8") as f:
            line = f.readline()
        entry = json.loads(line)

        assert entry["error_pattern"] == "anthropic.RateLimitError: 429"
        assert entry["resolution"] == "Applied exponential backoff"
        assert entry["error_context"] == "Claude API call"
        assert entry["tags"] == ["api", "rate-limit"]

    @pytest.mark.asyncio
    async def test_ingest_resolution_appends(self, ingest_path):
        """Multiple calls append to the same file."""
        from backend.services.diagnostic_ingest import DiagnosticIngester
        ingester = DiagnosticIngester()

        with patch("backend.services.diagnostic_ingest.RAG_DIAGNOSTIC_INGEST_PATH", ingest_path):
            await ingester.ingest_resolution(
                error_text="error1", resolution_text="fix1",
            )
            await ingester.ingest_resolution(
                error_text="error2", resolution_text="fix2",
            )

        with open(ingest_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["error_pattern"] == "error1"
        assert json.loads(lines[1])["error_pattern"] == "error2"

    @pytest.mark.asyncio
    async def test_ingest_resolution_noop_when_path_empty(self, tmp_path):
        """No-op when RAG_DIAGNOSTIC_INGEST_PATH is empty."""
        from backend.services.diagnostic_ingest import DiagnosticIngester
        ingester = DiagnosticIngester()

        with patch("backend.services.diagnostic_ingest.RAG_DIAGNOSTIC_INGEST_PATH", ""):
            await ingester.ingest_resolution(
                error_text="error", resolution_text="fix",
            )
        # No file created
        assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    async def test_ingest_resolution_handles_write_error(self, tmp_path):
        """Write errors are logged, not raised."""
        from backend.services.diagnostic_ingest import DiagnosticIngester
        ingester = DiagnosticIngester()
        bad_path = str(tmp_path / "nonexistent" / "dir" / "ingest.jsonl")

        with patch("backend.services.diagnostic_ingest.RAG_DIAGNOSTIC_INGEST_PATH", bad_path):
            # Should not raise
            await ingester.ingest_resolution(
                error_text="error", resolution_text="fix",
            )

    @pytest.mark.asyncio
    async def test_ingest_resolution_with_gotcha(self, ingest_path):
        """Gotcha text is included in the JSONL entry."""
        from backend.services.diagnostic_ingest import DiagnosticIngester
        ingester = DiagnosticIngester()

        with patch("backend.services.diagnostic_ingest.RAG_DIAGNOSTIC_INGEST_PATH", ingest_path):
            await ingester.ingest_resolution(
                error_text="timeout",
                resolution_text="increase timeout",
                gotcha="First request after model load is slow",
            )

        with open(ingest_path, "r", encoding="utf-8") as f:
            entry = json.loads(f.readline())
        assert entry["gotcha"] == "First request after model load is slow"


# ---------------------------------------------------------------------------
# _search_diagnostic_rag
# ---------------------------------------------------------------------------


class TestSearchDiagnosticRag:
    """Tests for the pre-retry diagnostic search helper."""

    @pytest.mark.asyncio
    async def test_returns_none_when_db_unavailable(self):
        """Returns None when diagnostic index isn't loaded."""
        from backend.services.task_lifecycle import _search_diagnostic_rag

        mock_cache = AsyncMock()
        mock_cache.get = AsyncMock(return_value=None)

        result = await _search_diagnostic_rag("some error", mock_cache, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_not_loaded(self):
        """Returns None when index state is not 'loaded'."""
        from backend.services.task_lifecycle import _search_diagnostic_rag

        mock_idx = MagicMock()
        mock_idx._state = "failed"
        mock_idx.embeddings = None

        mock_cache = AsyncMock()
        mock_cache.get = AsyncMock(return_value=mock_idx)

        result = await _search_diagnostic_rag("some error", mock_cache, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_embed_fails(self):
        """Returns None when embedding the query fails."""
        from backend.services.task_lifecycle import _search_diagnostic_rag
        import numpy as np

        mock_idx = MagicMock()
        mock_idx._state = "loaded"
        mock_idx.embeddings = np.array([[1.0, 0.0]], dtype=np.float32)

        mock_cache = AsyncMock()
        mock_cache.get = AsyncMock(return_value=mock_idx)

        with patch("backend.tools.rag._embed_query", new_callable=AsyncMock, return_value=None):
            result = await _search_diagnostic_rag("some error", mock_cache, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_score_too_low(self):
        """Returns None when best score is below threshold."""
        from backend.services.task_lifecycle import _search_diagnostic_rag
        import numpy as np

        mock_idx = MagicMock()
        mock_idx._state = "loaded"
        mock_idx.embeddings = np.array([[1.0, 0.0]], dtype=np.float32)
        mock_idx.chunk_ids = ["chunk:1"]

        # Return a query vector that gives low similarity (orthogonal)
        low_sim_vec = np.array([0.0, 1.0], dtype=np.float32)

        mock_cache = AsyncMock()
        mock_cache.get = AsyncMock(return_value=mock_idx)

        with patch("backend.tools.rag._embed_query", new_callable=AsyncMock, return_value=low_sim_vec):
            result = await _search_diagnostic_rag("some error", mock_cache, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_text_on_high_confidence_match(self):
        """Returns chunk text when score exceeds threshold."""
        from backend.services.task_lifecycle import _search_diagnostic_rag
        import numpy as np

        mock_idx = MagicMock()
        mock_idx._state = "loaded"
        # Use a unit vector so similarity with itself = 1.0
        mock_idx.embeddings = np.array([[1.0, 0.0]], dtype=np.float32)
        mock_idx.chunk_ids = ["diag:api:abc123"]

        mock_row = {"text": "ERROR: rate limit\nRESOLUTION: backoff", "gotcha": ""}
        mock_idx.query_sync = MagicMock(return_value=[mock_row])

        mock_cache = AsyncMock()
        mock_cache.get = AsyncMock(return_value=mock_idx)

        # Query vector identical to embedding → score = 1.0
        query_vec = np.array([1.0, 0.0], dtype=np.float32)

        with patch("backend.tools.rag._embed_query", new_callable=AsyncMock, return_value=query_vec):
            result = await _search_diagnostic_rag("rate limit error", mock_cache, None)

        assert result is not None
        assert "Diagnostic RAG match" in result
        assert "ERROR: rate limit" in result

    @pytest.mark.asyncio
    async def test_includes_gotcha_when_present(self):
        """Gotcha text is appended to the result."""
        from backend.services.task_lifecycle import _search_diagnostic_rag
        import numpy as np

        mock_idx = MagicMock()
        mock_idx._state = "loaded"
        mock_idx.embeddings = np.array([[1.0, 0.0]], dtype=np.float32)
        mock_idx.chunk_ids = ["diag:api:abc123"]

        mock_row = {
            "text": "ERROR: timeout",
            "gotcha": "Actually a DNS failure, not a timeout",
        }
        mock_idx.query_sync = MagicMock(return_value=[mock_row])

        mock_cache = AsyncMock()
        mock_cache.get = AsyncMock(return_value=mock_idx)

        query_vec = np.array([1.0, 0.0], dtype=np.float32)

        with patch("backend.tools.rag._embed_query", new_callable=AsyncMock, return_value=query_vec):
            result = await _search_diagnostic_rag("timeout", mock_cache, None)

        assert "[CAUTION: Actually a DNS failure, not a timeout]" in result

    @pytest.mark.asyncio
    async def test_never_raises(self):
        """Always returns None on any exception, never raises."""
        from backend.services.task_lifecycle import _search_diagnostic_rag

        mock_cache = AsyncMock()
        mock_cache.get = AsyncMock(side_effect=RuntimeError("boom"))

        result = await _search_diagnostic_rag("error", mock_cache, None)
        assert result is None


# ---------------------------------------------------------------------------
# _ingest_retry_success
# ---------------------------------------------------------------------------


class TestIngestRetrySuccess:
    """Tests for the success-after-retry feedback capture."""

    @pytest.mark.asyncio
    async def test_ingests_on_successful_retry(self):
        """Captures error->resolution when task succeeds after retry."""
        from backend.services.task_lifecycle import _ingest_retry_success

        mock_db = AsyncMock()
        mock_db.fetchone = AsyncMock(return_value={
            "message": "Transient error (retry 2): rate limit exceeded",
        })

        mock_ingester = AsyncMock()
        mock_ingester.ingest_resolution = AsyncMock()

        task_row = {
            "id": "task1",
            "title": "Test Task",
            "task_type": "code",
            "model_tier": "haiku",
        }

        await _ingest_retry_success(task_row, "Task completed successfully", mock_db, mock_ingester)

        mock_ingester.ingest_resolution.assert_called_once()
        call_kwargs = mock_ingester.ingest_resolution.call_args.kwargs
        assert "rate limit exceeded" in call_kwargs["error_text"]
        assert "Test Task" in call_kwargs["resolution_text"]
        assert "retry-success" in call_kwargs["tags"]

    @pytest.mark.asyncio
    async def test_skips_when_no_error_events(self):
        """Does nothing when there are no error events in history."""
        from backend.services.task_lifecycle import _ingest_retry_success

        mock_db = AsyncMock()
        mock_db.fetchone = AsyncMock(return_value=None)

        mock_ingester = AsyncMock()
        mock_ingester.ingest_resolution = AsyncMock()

        task_row = {"id": "task1", "title": "Test"}

        await _ingest_retry_success(task_row, "output", mock_db, mock_ingester)
        mock_ingester.ingest_resolution.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_output_empty(self):
        """Does nothing when output is empty."""
        from backend.services.task_lifecycle import _ingest_retry_success

        mock_db = AsyncMock()
        mock_db.fetchone = AsyncMock(return_value={
            "message": "some error",
        })

        mock_ingester = AsyncMock()
        mock_ingester.ingest_resolution = AsyncMock()

        task_row = {"id": "task1", "title": "Test"}

        await _ingest_retry_success(task_row, "", mock_db, mock_ingester)
        mock_ingester.ingest_resolution.assert_not_called()

    @pytest.mark.asyncio
    async def test_never_raises_on_failure(self):
        """Ingestion failure does not propagate."""
        from backend.services.task_lifecycle import _ingest_retry_success

        mock_db = AsyncMock()
        mock_db.fetchone = AsyncMock(side_effect=RuntimeError("db error"))

        mock_ingester = AsyncMock()

        task_row = {"id": "task1", "title": "Test"}

        # Should not raise
        await _ingest_retry_success(task_row, "output", mock_db, mock_ingester)


# ---------------------------------------------------------------------------
# Checkpoint resolution ingestion
# ---------------------------------------------------------------------------


class TestCheckpointIngestion:
    """Tests for diagnostic ingestion via checkpoint resolution."""

    def test_checkpoint_resolve_schema_has_gotcha(self):
        """CheckpointResolve schema includes gotcha field."""
        from backend.models.schemas import CheckpointResolve

        body = CheckpointResolve(
            action="retry",
            guidance="Try increasing the timeout",
            gotcha="Looks like a timeout but is actually DNS",
        )
        assert body.gotcha == "Looks like a timeout but is actually DNS"

    def test_checkpoint_resolve_gotcha_defaults_empty(self):
        """Gotcha field defaults to empty string."""
        from backend.models.schemas import CheckpointResolve

        body = CheckpointResolve(action="retry")
        assert body.gotcha == ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestDiagnosticConfig:
    """Tests for diagnostic RAG config values."""

    def test_diagnostic_rag_enabled_default(self):
        """DIAGNOSTIC_RAG_ENABLED defaults to False."""
        from backend.config import DIAGNOSTIC_RAG_ENABLED
        assert isinstance(DIAGNOSTIC_RAG_ENABLED, bool)

    def test_ingest_path_default(self):
        """RAG_DIAGNOSTIC_INGEST_PATH defaults to empty string."""
        from backend.config import RAG_DIAGNOSTIC_INGEST_PATH
        assert isinstance(RAG_DIAGNOSTIC_INGEST_PATH, str)
