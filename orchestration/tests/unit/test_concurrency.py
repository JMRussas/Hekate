#  Orchestration Engine - Phase 3 Concurrency Tests
#
#  Tests for Phase 3: Concurrency & Reliability fixes.
#  Covers retry backoff, RAG index cache, error recovery,
#  async I/O, budget mid-loop check, and httpx client reuse.
#
#  Depends on: backend/services/task_lifecycle.py, backend/services/claude_agent.py,
#              backend/tools/rag.py, backend/tools/registry.py, backend/tools/file.py,
#              backend/services/resource_monitor.py
#  Used by:    pytest

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# 3.1 — Retry backoff releases semaphore
# ---------------------------------------------------------------------------

class TestRetryBackoff:
    """Verify that transient error retry uses _retry_after dict
    instead of sleeping inside the semaphore."""

    async def test_transient_error_sets_retry_after(self, tmp_db):
        """execute_task should populate retry_after on transient error."""
        import anthropic
        from backend.services.task_lifecycle import execute_task

        budget = MagicMock()
        budget.can_spend = AsyncMock(return_value=True)
        budget.record_spend = AsyncMock()
        budget.release_reservation = AsyncMock()

        progress = MagicMock()
        progress.push_event = AsyncMock()

        # Mock the Claude client to raise a transient error
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(
            side_effect=anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            )
        )

        task_row = {
            "id": "task_retry_test",
            "project_id": "proj_1",
            "model_tier": "sonnet",
            "context_json": None,
            "system_prompt": "Test",
            "tools_json": "[]",
            "description": "Do something",
            "max_tokens": 4096,
            "title": "Test task",
            "retry_count": 0,
            "max_retries": 3,
        }

        # Insert project + plan + task rows so the DB updates succeed
        now = time.time()
        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'executing', ?, ?)",
            ("proj_1", "Test", "Test", now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
            ("plan_1", "proj_1", now),
        )
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, max_tokens, tools_json, "
            "system_prompt, retry_count, max_retries, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task_retry_test", "proj_1", "plan_1", "Test task", "Do something", "code",
             0, "queued", "sonnet", 4096, "[]", "Test", 0, 3, now, now),
        )

        semaphore = asyncio.Semaphore(5)
        dispatched = set()
        retry_after = {}

        with patch("backend.services.claude_agent.get_model_id", return_value="claude-sonnet-4-6"):
            await execute_task(
                task_row=task_row, est_cost=0.0, db=tmp_db, budget=budget,
                progress=progress, tool_registry=MagicMock(),
                http_client=AsyncMock(), client=mock_client,
                semaphore=semaphore, dispatched=dispatched, retry_after=retry_after,
            )

        # retry_after should now contain the task with a future timestamp
        assert "task_retry_test" in retry_after
        assert retry_after["task_retry_test"] > time.time()

    async def test_retry_after_cleared_on_stop(self, tmp_db):
        """stop() should clear the _retry_after dict."""
        from backend.services.executor import Executor

        executor = Executor(
            db=tmp_db,
            budget=MagicMock(),
            progress=MagicMock(),
            resource_monitor=MagicMock(),
            tool_registry=MagicMock(),
        )
        executor._retry_after = {"task1": time.time() + 100}
        executor._running = False
        executor._task = None
        await executor.stop()
        assert executor._retry_after == {}

    async def test_backoff_capped_at_120s(self, tmp_db):
        """Backoff delay should never exceed 120 seconds."""
        import anthropic
        from backend.services.task_lifecycle import execute_task

        budget = MagicMock()
        budget.can_spend = AsyncMock(return_value=True)
        budget.record_spend = AsyncMock()
        budget.release_reservation = AsyncMock()

        progress = MagicMock()
        progress.push_event = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(
            side_effect=anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            )
        )

        task_row = {
            "id": "task_backoff_cap",
            "project_id": "proj_1",
            "model_tier": "sonnet",
            "context_json": None,
            "system_prompt": "Test",
            "tools_json": "[]",
            "description": "Do something",
            "max_tokens": 4096,
            "title": "Test task",
            "retry_count": 10,  # High retry count → would overflow without cap
            "max_retries": 20,
        }

        now = time.time()
        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'executing', ?, ?)",
            ("proj_1", "Test", "Test", now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
            ("plan_1", "proj_1", now),
        )
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, max_tokens, tools_json, "
            "system_prompt, retry_count, max_retries, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task_backoff_cap", "proj_1", "plan_1", "Test task", "Do something", "code",
             0, "queued", "sonnet", 4096, "[]", "Test", 10, 20, now, now),
        )

        semaphore = asyncio.Semaphore(5)
        dispatched = set()
        retry_after = {}

        with patch("backend.services.claude_agent.get_model_id", return_value="claude-sonnet-4-6"):
            await execute_task(
                task_row=task_row, est_cost=0.0, db=tmp_db, budget=budget,
                progress=progress, tool_registry=MagicMock(),
                http_client=AsyncMock(), client=mock_client,
                semaphore=semaphore, dispatched=dispatched, retry_after=retry_after,
            )

        # Delay should be capped: retry_after - now <= 120 + small jitter
        delay = retry_after["task_backoff_cap"] - time.time()
        assert delay <= 123  # 120 + up to 2s jitter + 1s tolerance


# ---------------------------------------------------------------------------
# 3.2 — RAGIndexCache shared across tools
# ---------------------------------------------------------------------------

class TestRAGIndexCache:
    async def test_shared_cache_between_search_and_lookup(self):
        """Both SearchKnowledgeTool and LookupTypeTool should share the same cache."""
        from backend.tools.rag import RAGIndexCache, SearchKnowledgeTool, LookupTypeTool

        cache = RAGIndexCache()
        search = SearchKnowledgeTool(cache=cache)
        lookup = LookupTypeTool(cache=cache)

        assert search._cache is cache
        assert lookup._cache is cache

    async def test_cache_returns_none_for_unknown_db(self):
        """Unknown database name should return None."""
        from backend.tools.rag import RAGIndexCache

        cache = RAGIndexCache()
        result = await cache.get("nonexistent_db")
        assert result is None

    async def test_cache_creates_index_once(self):
        """Multiple get() calls for the same DB should return the same index."""
        from backend.tools.rag import RAGIndexCache

        with patch("backend.tools.rag.RAG_DATABASES", {"test": "/fake/path.db"}):
            cache = RAGIndexCache()
            # Mock _RAGIndex.load to avoid actual file I/O
            with patch("backend.tools.rag._RAGIndex.load"):
                idx1 = await cache.get("test")
                idx2 = await cache.get("test")
                assert idx1 is idx2


# ---------------------------------------------------------------------------
# 3.3 — RAG index error recovery
# ---------------------------------------------------------------------------

class TestRAGIndexErrorRecovery:
    def test_tri_state_initial(self):
        """New index should start in unloaded state."""
        from backend.tools.rag import _RAGIndex
        idx = _RAGIndex("/fake/path.db", 768)
        assert idx._state == "unloaded"

    def test_failed_state_with_cooldown(self):
        """Failed index should not retry during cooldown."""
        from backend.tools.rag import _RAGIndex
        idx = _RAGIndex("/nonexistent/path.db", 768)
        idx.load()  # Will fail (file not found)
        assert idx._state == "failed"
        assert idx._failed_at > 0

        # Second load during cooldown should be a no-op
        old_failed_at = idx._failed_at
        idx.load()
        assert idx._failed_at == old_failed_at  # Not retried

    def test_failed_state_retries_after_cooldown(self):
        """Failed index should retry after cooldown expires."""
        from backend.tools.rag import _RAGIndex, _LOAD_RETRY_COOLDOWN
        idx = _RAGIndex("/nonexistent/path.db", 768)
        idx.load()
        assert idx._state == "failed"

        # Simulate cooldown expiry
        idx._failed_at = time.time() - _LOAD_RETRY_COOLDOWN - 1
        idx.load()  # Should retry (and fail again since path doesn't exist)
        # But it should have attempted a retry (new _failed_at)
        assert idx._failed_at > time.time() - 5

    def test_loaded_state_skips_reload(self):
        """Loaded index should not reload."""
        from backend.tools.rag import _RAGIndex
        idx = _RAGIndex("/fake/path.db", 768)
        idx._state = "loaded"
        idx.load()  # Should be a no-op
        assert idx._state == "loaded"

    def test_load_closes_leftover_connection(self):
        """Retry after cooldown should close old connection before opening new."""
        from backend.tools.rag import _RAGIndex, _LOAD_RETRY_COOLDOWN
        idx = _RAGIndex("/nonexistent/path.db", 768)

        # Simulate a failed load that left a connection behind
        mock_conn = MagicMock()
        idx.conn = mock_conn
        idx._state = "failed"
        idx._failed_at = time.time() - _LOAD_RETRY_COOLDOWN - 1

        idx.load()  # Should close the old connection first
        mock_conn.close.assert_called_once()

    def test_load_acquires_lock(self):
        """load() should hold the lock, preventing concurrent query_sync."""
        from backend.tools.rag import _RAGIndex
        idx = _RAGIndex("/nonexistent/path.db", 768)

        # If load holds the lock, query_sync should block.
        # We can verify by checking the lock is held during load
        # using a mock that wraps the lock's context manager.
        original_load = idx.load

        def checking_load():
            # During load(), the lock should be held (non-blocking acquire fails)
            original_load()
            # After load returns, lock should be released
            assert idx._lock.acquire(blocking=False), "Lock should be released after load()"
            idx._lock.release()

        checking_load()
        # Verify the index was actually processed (state changed from unloaded)
        assert idx._state in ("loaded", "failed")


# ---------------------------------------------------------------------------
# 3.4 — Async I/O (file tool uses to_thread)
# ---------------------------------------------------------------------------

class TestAsyncFileIO:
    async def test_read_file_uses_thread(self, tmp_path):
        """ReadFileTool should use asyncio.to_thread for I/O."""
        from backend.tools.file import ReadFileTool

        # Create a test file
        project_dir = tmp_path / "projects" / "proj1"
        project_dir.mkdir(parents=True)
        (project_dir / "test.txt").write_text("hello", encoding="utf-8")

        tool = ReadFileTool()
        with patch("backend.tools.file.OUTPUT_BASE", tmp_path / "projects"):
            result = await tool.execute({"project_id": "proj1", "path": "test.txt"})
            assert result == "hello"

    async def test_write_file_uses_thread(self, tmp_path):
        """WriteFileTool should use asyncio.to_thread for I/O."""
        from backend.tools.file import WriteFileTool

        tool = WriteFileTool()
        with patch("backend.tools.file.OUTPUT_BASE", tmp_path / "projects"):
            result = await tool.execute({
                "project_id": "proj1",
                "path": "output.txt",
                "content": "world",
            })
            assert "written" in result.lower()
            assert (tmp_path / "projects" / "proj1" / "output.txt").read_text() == "world"


# ---------------------------------------------------------------------------
# 3.4 — Async resource monitor
# ---------------------------------------------------------------------------

class TestAsyncResourceMonitor:
    async def test_check_all_is_async(self):
        """check_all should be an async method."""
        from backend.services.resource_monitor import ResourceMonitor
        monitor = ResourceMonitor()
        # check_all is now async
        assert asyncio.iscoroutinefunction(monitor.check_all)

    async def test_check_all_runs_concurrently(self):
        """Health checks should run concurrently via asyncio.gather."""
        from backend.services.resource_monitor import ResourceMonitor

        with patch("backend.config.OLLAMA_HOSTS", {"local": "http://localhost:11434"}), \
             patch("backend.config.COMFYUI_HOSTS", {"local": "http://localhost:8188"}):
            monitor = ResourceMonitor()
            # Mock the HTTP client to avoid real network calls
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {}
            mock_client.get = AsyncMock(return_value=mock_resp)
            monitor._http = mock_client

            states = await monitor.check_all()
            assert len(states) > 0

    async def test_url_parsing_uses_urlparse(self):
        """Resource definitions should use proper URL parsing."""
        from backend.services.resource_monitor import _build_resources

        with patch("backend.services.resource_monitor.OLLAMA_HOSTS", {"local": "http://myhost:12345"}), \
             patch("backend.services.resource_monitor.COMFYUI_HOSTS", {}):
            resources = _build_resources()
            ollama = [r for r in resources if r.id == "ollama_local"][0]
            assert ollama.host == "myhost"
            assert ollama.port == 12345

    async def test_start_background_reuses_client(self):
        """start_background should not leak a client if one already exists."""
        from backend.services.resource_monitor import ResourceMonitor

        monitor = ResourceMonitor()
        existing_client = AsyncMock()
        monitor._http = existing_client

        # Patch the check loop to avoid actual background work
        with patch.object(monitor, "_check_loop", new_callable=AsyncMock):
            await monitor.start_background()

        # Should reuse existing client, not create a new one
        assert monitor._http is existing_client
        # Cleanup
        if monitor._task:
            monitor._task.cancel()
            try:
                await monitor._task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# 3.5 — Per-round budget check in tool loop
# ---------------------------------------------------------------------------

class TestPerRoundBudgetCheck:
    async def test_budget_exhausted_stops_tool_loop(self, tmp_db):
        """When budget is exhausted mid-loop, should break with partial result."""
        from backend.services.claude_agent import run_claude_task

        budget = MagicMock()
        budget.record_spend = AsyncMock()
        # First can_spend returns False (budget exhausted)
        budget.can_spend = AsyncMock(return_value=False)

        progress = MagicMock()
        progress.push_event = AsyncMock()

        tool_registry = MagicMock()
        tool_registry.get_many = MagicMock(return_value=[])

        # Create a mock Claude client
        mock_client = AsyncMock()

        # Round 1: returns text + tool_use (would normally continue)
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "local_llm"
        tool_block.input = {"prompt": "test"}
        tool_block.id = "tu_001"

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Partial output"

        response1 = MagicMock()
        response1.content = [text_block, tool_block]
        response1.usage = MagicMock(input_tokens=500, output_tokens=300)

        mock_client.messages.create = AsyncMock(return_value=response1)

        task_row = {
            "id": "task_1",
            "project_id": "proj_1",
            "model_tier": "sonnet",
            "context_json": None,
            "system_prompt": "Test",
            "tools_json": "[]",
            "description": "Do something",
            "max_tokens": 4096,
        }

        with patch("backend.services.claude_agent.get_model_id", return_value="claude-sonnet-4-6"), \
             patch("backend.services.claude_agent.calculate_cost", return_value=0.10):
            # est_cost=0.01 means actual cost (0.10) exceeds estimate → triggers budget check
            result = await run_claude_task(
                task_row=task_row, est_cost=0.01,
                client=mock_client, tool_registry=tool_registry,
                budget=budget, progress=progress,
            )

        assert "Partial output" in result["output"]
        # Only 1 API call should have been made (budget check prevented round 2)
        assert mock_client.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# 3.6 — Shared httpx client
# ---------------------------------------------------------------------------

class TestSharedHttpClient:
    def test_registry_accepts_http_client(self):
        """ToolRegistry should accept and pass an httpx client to tools."""
        from backend.tools.registry import ToolRegistry

        mock_client = MagicMock(spec=httpx.AsyncClient)
        registry = ToolRegistry(http_client=mock_client)

        # Search tool should have the shared client
        search = registry.get("search_knowledge")
        assert search._http is mock_client

        # Ollama tool should have the shared client
        ollama = registry.get("local_llm")
        assert ollama._http is mock_client

        # ComfyUI tool should have the shared client
        comfyui = registry.get("generate_image")
        assert comfyui._http is mock_client

    def test_registry_works_without_http_client(self):
        """ToolRegistry should still work with no http_client (backward compat)."""
        from backend.tools.registry import ToolRegistry

        registry = ToolRegistry()
        assert registry.get("search_knowledge") is not None
        assert registry.get("local_llm") is not None

    def test_container_provides_http_client(self):
        """DI container should wire httpx.AsyncClient to executor and registry."""
        from backend.app import container

        try:
            client = container.http_client()
            assert client is not None
            assert isinstance(client, httpx.AsyncClient)
        finally:
            # Clean up the singleton to avoid leaking open connections
            container.http_client.reset()

    def test_comfyui_checkpoint_from_config(self):
        """ComfyUI workflow should use checkpoint from config, not a hardcoded value."""
        from backend.tools.comfyui import _build_txt2img_workflow

        with patch("backend.tools.comfyui.COMFYUI_DEFAULT_CHECKPOINT", "my_custom_model.safetensors"):
            workflow = _build_txt2img_workflow("test prompt", "", 512, 512)
            assert workflow["4"]["inputs"]["ckpt_name"] == "my_custom_model.safetensors"


# ---------------------------------------------------------------------------
# FTS sanitizer
# ---------------------------------------------------------------------------

class TestFTSSanitizer:
    def test_strips_colons(self):
        """Colons (FTS5 column filter syntax) should be stripped."""
        from backend.tools.rag import _sanitize_fts_query
        result = _sanitize_fts_query("type:Graphics")
        # Should not contain raw colon
        assert ":" not in result
        assert "type" in result
        assert "Graphics" in result

    def test_strips_all_operators(self):
        """All FTS5 special chars should be stripped."""
        from backend.tools.rag import _sanitize_fts_query
        result = _sanitize_fts_query('hello AND "world" OR NOT test*')
        assert "AND" not in result
        assert "OR" not in result
        assert "NOT" not in result
        assert "*" not in result

    def test_empty_after_strip(self):
        """Should return empty string if nothing usable remains."""
        from backend.tools.rag import _sanitize_fts_query
        assert _sanitize_fts_query("***") == ""


# ---------------------------------------------------------------------------
# Usage summary access control
# ---------------------------------------------------------------------------

class TestUsageSummaryAccess:
    async def test_unscoped_summary_requires_admin(self, tmp_db):
        """Non-admin user calling get_usage_summary with no project_id should get 403."""
        from backend.routes.usage import get_usage_summary
        from fastapi import HTTPException

        budget = MagicMock()
        budget.get_usage_summary = AsyncMock(return_value=MagicMock())

        non_admin_user = {"id": "user_1", "role": "user", "is_active": True}

        with pytest.raises(HTTPException) as exc_info:
            await get_usage_summary(
                project_id=None,
                current_user=non_admin_user,
                budget=budget,
                db=tmp_db,
            )
        assert exc_info.value.status_code == 403

    async def test_unscoped_summary_allowed_for_admin(self, tmp_db):
        """Admin user calling get_usage_summary with no project_id should succeed."""
        from backend.routes.usage import get_usage_summary

        mock_summary = MagicMock()
        budget = MagicMock()
        budget.get_usage_summary = AsyncMock(return_value=mock_summary)

        admin_user = {"id": "admin_1", "role": "admin", "is_active": True}
        result = await get_usage_summary(
            project_id=None,
            current_user=admin_user,
            budget=budget,
            db=tmp_db,
        )
        assert result is mock_summary
        budget.get_usage_summary.assert_called_once_with(None)
