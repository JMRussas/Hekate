"""Trusted supervisor formatting for supervised-task-spec.v1 (CA-ISSUE-016). Plugs into the EXISTING adapter hook
(cli_worker.CliConfig.finalize, run in CliWorker._capture after the worker exited and BEFORE the supervisor's artifact
commit); there is no second engine. It is not a worker permission and not verifier normalization.

For one round, with the spec's formatter descriptor (pinned Prettier entry sha/version, pinned config sha, a subset of the
allow paths, finite timeout/output) and the already pinned Node:
  1. the candidate's raw diff must be exactly allowlisted regular 100644 modifications (else `failed`: the candidate's fault);
  2. a FRESH owned worktree at the task base gets pristine deps (the worker's own node_modules is never executed), the
     descriptor's pins, the oracle/lock/tool entries and the candidate's bytes of the changed formatter paths;
  3. Node + Prettier run twice there under the bounded runner, a literal argv built from validated values only; the pins
     and immutable inputs are re-hashed right before and after each run and the raw diff there must stay inside the targets;
  4. the second run must leave every byte as the first left it (idempotence);
  5. only then are the formatted bytes written to the candidate paths (each still holding its recorded pre bytes), the
     candidate's raw diff is re-checked, and the supervisor commits as before.
Formatter failure, timeout, pin or scope doubt, non-idempotence or any host error is `unknown` (the pilot stops; no model
retry). Pre- and post-format bytes and sha256 are kept under <run_dir>/format-r<N>/ and returned as HOST derivation
evidence, separate from the worker's provider usage. Limit: the fresh worktree's node_modules is only as pristine as
`npm ci` from the hash-pinned lockfile; only the Prettier ENTRY file and config are individually hashed.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Callable

from e1 import cli_worker as W
from e1 import pilot_real as R
from e1 import task_runner as TR
from e1 import task_spec as T

VERSION = "hekate-format-derivation.v0"
FILE_MAX = 1 << 20
MODIFIED = ("100644", "100644", "M")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_regular(root: Path, rel: str) -> bytes | None:
    """A regular file at `rel` under `root` (no link on the path, at most FILE_MAX bytes), else None."""
    cur = Path(root)
    try:
        for part in Path(rel).parts:
            cur = cur / part
            if stat.S_ISLNK(os.lstat(cur).st_mode):
                return None
        st = os.lstat(cur)
        return cur.read_bytes() if stat.S_ISREG(st.st_mode) and st.st_size <= FILE_MAX else None
    except OSError:
        return None


def pins_ok(root: Path, f: dict[str, Any]) -> bool:
    """The Prettier entry and the config, regular files with their pinned sha256."""
    return all((b := read_regular(root, p["path"])) is not None and _sha(b) == p["sha256"] for p in (f["prettierEntry"], f["config"]))


def format_argv(spec: T.TaskSpec, targets: list[str]) -> list[str]:
    """Literal argv from validated values only: pinned Node, pinned entry, fixed flags, the pinned config, the targets."""
    f = spec.formatter
    return [spec.doc["hashes"]["pinnedNodeExe"]["path"], f["prettierEntry"]["path"], "--write", "--config", f["config"]["path"],
            "--no-editorconfig", *targets]


def records(wt: Path) -> list[tuple[str, str, str, str]] | None:
    """The raw diff of `wt` against its HEAD (everything staged first), or None when git cannot say."""
    if TR._git("add", "-A", cwd=wt).returncode != 0:
        return None
    p = TR._git("diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev", "HEAD", cwd=wt)
    return R.diff_records(p.stdout) if p.returncode == 0 else None


def scoped(recs: list[tuple[str, str, str, str]], allowed: set[str]) -> bool:
    return all(r[:3] == MODIFIED and r[3] in allowed for r in recs)


def make_finalize(spec: T.TaskSpec, repo: Path, run_root: Path, run_dir_of: Callable[[], Path]) -> Callable[[Path, int], dict[str, Any]]:
    return lambda wt, rnd: format_candidate(spec, repo, run_root, Path(run_dir_of()), Path(wt), rnd)


def format_candidate(spec: T.TaskSpec, repo: Path, run_root: Path, run_dir: Path, wt: Path, rnd: int) -> dict[str, Any]:
    f, d = spec.formatter, spec.doc
    if f is None:
        raise W.FinalizeRefused("unknown", "no_formatter")
    out = run_dir / f"format-r{rnd}"
    ev: dict[str, Any] = {"version": VERSION, "kind": "host-derivation", "usage": "none: not a provider call and not provider usage",
                          "specSha256": spec.sha256, "round": rnd, "dir": str(out), "formatter": f,
                          "node": d["hashes"]["pinnedNodeExe"], "runs": []}

    def stop(status: str, reason: str, detail: Any = None):
        raise W.FinalizeRefused(status, reason, dict(ev, refused=reason, detail=detail))

    try:
        T.validate(d)                                           # re-validated before any spawn
    except T.SpecRefused as e:
        stop("unknown", "formatter_spec_invalid", e.code)
    allow = {a["path"] for a in d["allow"]}
    recs = records(wt)
    if recs is None:
        stop("unknown", "candidate_diff_unreadable")
    ev["candidateRecords"] = [list(r) for r in recs]
    if not scoped(recs, allow):
        stop("failed", "diff_outside_allowlist")                # the candidate's fault, same as the verifier's rejection
    changed = {r[3] for r in recs}
    targets = [p for p in f["paths"] if p in changed]
    if not targets:
        ev["skipped"] = "no_formatter_path_changed"             # nothing to format (an empty diff fails later, unchanged)
        return ev
    pre: dict[str, bytes] = {}
    for p in targets:
        b = read_regular(wt, p)
        if b is None:
            stop("unknown", "candidate_file_unreadable", p)
        pre[p] = b
    try:
        for p, b in pre.items():
            dest = out / "pre" / p
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "xb") as fh:                         # the immutable pre-format candidate bytes
                fh.write(b)
    except OSError as e:
        stop("unknown", "evidence_write_failed", type(e).__name__)
    ev["preSha256"] = {p: _sha(b) for p, b in pre.items()}

    fwt = out / "wt"
    add = TR._git("worktree", "add", "--detach", str(fwt), d["source"]["taskBaseCommit"], cwd=repo)
    if add.returncode != 0:
        stop("unknown", "format_worktree_failed", TR.git_stderr(add))
    try:
        ev["deps"] = TR.install_deps(spec, fwt, run_root)
    except TR.PreflightRefused as e:
        stop("unknown", "format_deps_failed", [e.code, e.detail])
    try:
        for p, b in pre.items():
            (fwt / p).write_bytes(b)
    except OSError as e:
        stop("unknown", "format_stage_failed", type(e).__name__)

    def run_once(label: str) -> dict[str, bytes]:
        def guard(when: str) -> None:
            try:
                TR.check_pins(spec)
                TR.check_tree(spec, fwt)
            except TR.PreflightRefused as e:
                stop("unknown", f"integrity_{when}_format", [e.code, e.detail])
            if not pins_ok(fwt, f):
                stop("unknown", f"formatter_pin_mismatch_{when}")
        guard("before")
        argv = format_argv(spec, targets)
        try:
            b = R.run_bounded(argv, cwd=fwt, timeout_s=f["timeoutS"], keep=f["outputKeepBytes"], env=W.worker_env())
        except OSError:
            stop("unknown", "format_spawn_failed")
        ev["runs"].append({"run": label, "argv": argv, "rc": b.rc, "timedOut": b.timed_out, "killVerified": b.kill_verified,
                           "drained": b.drained, "outputSha256": b.sha256, "outputBytes": b.total,
                           "outputHead": b.head.decode("utf-8", errors="replace")})
        if not b.kill_verified or not b.drained:
            stop("unknown", "format_kill_or_drain_unconfirmed")
        if b.timed_out:
            stop("unknown", "format_timeout")
        if b.rc != 0:
            stop("unknown", "format_failed")
        guard("after")
        after = records(fwt)
        if after is None or not scoped(after, set(targets)):
            stop("unknown", "format_out_of_scope", [list(r) for r in after] if after is not None else None)
        got = {p: read_regular(fwt, p) for p in targets}
        if any(v is None for v in got.values()):
            stop("unknown", "format_result_unreadable")
        return got

    post = run_once("format")
    again = run_once("idempotence")
    ev["postSha256"] = {p: _sha(b) for p, b in post.items()}
    if again != post:
        stop("unknown", "format_not_idempotent", {p: _sha(b) for p, b in again.items()})

    for p in targets:                                            # the candidate must still be exactly what was formatted
        cur = read_regular(wt, p)
        if cur != pre[p]:
            stop("unknown", "candidate_changed_during_format", p)
    try:
        for p, b in post.items():
            dest = out / "post" / p
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "xb") as fh:
                fh.write(b)
            (wt / p).write_bytes(b)
    except OSError as e:
        stop("unknown", "format_apply_failed", type(e).__name__)
    final = records(wt)
    if final is None or not scoped(final, allow) or not {r[3] for r in final} <= changed \
            or any(read_regular(wt, p) != post[p] for p in targets):
        stop("unknown", "format_result_out_of_scope", [list(r) for r in final] if final is not None else None)
    ev["files"] = [{"path": p, "changed": pre[p] != post[p], "preSha256": _sha(pre[p]), "preBytes": len(pre[p]),
                    "postSha256": _sha(post[p]), "postBytes": len(post[p]), "preFile": str(out / "pre" / p),
                    "postFile": str(out / "post" / p)} for p in targets]
    ev["idempotent"] = True
    return ev
