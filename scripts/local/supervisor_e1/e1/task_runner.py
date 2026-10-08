"""Operator task runner v0 (bridge msgs 1626/1632; spec schema supervised-task-spec.v0). TEST-SCOPED.

ONE closed task spec drives the EXISTING pilot (e1/pilot.py), the real-worker adapter (e1/cli_worker.py), and
optionally the real-H1 export (e1/pilot_export.py). It is not a second engine and has no workflow DSL:
every command is a literal argv whose program is the hash-pinned Node.

  plan       validate the spec only: NO clone, NO install, NO test, NO spawn
  preflight  owned clone at the task base, anchor/oracle checks, pristine deps, the STRUCTURED baseline
             proof -- all BEFORE any model spend; writes <run-root>/preflight.json
  run        requires a passing preflight of the same spec sha256 and a --root-go; one supervised pilot

Guarantees (and their limits):
- The user's source checkout is only READ (`git clone --no-local`). Every worktree and ref lives in the
  OWNED clone under the run root; nothing is integrated or pushed automatically.
- Dependencies come from the task base's lockfile (hash-checked) via `npm ci --ignore-scripts` under the
  pinned Node/npm, bounded and tree-killed. The worker gets its own install; the verifier installs FRESH
  AFTER the worker exits, in its own worktree. This is NOT OS isolation.
- The raw diff (exact allowlist, regular 100644 modifications only), the oracle file hashes AT THE ARTIFACT
  and the lockfile are checked BEFORE any artifact code runs. Integrity or infrastructure doubt is an
  `uncertain` verdict, which stops the pilot without a decision or a retry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from e1 import cli_worker as W
from e1 import consumer as C
from e1 import pilot as P
from e1 import pilot_real as R
from e1 import task_spec as T

PREFLIGHT = "preflight.json"
CLEAN_STATUS = ["!! node_modules/"]
VITEST_ENTRY = "node_modules/vitest/vitest.mjs"
TSC_ENTRY = "node_modules/typescript/bin/tsc"
NPMRCS = {"user": "npm-empty-user.npmrc", "global": "npm-empty-global.npmrc"}   # owned EMPTY npm configs (1683/1689)
ERR_KEEP = 4096          # stderr prefix kept for report-bearing runs (stdout alone is the report)


def stderr_evidence(b: R.Bounded) -> dict[str, Any]:
    """stderr of a report-bearing run: evidence only (a Node warning is not a failure by itself)."""
    return {"stderrBytes": b.err_total, "stderrSha256": b.err_sha256, "stderrHead": b.err_head.decode("utf-8", errors="replace")}


class PreflightRefused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(f"{code}: {detail}" if detail is not None else code)
        self.code, self.detail = code, detail


def _git(*args: str, cwd: Path, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=text, timeout=300, env=W.git_env())


def git_stderr(p: subprocess.CompletedProcess) -> str:
    """A failed git call's stderr, bounded (msg 1703 D2): the cause travels with the refusal."""
    err = p.stderr if isinstance(p.stderr, str) else (p.stderr or b"").decode("utf-8", errors="replace")
    return err[-ERR_KEEP:]


def sha256_file(p: Path) -> str:
    return R.sha256_file(Path(p))


# --- pins ------------------------------------------------------------------------------------------------

def check_pins(spec: T.TaskSpec) -> None:
    """The pinned Node and npm CLI, by sha256, before ANY spawn (re-checked before every install/step)."""
    h = spec.doc["hashes"]
    for key, code in (("pinnedNodeExe", "node_hash_mismatch"), ("npmCli", "npm_hash_mismatch")):
        try:
            got = sha256_file(Path(h[key]["path"]))
        except OSError:
            raise PreflightRefused(code, "unreadable") from None
        if got != h[key]["sha256"]:
            raise PreflightRefused(code, got)


def check_tree(spec: T.TaskSpec, wt: Path) -> None:
    """The IMMUTABLE inputs on disk in `wt`, re-hashed right before every spawn there: the oracle files,
    the lockfile and (once installed) the vitest/tsc entries. A previous step cannot have changed them."""
    h = spec.doc["hashes"]
    want = {f["path"]: f["sha256"] for f in spec.doc["oracle"]["files"]}
    want["package-lock.json"] = h["packageLock"]
    if (Path(wt) / "node_modules").exists():
        want.update({VITEST_ENTRY: h["vitestEntry"], TSC_ENTRY: h["tscEntry"]})
    for rel, sha in want.items():
        p = Path(wt) / rel
        try:
            ok = p.is_file() and not p.is_symlink() and sha256_file(p) == sha
        except OSError:
            ok = False
        if not ok:
            raise PreflightRefused("tree_integrity", rel)


# --- the owned clone and the anchor/oracle checks ------------------------------------------------------------

def diff_records(repo: Path, a: str, b: str) -> list[tuple[str, str, str, str]]:
    p = _git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", a, b, cwd=repo)
    recs = R.diff_records(p.stdout) if p.returncode == 0 else None
    if recs is None:
        raise PreflightRefused("git_failed", "diff")
    return recs


def blob_sha256(repo: Path, rev: str, path: str) -> str | None:
    p = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, timeout=120, env=W.git_env())
    return hashlib.sha256(p.stdout).hexdigest() if p.returncode == 0 else None


def owned_clone(spec: T.TaskSpec, run_root: Path) -> Path:
    """`git clone --no-local` of the source (read only), detached at the task base, then the checks:
    anchor is an ancestor of the base; anchor..base touches EXACTLY the oracle files (A or M, new mode
    100644); every oracle blob at the base has its declared sha256."""
    src, base, anchor = spec.doc["source"]["repo"], spec.doc["source"]["taskBaseCommit"], spec.doc["source"]["anchorCommit"]
    repo = run_root / "repo"
    if repo.exists():
        raise PreflightRefused("clone_exists")
    # core.longpaths in the CLONE's own config (msgs 1703/1704): every later checkout, worktree and status in
    # the owned clone tolerates paths over 260 chars on Windows. Nothing global or in the source changes.
    c = subprocess.run(["git", "clone", "-c", "core.longpaths=true", "--no-local", "--no-checkout", "-q", src, str(repo)],
                       capture_output=True, text=True, timeout=900, env=W.git_env())
    if c.returncode != 0:
        raise PreflightRefused("clone_failed", git_stderr(c))
    co = _git("checkout", "-q", "--detach", base, cwd=repo)
    if co.returncode != 0:
        raise PreflightRefused("base_missing", git_stderr(co))
    if _git("rev-parse", "HEAD", cwd=repo).stdout.strip() != base:
        raise PreflightRefused("base_mismatch")
    if _git("merge-base", "--is-ancestor", anchor, base, cwd=repo).returncode != 0:
        raise PreflightRefused("anchor_not_ancestor")
    oracle = {f["path"]: f["sha256"] for f in spec.doc["oracle"]["files"]}
    recs = diff_records(repo, anchor, base)
    if sorted(r[3] for r in recs) != sorted(oracle) or any(r[2] not in ("A", "M") or r[1] != "100644" for r in recs):
        raise PreflightRefused("anchor_diff_not_oracle", [list(r) for r in recs])
    for path, want in oracle.items():
        if blob_sha256(repo, base, path) != want:
            raise PreflightRefused("oracle_hash_mismatch", path)
    return repo


# --- dependencies ------------------------------------------------------------------------------------------

def make_npmrc(run_root: Path) -> None:
    """The run root's OWNED empty npm configs, one for user and one for global (npm refuses the same file as
    both, msg 1689): created once, exclusively, by preflight; outside every worktree."""
    for name in NPMRCS.values():
        with open(Path(run_root) / name, "xb"):
            pass


def npm_env(spec: T.TaskSpec, run_root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    """The environment for npm ONLY (msgs 1675-1689): the worker allowlist plus
    - npm_config_cache: the operator's warm cache, REQUIRED, an absolute existing directory (native Path
      rules: a drive-relative `C:x` or a relative path is refused; a UNC path must exist). Fail closed:
      absent -> npm_cache_required, invalid -> npm_cache_invalid; there is no silent default cache.
    - npm_config_userconfig / npm_config_globalconfig: the run root's two DISTINCT owned EMPTY files, each
      re-checked as an empty regular file before every spawn, so no ambient npmrc changes registry, auth,
      proxy or cache.
    npm's builtin npmrc (beside the pinned CLI) cannot be disabled by env; these env values override it,
    and its sha256 is recorded as provenance. No config contents or credentials are recorded."""
    raw = os.environ.get("npm_config_cache")
    if not raw:
        raise PreflightRefused("npm_cache_required")
    cache = Path(raw)
    if not cache.is_absolute() or not cache.is_dir():
        raise PreflightRefused("npm_cache_invalid", raw)
    rcs: dict[str, Path] = {}
    for role, name in NPMRCS.items():
        rc = Path(run_root) / name
        try:
            st = os.lstat(rc)
        except OSError:
            raise PreflightRefused("npmrc_missing", str(rc)) from None
        if not stat.S_ISREG(st.st_mode) or st.st_size != 0:
            raise PreflightRefused("npmrc_not_empty", str(rc))
        rcs[role] = rc
    builtin = Path(spec.doc["hashes"]["npmCli"]["path"]).parents[1] / "npmrc"
    try:
        builtin_sha = sha256_file(builtin) if builtin.is_file() else None
    except OSError:
        builtin_sha = None
    env = {**W.worker_env(), "npm_config_cache": str(cache), "npm_config_userconfig": str(rcs["user"]),
           "npm_config_globalconfig": str(rcs["global"])}
    ev = {"npmCache": str(cache), "cacachePresent": (cache / "_cacache").is_dir(),
          "userconfig": {"path": str(rcs["user"]), "sha256": sha256_file(rcs["user"])},
          "globalconfig": {"path": str(rcs["global"]), "sha256": sha256_file(rcs["global"])},
          "builtinNpmrc": str(builtin), "builtinNpmrcSha256": builtin_sha}
    return env, ev


def install_deps(spec: T.TaskSpec, wt: Path, run_root: Path) -> dict[str, Any]:
    """Pristine `npm ci` in `wt` from the lockfile (hash-checked BEFORE), entries hash-checked AFTER, under
    npm_env(). The bounded output head is kept on success AND refusal. Raises PreflightRefused (callers
    map it to an uncertain stop)."""
    check_pins(spec)
    h, deps = spec.doc["hashes"], spec.doc["deps"]
    lock = Path(wt) / "package-lock.json"
    if not lock.is_file() or sha256_file(lock) != h["packageLock"]:
        raise PreflightRefused("lock_mismatch")
    if (Path(wt) / "node_modules").exists():
        raise PreflightRefused("deps_not_pristine")
    env, npm_ev = npm_env(spec, run_root)                       # checked right before the spawn
    argv = [h["pinnedNodeExe"]["path"], h["npmCli"]["path"], "ci", "--ignore-scripts", "--no-audit", "--no-fund",
            "--offline" if deps["network"] == "offline" else "--prefer-offline"]
    b = R.run_bounded(argv, cwd=Path(wt), timeout_s=deps["timeoutS"], keep=deps["outputKeepBytes"], env=env)
    ev = {"rc": b.rc, "timedOut": b.timed_out, "killVerified": b.kill_verified, "drained": b.drained, "outputSha256": b.sha256,
          "outputBytes": b.total, "outputHead": b.head.decode("utf-8", errors="replace"), "npm": npm_ev}
    if not (b.kill_verified and b.drained) or b.timed_out or b.rc != 0:
        raise PreflightRefused("deps_install_failed", ev)
    for key, rel in (("vitestEntry", VITEST_ENTRY), ("tscEntry", TSC_ENTRY)):
        p = Path(wt) / rel
        if not p.is_file() or sha256_file(p) != h[key]:
            raise PreflightRefused("deps_entry_mismatch", rel)
    return dict(ev, lockSha256=h["packageLock"])


# --- the structured baseline proof ----------------------------------------------------------------------

def report_cases(report: Any, wt: Path) -> tuple[set[tuple], dict[str, int]]:
    """{(file relative to wt, fullName, status, first line of failureMessages[0] | None)} and the counts."""
    if not isinstance(report, dict) or not isinstance(report.get("testResults"), list):
        raise PreflightRefused("baseline_report_shape")
    root = Path(wt).resolve()
    out: set[tuple] = set()
    for tr in report["testResults"]:
        if not isinstance(tr, dict) or tr.get("message") != "" or not isinstance(tr.get("assertionResults"), list):
            raise PreflightRefused("baseline_suite_error")
        try:
            rel = Path(tr["name"]).resolve().relative_to(root).as_posix()
        except (KeyError, TypeError, ValueError):
            raise PreflightRefused("baseline_report_shape", "a test file outside the worktree") from None
        for a in tr["assertionResults"]:
            fm = a.get("failureMessages") or []
            first = fm[0].splitlines()[0] if fm and isinstance(fm[0], str) and fm[0] else None
            out.add((rel, a.get("fullName"), a.get("status"), first))
    counts = {k: report.get(k) for k in ("numFailedTests", "numPassedTests", "numTotalTests")}
    return out, counts


def baseline(spec: T.TaskSpec, repo: Path, run_root: Path) -> dict[str, Any]:
    """Run the baseline argv at the task base with pristine deps. It must END NORMALLY (verified exit,
    drained, not timed out, not truncated) with expectedExit, and its JSON report must name EXACTLY the
    declared cases (file, fullName, status, failure first line); no suite-level error; counts agree."""
    b0 = spec.doc["oracle"]["baseline"]
    wt = run_root / "baseline"
    add = _git("worktree", "add", "--detach", str(wt), spec.doc["source"]["taskBaseCommit"], cwd=repo)
    if add.returncode != 0:
        raise PreflightRefused("baseline_worktree_failed", git_stderr(add))
    deps = install_deps(spec, wt, run_root)
    check_pins(spec)
    check_tree(spec, wt)
    b = R.run_bounded(list(b0["argv"]), cwd=wt, timeout_s=b0["timeoutS"], keep=b0["reportMaxBytes"], env=W.worker_env(),
                      merge=False, err_keep=ERR_KEEP)
    ev = {"rc": b.rc, "timedOut": b.timed_out, "killVerified": b.kill_verified, "drained": b.drained, "reportSha256": b.sha256,
          "reportBytes": b.total, **stderr_evidence(b), "deps": deps}
    if not (b.kill_verified and b.drained) or b.timed_out:
        raise PreflightRefused("baseline_not_normal", ev)
    if b.total != len(b.head):
        raise PreflightRefused("baseline_report_truncated", ev)
    if b.rc != b0["expectedExit"]:
        raise PreflightRefused("spec_baseline_mismatch", dict(ev, reason="exit"))
    try:
        report = json.loads(b.head.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PreflightRefused("spec_baseline_mismatch", dict(ev, reason="report_not_json")) from None
    got, counts = report_cases(report, wt)
    want = {(c["file"], c["fullName"], c["status"], c["failureFirstLine"]) for c in b0["cases"]}
    n_failed = sum(1 for c in b0["cases"] if c["status"] == "failed")
    if got != want or counts["numFailedTests"] != n_failed or counts["numPassedTests"] != len(b0["cases"]) - n_failed:
        raise PreflightRefused("spec_baseline_mismatch", dict(ev, reason="cases", missing=sorted(map(list, want - got))[:20],
                                                              extra=sorted(map(list, got - want), key=str)[:20]))
    return dict(ev, cases=len(want), failed=n_failed)


def preflight(spec: T.TaskSpec, run_root: Path) -> dict[str, Any]:
    """Everything before any model spend. Writes <run_root>/preflight.json (ok true or the refusal)."""
    run_root = Path(run_root)
    if run_root.exists():
        raise PreflightRefused("run_root_exists")
    run_root.mkdir(parents=True)
    make_npmrc(run_root)
    rec: dict[str, Any] = {"specSha256": spec.sha256, "ok": False}
    try:
        check_pins(spec)
        repo = owned_clone(spec, run_root)
        rec.update(repo=str(repo), head=spec.doc["source"]["taskBaseCommit"], anchor=spec.doc["source"]["anchorCommit"],
                   oracle=spec.doc["oracle"]["files"])
        rec["baseline"] = baseline(spec, repo, run_root)
        rec["ok"] = True
    except PreflightRefused as e:
        rec.update(refused=e.code, detail=e.detail)
    (run_root / PREFLIGHT).write_text(json.dumps(rec, indent=1, sort_keys=True, default=str), encoding="utf-8", newline="\n")
    if not rec["ok"]:
        raise PreflightRefused(rec["refused"], rec.get("detail"))
    return rec


def oracle_outcome(spec: T.TaskSpec, b: R.Bounded, wt: Path) -> tuple[str, str]:
    """The oracle step's decision from its COMPLETE structured report. A report that is missing, truncated
    or not JSON is not evidence either way -> uncertain. A complete report with a nonzero exit -> rejected.
    Exit 0 is accepted only when the reported (file, fullName) set is EXACTLY the declared one, every case
    passed, nothing failed, and no suite-level error is reported. The report is STDOUT ONLY (merge=False)."""
    if b.total != len(b.head):
        return "uncertain", "oracle_report_truncated"
    try:
        report = json.loads(b.head.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "uncertain", "oracle_report_unusable"
    if not isinstance(report, dict) or not isinstance(report.get("testResults"), list):
        return "uncertain", "oracle_report_unusable"
    if b.rc != 0:
        return "rejected", "step_failed:oracle"
    try:
        got, counts = report_cases(report, wt)
    except PreflightRefused as e:
        return "rejected", f"oracle_report_{e.code}"
    status = {(f, n): st for f, n, st, _ in got}
    declared = {(c["file"], c["fullName"]) for c in spec.doc["oracle"]["baseline"]["cases"]}
    if set(status) != declared or len(status) != len(got):
        return "rejected", "oracle_case_set_not_declared"            # exactly the declared identities, as at the baseline
    if counts["numFailedTests"] != 0 or any(status[k] != "passed" for k in declared):
        return "rejected", "oracle_cases_not_all_passed"
    return "accepted", "all_steps_pass"


FAILURE_LINE = re.compile(r"^\s*(?:FAIL|×|✗)\s+\S.*$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
FAILURE_LINES_MAX = 20


def failure_lines(text: str) -> list[str]:
    """Best-effort names of failing tests from RETAINED output (vitest prints `FAIL  <file> > <test>` /
    `× <test>` before its summary): at most 20 lines, each at most 300 chars. Evidence only, never a decision."""
    out = []
    for ln in ANSI.sub("", text).splitlines():
        if FAILURE_LINE.match(ln) and ln.strip() not in out:
            out.append(ln.strip()[:300])
            if len(out) == FAILURE_LINES_MAX:
                break
    return out


def step_log(stem: Path, b: R.Bounded, report: bool) -> dict[str, Any]:
    """Write a verify step's RETAINED output (the bounded prefix run_bounded kept; outputKeepBytes, unchanged)
    to `<stem>.log` beside -- never inside -- the verify worktree, created exclusively, and reference it by
    path and sha256 (msgs 1775/1780). `outputSha256`/`outputBytes` stay the digest/count of the WHOLE stream;
    `logTruncated` says the log holds only a prefix. A report step also gets its stderr prefix as `<stem>.stderr.log`.
    A write failure is recorded (`logError`) and never changes the verdict."""
    ev: dict[str, Any] = {}
    parts = [("log", stem.with_name(stem.name + ".log"), b.head)]
    if report:
        parts.append(("stderrLog", stem.with_name(stem.name + ".stderr.log"), b.err_head))
    for key, path, data in parts:
        try:
            with open(path, "xb") as f:
                f.write(data)
            ev[key] = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        except OSError as e:
            ev.setdefault("logError", []).append({"part": key, "type": type(e).__name__})
    ev["logTruncated"] = b.total != len(b.head)
    ev["failureLines"] = failure_lines(b.head.decode("utf-8", errors="replace")) if b.rc not in (0, None) or b.timed_out else []
    return ev


# --- the spec verifier-------------------------------------------------------------------------------------

@dataclass
class SpecVerifier:
    """accepted | rejected for a CONFIRMED artifact outcome; `uncertain` when the verifier itself cannot
    establish one (an unverifiable view, git/infra failure, deps failure, unconfirmed kill or drain)."""
    spec: T.TaskSpec
    repo: Path
    run_dir_of: Callable[[], Path]
    run_root: Path                                   # holds the owned empty npmrcs (never inside a worktree)

    def __post_init__(self):
        self.reports: list[dict[str, Any]] = []

    def __call__(self, order: P.ReviewOrder) -> P.Verdict:
        rep: dict[str, Any] = {"round": order.round, "artifactRef": order.artifact_ref, "viewDigest": order.view_digest,
                               "candidateDigest": order.candidate_digest, "specSha256": self.spec.sha256}
        self.reports.append(rep)

        def verdict(decision: str, why: str) -> P.Verdict:
            rep.update(decision=decision, why=why)
            return P.Verdict(decision, json.dumps(rep, sort_keys=True, default=str))

        art = order.artifact_ref
        view = R.parse_view(order.view_part, order.view_digest)
        if view is None:
            return verdict("uncertain", "view_unverifiable")
        if view.get("candidateDigest") != order.candidate_digest:
            return verdict("uncertain", "view_candidate_mismatch")
        if not (isinstance(art, str) and W.SHA40.fullmatch(art)) or R.view_artifact(view) != art:
            return verdict("uncertain", "artifact_not_bound_in_view")
        return self.check_artifact(art, order.round, rep, verdict)

    def check_artifact(self, art: str, rnd: int, rep: dict[str, Any], verdict: Callable[[str, str], P.Verdict]) -> P.Verdict:
        """Everything AFTER the view binding: parent, raw diff and oracle blobs (before any artifact code), a
        fresh worktree, fresh deps, every step, the clean tree. Shared by the pilot's review and the
        verifier-only re-check (verify_only), which binds the artifact from a stopped run's records instead."""
        d = self.spec.doc
        base = d["source"]["taskBaseCommit"]
        if not (isinstance(art, str) and W.SHA40.fullmatch(art)):
            return verdict("uncertain", "artifact_not_a_sha")
        parent = _git("rev-parse", f"{art}^", cwd=self.repo)
        if parent.returncode != 0:
            rep["gitStderr"] = git_stderr(parent)
            return verdict("uncertain", "git_failed")
        if parent.stdout.strip() != base:
            return verdict("uncertain", "artifact_parent_not_base")
        try:
            recs = diff_records(self.repo, base, art)
        except PreflightRefused:
            return verdict("uncertain", "git_failed")
        rep["diffRecords"] = [list(r) for r in recs]
        allow = {a["path"] for a in d["allow"]}
        # BEFORE any artifact code runs: exactly allowlisted regular 100644 modifications ...
        if not recs or any(r != ("100644", "100644", "M", r[3]) or r[3] not in allow for r in recs):
            return verdict("rejected", "diff_outside_allowlist")
        # ... and the oracle blobs unchanged AT THE ARTIFACT. (The lockfile cannot be in an allowed diff; on disk,
        # it, the oracle files and the tool entries are re-hashed by check_tree before every spawn.)
        for f in d["oracle"]["files"]:
            if blob_sha256(self.repo, art, f["path"]) != f["sha256"]:
                return verdict("rejected", "oracle_changed")
        wt = Path(self.run_dir_of()) / f"verify-r{rnd}"
        if wt.exists():
            return verdict("uncertain", "verify_worktree_exists")
        add = _git("worktree", "add", "--detach", str(wt), art, cwd=self.repo)
        if add.returncode != 0:
            rep["gitStderr"] = git_stderr(add)
            return verdict("uncertain", "verify_worktree_failed")
        rep["worktree"] = str(wt)
        if _git("rev-parse", "HEAD", cwd=wt).stdout.strip() != art:   # the verified tree IS the artifact commit
            return verdict("uncertain", "verify_head_not_artifact")
        try:                                                        # pristine deps, AFTER the worker exited
            rep["deps"] = install_deps(self.spec, wt, self.run_root)
        except PreflightRefused as e:
            rep["depsRefused"] = [e.code, e.detail]
            return verdict("uncertain", "verify_deps_failed")
        steps = []
        rep["steps"] = steps
        oracle = ("uncertain", "oracle_not_run")
        for s in d["verify"]["steps"]:
            try:
                check_pins(self.spec)                               # pinned tools and immutable inputs, before EVERY spawn
                check_tree(self.spec, wt)
            except PreflightRefused as e:
                rep["integrity"] = [e.code, e.detail]
                return verdict("uncertain", f"integrity_before:{s['name']}")
            report = s["name"] == "oracle"                         # stdout alone is the structured report
            try:
                b = R.run_bounded(list(s["argv"]), cwd=wt, timeout_s=s["timeoutS"], keep=s["outputKeepBytes"], env=W.worker_env(),
                                  merge=not report, err_keep=ERR_KEEP)
            except OSError:
                return verdict("uncertain", "verify_spawn_failed")
            steps.append({"name": s["name"], "rc": b.rc, "timedOut": b.timed_out, "killVerified": b.kill_verified,
                          "drained": b.drained, "outputSha256": b.sha256, "outputBytes": b.total,
                          "outputTail": b.head.decode("utf-8", errors="replace")[-400:], **(stderr_evidence(b) if report else {}),
                          **step_log(Path(self.run_dir_of()) / f"verify-r{rnd}-{s['name']}", b, report)})
            if not b.kill_verified:
                return verdict("uncertain", "verify_kill_unconfirmed")
            if not b.drained:
                return verdict("uncertain", "verify_not_drained")
            if b.timed_out:
                return verdict("rejected", f"step_timeout:{s['name']}")
            if s["name"] == "oracle":
                oracle = oracle_outcome(self.spec, b, wt)
                if oracle[0] != "accepted":
                    return verdict(*oracle)
            elif b.rc != 0:
                return verdict("rejected", f"step_failed:{s['name']}")
        status = _git("status", "--porcelain", "--untracked-files=all", "--ignored=matching", cwd=wt)
        if status.returncode != 0:
            rep["gitStderr"] = git_stderr(status)
            return verdict("uncertain", "git_failed")
        lines = status.stdout.splitlines()
        rep["status"] = lines[:20]
        if lines != CLEAN_STATUS:
            return verdict("rejected", "verify_tree_dirty")
        try:
            check_tree(self.spec, wt)
        except PreflightRefused as e:
            rep["integrity"] = [e.code, e.detail]
            return verdict("uncertain", "integrity_after_steps")
        return verdict(*oracle)


# --- the run --------------------------------------------------------------------------------------------

def load_preflight(spec: T.TaskSpec, run_root: Path) -> dict[str, Any]:
    p = Path(run_root) / PREFLIGHT
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PreflightRefused("preflight_missing") from None
    if rec.get("ok") is not True or rec.get("specSha256") != spec.sha256:
        raise PreflightRefused("preflight_not_passing_for_this_spec")
    repo = (Path(run_root) / "repo").resolve()
    if not isinstance(rec.get("repo"), str) or Path(rec["repo"]).resolve() != repo:
        raise PreflightRefused("preflight_repo_not_owned")             # only the run root's OWN clone, never a recorded path
    if _git("rev-parse", "HEAD", cwd=repo).stdout.strip() != spec.doc["source"]["taskBaseCommit"]:
        raise PreflightRefused("clone_moved")
    return dict(rec, repo=str(repo))


def run(spec: T.TaskSpec, run_root: Path, *, executable: tuple[Path, ...], executable_sha256: str, setup, client, aj,
        project_id: str, execution_kind: str, root_go: str | None, task_suffix: str = "",
        timeouts: tuple[int, int, int] = (1200, 600, 300)) -> tuple[P.PilotResult, dict[str, Any]]:
    """ONE supervised pilot on the owned clone. Requires a passing preflight of THIS spec."""
    rec = load_preflight(spec, run_root)
    check_pins(spec)
    repo, d = Path(rec["repo"]), spec.doc
    params = R.RealParams(executable, executable_sha256, model=d["worker"]["model"], max_budget_usd=d["worker"]["budgetUsd"],
                          max_rounds=d["worker"]["maxRounds"], max_turns=d["worker"]["maxTurns"], total_timeout_s=timeouts[0],
                          first_output_timeout_s=timeouts[1], inactivity_timeout_s=timeouts[2],
                          test_command=d["worker"]["testCommand"])
    try:
        R.validate_params(params)
        R.check_executable(params)
    except R.RunnerRefused as e:
        raise PreflightRefused(e.code) from None
    cfgp = P.PilotConfig(repo=repo, base=d["source"]["taskBaseCommit"], workspace=Path(run_root), project_id=project_id,
                         task_text=d["task"]["text"] + task_suffix, criteria=d["task"]["criteria"], max_rounds=d["worker"]["maxRounds"],
                         run_id=uuid.uuid4().hex[:12])
    resolved = P.resolve(cfgp)
    holder: dict[str, Any] = {}

    def worker(order: P.WorkOrder) -> P.WorkReport:
        if R.sha256_file(executable[-1]) != executable_sha256:
            return P.WorkReport("failed", reason="executable_hash_mismatch")
        if "w" not in holder:
            cfg = R.adapter_config(params, repo=repo, base_sha=d["source"]["taskBaseCommit"], run_id=cfgp.run_id,
                                   run_dir=resolved.run_dir.resolve())
            # the worker's OWN dependencies, installed after `worktree add` and before launch (the prepare hook)
            holder["w"] = W.CliWorker(W.CliConfig(**{**cfg.__dict__, "prepare": lambda wt: install_deps(spec, wt, root)}))
        return holder["w"](order)

    root = Path(run_root).resolve()
    verifier = SpecVerifier(spec, repo, lambda: resolved.run_dir.resolve(), root)
    pilot = P.Pilot(resolved, setup, client, aj, worker=worker, reviewer=verifier, execution_kind=execution_kind)
    result = pilot.run()
    evidence: dict[str, Any] = {"rootGo": root_go, "specSha256": spec.sha256, "preflight": rec, "repo": str(repo),
                                "runDir": str(resolved.run_dir), "outcome": result.outcome, "reason": result.reason,
                                "detail": result.detail, "verifier": verifier.reports, "databaseRetained": False, "errors": []}

    def best_effort(part: str, fn: Callable[[], Any]) -> None:
        try:
            evidence[part] = fn()
        except Exception as e:  # noqa: BLE001 -- evidence is best effort; each failure is recorded by part and type
            evidence["errors"].append({"part": part, "type": type(e).__name__})

    best_effort("adapter", lambda: {str(k): vars(v) for k, v in (holder["w"].evidence.items() if "w" in holder else [])})
    best_effort("records", lambda: {r.claim_key: pilot.records_of(r.claim_key) for r in result.rounds})
    best_effort("ownedRefs", lambda: _git("for-each-ref", "--format=%(objectname) %(refname)", W.REF_ROOT,
                                          cwd=repo).stdout.splitlines())
    best_effort("journal", lambda: R.journal_dump(aj.dsn, pilot.root))
    (resolved.run_dir / "evidence.json").write_text(json.dumps(evidence, indent=1, sort_keys=True, default=str), encoding="utf-8",
                                                     newline="\n")
    return result, evidence


# --- verifier-only re-check of a stopped run's artifact (msg 1704 option 1) ---------------------------------

VERIFY_EVIDENCE = "verify-evidence.json"


def verify_only(spec: T.TaskSpec, run_root: Path, pilot_dir: Path, out: Path, *, root_go: str) -> dict[str, Any]:
    """ONE fresh verifier check of the artifact of a run that STOPPED WITHOUT A DECISION (review_uncertain).
    No model, no harness, no journal: the original run.json/evidence.json are only READ, bound by sha256,
    and never changed; the original needs_operator outcome stands. The artifact is bound from those records
    (the last round's artifact/view/candidate digests, which the original verifier's view binding already
    checked, the captured result and the run-owned ref). Fresh worktree under `out`, fresh deps, every step.
    Writes <out>/verify-evidence.json; refuses typed before any effect when a binding does not hold."""
    run_root, pilot_dir, out = Path(run_root).resolve(), Path(pilot_dir).resolve(), Path(out).resolve()
    rec = load_preflight(spec, run_root)
    repo = Path(rec["repo"])
    if pilot_dir.parent != run_root or out.parent != run_root:
        raise PreflightRefused("verify_paths_not_in_run_root")
    if out.exists():
        raise PreflightRefused("verify_out_exists")
    try:
        run_raw, ev_raw = (pilot_dir / "run.json").read_bytes(), (pilot_dir / "evidence.json").read_bytes()
        run, ev = json.loads(run_raw), json.loads(ev_raw)
    except (OSError, ValueError):
        raise PreflightRefused("verify_records_unreadable") from None
    try:
        last = run["rounds"][-1]
        prior = ev["verifier"][-1]
        captured = [json.loads(j["data"]) for j in ev["journal"]
                    if j["kind"] == "result_captured" and j["claimKey"] == last["claim_key"]]
    except (KeyError, IndexError, TypeError, ValueError):
        raise PreflightRefused("verify_records_shape") from None
    art = last["artifact_ref"]
    ref = f"{W.REF_ROOT}/{run['runId']}/r{last['round']}"
    checks = {
        "run_stopped_without_decision": run.get("outcome") == "needs_operator" and run.get("reason") == "review_uncertain"
                                        and last.get("decision") is None,
        "same_spec": ev.get("specSha256") == spec.sha256 and prior.get("specSha256") == spec.sha256,
        "same_base": run.get("baseSha") == spec.doc["source"]["taskBaseCommit"],
        "prior_review_binds_round": (prior.get("round"), prior.get("artifactRef"), prior.get("viewDigest"),
                                     prior.get("candidateDigest"), prior.get("decision"))
                                    == (last["round"], art, last["view_digest"], last["candidate_digest"], "uncertain"),
        # The ONE supported checkpoint (msgs 1703/1718): the original stopped at verify_worktree_failed, i.e. AFTER its
        # view/candidate/artifact binding and the raw diff read (diffRecords is written only then). Any other stop refuses.
        "prior_review_passed_binding": prior.get("why") == "verify_worktree_failed"
                                       and isinstance(prior.get("diffRecords"), list) and bool(prior["diffRecords"]),
        "receipt_binds_artifact": len(captured) == 1 and captured[0].get("artifactRef") == art
                                  and captured[0].get("parentRef") == spec.doc["source"]["taskBaseCommit"]
                                  and captured[0].get("runRef") == ref,
        "owned_ref_binds_artifact": _git("rev-parse", "--verify", "-q", ref, cwd=repo).stdout.strip() == art,
    }
    if not all(checks.values()):
        raise PreflightRefused("verify_binding_failed", checks)
    longpaths = _git("config", "--local", "--get", "core.longpaths", cwd=repo).stdout.strip()
    out.mkdir()
    rep: dict[str, Any] = {"mode": "verifier-only", "round": last["round"], "artifactRef": art, "viewDigest": last["view_digest"],
                           "candidateDigest": last["candidate_digest"], "specSha256": spec.sha256}

    def verdict(decision: str, why: str) -> P.Verdict:
        rep.update(decision=decision, why=why)
        return P.Verdict(decision, json.dumps(rep, sort_keys=True, default=str))

    SpecVerifier(spec, repo, lambda: out, run_root).check_artifact(art, last["round"], rep, verdict)
    record = {"mode": "verifier-only", "rootGo": root_go, "specSha256": spec.sha256, "preflightOk": True,
              "label": "artifact re-verification using the recorded binding; NOT a replay or recovery of the original "
                       "review (H1/PlanStore) decision",
              "trustBasis": "the original verifier's preserved report, which passed the view/candidate/artifact binding and "
                            "read the raw diff before it stopped uncertain; the run's result_captured receipt and run-owned "
                            "ref; and this re-check's own parent, diff, oracle-blob, deps and step checks on the artifact. "
                            "The raw review view bytes were not preserved and were not re-parsed or re-created.",
              "bound": {"pilotDir": str(pilot_dir), "runJsonSha256": hashlib.sha256(run_raw).hexdigest(),
                        "evidenceJsonSha256": hashlib.sha256(ev_raw).hexdigest(), "runId": run["runId"], "claimKey": last["claim_key"],
                        "artifactRef": art, "runRef": ref, "viewDigest": last["view_digest"], "candidateDigest": last["candidate_digest"],
                        "receipt": captured[0], "priorReview": prior, "checks": checks},
              "cloneCoreLongpaths": longpaths or None,
              "originalOutcome": {"outcome": run["outcome"], "reason": run["reason"], "unchanged": True},
              "report": rep, "decision": rep.get("decision"), "why": rep.get("why")}
    (out / VERIFY_EVIDENCE).write_text(json.dumps(record, indent=1, sort_keys=True, default=str), encoding="utf-8", newline="\n")
    return record


def main(argv: list[str], *, harness_factory: Callable[[], Any] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="task_runner")
    ap.add_argument("command", choices=("plan", "preflight", "run", "verify"))
    ap.add_argument("--pilot-dir", type=Path, help="verify: the stopped run's pilot-<id> directory under the run root")
    ap.add_argument("--out", type=Path, help="verify: a NEW directory under the run root for the fresh evidence")
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--run-root", type=Path)
    ap.add_argument("--exe", type=Path)
    ap.add_argument("--exe-arg", type=Path, help="offline tests only: the fake CLI")
    ap.add_argument("--exe-sha256")
    ap.add_argument("--launch-real-model", action="store_true")
    ap.add_argument("--root-go")
    a = ap.parse_args(argv)
    try:
        spec = T.load(a.spec)
    except T.SpecRefused as e:
        print(f"REFUSED {e.code}")
        return 2
    d = spec.doc
    summary = {"specSha256": spec.sha256, "issue": d["metadata"]["issue"], "taskBaseCommit": d["source"]["taskBaseCommit"],
               "anchorCommit": d["source"]["anchorCommit"], "allow": [x["path"] for x in d["allow"]],
               "oracle": [x["path"] for x in d["oracle"]["files"]], "baselineCases": len(d["oracle"]["baseline"]["cases"]),
               "verifySteps": [s["name"] for s in d["verify"]["steps"]], "worker": d["worker"]}
    print(json.dumps(summary, indent=1))
    if a.command == "plan":
        print("PLAN ONLY: no clone, install, test or spawn.")
        return 0
    if a.run_root is None:
        print("REFUSED run_root_required")
        return 2
    if a.command == "preflight":
        try:
            rec = preflight(spec, a.run_root.resolve())
        except PreflightRefused as e:
            print(f"REFUSED {e.code}")
            return 1
        print(json.dumps({"preflight": "ok", "baseline": rec["baseline"]["cases"], "failed": rec["baseline"]["failed"]}))
        return 0
    if a.command == "verify":
        if not (a.pilot_dir and a.out and a.root_go):
            print("REFUSED verify_needs --pilot-dir --out --root-go")
            return 2
        try:
            rec = verify_only(spec, a.run_root, a.pilot_dir, a.out, root_go=a.root_go)
        except PreflightRefused as e:
            print(f"REFUSED {e.code} {json.dumps(e.detail, default=str)}")
            return 1
        print(json.dumps({"decision": rec["decision"], "why": rec["why"], "evidence": str(Path(a.out).resolve() / VERIFY_EVIDENCE)}))
        return 0 if rec["decision"] == "accepted" else 1
    if not (a.launch_real_model and a.root_go and a.exe and a.exe_sha256):
        print("REFUSED run_needs --exe --exe-sha256 --launch-real-model --root-go")
        return 2
    from e1.acts import E2C_BOUNDS
    from e1.acts_durable import ActsJournal, install_acts
    from e1.durable import install
    from e1.handoff_durable import install_handoff
    from e1.wire import SetupClient, SupervisorClient
    if harness_factory is None:
        from e1.harness import Harness
        harness_factory = Harness
    h = harness_factory()
    h.start()
    ok = False
    try:
        install(h.dsn, E2C_BOUNDS)
        install_acts(h.dsn)
        install_handoff(h.dsn)
        aj = ActsJournal(h.dsn, f"task-runner#{uuid.uuid4().hex[:8]}", now=1000.0).open()
        try:
            command = (a.exe.resolve(),) + ((a.exe_arg.resolve(),) if a.exe_arg else ())
            res, ev = run(spec, a.run_root.resolve(), executable=command, executable_sha256=a.exe_sha256,
                          setup=SetupClient(h.base_url), client=SupervisorClient(h.base_url), aj=aj, project_id=h.project_id,
                          execution_kind="fake-cli" if a.exe_arg else "claude-cli", root_go=a.root_go)
        finally:
            aj.close()
        print(json.dumps({"outcome": res.outcome, "reason": res.reason, "detail": res.detail, "rounds": len(res.rounds),
                          "evidence": str(Path(ev["runDir"]) / "evidence.json")}, default=str))
        ok = res.outcome == "accepted"
    except PreflightRefused as e:
        print(f"REFUSED {e.code}")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR {type(e).__name__}")
    finally:
        problems = h.stop(keep_work=not ok)
        if problems:
            print("HARNESS " + "; ".join(problems))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
