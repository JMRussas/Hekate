#  Orchestration Engine - Git Service
#
#  Core git operations via subprocess. Stateless — all git state lives
#  on disk or in the database. All async methods wrap sync subprocess
#  calls via asyncio.to_thread() with configurable timeout.
#
#  Depends on: config.py, exceptions.py, db/connection.py
#  Used by:    container.py, services/git_lifecycle.py (Phase 2)

import asyncio
import logging
import subprocess
from pathlib import Path

from backend.config import (
    GIT_BRANCH_PREFIX,
    GIT_COMMAND_TIMEOUT,
    GIT_COMMIT_AUTHOR,
    GIT_PR_REMOTE,
)
from backend.db.connection import Database
from backend.exceptions import GitError

logger = logging.getLogger("orchestration.git")


class GitService:
    """Stateless git operations service.

    All methods delegate to subprocess.run wrapped in asyncio.to_thread().
    The Database reference is stored for future use by lifecycle hooks
    (Phase 2) but is not used by low-level git operations.
    """

    def __init__(self, *, db: Database):
        self._db = db

    # ------------------------------------------------------------------
    # Low-level helpers (sync — called via to_thread)
    # ------------------------------------------------------------------

    @staticmethod
    def _run_git_sync(
        *args: str,
        cwd: str | Path,
        timeout: int | None = None,
    ) -> str:
        """Run a git command synchronously. Raises GitError on failure."""
        # -c safe.directory=<cwd> avoids dubious ownership errors when NSSM
        # runs as LocalSystem but the repo is owned by the user account.
        safe_dir = str(cwd).replace("\\", "/")
        cmd = ["git", "-c", f"safe.directory={safe_dir}"] + list(args)
        timeout = timeout or GIT_COMMAND_TIMEOUT
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"Git command timed out after {timeout}s: {' '.join(cmd)}")
        except OSError as e:
            raise GitError(f"Failed to run git: {e}")

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise GitError(f"git {args[0]} failed (rc={result.returncode}): {stderr}")

        return result.stdout.strip()

    @staticmethod
    def _run_git_ok_sync(
        *args: str,
        cwd: str | Path,
        timeout: int | None = None,
    ) -> tuple[bool, str]:
        """Run a git command, returning (success, output) without raising."""
        safe_dir = str(cwd).replace("\\", "/")
        cmd = ["git", "-c", f"safe.directory={safe_dir}"] + list(args)
        timeout = timeout or GIT_COMMAND_TIMEOUT
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode == 0, result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return False, ""

    # ------------------------------------------------------------------
    # Repo validation
    # ------------------------------------------------------------------

    async def validate_repo(self, repo_path: str | Path) -> dict:
        """Check if a path is a valid git repository.

        Returns dict with: exists, is_git, current_branch, has_remote.
        """
        p = Path(repo_path)
        if not p.exists():
            return {"exists": False, "is_git": False, "current_branch": None, "has_remote": False}
        if not p.is_dir():
            return {"exists": True, "is_git": False, "current_branch": None, "has_remote": False}

        ok, _ = await asyncio.to_thread(
            self._run_git_ok_sync, "rev-parse", "--is-inside-work-tree", cwd=repo_path,
        )
        if not ok:
            return {"exists": True, "is_git": False, "current_branch": None, "has_remote": False}

        branch = await self.get_current_branch(repo_path)
        ok_remote, _ = await asyncio.to_thread(
            self._run_git_ok_sync, "remote", "get-url", "origin", cwd=repo_path,
        )

        return {
            "exists": True,
            "is_git": True,
            "current_branch": branch,
            "has_remote": ok_remote,
        }

    # ------------------------------------------------------------------
    # Branch operations
    # ------------------------------------------------------------------

    async def create_branch(
        self, cwd: str | Path, name: str, base: str = "HEAD",
    ) -> str:
        """Create a new branch from base. Returns the branch name."""
        await asyncio.to_thread(
            self._run_git_sync, "branch", name, base, cwd=cwd,
        )
        return name

    async def checkout(self, cwd: str | Path, branch: str) -> None:
        """Checkout an existing branch."""
        await asyncio.to_thread(
            self._run_git_sync, "checkout", branch, cwd=cwd,
        )

    async def branch_exists(self, cwd: str | Path, branch: str) -> bool:
        """Check if a branch exists locally."""
        ok, _ = await asyncio.to_thread(
            self._run_git_ok_sync, "rev-parse", "--verify", f"refs/heads/{branch}", cwd=cwd,
        )
        return ok

    async def ensure_repo(self, cwd: str | Path) -> bool:
        """Ensure a directory is a git repository. Initializes if not.

        Handles:
        - Directory doesn't exist → creates it + git init
        - Directory exists but not a repo → git init
        - Dubious ownership (NSSM LocalSystem) → adds safe.directory

        Returns True if a new repo was created, False if it already existed.
        """
        cwd = Path(cwd)
        cwd.mkdir(parents=True, exist_ok=True)

        # Add safe.directory to avoid dubious ownership errors when NSSM
        # runs as LocalSystem but the repo is owned by the user account.
        await self._ensure_safe_directory(cwd)

        ok, _ = await asyncio.to_thread(
            self._run_git_ok_sync, "rev-parse", "--git-dir", cwd=cwd,
        )
        if ok:
            return False

        # Not a git repo — initialize with an empty commit
        await asyncio.to_thread(self._run_git_sync, "init", cwd=cwd)
        await asyncio.to_thread(
            self._run_git_sync,
            "commit", "--allow-empty", "-m", "Initial commit",
            cwd=cwd,
        )
        logger.info("Initialized new git repo at %s", cwd)
        return True

    @staticmethod
    async def _ensure_safe_directory(cwd: Path) -> None:
        """Add cwd to git's global safe.directory if not already present."""
        # Normalize to forward slashes for git config
        normalized = str(cwd).replace("\\", "/")
        try:
            result = subprocess.run(
                ["git", "config", "--global", "--get-all", "safe.directory"],
                capture_output=True, text=True, timeout=5,
            )
            existing = result.stdout.strip().splitlines()
            if normalized in existing or "*" in existing:
                return
        except Exception:
            pass  # If we can't read, just add it

        try:
            subprocess.run(
                ["git", "config", "--global", "--add", "safe.directory", normalized],
                capture_output=True, text=True, timeout=5,
            )
            logger.info("Added safe.directory: %s", normalized)
        except Exception as e:
            logger.warning("Failed to add safe.directory for %s: %s", cwd, e)

    async def _is_dirty(self, cwd: str | Path) -> bool:
        """Check if the working tree has uncommitted changes."""
        ok, output = await asyncio.to_thread(
            self._run_git_ok_sync, "status", "--porcelain", cwd=cwd,
        )
        return bool(output.strip())

    async def _stash_and_checkout(self, cwd: str | Path, branch: str) -> None:
        """Stash dirty changes, checkout branch, then pop stash."""
        await asyncio.to_thread(
            self._run_git_sync, "stash", "push", "-m",
            f"auto-stash before checkout to {branch}",
            cwd=cwd,
        )
        logger.info("Auto-stashed dirty changes in %s", cwd)
        try:
            await self.checkout(cwd, branch)
        except GitError:
            # Restore stash if checkout still fails
            await asyncio.to_thread(
                self._run_git_ok_sync, "stash", "pop", cwd=cwd,
            )
            raise

    async def _get_repo_root(self, cwd: str | Path) -> str:
        """Get the git repo root for a given path."""
        return await asyncio.to_thread(
            self._run_git_sync, "rev-parse", "--show-toplevel", cwd=cwd,
        )

    async def ensure_feature_branch(
        self,
        cwd: str | None,
        branch_name: str,
        base_branch: str = "main",
    ) -> bool:
        """Idempotent branch creation and checkout for project execution.

        Self-healing:
        - No git repo → auto git-init
        - Dubious ownership → safe.directory
        - Base branch missing → falls back to current branch
        - Dirty working tree → auto-stash before checkout
        - Subdirectory of another repo → uses worktree

        Returns True if the branch was checked out, False if skipped.
        """
        if not cwd:
            return False

        # Self-healing: initialize git repo if needed
        was_new = await self.ensure_repo(cwd)

        # Detect if cwd is a subdirectory of a different repo (e.g.
        # C:\Hekate\Odin inside the Hekate repo). If so, the project
        # needs its own worktree to avoid switching branches on the parent.
        repo_root = await self._get_repo_root(cwd)
        repo_root_norm = Path(repo_root).resolve()
        cwd_norm = Path(cwd).resolve()
        if not was_new and repo_root_norm != cwd_norm:
            # cwd is inside a parent repo — create a worktree
            logger.info(
                "Path %s is inside repo %s — creating worktree", cwd, repo_root,
            )
            worktree_path = repo_root_norm / ".worktrees" / branch_name.replace("/", "-")
            if worktree_path.exists():
                # Worktree already exists — use it
                logger.info("Worktree already exists at %s", worktree_path)
                return True

            # Resolve base branch
            if not await self.branch_exists(repo_root, base_branch):
                base_branch = await self.get_current_branch(repo_root)

            # Create branch if needed (on the parent repo)
            if not await self.branch_exists(repo_root, branch_name):
                await self.create_branch(repo_root, branch_name, base=base_branch)

            # Create worktree
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                self._run_git_sync,
                "worktree", "add", str(worktree_path), branch_name,
                cwd=repo_root,
            )
            logger.info("Created worktree at %s on branch %s", worktree_path, branch_name)
            return True

        # Resolve base branch — if the requested base doesn't exist,
        # fall back to whatever branch the repo is actually on.
        if not await self.branch_exists(cwd, base_branch):
            actual = await self.get_current_branch(cwd)
            logger.info(
                "Base branch '%s' not found in %s, using '%s' instead",
                base_branch, cwd, actual,
            )
            base_branch = actual

        if not await self.branch_exists(cwd, branch_name):
            await self.create_branch(cwd, branch_name, base=base_branch)
            logger.info("Created branch %s from %s in %s", branch_name, base_branch, cwd)

        current = await self.get_current_branch(cwd)
        if current != branch_name:
            # Self-healing: stash dirty changes before checkout
            if await self._is_dirty(cwd):
                await self._stash_and_checkout(cwd, branch_name)
            else:
                await self.checkout(cwd, branch_name)
            logger.info("Checked out branch %s in %s", branch_name, cwd)

        return True

    async def delete_branch(self, cwd: str | Path, name: str) -> None:
        """Delete a local branch."""
        await asyncio.to_thread(
            self._run_git_sync, "branch", "-D", name, cwd=cwd,
        )

    async def merge_branch(
        self, cwd: str | Path, source: str, target: str,
    ) -> dict:
        """Merge source branch into target. Attempts fast-forward first.

        Returns dict with: success, merge_type ('ff' or 'merge'), conflict_files.
        """
        # Checkout target
        await self.checkout(cwd, target)

        # Try fast-forward first
        ok, _ = await asyncio.to_thread(
            self._run_git_ok_sync, "merge", "--ff-only", source, cwd=cwd,
        )
        if ok:
            return {"success": True, "merge_type": "ff", "conflict_files": []}

        # Try regular merge
        ok, output = await asyncio.to_thread(
            self._run_git_ok_sync, "merge", "--no-edit", source, cwd=cwd,
        )
        if ok:
            return {"success": True, "merge_type": "merge", "conflict_files": []}

        # Merge conflict — get conflicting files, then abort
        # Unmerged status codes: UU (both modified), AA (both added),
        # DD (both deleted), AU/UA (add vs modify), DU/UD (delete vs modify)
        _UNMERGED = {"DD", "AU", "UA", "UU", "AA", "DU", "UD"}
        status = await self.get_status(cwd)
        conflict_files = [
            line[3:] for line in status.split("\n")
            if len(line) >= 3 and line[:2] in _UNMERGED
        ]
        await asyncio.to_thread(
            self._run_git_ok_sync, "merge", "--abort", cwd=cwd,
        )
        return {"success": False, "merge_type": "conflict", "conflict_files": conflict_files}

    # ------------------------------------------------------------------
    # Worktree operations
    # ------------------------------------------------------------------

    async def create_worktree(
        self, repo_path: str | Path, worktree_path: str | Path, branch: str,
    ) -> str:
        """Create a git worktree at the given path on the given branch."""
        Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            self._run_git_sync, "worktree", "add", str(worktree_path), "-b", branch,
            cwd=repo_path,
        )
        return str(worktree_path)

    async def remove_worktree(
        self, repo_path: str | Path, worktree_path: str | Path,
    ) -> None:
        """Remove a git worktree."""
        await asyncio.to_thread(
            self._run_git_ok_sync, "worktree", "remove", str(worktree_path), "--force",
            cwd=repo_path,
        )
        # Prune stale worktree entries
        await asyncio.to_thread(
            self._run_git_ok_sync, "worktree", "prune", cwd=repo_path,
        )

    # ------------------------------------------------------------------
    # Commit operations
    # ------------------------------------------------------------------

    async def stage_and_commit(
        self,
        cwd: str | Path,
        message: str,
        *,
        author: str | None = None,
    ) -> str | None:
        """Stage all changes and commit. Returns SHA or None if nothing to commit."""
        # Check if there are any changes
        ok, status = await asyncio.to_thread(
            self._run_git_ok_sync, "status", "--porcelain", cwd=cwd,
        )
        if not status.strip():
            return None

        await asyncio.to_thread(
            self._run_git_sync, "add", "-A", cwd=cwd,
        )

        author = author or GIT_COMMIT_AUTHOR
        await asyncio.to_thread(
            self._run_git_sync,
            "commit", "-m", message, f"--author={author}",
            cwd=cwd,
        )

        sha = await asyncio.to_thread(
            self._run_git_sync, "rev-parse", "HEAD", cwd=cwd,
        )
        return sha

    # ------------------------------------------------------------------
    # Diff / status / log
    # ------------------------------------------------------------------

    async def get_diff(
        self,
        cwd: str | Path,
        against: str = "HEAD~1",
        *,
        stat_only: bool = False,
    ) -> str:
        """Get diff against a reference."""
        args = ["diff", "--no-color", against]
        if stat_only:
            args.append("--stat")
        return await asyncio.to_thread(
            self._run_git_sync, *args, cwd=cwd,
        )

    async def get_diff_staged(self, cwd: str | Path) -> str:
        """Get diff of staged changes."""
        return await asyncio.to_thread(
            self._run_git_sync, "diff", "--cached", "--no-color", cwd=cwd,
        )

    async def get_diff_working(self, cwd: str | Path) -> str:
        """Get diff of unstaged working tree changes."""
        return await asyncio.to_thread(
            self._run_git_sync, "diff", "--no-color", cwd=cwd,
        )

    async def get_status(self, cwd: str | Path) -> str:
        """Get short status output."""
        return await asyncio.to_thread(
            self._run_git_sync, "status", "--short", cwd=cwd,
        )

    async def get_current_branch(self, cwd: str | Path) -> str:
        """Get the current branch name."""
        return await asyncio.to_thread(
            self._run_git_sync, "rev-parse", "--abbrev-ref", "HEAD", cwd=cwd,
        )

    async def get_log(
        self, cwd: str | Path, count: int = 10,
    ) -> list[dict]:
        """Get recent commit log as list of dicts."""
        sep = "\x1f"  # ASCII unit separator — unlikely in commit metadata
        output = await asyncio.to_thread(
            self._run_git_sync,
            "log", f"-{count}", f"--format=%H{sep}%an{sep}%ae{sep}%s{sep}%aI", "--no-color",
            cwd=cwd,
        )
        if not output:
            return []

        entries = []
        for line in output.split("\n"):
            parts = line.split(sep, 4)
            if len(parts) == 5:
                entries.append({
                    "sha": parts[0],
                    "author_name": parts[1],
                    "author_email": parts[2],
                    "message": parts[3],
                    "date": parts[4],
                })
        return entries

    # ------------------------------------------------------------------
    # Dirty state detection
    # ------------------------------------------------------------------

    async def check_dirty(self, cwd: str | Path) -> dict:
        """Check if the working tree has uncommitted changes.

        Returns dict with: is_dirty, files, is_ours, status_output.
        The 'is_ours' heuristic checks recent commit authors, .orchestration/
        directory, and branch prefix to guess if the state belongs to us.
        """
        status = await self.get_status(cwd)
        is_dirty = bool(status.strip())

        files = [
            line[3:] for line in status.split("\n")
            if line.strip()
        ] if is_dirty else []

        # "Is ours" heuristic
        is_ours = False
        if is_dirty:
            # Check if .orchestration/ directory exists
            orch_dir = Path(cwd) / ".orchestration"
            if orch_dir.exists():
                is_ours = True

            # Check if current branch matches our prefix
            if not is_ours:
                branch = await self.get_current_branch(cwd)
                if branch.startswith(f"{GIT_BRANCH_PREFIX}/"):
                    is_ours = True

            # Check if recent commits match our author
            if not is_ours:
                log = await self.get_log(cwd, count=3)
                for entry in log:
                    if GIT_COMMIT_AUTHOR.split("<")[0].strip() in entry.get("author_name", ""):
                        is_ours = True
                        break

        return {
            "is_dirty": is_dirty,
            "files": files,
            "is_ours": is_ours,
            "status_output": status,
        }

    async def backup_dirty_state(
        self, cwd: str | Path, backup_branch: str,
    ) -> str:
        """Commit all dirty state to a backup branch. Returns the backup SHA.

        After this call, the original branch is clean — all dirty changes
        have been committed to the backup branch.
        """
        original_branch = await self.get_current_branch(cwd)

        # Create and checkout backup branch
        await asyncio.to_thread(
            self._run_git_sync, "checkout", "-b", backup_branch, cwd=cwd,
        )

        sha = await self.stage_and_commit(
            cwd,
            f"Backup dirty state from {original_branch}",
            author=GIT_COMMIT_AUTHOR,
        )

        # Return to original branch
        await self.checkout(cwd, original_branch)

        return sha or ""

    async def discard_changes(self, cwd: str | Path) -> None:
        """Discard all uncommitted changes in the working tree."""
        await asyncio.to_thread(
            self._run_git_ok_sync, "checkout", "--", ".", cwd=cwd,
        )
        # Also clean untracked files
        await asyncio.to_thread(
            self._run_git_ok_sync, "clean", "-fd", cwd=cwd,
        )

    async def revert_commit(self, cwd: str | Path, sha: str) -> None:
        """Revert a specific commit (creates a new revert commit)."""
        await asyncio.to_thread(
            self._run_git_sync, "revert", "--no-edit", sha, cwd=cwd,
        )

    # ------------------------------------------------------------------
    # Remote / PR
    # ------------------------------------------------------------------

    async def pull_branch(
        self,
        cwd: str | Path,
        branch: str,
        remote: str | None = None,
    ) -> None:
        """Pull latest changes for a branch from remote."""
        remote = remote or GIT_PR_REMOTE
        await asyncio.to_thread(
            self._run_git_sync, "pull", remote, branch, cwd=cwd,
        )

    async def push_branch(
        self,
        cwd: str | Path,
        branch: str,
        remote: str | None = None,
    ) -> None:
        """Push a branch to remote."""
        remote = remote or GIT_PR_REMOTE
        await asyncio.to_thread(
            self._run_git_sync, "push", "-u", remote, branch, cwd=cwd,
        )

    async def create_pr(
        self,
        cwd: str | Path,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict | None:
        """Create a PR via the gh CLI. Returns {url} or None if gh unavailable."""
        try:
            result = await asyncio.to_thread(
                lambda: subprocess.run(
                    [
                        "gh", "pr", "create",
                        "--head", head,
                        "--base", base,
                        "--title", title,
                        "--body", body,
                    ],
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=GIT_COMMAND_TIMEOUT,
                )
            )
            if result.returncode == 0:
                url = result.stdout.strip()
                return {"url": url}
            else:
                logger.warning("gh pr create failed: %s", result.stderr.strip())
                return None
        except subprocess.TimeoutExpired:
            logger.warning("gh pr create timed out after %ss", GIT_COMMAND_TIMEOUT)
            return None
        except (FileNotFoundError, OSError):
            logger.warning("gh CLI not found — PR creation unavailable")
            return None
