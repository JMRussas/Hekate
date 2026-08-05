# Git Integration

Hekate isolates each project's work in a git worktree, auto-stages verified files, and creates pull requests on project completion. This guide covers how the git workflow operates and what to watch out for.

---

## Worktree Creation

When a project has a `repo_path`, the engine creates a git worktree to isolate the project's branch from the main repository.

### Location

```
{repo_path}/.worktrees/{project-slug}/
```

For example, if `repo_path` is `C:/Repos/my-app` and the project name is "Add User Auth", the worktree lives at:

```
C:/Repos/my-app/.worktrees/add-user-auth/
```

### Branch naming

The worktree checks out a new branch:

```
hekate/{project-slug}
```

For example: `hekate/add-user-auth`

### What happens during creation

1. Engine calls `git worktree add .worktrees/{slug} -b hekate/{slug}` from the repo root
2. The worktree gets its own working directory and index
3. CLI executors (Hermes) run inside the worktree directory, not the main repo
4. The main repo's branch is unaffected -- you can keep working on `main`

!!! warning "Worktree fallback"
    If worktree creation fails (e.g., the branch already exists, or git worktree is unavailable), the engine falls back to `git checkout`. **This switches the main repo's branch**, which will disrupt any other work in progress. Always ensure worktree creation succeeds.

### Cleaning up worktrees

Worktrees persist after project completion. To clean up:

```bash
cd C:/Repos/my-app
git worktree list                          # See all worktrees
git worktree remove .worktrees/add-user-auth  # Remove a specific one
git worktree prune                         # Remove stale entries
```

!!! tip "The `.worktrees/` directory is gitignored"
    Hekate adds `.worktrees/` to `.gitignore` automatically. Worktree directories never appear in commits.

---

## Auto-Staging (Hephaestus)

After Mimir verifies a task's output, Hephaestus handles git operations:

### Flow

1. **Receive `task_verified` event** with the list of affected files
2. **Syntax check**: Run `compile()` on all Python files to catch syntax errors before staging
3. **Git add**: Stage the affected files via `git add -- file1.py file2.py ...`
4. **Emit result**: `files_staged` on success, `stage_failed` on failure

### What gets staged

Only files listed in the task's `affected_files` are staged. Hephaestus does not run `git add -A` or stage untracked files outside the task's scope.

### Syntax check

Before staging, Hephaestus compiles all `.py` files to check for syntax errors:

```python
for f in files:
    if f.endswith(".py"):
        with open(full_path, "r") as fh:
            compile(fh.read(), full_path, "exec")
```

If a syntax error is found, staging is aborted and a `stage_failed` event is emitted. The task may be retried.

!!! note "Non-Python files"
    Non-Python files (TypeScript, C#, etc.) are staged without syntax checking at this stage. Language-specific validation happens during the Mimir verification step.

---

## PR Creation

When a project completes (all tasks verified and staged), the engine creates a pull request.

### Automatic PR flow

1. All tasks in the project are `completed`
2. Odin marks the project as `completed`
3. The engine pushes the worktree branch to the remote
4. A pull request is created from `hekate/{project-slug}` to the base branch

### PR content

The PR includes:
- Title: project name
- Body: summary of completed tasks, files changed, and any verification notes
- Branch: `hekate/{project-slug}` -> `main` (or the configured `git_base_branch`)

---

## Auto-Merge

When enabled via the review cycle configuration, completed PRs can be auto-merged:

```json
{
  "config": {
    "review_cycle": {
      "enabled": true,
      "auto_commit": true,
      "pr_on_wave_complete": true
    }
  }
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `review_cycle.enabled` | `false` | Enable code review after verification |
| `review_cycle.auto_commit` | `false` | Auto-commit approved code |
| `review_cycle.max_iterations` | 2 | Max review-fix iterations per task |
| `review_cycle.pr_on_wave_complete` | `false` | Create a PR after each wave completes (not just project completion) |

!!! warning "Auto-merge risks"
    Auto-merge bypasses human review of the final PR. Use this only for low-risk projects or when the review cycle has already caught issues during execution.

---

## Branch Switching Warnings

Several scenarios can cause unexpected branch switching:

### Worktree creation failure

If `git worktree add` fails, the engine falls back to `git checkout hekate/{slug}`. This switches the main repo away from whatever branch you were on.

**Mitigation:** Ensure the branch name does not conflict with existing branches. If you see unexpected branch switches, check the engine logs for worktree creation errors.

### Multiple projects on the same repo

Each project creates its own worktree and branch. This is safe -- worktrees are independent. However, if two projects modify the same files, merge conflicts will arise when PRs are merged.

### Manual branch switching during execution

Do not manually switch branches in a repo that has active worktrees. Git worktrees share the same `.git` directory, and certain operations (like `git checkout` in the main repo) can interfere with worktree state.

---

## Git Operations Summary

| Operation | When | Handler | Command |
|-----------|------|---------|---------|
| Worktree create | Project starts executing | Engine | `git worktree add .worktrees/{slug} -b hekate/{slug}` |
| File staging | After task verification | Hephaestus | `git add -- {files}` |
| Branch push | Project completion | Engine | `git push -u origin hekate/{slug}` |
| PR creation | After push | Engine | `gh pr create` or API call |
| Worktree cleanup | Manual | User | `git worktree remove .worktrees/{slug}` |

---

## Troubleshooting

### "fatal: '{branch}' is already checked out"

The branch already exists, possibly from a previous failed run. Remove the stale worktree:

```bash
git worktree remove .worktrees/{slug}
git branch -D hekate/{slug}
```

### Files not staged

Check if Hephaestus received `affected_files` in the `task_verified` event. If the list is empty, no files are staged. This can happen if the task output does not report which files were modified.

### Merge conflicts on PR

When multiple projects modify the same repo, merge the earlier PR first, then rebase the later branch:

```bash
cd .worktrees/{slug}
git rebase main
git push --force-with-lease
```
