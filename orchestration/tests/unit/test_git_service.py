#  Orchestration Engine - GitService Unit Tests
#
#  All tests use real temporary git repos via tmp_path.
#  No mocking of git itself.
#
#  Depends on: backend/services/git_service.py
#  Used by:    CI test suite

import pytest

from backend.exceptions import GitError
from backend.services.git_service import GitService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo), capture_output=True, check=True,
    )

    # Initial commit (empty repos cause issues)
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo), capture_output=True, check=True,
    )

    return repo


@pytest.fixture
def git_service():
    """Create a GitService instance (db=None since low-level ops don't need it)."""
    return GitService(db=None)


# ---------------------------------------------------------------------------
# Validate Repo
# ---------------------------------------------------------------------------

class TestValidateRepo:
    async def test_valid_repo(self, git_repo, git_service):
        info = await git_service.validate_repo(git_repo)
        assert info["exists"] is True
        assert info["is_git"] is True
        assert info["current_branch"] is not None

    async def test_nonexistent_path(self, tmp_path, git_service):
        info = await git_service.validate_repo(tmp_path / "nope")
        assert info["exists"] is False
        assert info["is_git"] is False

    async def test_not_a_repo(self, tmp_path, git_service):
        plain = tmp_path / "plain_dir"
        plain.mkdir()
        info = await git_service.validate_repo(plain)
        assert info["exists"] is True
        assert info["is_git"] is False

    async def test_file_not_dir(self, tmp_path, git_service):
        f = tmp_path / "afile.txt"
        f.write_text("hello")
        info = await git_service.validate_repo(f)
        assert info["exists"] is True
        assert info["is_git"] is False


# ---------------------------------------------------------------------------
# Branch Operations
# ---------------------------------------------------------------------------

class TestBranchOps:
    async def test_create_and_checkout(self, git_repo, git_service):
        await git_service.create_branch(git_repo, "feature/test")
        assert await git_service.branch_exists(git_repo, "feature/test")

        await git_service.checkout(git_repo, "feature/test")
        branch = await git_service.get_current_branch(git_repo)
        assert branch == "feature/test"

    async def test_branch_exists_false(self, git_repo, git_service):
        assert not await git_service.branch_exists(git_repo, "nonexistent")

    async def test_delete_branch(self, git_repo, git_service):
        await git_service.create_branch(git_repo, "to-delete")
        assert await git_service.branch_exists(git_repo, "to-delete")

        await git_service.delete_branch(git_repo, "to-delete")
        assert not await git_service.branch_exists(git_repo, "to-delete")

    async def test_create_branch_from_base(self, git_repo, git_service):
        # Create a branch from HEAD explicitly
        current = await git_service.get_current_branch(git_repo)
        await git_service.create_branch(git_repo, "from-base", base=current)
        assert await git_service.branch_exists(git_repo, "from-base")


# ---------------------------------------------------------------------------
# Commit Operations
# ---------------------------------------------------------------------------

class TestCommitOps:
    async def test_commit_with_changes(self, git_repo, git_service):
        (git_repo / "new_file.txt").write_text("content")
        sha = await git_service.stage_and_commit(git_repo, "Add new file")
        assert sha is not None
        assert len(sha) == 40  # Full SHA

    async def test_commit_no_changes(self, git_repo, git_service):
        sha = await git_service.stage_and_commit(git_repo, "Nothing to commit")
        assert sha is None

    async def test_commit_custom_author(self, git_repo, git_service):
        (git_repo / "authored.txt").write_text("by custom author")
        sha = await git_service.stage_and_commit(
            git_repo, "Custom author commit",
            author="Custom Bot <bot@example.com>",
        )
        assert sha is not None

        log = await git_service.get_log(git_repo, count=1)
        assert log[0]["author_name"] == "Custom Bot"


# ---------------------------------------------------------------------------
# Diff Operations
# ---------------------------------------------------------------------------

class TestDiffOps:
    async def test_diff_after_commit(self, git_repo, git_service):
        (git_repo / "changed.txt").write_text("v1")
        await git_service.stage_and_commit(git_repo, "v1")

        diff = await git_service.get_diff(git_repo, against="HEAD~1")
        assert "changed.txt" in diff

    async def test_diff_stat_only(self, git_repo, git_service):
        (git_repo / "stat.txt").write_text("content")
        await git_service.stage_and_commit(git_repo, "stat test")

        diff = await git_service.get_diff(git_repo, against="HEAD~1", stat_only=True)
        assert "stat.txt" in diff
        # Stat output has summary line with insertions
        assert "insertion" in diff or "file changed" in diff

    async def test_diff_staged(self, git_repo, git_service):
        import subprocess
        (git_repo / "staged.txt").write_text("staged content")
        subprocess.run(["git", "add", "staged.txt"], cwd=str(git_repo), check=True)
        diff = await git_service.get_diff_staged(git_repo)
        assert "staged.txt" in diff

    async def test_diff_working(self, git_repo, git_service):
        # Modify an existing tracked file
        (git_repo / "README.md").write_text("modified\n")
        diff = await git_service.get_diff_working(git_repo)
        assert "README.md" in diff


# ---------------------------------------------------------------------------
# Status and Log
# ---------------------------------------------------------------------------

class TestStatusAndLog:
    async def test_status_clean(self, git_repo, git_service):
        status = await git_service.get_status(git_repo)
        assert status == ""

    async def test_status_dirty(self, git_repo, git_service):
        (git_repo / "untracked.txt").write_text("untracked")
        status = await git_service.get_status(git_repo)
        assert "untracked.txt" in status

    async def test_current_branch(self, git_repo, git_service):
        branch = await git_service.get_current_branch(git_repo)
        # Default branch name varies by git config
        assert branch in ("main", "master")

    async def test_log_entries(self, git_repo, git_service):
        log = await git_service.get_log(git_repo, count=5)
        assert len(log) >= 1
        assert log[0]["message"] == "Initial commit"
        assert "sha" in log[0]
        assert len(log[0]["sha"]) == 40

    async def test_log_limit(self, git_repo, git_service):
        # Add several commits
        for i in range(5):
            (git_repo / f"file_{i}.txt").write_text(f"content {i}")
            await git_service.stage_and_commit(git_repo, f"Commit {i}")

        log = await git_service.get_log(git_repo, count=3)
        assert len(log) == 3


# ---------------------------------------------------------------------------
# Dirty State Detection
# ---------------------------------------------------------------------------

class TestDirtyState:
    async def test_clean_repo(self, git_repo, git_service):
        state = await git_service.check_dirty(git_repo)
        assert state["is_dirty"] is False
        assert state["files"] == []
        assert state["is_ours"] is False

    async def test_dirty_foreign(self, git_repo, git_service):
        (git_repo / "foreign.txt").write_text("foreign changes")
        state = await git_service.check_dirty(git_repo)
        assert state["is_dirty"] is True
        assert len(state["files"]) > 0
        # No .orchestration dir, no matching branch prefix, no matching author
        assert state["is_ours"] is False

    async def test_dirty_ours_by_directory(self, git_repo, git_service):
        orch_dir = git_repo / ".orchestration"
        orch_dir.mkdir()
        (orch_dir / "notes.md").write_text("task output")
        state = await git_service.check_dirty(git_repo)
        assert state["is_dirty"] is True
        assert state["is_ours"] is True

    async def test_dirty_ours_by_branch_prefix(self, git_repo, git_service):
        import subprocess
        subprocess.run(
            ["git", "checkout", "-b", "orch/test-project"],
            cwd=str(git_repo), capture_output=True, check=True,
        )
        (git_repo / "uncommitted.txt").write_text("stuff")
        state = await git_service.check_dirty(git_repo)
        assert state["is_dirty"] is True
        assert state["is_ours"] is True


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

class TestMerge:
    async def test_merge_fast_forward(self, git_repo, git_service):
        default = await git_service.get_current_branch(git_repo)
        await git_service.create_branch(git_repo, "ff-branch")
        await git_service.checkout(git_repo, "ff-branch")

        (git_repo / "ff.txt").write_text("fast forward content")
        await git_service.stage_and_commit(git_repo, "ff commit")

        result = await git_service.merge_branch(git_repo, "ff-branch", default)
        assert result["success"] is True
        assert result["merge_type"] == "ff"

    async def test_merge_conflict(self, git_repo, git_service):
        default = await git_service.get_current_branch(git_repo)

        # Create conflict: both branches modify README.md
        await git_service.create_branch(git_repo, "conflict-branch")

        # Modify on default branch
        (git_repo / "README.md").write_text("default changes\n")
        await git_service.stage_and_commit(git_repo, "default modify")

        # Modify on conflict branch
        await git_service.checkout(git_repo, "conflict-branch")
        (git_repo / "README.md").write_text("conflict changes\n")
        await git_service.stage_and_commit(git_repo, "conflict modify")

        result = await git_service.merge_branch(git_repo, "conflict-branch", default)
        assert result["success"] is False
        assert result["merge_type"] == "conflict"

    async def test_merge_regular(self, git_repo, git_service):
        default = await git_service.get_current_branch(git_repo)

        # Create divergent branches with non-conflicting changes
        await git_service.create_branch(git_repo, "diverge-branch")

        # Add file on default
        (git_repo / "default_file.txt").write_text("default")
        await git_service.stage_and_commit(git_repo, "default file")

        # Add different file on diverge branch
        await git_service.checkout(git_repo, "diverge-branch")
        (git_repo / "diverge_file.txt").write_text("diverge")
        await git_service.stage_and_commit(git_repo, "diverge file")

        result = await git_service.merge_branch(git_repo, "diverge-branch", default)
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Worktree
# ---------------------------------------------------------------------------

class TestWorktree:
    async def test_create_and_remove(self, git_repo, git_service, tmp_path):
        wt_path = tmp_path / "worktree"
        await git_service.create_worktree(git_repo, wt_path, "wt-branch")

        # Worktree should exist and be on the correct branch
        assert wt_path.exists()
        branch = await git_service.get_current_branch(wt_path)
        assert branch == "wt-branch"

        # Make a commit in worktree
        (wt_path / "wt_file.txt").write_text("from worktree")
        sha = await git_service.stage_and_commit(wt_path, "worktree commit")
        assert sha is not None

        # Remove worktree
        await git_service.remove_worktree(git_repo, wt_path)


# ---------------------------------------------------------------------------
# Backup and Discard
# ---------------------------------------------------------------------------

class TestBackupAndDiscard:
    async def test_backup_dirty_state(self, git_repo, git_service):
        original = await git_service.get_current_branch(git_repo)
        (git_repo / "dirty.txt").write_text("dirty")
        await git_service.backup_dirty_state(git_repo, "backup/test")

        # Should be back on original branch
        current = await git_service.get_current_branch(git_repo)
        assert current == original

        # Backup branch should exist
        assert await git_service.branch_exists(git_repo, "backup/test")

    async def test_discard_changes(self, git_repo, git_service):
        (git_repo / "README.md").write_text("modified\n")
        (git_repo / "untracked.txt").write_text("untracked")

        await git_service.discard_changes(git_repo)

        status = await git_service.get_status(git_repo)
        assert status == ""

    async def test_revert_commit(self, git_repo, git_service):
        (git_repo / "to_revert.txt").write_text("will be reverted")
        sha = await git_service.stage_and_commit(git_repo, "will revert this")
        assert sha is not None

        await git_service.revert_commit(git_repo, sha)

        # File should no longer exist
        assert not (git_repo / "to_revert.txt").exists()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrors:
    async def test_git_error_on_bad_command(self, git_repo, git_service):
        with pytest.raises(GitError):
            await git_service.checkout(git_repo, "nonexistent-branch-xyz")

    async def test_git_error_on_nonexistent_cwd(self, git_service, tmp_path):
        bad_path = tmp_path / "does_not_exist"
        with pytest.raises((GitError, OSError)):
            await git_service.get_status(bad_path)
