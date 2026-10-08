"""Runner-source provenance for a run (HK-ISSUE-016; root msgs 2125/2135). Host-observed, NOT authenticated.

observe() records, BEFORE a run's first effect, which Hekate source the runner process reports for itself:
the git HEAD and dirty state of the supervisor directory, and the sha256 of each loaded `e1.*` module's
source file. Expected operational failures (git missing, failing, timing out or over its output cap; an
unresolvable or unreadable module file) are recorded as bounded error fields. Anything unexpected propagates
to the caller's backstop (task_runner.record_provenance), which records it; nothing here changes a run's
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
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable

from e1 import cli_worker as W
from e1 import pilot_real as R

SCHEMA = "hekate-run-provenance.v0"
LABEL = "host-observed, not authenticated: on-disk bytes at observation, not loaded bytecode; not a no-spawn proof"
SCOPE = "scripts/local/supervisor_e1"
GIT_TIMEOUT_S = 20
DIRTY_MAX = 32
ERR_TAIL = 512                  # stderr bytes kept from a failed git call
GIT_OUT_CAP = 64 * 1024         # stdout bytes a git call may produce; more -> capture_overflow, result unknown
MODULES_MAX = 64
MODULE_BYTES_MAX = 1 << 20      # 1 MiB per module file
MODULE_ERRORS_MAX = 16
REQUIRED = ("e1/cli_worker.py", "e1/pilot.py", "e1/durable.py", "e1/acts_durable.py")   # absent -> not_loaded

Runner = Callable[..., R.Bounded]        # pilot_real.run_bounded: streamed, prefix-capped, tree-killed on timeout


def _git_failure(step: str, b: R.Bounded | None, typ: str | None = None) -> dict[str, Any]:
    err = b.err_head.decode("utf-8", errors="replace") if b is not None else ""
    return {"step": step, "rc": b.rc if b is not None else None, "type": typ, "stderr": err,
            "stderrBytes": b.err_total if b is not None else 0, "stdoutBytes": b.total if b is not None else 0}


def observe_git(src_dir: Path, *, env: dict[str, str] | None = None, run: Runner = R.run_bounded) -> dict[str, Any]:
    """HEAD of the repo holding src_dir and its dirty state within SCOPE. Every git call is streamed with its
    stdout capped at GIT_OUT_CAP and stderr at ERR_TAIL bytes, under GIT_TIMEOUT_S. A failure, timeout, undrained
    reader or over-cap output is recorded and leaves that result UNKNOWN (null), never clean or partial."""
    out: dict[str, Any] = {"toplevel": None, "head": None, "scope": SCOPE, "dirty": None, "dirtyPaths": [],
                           "dirtyPathsTruncated": False, "error": None}

    def git(step: str, *args: str) -> str | None:
        try:
            b = run(["git", "-C", str(src_dir), *args], cwd=Path(src_dir), timeout_s=GIT_TIMEOUT_S, keep=GIT_OUT_CAP,
                    env=env if env is not None else W.git_env(), merge=False, err_keep=ERR_TAIL)
        except (OSError, ValueError) as e:               # git missing, or an unusable directory
            out["error"] = _git_failure(step, None, type(e).__name__)
            return None
        typ = ("TimeoutExpired" if b.timed_out else "not_drained" if not b.drained
               else "capture_overflow" if b.total > GIT_OUT_CAP else None)
        if typ is not None or b.rc != 0:
            out["error"] = _git_failure(step, b, typ)
            return None
        return b.head.decode("utf-8", errors="replace")    # complete: total <= cap; non-UTF-8 bytes become U+FFFD

    top = git("rev-parse", "rev-parse", "--show-toplevel", "HEAD")
    if top is None:
        return out
    lines = top.splitlines()
    head = lines[1].strip() if len(lines) == 2 else ""
    if len(head) != 40 or any(c not in "0123456789abcdef" for c in head):
        out["error"] = {"step": "rev-parse", "rc": 0, "type": "unexpected_output", "stderr": "", "stderrBytes": 0,
                        "stdoutBytes": len(top)}
        return out
    out["toplevel"], out["head"] = lines[0].strip(), head
    st = git("status", "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--", f":(top){SCOPE}")
    if st is None:
        return out
    paths, fields = [], iter(st.split("\0"))
    for e in fields:
        if len(e) > 3:
            paths.append(e[3:])                         # repo-relative (porcelain -z)
            if "R" in e[:2] or "C" in e[:2]:
                next(fields, None)                     # a rename/copy (either XY column) is followed by its source path
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
        try:
            p = Path(f).resolve()
            found.append((p.relative_to(root).as_posix(), p))
        except ValueError:
            continue                                   # not part of the supervisor source
        except (OSError, RuntimeError, TypeError) as e:
            errors.append({"module": str(f)[-200:], "type": type(e).__name__})   # an unresolvable module path
            continue
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
            run: Runner = R.run_bounded) -> dict[str, Any]:
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
