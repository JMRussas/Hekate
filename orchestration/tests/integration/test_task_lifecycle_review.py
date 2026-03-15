#  Orchestration Engine - Integration Tests for Review Cycle
#
#  Tests _run_review_cycle from task_lifecycle.py with mocked
#  GitService and CodeReviewer to verify the review loop handles
#  approvals, rejections, iteration limits, and auto-commit.
#
#  Depends on: backend/services/task_lifecycle.py, tests/conftest.py
#  Used by:    CI test suite

import json
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.enums import TaskStatus

from tests.conftest import create_test_project, create_test_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task_row(
    *,
    task_id="task_rv_001",
    project_id="proj1",
    title="Fix widget",
    description="Fix the broken widget",
    task_type="code",
    context_json="[]",
):
    """Build a real sqlite3.Row so tests exercise the same code path as production.

    sqlite3.Row supports row["key"] but NOT row.get("key"), which is the exact
    constraint the production code must handle via _row_get().
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE _tmp "
        "(id TEXT, project_id TEXT, title TEXT, description TEXT, "
        "task_type TEXT, context_json TEXT)"
    )
    conn.execute(
        "INSERT INTO _tmp VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, project_id, title, description, task_type, context_json),
    )
    row = conn.execute("SELECT * FROM _tmp").fetchone()
    conn.close()
    return row


def _make_review(verdict="approved", issues=None, summary="Looks good"):
    return {
        "verdict": verdict,
        "issues": issues or [],
        "summary": summary,
    }


def _stub_kwargs(db, *, task_row=None, result=None):
    """Build the full kwarg dict for _run_review_cycle."""
    row = task_row or _make_task_row()
    return dict(
        task_row=row,
        task_id=row["id"],
        project_id=row["project_id"],
        result=result or {"output": "created widget.py"},
        tier=MagicMock(),
        db=db,
        budget=MagicMock(),
        progress=AsyncMock(),
        client=AsyncMock(),
        tool_registry=MagicMock(),
        http_client=AsyncMock(),
        semaphore=MagicMock(),
        dispatched=set(),
        retry_after={},
    )


# Patch targets: local imports inside _run_review_cycle import from these modules
_PATCH_RESOLVE_CWD = "backend.services.cli_common.resolve_cwd"
_PATCH_GIT_SERVICE = "backend.services.git_service.GitService"
_PATCH_REVIEW_CODE = "backend.services.code_reviewer.review_code"
_PATCH_FORMAT_FEEDBACK = "backend.services.code_reviewer.format_review_feedback"
_PATCH_AUTO_COMMIT = "backend.services.task_lifecycle.REVIEW_AUTO_COMMIT"
_PATCH_MAX_ITERATIONS = "backend.services.task_lifecycle.REVIEW_MAX_ITERATIONS"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestReviewCycleApproval:
    """Tests for the 'approved' verdict path."""

    async def test_approved_returns_false(self, tmp_db):
        """Approved review returns False (proceed to completion)."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_001")

        kwargs = _stub_kwargs(tmp_db)

        with (
            patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value="/fake/repo"),
            patch(_PATCH_GIT_SERVICE) as MockGit,
            patch(_PATCH_REVIEW_CODE, new_callable=AsyncMock, return_value=_make_review("approved")),
        ):
            git_inst = MockGit.return_value
            git_inst.get_status = AsyncMock(return_value="M widget.py")
            git_inst.get_diff_working = AsyncMock(return_value="diff content")
            git_inst.stage_and_commit = AsyncMock(return_value="abc1234")

            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is False

    async def test_approved_auto_commits_when_enabled(self, tmp_db):
        """Approved review with diff triggers auto-commit."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_001")

        kwargs = _stub_kwargs(tmp_db)

        with (
            patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value="/fake/repo"),
            patch(_PATCH_GIT_SERVICE) as MockGit,
            patch(_PATCH_REVIEW_CODE, new_callable=AsyncMock, return_value=_make_review("approved")),
            patch(_PATCH_AUTO_COMMIT, True),
        ):
            git_inst = MockGit.return_value
            git_inst.get_status = AsyncMock(return_value="M widget.py")
            git_inst.get_diff_working = AsyncMock(return_value="diff content")
            git_inst.stage_and_commit = AsyncMock(return_value="abc1234")

            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is False
        git_inst.stage_and_commit.assert_called_once()

    async def test_approved_no_commit_when_disabled(self, tmp_db):
        """Approved review with auto_commit=False skips commit."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_001")

        kwargs = _stub_kwargs(tmp_db)

        with (
            patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value="/fake/repo"),
            patch(_PATCH_GIT_SERVICE) as MockGit,
            patch(_PATCH_REVIEW_CODE, new_callable=AsyncMock, return_value=_make_review("approved")),
            patch(_PATCH_AUTO_COMMIT, False),
        ):
            git_inst = MockGit.return_value
            git_inst.get_status = AsyncMock(return_value="M widget.py")
            git_inst.get_diff_working = AsyncMock(return_value="diff content")
            git_inst.stage_and_commit = AsyncMock(return_value=None)

            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is False
        git_inst.stage_and_commit.assert_not_called()


@pytest.mark.asyncio
class TestReviewCycleRejection:
    """Tests for the 'changes_requested' verdict path."""

    async def test_changes_requested_requeues_task(self, tmp_db):
        """First rejection re-queues task to PENDING with feedback in context."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_002")

        kwargs = _stub_kwargs(
            tmp_db,
            task_row=_make_task_row(task_id="task_rv_002"),
        )

        with (
            patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value="/fake/repo"),
            patch(_PATCH_GIT_SERVICE) as MockGit,
            patch(
                _PATCH_REVIEW_CODE,
                new_callable=AsyncMock,
                return_value=_make_review(
                    "changes_requested",
                    issues=[{"severity": "error", "file": "widget.py", "description": "Missing null check"}],
                    summary="Missing null check in widget.py",
                ),
            ),
            patch(_PATCH_FORMAT_FEEDBACK, return_value="Review feedback: Missing null check"),
            patch(_PATCH_MAX_ITERATIONS, 2),
        ):
            git_inst = MockGit.return_value
            git_inst.get_status = AsyncMock(return_value="M widget.py")
            git_inst.get_diff_working = AsyncMock(return_value="diff content")

            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is True

        # Verify task was set back to PENDING
        row = await tmp_db.fetchone(
            "SELECT status, context_json FROM tasks WHERE id = ?",
            ("task_rv_002",),
        )
        assert row["status"] == TaskStatus.PENDING

        # Verify feedback was appended to context
        ctx = json.loads(row["context_json"])
        review_entries = [e for e in ctx if e.get("type") == "review_feedback"]
        assert len(review_entries) == 1
        assert "null check" in review_entries[0]["content"].lower()

    async def test_changes_requested_at_limit_escalates(self, tmp_db):
        """When iteration limit is reached, task moves to NEEDS_REVIEW."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_003")

        # Pre-populate context with 2 prior review feedbacks (at the limit)
        prior_ctx = json.dumps([
            {"type": "review_feedback", "content": "Fix iteration 1"},
            {"type": "review_feedback", "content": "Fix iteration 2"},
        ])
        await tmp_db.execute_write(
            "UPDATE tasks SET context_json = ? WHERE id = ?",
            (prior_ctx, "task_rv_003"),
        )

        kwargs = _stub_kwargs(
            tmp_db,
            task_row=_make_task_row(
                task_id="task_rv_003",
                context_json=prior_ctx,
            ),
        )

        with (
            patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value="/fake/repo"),
            patch(_PATCH_GIT_SERVICE) as MockGit,
            patch(
                _PATCH_REVIEW_CODE,
                new_callable=AsyncMock,
                return_value=_make_review("changes_requested", summary="Still has issues"),
            ),
            patch(_PATCH_MAX_ITERATIONS, 2),
        ):
            git_inst = MockGit.return_value
            git_inst.get_status = AsyncMock(return_value="M widget.py")
            git_inst.get_diff_working = AsyncMock(return_value="diff content")

            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is True

        row = await tmp_db.fetchone(
            "SELECT status, error FROM tasks WHERE id = ?",
            ("task_rv_003",),
        )
        assert row["status"] == TaskStatus.NEEDS_REVIEW
        assert "2 iterations" in row["error"]


@pytest.mark.asyncio
class TestReviewCycleEdgeCases:
    """Edge cases and special scenarios."""

    async def test_no_repo_path_skips_review(self, tmp_db):
        """When resolve_cwd returns None, review is skipped (returns False)."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_004")

        kwargs = _stub_kwargs(
            tmp_db,
            task_row=_make_task_row(task_id="task_rv_004"),
        )

        with patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value=None):
            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is False

    async def test_git_diff_failure_still_reviews(self, tmp_db):
        """If git diff throws, review proceeds with diff_text=None."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_005")

        kwargs = _stub_kwargs(
            tmp_db,
            task_row=_make_task_row(task_id="task_rv_005"),
        )

        with (
            patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value="/fake/repo"),
            patch(_PATCH_GIT_SERVICE, side_effect=Exception("git not found")),
            patch(
                _PATCH_REVIEW_CODE,
                new_callable=AsyncMock,
                return_value=_make_review("approved"),
            ) as mock_review,
        ):
            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is False
        mock_review.assert_called_once()
        assert mock_review.call_args.kwargs.get("diff_text") is None

    async def test_scoped_diff_uses_affected_files(self, tmp_db):
        """When affected_files exist in context, diff is scoped to those files."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_006")

        ctx_with_affected = json.dumps([
            {"type": "affected_files", "content": "src/widget.py, src/utils.py"},
        ])
        kwargs = _stub_kwargs(
            tmp_db,
            task_row=_make_task_row(
                task_id="task_rv_006",
                context_json=ctx_with_affected,
            ),
        )

        with (
            patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value="/fake/repo"),
            patch(_PATCH_GIT_SERVICE) as MockGit,
            patch(_PATCH_REVIEW_CODE, new_callable=AsyncMock, return_value=_make_review("approved")),
            patch(_PATCH_AUTO_COMMIT, False),
        ):
            git_inst = MockGit.return_value
            git_inst.get_status = AsyncMock(return_value="M src/widget.py")
            git_inst._run_git_sync = MagicMock(return_value="scoped diff")

            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is False
        # Verify scoped diff was used (via asyncio.to_thread calling _run_git_sync)
        git_inst._run_git_sync.assert_called_once()
        call_args = git_inst._run_git_sync.call_args
        assert "src/widget.py" in call_args[0]
        assert "src/utils.py" in call_args[0]

    async def test_prior_feedback_passed_to_reviewer(self, tmp_db):
        """On iteration 2+, prior feedback is forwarded to review_code."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_007")

        prior_ctx = json.dumps([
            {"type": "review_feedback", "content": "Fix the null check in line 42"},
        ])
        kwargs = _stub_kwargs(
            tmp_db,
            task_row=_make_task_row(
                task_id="task_rv_007",
                context_json=prior_ctx,
            ),
        )

        with (
            patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value="/fake/repo"),
            patch(_PATCH_GIT_SERVICE) as MockGit,
            patch(
                _PATCH_REVIEW_CODE,
                new_callable=AsyncMock,
                return_value=_make_review("approved"),
            ) as mock_review,
            patch(_PATCH_AUTO_COMMIT, False),
        ):
            git_inst = MockGit.return_value
            git_inst.get_status = AsyncMock(return_value="M widget.py")
            git_inst.get_diff_working = AsyncMock(return_value="diff")

            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is False
        mock_review.assert_called_once()
        assert mock_review.call_args.kwargs["iteration"] == 1
        assert "null check" in mock_review.call_args.kwargs["prior_feedback"]

    async def test_auto_commit_failure_still_approves(self, tmp_db):
        """If auto-commit throws, the review still returns False (approved)."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_008")

        kwargs = _stub_kwargs(tmp_db, task_row=_make_task_row(task_id="task_rv_008"))

        with (
            patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value="/fake/repo"),
            patch(_PATCH_GIT_SERVICE) as MockGit,
            patch(_PATCH_REVIEW_CODE, new_callable=AsyncMock, return_value=_make_review("approved")),
            patch(_PATCH_AUTO_COMMIT, True),
        ):
            git_inst = MockGit.return_value
            git_inst.get_status = AsyncMock(return_value="M widget.py")
            git_inst.get_diff_working = AsyncMock(return_value="diff content")
            git_inst.stage_and_commit = AsyncMock(
                side_effect=Exception("Permission denied"),
            )

            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is False

    async def test_clean_working_tree_skips_diff(self, tmp_db):
        """Empty git status means no diff is passed to review_code."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_rv_009")

        kwargs = _stub_kwargs(tmp_db, task_row=_make_task_row(task_id="task_rv_009"))

        with (
            patch(_PATCH_RESOLVE_CWD, new_callable=AsyncMock, return_value="/fake/repo"),
            patch(_PATCH_GIT_SERVICE) as MockGit,
            patch(
                _PATCH_REVIEW_CODE,
                new_callable=AsyncMock,
                return_value=_make_review("approved"),
            ) as mock_review,
            patch(_PATCH_AUTO_COMMIT, False),
        ):
            git_inst = MockGit.return_value
            git_inst.get_status = AsyncMock(return_value="")

            from backend.services.task_lifecycle import _run_review_cycle

            result = await _run_review_cycle(**kwargs)

        assert result is False
        assert mock_review.call_args.kwargs.get("diff_text") is None
