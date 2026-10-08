"""HK-ISSUE-007 pilot runner: ONE supervised real-worker run on a DISPOSABLE task, composed from the
accepted pilot driver (e1/pilot.py), the real-worker adapter (e1/cli_worker.py) and an independent,
deterministic verifier. TEST-SCOPED. Offline tests drive it with the fake CLI. A real model run is the
operator entrypoint `main(... --launch-real-model --root-go <msg>)` and needs a root GO on the reported
parameters (msgs 1500, 1510); nothing here launches a model by itself.

- Every parameter is validated BEFORE anything is written (typed RunnerRefused, no writes).
- The task repo is a THROWAWAY git repo under the run root (never the Hekate checkout): calc.py with
  `add()` unimplemented and stdlib unittest tests; its single commit is the base.
- The executable is pinned by sha256 and re-checked immediately before EVERY spawn.
- The verifier (the pilot's "fresh reviewer") sees only the ReviewOrder. It parses the verified view
  part, checks its digest, and requires the artifact AT THE REVIEW IDENTITY of the view (a structured
  binding, not a substring), checks out the artifact in its OWN fresh detached worktree, enforces a
  diff allowlist, runs the tests with bytecode writing disabled, bounded streamed output and a
  timeout with a verified tree kill, requires the verification tree to be clean afterwards, and
  decides accepted/rejected with a digest of the evidence.
- Every non-ok or ambiguous outcome stops as needs_operator. Evidence (run.json, evidence.json with
  the full journal records, worktrees and refs/hekate-pilot/<runId>/r<n> in the throwaway repo) is
  retained until an explicit operator cleanup.
- Limitation (register HK-ISSUE-007): the tree kill is proven only for a LIVE parent. For a worker
  parent that already exited, `tree_kill` returns True and surviving descendants are UNOBSERVABLE in
  the current scope: neither detected nor contained.
- Model provenance: the requested alias (launch_intent) and the CLI-REPORTED model identifiers
  (exited/result_captured `reportedModels`, from system/init and assistant.message.model), kept
  separate. These are what the CLI states, not an authenticated claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from e1 import cli_worker as W
from e1 import pilot as P

TASK_TEXT = ("Implement add(a, b) in calc.py so that `python -B -m unittest -q` passes (run exactly that command to check). "
             "Do not edit test_calc.py or add files.")
CRITERIA = "python -B -m unittest -q passes; only calc.py changes (mode 100644)"
DIFF_ALLOWLIST = ("calc.py",)
TEST_ARGV = ("-B", "-m", "unittest", "-q")       # run with the verifier's own absolute Python; -B: no bytecode files
VERIFY_TIMEOUT_S = 120
VERIFY_OUTPUT_KEEP = 64 << 10
HEX64 = re.compile(r"^[0-9a-f]{64}$")

CALC = "def add(a, b):\n    raise NotImplementedError\n"
TESTS = ("import unittest\n\nfrom calc import add\n\n\nclass AddTest(unittest.TestCase):\n"
         "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
         "    def test_negative(self):\n        self.assertEqual(add(-1, 1), 0)\n\n\n"
         "if __name__ == \"__main__\":\n    unittest.main()\n")


class RunnerRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Every runner/verifier git call uses the adapter's CONTROLLED git environment (no system or global config)."""
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120, env=W.git_env())


def diff_records(raw: str) -> list[tuple[str, str, str, str]] | None:
    """`git diff --raw -z --no-renames` as (old_mode, new_mode, status, path) records; None if malformed.
    NUL-delimited, so a path with spaces or newlines is one exact path, never split."""
    parts = raw.split("\0")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if len(parts) % 2:
        return None
    out = []
    for meta, path in zip(parts[0::2], parts[1::2]):
        f = meta.split(" ")
        if len(f) != 5 or not f[0].startswith(":"):
            return None
        out.append((f[0][1:], f[1], f[4], path))
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- parameters (validated before ANY write) -----------------------------------------------------------

@dataclass(frozen=True)
class RealParams:
    executable: tuple[Path, ...]          # (claude.exe,) for a real run; (python, fake_cli.py) offline
    executable_sha256: str                # of executable[-1], re-checked right before every spawn
    model: str = "sonnet"
    max_budget_usd: str = "1.00"          # per round
    max_rounds: int = 2
    max_turns: int = 20
    total_timeout_s: int = 1200
    first_output_timeout_s: int = 600
    inactivity_timeout_s: int = 300
    test_command: str = "python -B -m unittest -q"   # the worker's auto-approved test run: no bytecode files


def adapter_config(params: RealParams, *, repo: Path, base_sha: str, run_id: str, run_dir: Path,
                   execution_kind: str = "claude-cli") -> W.CliConfig:
    return W.CliConfig(command=params.executable, repo=repo, base_sha=base_sha, run_id=run_id, run_dir=run_dir,
                       model=params.model, max_budget_usd=params.max_budget_usd, max_turns=params.max_turns,
                       total_timeout_s=params.total_timeout_s, first_output_timeout_s=params.first_output_timeout_s,
                       inactivity_timeout_s=params.inactivity_timeout_s, test_command=params.test_command,
                       execution_kind=execution_kind)


def validate_params(params: RealParams) -> None:
    """The COMPLETE parameter set, with no writes: the adapter's own validation on placeholder paths
    that already exist, plus the runner's bounds and the pinned hash."""
    if not isinstance(params, RealParams):
        raise RunnerRefused("params")
    if not (isinstance(params.executable_sha256, str) and HEX64.fullmatch(params.executable_sha256)):
        raise RunnerRefused("exe_hash_required", "a lowercase sha256 of the executable is required")
    if not (isinstance(params.max_rounds, int) and not isinstance(params.max_rounds, bool) and 1 <= params.max_rounds <= 2):
        raise RunnerRefused("rounds", "the pilot allows 1 or 2 rounds")
    if not (isinstance(params.executable, tuple) and params.executable and all(isinstance(p, Path) for p in params.executable)):
        raise RunnerRefused("config_command")
    here = params.executable[-1].parent if params.executable[-1].is_absolute() else Path.cwd()
    try:
        W.validate(adapter_config(params, repo=here.resolve(), base_sha="0" * 40, run_id="validate", run_dir=here.resolve()))
    except W.CliRefused as e:
        raise RunnerRefused(e.code, str(e)) from None


def check_executable(params: RealParams) -> None:
    """The pinned sha256 of executable[-1]; a typed refusal before any spawn, write or harness start."""
    try:
        got = sha256_file(params.executable[-1])
    except OSError:
        raise RunnerRefused("exe_unreadable") from None
    if got != params.executable_sha256:
        raise RunnerRefused("exe_hash_mismatch", got)


def create_task_repo(root: Path) -> tuple[Path, str]:
    """The disposable task repo (one base commit). Refuses to reuse an existing path."""
    repo = root / "repo"
    if repo.exists():
        raise RunnerRefused("task_repo_exists")
    repo.mkdir(parents=True)
    if _git("init", "-q", "-b", "main", cwd=repo).returncode != 0:
        raise RunnerRefused("task_repo_init")
    (repo / "calc.py").write_text(CALC, encoding="utf-8", newline="\n")
    (repo / "test_calc.py").write_text(TESTS, encoding="utf-8", newline="\n")
    _git("add", "-A", cwd=repo)
    c = _git("-c", "user.name=hekate-pilot", "-c", "user.email=hekate-pilot@hekate.local", "-c", "commit.gpgsign=false",
             "commit", "-qm", "pilot task base", cwd=repo)
    if c.returncode != 0:
        raise RunnerRefused("task_repo_commit")
    return repo, _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


# --- bounded test execution --------------------------------------------------------------------------------

@dataclass
class Bounded:
    rc: int | None
    timed_out: bool
    kill_verified: bool
    drained: bool
    head: bytes
    total: int
    sha256: str
    # merge=False only: stderr, bounded and digested on its own (head/total/sha256 are then stdout alone)
    err_head: bytes = b""
    err_total: int = 0
    err_sha256: str = ""


def run_bounded(argv: list[str], *, cwd: Path, timeout_s: int, keep: int, env: dict[str, str], merge: bool = True,
                err_keep: int = 4096) -> Bounded:
    """stdout+stderr merged and STREAMED: a kept prefix of `keep` bytes plus a digest and count of all of
    it. On timeout the process tree is killed and the reader joined. merge=False keeps stdout clean for a
    structured report: stderr gets its own reader (prefix of `err_keep`, digest, count); `drained` then
    means BOTH readers finished."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                            env=env, creationflags=flags, start_new_session=(sys.platform != "win32"))

    def reader(stream, cap: int):
        head, digest, total = bytearray(), hashlib.sha256(), [0]

        def read() -> None:
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total[0] += len(chunk)
                    room = cap - len(head)
                    if room > 0:
                        head.extend(chunk[:room])
            except (OSError, ValueError):
                pass

        t = threading.Thread(target=read, daemon=True)
        t.start()
        return t, head, digest, total

    out = reader(proc.stdout, keep)
    err = None if merge else reader(proc.stderr, err_keep)
    timed_out, verified = False, True
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        verified = W.tree_kill(proc, 20)
    threads = [out[0]] + ([err[0]] if err else [])
    for t in threads:
        t.join(timeout=20)
    b = Bounded(proc.returncode, timed_out, verified, not any(t.is_alive() for t in threads), bytes(out[1]), out[3][0],
                out[2].hexdigest())
    if err:
        b.err_head, b.err_total, b.err_sha256 = bytes(err[1]), err[3][0], err[2].hexdigest()
    return b


# --- the independent verifier ------------------------------------------------------------------------------

def parse_view(part: str, want_digest: str) -> dict[str, Any] | None:
    """The verified view from its emitted part: `<<handoff-view …>>\\n{canonical body}\\n<<viewDigest d>>\\n`.
    The body must hash to both the emitted digest and the ReviewOrder's view digest."""
    lines = part.split("\n")
    if len(lines) != 4 or not lines[0].startswith("<<handoff-view ") or lines[3] != "":
        return None
    m = re.fullmatch(r"<<viewDigest ([0-9a-f]{64})>>", lines[2])
    body = lines[1]
    if not m or hashlib.sha256(body.encode("utf-8")).hexdigest() != m.group(1) or m.group(1) != want_digest:
        return None
    try:
        view = json.loads(body)
    except ValueError:
        return None
    return view if isinstance(view, dict) else None


def view_artifact(view: dict[str, Any]) -> str | None:
    try:
        return view["mandatory"]["state"]["mandatory"]["identity"]["artifactRef"]
    except (KeyError, TypeError):
        return None


@dataclass
class Verifier:
    """A deterministic pilot reviewer. Bounds: one fresh worktree per round, the test timeout, a kept
    output prefix (+ a full digest), the DIFF_ALLOWLIST, and a clean verification tree afterwards."""
    repo: Path
    base_sha: str
    run_dir_of: Callable[[], Path]        # the pilot's run directory (exists once the run started)
    python: Path = field(default_factory=lambda: Path(sys.executable).resolve())
    timeout_s: int = VERIFY_TIMEOUT_S
    keep: int = VERIFY_OUTPUT_KEEP

    def __post_init__(self):
        self.reports: list[dict[str, Any]] = []

    def __call__(self, order: P.ReviewOrder) -> P.Verdict:
        """accepted | rejected for a CONFIRMED outcome of the artifact (tests pass / fail / time out under a
        verified kill, a diff outside the allowlist, a dirty tree). `uncertain` when the verifier itself cannot
        establish a verdict: an unverifiable or unbound view, a git or spawn failure, an unconfirmed kill or
        an undrained reader. The pilot stops on `uncertain` without a decision or a retry (msg 1517)."""
        rep: dict[str, Any] = {"round": order.round, "artifactRef": order.artifact_ref, "viewDigest": order.view_digest,
                               "candidateDigest": order.candidate_digest}
        self.reports.append(rep)

        def verdict(decision: str, why: str) -> P.Verdict:
            rep.update(decision=decision, why=why)
            return P.Verdict(decision, json.dumps(rep, sort_keys=True))

        art = order.artifact_ref
        view = parse_view(order.view_part, order.view_digest)
        if view is None:
            return verdict("uncertain", "view_unverifiable")
        if view.get("candidateDigest") != order.candidate_digest:
            return verdict("uncertain", "view_candidate_mismatch")
        if not (isinstance(art, str) and W.SHA40.fullmatch(art)) or view_artifact(view) != art:
            return verdict("uncertain", "artifact_not_bound_in_view")
        parent = _git("rev-parse", f"{art}^", cwd=self.repo)
        if parent.returncode != 0:
            return verdict("uncertain", "git_failed")
        if parent.stdout.strip() != self.base_sha:
            return verdict("uncertain", "artifact_parent_not_base")
        diff = _git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", self.base_sha, art, cwd=self.repo)
        if diff.returncode != 0:
            return verdict("uncertain", "git_failed")
        records = diff_records(diff.stdout)
        if records is None:
            return verdict("uncertain", "git_failed")
        rep["files"] = [r[3] for r in records]
        rep["diffRecords"] = [list(r) for r in records]
        # EXACTLY one record: a MODIFICATION of the regular file calc.py, mode 100644 before and after.
        # A symlink (120000), gitlink (160000), mode change (100755), add, delete, or any other path
        # (incl. "calc.py calc.py") is refused. Checked BEFORE any artifact code runs.
        if records != [("100644", "100644", "M", "calc.py")]:
            return verdict("rejected", "diff_outside_allowlist")
        wt = Path(self.run_dir_of()) / f"verify-r{order.round}"
        if wt.exists() or _git("worktree", "add", "--detach", str(wt), art, cwd=self.repo).returncode != 0:
            return verdict("uncertain", "verify_worktree_failed")
        rep["worktree"] = str(wt)
        env = W.worker_env()                                         # includes PYTHONDONTWRITEBYTECODE=1
        try:
            b = run_bounded([str(self.python), *TEST_ARGV], cwd=wt, timeout_s=self.timeout_s, keep=self.keep, env=env)
        except OSError:
            return verdict("uncertain", "verify_spawn_failed")
        rep.update(rc=b.rc, timedOut=b.timed_out, killVerified=b.kill_verified, drained=b.drained, outputSha256=b.sha256,
                   outputBytes=b.total, outputKeptBytes=len(b.head), outputTail=b.head.decode("utf-8", errors="replace")[-400:])
        if not b.kill_verified:
            return verdict("uncertain", "verify_kill_unconfirmed")
        if not b.drained:
            return verdict("uncertain", "verify_not_drained")
        if b.timed_out:
            return verdict("rejected", "tests_timeout")
        # Clean means nothing tracked changed AND nothing untracked OR IGNORED appeared (msg 1545).
        status = _git("status", "--porcelain", "--untracked-files=all", "--ignored", cwd=wt)
        if status.returncode != 0:
            return verdict("uncertain", "git_failed")
        rep["treeClean"] = not status.stdout.strip()
        if status.stdout.strip():
            return verdict("rejected", "verify_tree_dirty")
        return verdict("accepted", "tests_pass") if b.rc == 0 else verdict("rejected", "tests_fail")


# --- the runner -----------------------------------------------------------------------------------------

def plan(params: RealParams) -> dict[str, Any]:
    """Everything the root reviews before a launch: exe, version, argv, limits, verifier bounds, allowlist.
    The pinned hash is checked BEFORE the executable is ever spawned (even for --version)."""
    validate_params(params)
    check_executable(params)
    exe = params.executable[-1]
    cfg = adapter_config(params, repo=exe.parent.resolve(), base_sha="0" * 40, run_id="plan", run_dir=exe.parent.resolve())
    ver = subprocess.run([*map(str, params.executable), "--version"], capture_output=True, text=True, timeout=60)
    return {"executable": [str(p) for p in params.executable], "executableSha256": sha256_file(exe),
            "expectedSha256": params.executable_sha256, "version": ver.stdout.strip(),
            "argv": W.build_command(cfg)[len(params.executable):], "model": params.model,
            "runtimeModelProvenance": ("requested alias in launch_intent.command and exited/result_captured.requestedModel; "
                                       "CLI-REPORTED identifiers in exited/result_captured.reportedModels (system/init model, "
                                       "assistant.message.model; <= 8, pattern-checked; not authenticated)"),
            "toolSurface": {"exposed": "--tools " + ",".join(list(cfg.allowed_tools) + (["Bash"] if cfg.test_command else [])),
                            "autoApproved": list(cfg.allowed_tools) + ([f"Bash({cfg.test_command})"] if cfg.test_command else []),
                            "restricted": cfg.restricted},
            "budgetPerRoundUsd": params.max_budget_usd, "maxRounds": params.max_rounds, "maxTurns": params.max_turns,
            "timeoutsS": {"total": params.total_timeout_s, "firstOutput": params.first_output_timeout_s,
                          "inactivity": params.inactivity_timeout_s},
            "verifier": {"testArgv": ["<abs python>", *TEST_ARGV], "timeoutS": VERIFY_TIMEOUT_S, "outputKeepBytes": VERIFY_OUTPUT_KEEP,
                         "diffAllowlist": list(DIFF_ALLOWLIST), "requiresCleanTree": True, "bindsArtifactAt": "view.mandatory.state.mandatory.identity.artifactRef"},
            "envAllowlist": list(W.ENV_ALLOW), "task": TASK_TEXT, "criteria": CRITERIA}


def journal_dump(dsn: str, root: str) -> list[dict[str, Any]]:
    """Every journal record of the pilot plan's streams (the disposable DB is dropped after a run)."""
    import psycopg
    with psycopg.connect(dsn) as c:
        return [{"claimKey": r[0], "seq": r[1], "kind": r[2], "data": r[3]} for r in c.execute(
            "SELECT claim_key, seq, kind, data FROM supervisor_journal.records WHERE root = %s ORDER BY claim_key, seq", (root,))]


def run(params: RealParams, run_root: Path, *, setup, client, aj, project_id: str, execution_kind: str,
        task_suffix: str = "", root_go: str | None = None, export: bool = False,
        review_source: P.ReviewTaskSource | None = None, export_test_only: bool = False) -> tuple[P.PilotResult, dict]:
    """One pilot run. `task_suffix` is for offline tests only (the fake CLI's scenario line).

    export=True (plan 1532, opt-in): the review task comes from the PINNED real H1 (RealH1ReviewSource) and
    every round's composition is published as one handoff-export.v0 under <run_root>/exports, with
    provenance DERIVED by the Exporter. `review_source` / `export_test_only` exist for offline tests only
    (an injected stand-in H1 is labelled injected and needs test_only)."""
    validate_params(params)
    check_executable(params)                                       # before ANY write
    if run_root.exists():
        raise RunnerRefused("run_root_exists")
    run_root.mkdir(parents=True)
    repo, base = create_task_repo(run_root)
    cfgp = P.PilotConfig(repo=repo, base=base, workspace=run_root, project_id=project_id, task_text=TASK_TEXT + task_suffix,
                         criteria=CRITERIA, max_rounds=params.max_rounds, run_id=uuid.uuid4().hex[:12])
    resolved = P.resolve(cfgp)
    holder: dict[str, Any] = {}

    def worker(order: P.WorkOrder) -> P.WorkReport:
        if sha256_file(params.executable[-1]) != params.executable_sha256:      # re-checked before EVERY spawn
            return P.WorkReport("failed", reason="executable_hash_mismatch")
        if "w" not in holder:
            holder["w"] = W.CliWorker(adapter_config(params, repo=repo, base_sha=base, run_id=cfgp.run_id,
                                                     run_dir=resolved.run_dir.resolve(), execution_kind=execution_kind))
        return holder["w"](order)

    verifier = Verifier(repo, base, lambda: resolved.run_dir.resolve())
    exporter = None
    if export:
        from e1 import pilot_export as PE                      # the opt-in path only
        review_source = review_source if review_source is not None else PE.RealH1ReviewSource()
        (run_root / "exports").mkdir()
        exporter = PE.Exporter(out_root=run_root / "exports", run_id=cfgp.run_id, hekate_repo=Path(__file__).resolve().parent,
                               dsn=aj.dsn, execution_kind=execution_kind, source=review_source, test_only=export_test_only)
    pilot = P.Pilot(resolved, setup, client, aj, worker=worker, reviewer=verifier, execution_kind=execution_kind,
                    review_source=review_source, exporter=exporter)
    result = pilot.run()                    # never raises: unexpected failures become a typed needs_operator stop
    errors: list[dict[str, str]] = []

    def best_effort(name: str, fn: Callable[[], Any]) -> Any:
        """Evidence is collected BEST EFFORT: a failing part is recorded by exception TYPE only, and the
        rest is still written. Nothing here claims that every part succeeded."""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errors.append({"part": name, "error": type(e).__name__})
            return None

    evidence = {"rootGo": root_go, "params": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(params).items()},
                "repo": str(repo), "base": base, "runDir": str(resolved.run_dir), "outcome": result.outcome,
                "reason": result.reason, "detail": result.detail, "verifier": verifier.reports,
                "adapter": best_effort("adapter", lambda: {str(k): vars(v) for k, v in (holder["w"].evidence.items() if "w" in holder else [])}),
                "records": best_effort("records", lambda: {r.claim_key: pilot.records_of(r.claim_key) for r in result.rounds}),
                "journal": best_effort("journal", lambda: journal_dump(aj.dsn, pilot.root)),
                "exports": list(exporter.published) if exporter is not None else [],
                "databaseRetained": False,          # harness.stop always drops the disposable DB; keep_work keeps only its work folder
                "errors": errors}
    best_effort("write", lambda: (resolved.run_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=1, sort_keys=True, default=str), encoding="utf-8", newline="\n"))
    return result, evidence


def main(argv: list[str], *, harness_factory: Callable[[], Any] | None = None) -> int:
    """Operator entrypoint. Without --launch-real-model it prints the reviewable plan only. With it (and
    --root-go naming the approving message) it starts a DISPOSABLE harness, runs one pilot, prints the
    outcome and the evidence path, and stops the harness (keeping its work folder on failure)."""
    ap = argparse.ArgumentParser(prog="pilot_real")
    ap.add_argument("--exe", required=True, type=Path)
    ap.add_argument("--exe-arg", type=Path, help="offline tests only: a second command element (e.g. the fake CLI)")
    ap.add_argument("--exe-sha256", required=True)
    ap.add_argument("--run-root", required=True, type=Path)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--budget", default="1.00")
    ap.add_argument("--launch-real-model", action="store_true")
    ap.add_argument("--root-go")
    ap.add_argument("--export", action="store_true", help="opt-in: real pinned H1 review task + one handoff-export.v0 per round")
    a = ap.parse_args(argv)
    command = (a.exe.resolve(),) + ((a.exe_arg.resolve(),) if a.exe_arg else ())
    params = RealParams(command, a.exe_sha256, model=a.model, max_budget_usd=a.budget)
    try:
        validate_params(params)
        check_executable(params)                                   # before plan's --version spawn
        report = plan(params)
    except RunnerRefused as e:
        print(f"REFUSED {e.code}")
        return 2
    print(json.dumps(report, indent=1))
    if not a.launch_real_model:
        print("PLAN ONLY: no model launched (pass --launch-real-model --root-go <msg> after a root GO).")
        return 0
    if not a.root_go:
        print("REFUSED root_go_required")
        return 2
    if a.run_root.exists():
        print("REFUSED run_root_exists")
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
        aj = ActsJournal(h.dsn, f"pilot-real#{uuid.uuid4().hex[:8]}", now=1000.0).open()
        try:
            # HOST-DECLARED (HK-ISSUE-013): a second command element means the offline fake CLI, else the real CLI.
            kind = "fake-cli" if a.exe_arg else "claude-cli"
            res, ev = run(params, a.run_root.resolve(), setup=SetupClient(h.base_url), client=SupervisorClient(h.base_url), aj=aj,
                          project_id=h.project_id, execution_kind=kind, root_go=a.root_go, export=a.export)
        finally:
            aj.close()
        print(json.dumps({"outcome": res.outcome, "reason": res.reason, "detail": res.detail, "rounds": len(res.rounds),
                          "evidence": str(Path(ev["runDir"]) / "evidence.json")}, default=str))
        ok = res.outcome == "accepted"
    except RunnerRefused as e:
        print(f"REFUSED {e.code}")
    except Exception as e:  # noqa: BLE001 -- setup failure outside the pilot (journal install, task repo): type only
        print(f"ERROR {type(e).__name__} (the run root, if created, is kept for diagnosis)")
    finally:
        problems = h.stop(keep_work=not ok)
        if problems:
            print("HARNESS " + "; ".join(problems))
    return 0 if ok else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.exit(main(sys.argv[1:]))
