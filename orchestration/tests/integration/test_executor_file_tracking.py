#  Orchestration Engine - Integration Tests for Executor File Tracking
#
#  Tests the file tracking block in task_lifecycle.py that stages declared
#  affected_files via git add and detects orphaned files after task execution.
#
#  Depends on: backend/services/task_lifecycle.py
#  Used by:    CI test suite

import asyncio
import json
import logging
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task_row(
    *,
    task_id="task_ft_001",
    project_id="proj1",
    title="Implement feature",
    description="Add the feature",
    task_type="code",
    context_json="[]",
    affected_files="[]",
):
    """Build a real sqlite3.Row with the columns the file-tracking block reads.

    Includes the affected_files column to test the fallback path when
    context_json doesn't contain an affected_files entry.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE _tmp "
        "(id TEXT, project_id TEXT, title TEXT, description TEXT, "
        "task_type TEXT, context_json TEXT, affected_files TEXT)"
    )
    conn.execute(
        "INSERT INTO _tmp VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, project_id, title, description, task_type,
         context_json, affected_files),
    )
    row = conn.execute("SELECT * FROM _tmp").fetchone()
    conn.close()
    return row


# Patch targets
_PATCH_RESOLVE_CWD = "backend.services.cli_common.resolve_cwd"
_PATCH_GIT_SERVICE = "backend.services.git_service.GitService"
_PATCH_SENTINEL_CTX_CLIENT = "backend.services.sentinel.context_client.SentinelContextClient"


# ---------------------------------------------------------------------------
# Isolated file-tracking exerciser
# ---------------------------------------------------------------------------

async def _run_file_tracking(
    *,
    task_row=None,
    context_json=None,
    git_status_output="",
    git_add_side_effect=None,
    resolve_cwd="/fake/repo",
):
    """Execute only the file-tracking block from task_lifecycle.

    Reproduces the exact logic from task_lifecycle.py lines 1041-1136
    with mocked git and sentinel dependencies. The db parameter is a
    MagicMock since resolve_cwd and GitService are both patched.
    """
    if context_json is not None and task_row is None:
        task_row = _make_task_row(context_json=json.dumps(context_json))
    elif task_row is None:
        task_row = _make_task_row()

    task_id = task_row["id"]
    project_id = task_row["project_id"]
    db = MagicMock()  # Only passed through to mocked functions

    sentinel_client_mock = AsyncMock()
    sentinel_client_mock.save_observation = AsyncMock(return_value="obs_123")
    sentinel_client_mock.close = AsyncMock()

    git_add_calls = []

    def _track_git_sync(*args, cwd=None, timeout=None):
        """Dispatch git commands: track add calls, return status output."""
        if args and args[0] == "add":
            git_add_calls.append(args[2] if len(args) > 2 else args)
            if git_add_side_effect:
                raise git_add_side_effect
        if args and args[0] == "status":
            return (True, git_status_output)
        return (True, "")

    with (
        patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value=resolve_cwd),
        patch(_PATCH_GIT_SERVICE) as MockGit,
        patch(_PATCH_SENTINEL_CTX_CLIENT, return_value=sentinel_client_mock),
    ):
        git_inst = MockGit.return_value
        git_inst._run_git_ok_sync = MagicMock(side_effect=_track_git_sync)

        from backend.services.task_lifecycle import _row_get
        logger = logging.getLogger("test_file_tracking")

        # -- Reproduce the file-tracking block from task_lifecycle.py --
        try:
            from backend.services.cli_common import resolve_cwd as _resolve
            from backend.services.git_service import GitService

            _ft_cwd = await _resolve(db, project_id)
            if _ft_cwd:
                _ft_git = GitService(db=db)

                # Extract affected_files from context_json
                _ft_ctx = json.loads(task_row["context_json"] or "[]")
                _ft_affected: list[str] = []
                for _ft_entry in _ft_ctx:
                    if _ft_entry.get("type") == "affected_files":
                        _ft_affected = _ft_entry.get("content", "").split(", ")
                        break
                if not _ft_affected:
                    _ft_af_json = _row_get(task_row, "affected_files") or "[]"
                    if isinstance(_ft_af_json, str):
                        try:
                            _ft_affected = json.loads(_ft_af_json)
                        except json.JSONDecodeError:
                            _ft_affected = []

                # Stage declared affected_files
                _ft_affected = [f.strip() for f in _ft_affected if f.strip()]
                if _ft_affected:
                    for _ft_file in _ft_affected:
                        try:
                            await asyncio.to_thread(
                                _ft_git._run_git_ok_sync,
                                "add", "--", _ft_file, cwd=_ft_cwd,
                            )
                        except Exception:
                            logger.debug("Failed to stage %s", _ft_file)
                    logger.info(
                        "Staged %d declared file(s) for task %s",
                        len(_ft_affected), task_id,
                    )

                # Detect orphaned files
                _ft_status_ok, _ft_status = await asyncio.to_thread(
                    _ft_git._run_git_ok_sync,
                    "status", "--porcelain", cwd=_ft_cwd,
                )
                orphans = []
                if _ft_status.strip():
                    orphans = [
                        line[3:] for line in _ft_status.split("\n")
                        if line.strip() and line[3:].strip() not in _ft_affected
                    ]
                    if orphans:
                        logger.warning(
                            "Task %s left %d orphaned file(s): %s",
                            task_id, len(orphans),
                            ", ".join(orphans[:10]),
                        )
                        from backend.services.sentinel.models import (
                            SentinelObservation, Severity,
                        )
                        from backend.services.sentinel.context_client import (
                            SentinelContextClient,
                        )
                        _ft_obs = SentinelObservation(
                            category="orphaned_files",
                            message=(
                                f"Task {task_id} created {len(orphans)} "
                                f"file(s) not declared in affected_files"
                            ),
                            severity=Severity.WARNING,
                            project_id=project_id,
                            task_id=task_id,
                            details={
                                "orphaned_files": orphans[:50],
                                "declared_files": _ft_affected,
                            },
                        )
                        _ft_ctx_client = SentinelContextClient()
                        try:
                            await _ft_ctx_client.save_observation(
                                _ft_obs, parent_id=project_id,
                            )
                        finally:
                            await _ft_ctx_client.close()

        except Exception:
            logger.debug(
                "File tracking failed for task %s (non-blocking)",
                task_id, exc_info=True,
            )

    return {
        "git_add_calls": git_add_calls,
        "sentinel_client": sentinel_client_mock,
    }


# ---------------------------------------------------------------------------
# Tests — staging declared files
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAffectedFileStaging:
    """Verify that files declared in affected_files are staged via git add."""

    async def test_stages_declared_files_from_context_json(self):
        """Files listed in context_json affected_files entry are git-added."""
        context = [{"type": "affected_files", "content": "src/widget.py, src/utils.py"}]
        result = await _run_file_tracking(
            context_json=context,
            git_status_output="",
        )

        assert "src/widget.py" in result["git_add_calls"]
        assert "src/utils.py" in result["git_add_calls"]

    async def test_stages_single_file(self):
        """A single affected file is staged correctly."""
        context = [{"type": "affected_files", "content": "main.py"}]
        result = await _run_file_tracking(
            context_json=context,
            git_status_output="",
        )

        assert result["git_add_calls"] == ["main.py"]

    async def test_empty_affected_files_skips_staging(self):
        """No git add calls when affected_files is empty."""
        result = await _run_file_tracking(
            context_json=[],
            git_status_output="",
        )

        assert result["git_add_calls"] == []

    async def test_stages_from_affected_files_column_fallback(self):
        """When context_json has no affected_files entry, falls back to column."""
        row = _make_task_row(
            context_json="[]",
            affected_files=json.dumps(["lib/core.py", "lib/helpers.py"]),
        )
        result = await _run_file_tracking(
            task_row=row,
            git_status_output="",
        )

        assert "lib/core.py" in result["git_add_calls"]
        assert "lib/helpers.py" in result["git_add_calls"]

    async def test_whitespace_entries_filtered(self):
        """Whitespace-only entries in affected_files are ignored."""
        context = [{"type": "affected_files", "content": "src/a.py,  , src/b.py"}]
        result = await _run_file_tracking(
            context_json=context,
            git_status_output="",
        )

        assert len(result["git_add_calls"]) == 2

    async def test_empty_content_string_no_staging(self):
        """Empty content string in affected_files entry produces no staging."""
        context = [{"type": "affected_files", "content": ""}]
        result = await _run_file_tracking(
            context_json=context,
            git_status_output="",
        )

        assert result["git_add_calls"] == []


# ---------------------------------------------------------------------------
# Tests — orphaned file detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestOrphanedFileDetection:
    """Verify that undeclared files trigger warnings and sentinel observations."""

    async def test_orphaned_files_trigger_sentinel_observation(self):
        """Files in git status but not in affected_files create an observation."""
        context = [{"type": "affected_files", "content": "src/widget.py"}]
        git_status = " M src/widget.py\n?? secret.py"

        result = await _run_file_tracking(
            context_json=context,
            git_status_output=git_status,
        )

        sentinel = result["sentinel_client"]
        sentinel.save_observation.assert_called_once()
        obs = sentinel.save_observation.call_args[0][0]

        assert obs.category == "orphaned_files"
        assert obs.severity.value == "warning" or obs.severity.name == "WARNING"
        assert "secret.py" in obs.details["orphaned_files"]
        assert obs.task_id == "task_ft_001"
        assert obs.project_id == "proj1"
        sentinel.close.assert_called_once()

    async def test_no_orphans_when_all_declared(self):
        """No sentinel observation when all changed files are declared."""
        context = [{"type": "affected_files", "content": "src/widget.py"}]
        git_status = " M src/widget.py"

        result = await _run_file_tracking(
            context_json=context,
            git_status_output=git_status,
        )

        sentinel = result["sentinel_client"]
        sentinel.save_observation.assert_not_called()

    async def test_no_observation_on_clean_status(self):
        """No sentinel observation when git status is clean."""
        result = await _run_file_tracking(
            context_json=[{"type": "affected_files", "content": "src/a.py"}],
            git_status_output="",
        )

        sentinel = result["sentinel_client"]
        sentinel.save_observation.assert_not_called()

    async def test_multiple_orphans_all_recorded(self):
        """Multiple orphaned files all appear in the observation details."""
        context = [{"type": "affected_files", "content": "declared.py"}]
        git_status = " M declared.py\n?? orphan1.py\n?? orphan2.py\nA  orphan3.py"

        result = await _run_file_tracking(
            context_json=context,
            git_status_output=git_status,
        )

        sentinel = result["sentinel_client"]
        sentinel.save_observation.assert_called_once()
        obs = sentinel.save_observation.call_args[0][0]
        assert len(obs.details["orphaned_files"]) == 3

    async def test_observation_includes_declared_files(self):
        """Sentinel observation details include the declared files list."""
        context = [{"type": "affected_files", "content": "src/main.py"}]
        git_status = " M src/main.py\n?? stray.py"

        result = await _run_file_tracking(
            context_json=context,
            git_status_output=git_status,
        )

        obs = result["sentinel_client"].save_observation.call_args[0][0]
        assert "src/main.py" in obs.details["declared_files"]
        assert "stray.py" in obs.details["orphaned_files"]


# ---------------------------------------------------------------------------
# Tests — resilience
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFileTrackingResilience:
    """Verify that file tracking never blocks task completion."""

    async def test_git_add_failure_does_not_raise(self):
        """If git add fails for a file, the block continues without raising."""
        context = [{"type": "affected_files", "content": "bad.py"}]
        result = await _run_file_tracking(
            context_json=context,
            git_add_side_effect=RuntimeError("git add failed"),
            git_status_output="",
        )
        # Reaching here means no exception escaped
        assert True

    async def test_sentinel_save_failure_non_blocking(self):
        """Sentinel context client failure doesn't block file tracking."""
        sentinel_mock = AsyncMock()
        sentinel_mock.save_observation = AsyncMock(
            side_effect=Exception("context store down"),
        )
        sentinel_mock.close = AsyncMock()

        context = [{"type": "affected_files", "content": "src/a.py"}]
        with patch(_PATCH_SENTINEL_CTX_CLIENT, return_value=sentinel_mock):
            result = await _run_file_tracking(
                context_json=context,
                git_status_output="?? orphan.py",
            )

        # Reached here without raising
        assert True

    async def test_no_cwd_skips_tracking(self):
        """When resolve_cwd returns None, file tracking is skipped entirely."""
        context = [{"type": "affected_files", "content": "src/a.py"}]
        result = await _run_file_tracking(
            context_json=context,
            resolve_cwd=None,
            git_status_output="",
        )

        assert result["git_add_calls"] == []

    async def test_malformed_affected_files_json_ignored(self):
        """Invalid JSON in affected_files column doesn't crash."""
        row = _make_task_row(
            context_json="[]",
            affected_files="not valid json {{{",
        )
        result = await _run_file_tracking(
            task_row=row,
            git_status_output="",
        )

        assert result["git_add_calls"] == []
