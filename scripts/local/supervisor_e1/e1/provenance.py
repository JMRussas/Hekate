"""Runner-source provenance for a run (HK-ISSUE-016; root msgs 2125/2135). Host-observed, NOT authenticated.

observe() records, BEFORE a run's first effect, which Hekate source the runner process reports for itself:
the git HEAD and dirty state of the supervisor directory, and the sha256 of each loaded `e1.*` module's
source file. It never raises: every failure is a bounded error field, and nothing here changes a run's
outcome, refusal or spend.

LIMITS (what this is NOT):
- The module hashes are of the .py files ON DISK when observed, not of the bytecode the interpreter loaded.
  A file changed after import (or a stale .pyc) is not detected.
- HEAD and dirty state come from the same host's git, scoped to the supervisor directory. Nothing is signed;
  a process that lies about itself is not detected.
- It covers the process that calls it (task_runner.run), including in-process callers such as plan_cli ->
  plan_run. A separate preflight process is not covered.
- It is NOT a no-spawn or ordering proof (HK-ISSUE-015): it binds a run to the source it reported, for review.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable

SCHEMA = "hekate-run-provenance.v0"
LABEL = "host-observed, not authenticated: on-disk bytes at observation, not loaded bytecode; not a no-spawn proof"
SCOPE = "scripts/local/supervisor_e1"
GIT_TIMEOUT_S = 20
DIRTY_MAX = 32
ERR_TAIL = 512
MODULES_MAX = 64
MODULE_BYTES_MAX = 1 << 20      # 1 MiB per module file
MODULE_ERRORS_MAX = 16
REQUIRED = ("e1/cli_worker.py", "e1/pilot.py", "e1/durable.py", "e1/acts_durable.py")   # absent -> not_loaded

Runner = Callable[..., subprocess.CompletedProcess]


def _git_failure(step: str, p: subprocess.CompletedProcess | None, e: BaseException | None = None) -> dict[str, Any]:
    err = (p.stderr or "") if p is not None else ""
    return {"step": step, "rc": p.returncode if p is not None else None, "type": type(e).__name__ if e else None,
            "stderr": err[-ERR_TAIL:]}


def observe_git(src_dir: Path, *, env: dict[str, str] | None = None, run: Runner = subprocess.run) -> dict[str, Any]:
    """HEAD of the repo holding src_dir and its dirty state within SCOPE; failures are recorded, never raised."""
    out: dict[str, Any] = {"toplevel": None, "head": None, "scope": SCOPE, "dirty": None, "dirtyPaths": [],
                           "dirtyPathsTruncated": False, "error": None}

    def git(step: str, *args: str) -> subprocess.CompletedProcess | None:
        try:
            p = run(["git", "-C", str(src_dir), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            out["error"] = _git_failure(step, None, e)
            return None
        if p.returncode != 0:
            out["error"] = _git_failure(step, p)
            return None
        return p

    top = git("rev-parse", "rev-parse", "--show-toplevel", "HEAD")
    if top is None:
        return out
    lines = top.stdout.splitlines()
    head = lines[1].strip() if len(lines) == 2 else ""
    if len(head) != 40 or any(c not in "0123456789abcdef" for c in head):
        out["error"] = {"step": "rev-parse", "rc": top.returncode, "type": None, "stderr": "unexpected rev-parse output"}
        return out
    out["toplevel"], out["head"] = lines[0].strip(), head
    st = git("status", "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--", f":(top){SCOPE}")
    if st is None:
        return out
    paths, fields = [], iter(st.stdout.split("\0"))
    for e in fields:
        if len(e) > 3:
            paths.append(e[3:])                         # repo-relative (porcelain -z)
            if e[0] in "RC":
                next(fields, None)                     # a rename/copy entry is followed by its source path
    out["dirty"] = bool(paths)
    out["dirtyPaths"], out["dirtyPathsTruncated"] = paths[:DIRTY_MAX], len(paths) > DIRTY_MAX
    return out


def observe_modules(src_dir: Path, modules: Iterable[ModuleType]) -> dict[str, Any]:
    """sha256 of each module's source file under src_dir, bounded in count and per-file size."""
    root = Path(src_dir).resolve()
    hashes: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    found: list[tuple[str, Path]] = []
    for m in modules:
        f = getattr(m, "__file__", None)
        if not f:
            continue
        p = Path(f).resolve()
        try:
            found.append((p.relative_to(root).as_posix(), p))
        except ValueError:
            continue                                   # not part of the supervisor source
    found.sort()
    for rel, p in found[:MODULES_MAX]:
        try:
            with open(p, "rb") as fh:
                data = fh.read(MODULE_BYTES_MAX + 1)
        except OSError as e:
            errors.append({"module": rel, "type": type(e).__name__})
            continue
        if len(data) > MODULE_BYTES_MAX:
            errors.append({"module": rel, "type": "too_large"})          # never a partial hash
            continue
        hashes[rel] = hashlib.sha256(data).hexdigest()
    names = {rel for rel, _ in found}
    errors += [{"module": r, "type": "not_loaded"} for r in REQUIRED if r not in names]
    return {"modules": hashes, "modulesTruncated": len(found) > MODULES_MAX,
            "moduleErrors": errors[:MODULE_ERRORS_MAX], "moduleErrorsTruncated": len(errors) > MODULE_ERRORS_MAX}


def observe(src_dir: Path, modules: Iterable[ModuleType], *, env: dict[str, str] | None = None,
            run: Runner = subprocess.run) -> dict[str, Any]:
    """The whole record. src_dir is the supervisor directory (the parent of the e1 package)."""
    return {"schema": SCHEMA, "label": LABEL, "observedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python": python_info(), "git": observe_git(src_dir, env=env, run=run),
            **observe_modules(src_dir, modules)}


def python_info() -> dict[str, Any]:
    try:
        from importlib.metadata import version
        psycopg = version("psycopg")
    except Exception:  # noqa: BLE001 -- informational only
        psycopg = None
    return {"version": platform.python_version(), "implementation": platform.python_implementation(),
            "executable": sys.executable, "psycopg": psycopg}


def loaded_e1_modules() -> list[ModuleType]:
    return [m for name, m in list(sys.modules.items()) if (name == "e1" or name.startswith("e1.")) and m is not None]
