"""HK-ISSUE-007 runner (e1/pilot_real.py) OFFLINE: the full pilot loop with the real-worker adapter driving
the FAKE CLI on the disposable calc task, the independent verifier, and the disposable harness database.
No model is launched."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from e1 import consumer as C
from e1 import pilot as P
from e1 import pilot_real as R
from e1.acts import E2C_BOUNDS
from e1.acts_durable import ActsJournal, install_acts
from e1.durable import install
from e1.handoff_durable import install_handoff
from e2b_support import reset_schema
from helpers import key

FAKE = Path(__file__).resolve().parent / "fake_cli.py"
PY = Path(sys.executable).resolve()


def params(**over) -> R.RealParams:
    base = dict(executable=(PY, FAKE), executable_sha256=R.sha256_file(FAKE), model="fake-model", max_budget_usd="0.50",
                max_rounds=2, max_turns=5, total_timeout_s=60, first_output_timeout_s=30, inactivity_timeout_s=30)
    base.update(over)
    return R.RealParams(**base)


@pytest.fixture
def realrun(harness, setup, client, tmp_path):
    opened = []

    def go(scenario: str, **over):
        reset_schema(harness.dsn)
        install(harness.dsn, E2C_BOUNDS)
        install_acts(harness.dsn)
        install_handoff(harness.dsn)
        aj = ActsJournal(harness.dsn, f"pilot-real#{key()[:8]}", now=1000.0).open()
        opened.append(aj)
        return R.run(params(**over), tmp_path / "pilot-real", setup=setup, client=client, aj=aj, project_id=harness.project_id,
                     task_suffix=f"\nFAKE-SCENARIO: {scenario}\n")
    yield go
    for aj in opened:
        aj.close()


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout.strip()


# ------------------------------------------------------------------------ the loop with the fake CLI

def test_correct_work_is_independently_verified_and_accepted(realrun):
    res, ev = realrun("calc_ok")
    assert (res.outcome, res.reason, len(res.rounds)) == ("accepted", None, 1)
    v = ev["verifier"][0]
    assert (v["decision"], v["why"], v["files"], v["rc"], v["treeClean"]) == ("accepted", "tests_pass", ["calc.py"], 0, True)
    art, repo = res.rounds[0].artifact_ref, Path(ev["repo"])
    run_id = Path(ev["runDir"]).name[len("pilot-"):]
    assert git(repo, "rev-parse", f"{art}^") == ev["base"] and git(repo, "rev-parse", f"refs/hekate-pilot/{run_id}/r1") == art
    assert Path(v["worktree"]).is_dir() and Path(v["worktree"]) != Path(ev["runDir"]) / "wt-r1"   # its OWN worktree
    kinds = ev["records"][res.rounds[0].claim_key]
    assert kinds.index("launch_intent") < kinds.index("launched") < kinds.index("worker_ack") < kinds.index("result_captured")
    assert {j["kind"] for j in ev["journal"]} >= {"launch_intent", "launched", "exited", "result_captured", "worker_ack"}
    assert (Path(ev["runDir"]) / "evidence.json").is_file() and (Path(ev["runDir"]) / "run.json").is_file()
    assert git(repo, "branch", "--show-current") == "main" and git(repo, "rev-parse", "HEAD") == ev["base"]


def test_failing_tests_are_rejected_then_the_fix_round_is_accepted(realrun):
    res, ev = realrun("calc_bad_then_ok")
    assert (res.outcome, len(res.rounds)) == ("accepted", 2)
    assert [(v["decision"], v["why"]) for v in ev["verifier"]] == [("rejected", "tests_fail"), ("accepted", "tests_pass")]
    assert res.rounds[1].previous["evidenceRef"] == res.rounds[0].evidence_ref and res.rounds[1].attempt_epoch == 2


def test_two_failing_rounds_stop_for_an_operator(realrun):
    res, ev = realrun("calc_bad")
    assert (res.outcome, res.reason, len(res.rounds)) == ("needs_operator", "max_rounds", 2)
    assert {v["why"] for v in ev["verifier"]} == {"tests_fail"}


def test_a_diff_outside_the_allowlist_is_rejected_before_any_test_runs(realrun):
    res, ev = realrun("calc_touch_test", max_rounds=1)
    assert (res.outcome, res.reason) == ("needs_operator", "max_rounds")
    v = ev["verifier"][0]
    assert (v["decision"], v["why"]) == ("rejected", "diff_outside_allowlist") and sorted(v["files"]) == ["calc.py", "test_calc.py"]
    assert "rc" not in v and "worktree" not in v


def test_an_executable_hash_mismatch_is_refused_before_any_write(tmp_path):
    with pytest.raises(R.RunnerRefused) as e:
        R.run(params(executable_sha256="0" * 64), tmp_path / "x", setup=None, client=None, aj=None, project_id="p")
    assert e.value.code == "exe_hash_mismatch" and list(tmp_path.iterdir()) == []


def test_plan_checks_the_hash_before_spawning_the_executable(monkeypatch):
    spawned = []

    def no_spawn(*a, **kw):
        spawned.append(a)
        raise AssertionError("the executable was spawned")
    monkeypatch.setattr(R.subprocess, "run", no_spawn)
    with pytest.raises(R.RunnerRefused) as e:
        R.plan(params(executable_sha256="0" * 64))
    assert e.value.code == "exe_hash_mismatch" and spawned == []


def test_the_executable_is_rechecked_before_every_spawn(realrun, monkeypatch):
    real = R.sha256_file
    calls = {"n": 0}

    def flip(p):            # call 1 = the fixture computing the pin, call 2 = the up-front check; then it changes
        calls["n"] += 1
        return real(p) if calls["n"] <= 2 else "f" * 64
    monkeypatch.setattr(R, "sha256_file", flip)
    res, ev = realrun("calc_ok")
    assert (res.outcome, res.reason, res.detail) == ("needs_operator", "worker_failed", "executable_hash_mismatch")
    assert "launch_intent" not in ev["records"][res.rounds[0].claim_key]


def test_the_workers_own_test_run_leaves_only_calc_py_in_the_artifact(realrun):
    """Repro A (msg 1548): the fake runs the auto-approved `python -B -m unittest -q` before exiting."""
    res, ev = realrun("calc_ok")
    assert res.outcome == "accepted" and ev["verifier"][0]["diffRecords"] == [["100644", "100644", "M", "calc.py"]]


def test_an_unexpected_failure_after_the_worker_still_writes_run_and_evidence(realrun, monkeypatch):
    """Repro 4 (msg 1548): an exception injected in CD.fresh (after the worker) is a typed stop."""
    from e1 import consumer_durable as CD

    def boom(*a, **kw):
        raise RuntimeError("fresh failed")
    monkeypatch.setattr(CD, "fresh", boom)
    res, ev = realrun("calc_ok")
    assert (res.outcome, res.reason, res.detail) == ("needs_operator", "unexpected_error", "RuntimeError")
    run_json = json.loads((Path(ev["runDir"]) / "run.json").read_text(encoding="utf-8"))
    evidence = json.loads((Path(ev["runDir"]) / "evidence.json").read_text(encoding="utf-8"))
    assert run_json["reason"] == "unexpected_error" and evidence["reason"] == "unexpected_error"
    assert evidence["databaseRetained"] is False and isinstance(evidence["errors"], list)


def launches(ev) -> int:
    return sum(1 for j in ev["journal"] if j["kind"] == "launch_intent")


def test_an_uncertain_verdict_stops_without_a_decision_or_a_second_launch(realrun, setup, monkeypatch):
    monkeypatch.setattr(R, "run_bounded", lambda *a, **kw: R.Bounded(None, True, False, True, b"", 0, "0" * 64))
    res, ev = realrun("calc_bad_then_ok")                             # would need a fix round if it were a rejection
    assert (res.outcome, res.reason, len(res.rounds)) == ("needs_operator", "review_uncertain", 1)
    assert json.loads(res.detail)["why"] == "verify_kill_unconfirmed" and res.rounds[0].decision is None
    assert launches(ev) == 1                                          # no second worker
    node = next(n for n in setup.plan(res.root).body["nodes"] if n["id"] == res.leaf)
    assert node.get("acceptance") is None and node["work"] == "done"  # no decision recorded
    assert json.loads((Path(ev["runDir"]) / "run.json").read_text(encoding="utf-8"))["reason"] == "review_uncertain"


def test_a_verifier_exception_is_a_typed_stop_that_keeps_run_json(realrun, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("verifier crashed")
    monkeypatch.setattr(R, "run_bounded", boom)
    res, ev = realrun("calc_ok")
    assert (res.outcome, res.reason, res.detail) == ("needs_operator", "reviewer_error", "RuntimeError")
    assert (Path(ev["runDir"]) / "run.json").is_file() and launches(ev) == 1


def test_a_worker_exception_is_a_typed_stop_that_keeps_run_json(realrun, monkeypatch):
    from e1 import cli_worker as W

    def boom(self, order):
        raise RuntimeError("adapter crashed")
    monkeypatch.setattr(W.CliWorker, "__call__", boom)
    res, ev = realrun("calc_ok")
    assert (res.outcome, res.reason, res.detail) == ("needs_operator", "worker_error", "RuntimeError")
    assert (Path(ev["runDir"]) / "run.json").is_file()


# ------------------------------------------------------------------------ parameters: refused before any write

@pytest.mark.parametrize("over, code", [
    ({"executable_sha256": ""}, "exe_hash_required"), ({"executable_sha256": "A" * 64}, "exe_hash_required"),
    ({"max_rounds": 3}, "rounds"), ({"max_rounds": True}, "rounds"),
    ({"executable": (Path("claude"),)}, "config_command"), ({"model": "m;x"}, "config_model"),
    ({"max_budget_usd": "1"}, "config_budget"), ({"max_turns": 0}, "config_turns"),
    ({"first_output_timeout_s": 999}, "config_timeouts"), ({"test_command": "python -m unittest; rm -rf ."}, "config_test_command"),
])
def test_malformed_params_are_refused_with_no_writes(tmp_path, over, code):
    with pytest.raises(R.RunnerRefused) as e:
        R.run(params(**over), tmp_path / "x", setup=None, client=None, aj=None, project_id="p")
    assert e.value.code == code and list(tmp_path.iterdir()) == []


def test_an_existing_run_root_is_refused(tmp_path):
    (tmp_path / "x").mkdir()
    with pytest.raises(R.RunnerRefused) as e:
        R.run(params(), tmp_path / "x", setup=None, client=None, aj=None, project_id="p")
    assert e.value.code == "run_root_exists" and list((tmp_path / "x").iterdir()) == []


# ------------------------------------------------------------------------ the verifier, directly

def artifact(repo: Path, base: str, calc: str, extra: dict | None = None) -> str:
    """Commit a candidate calc.py on a detached HEAD (the user's branch is not moved)."""
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach", base], check=True)
    (repo / "calc.py").write_text(calc, encoding="utf-8")
    for name, text in (extra or {}).items():
        (repo / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "candidate"], check=True)
    sha = git(repo, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    return sha


def order(art_in_view: str, art: str, *, cand="c" * 64, rnd=1, tamper=False) -> P.ReviewOrder:
    view = {"candidateDigest": cand, "destination": f"mentions {art} incidentally",
            "mandatory": {"state": {"mandatory": {"identity": {"artifactRef": art_in_view}}}}}
    part, digest = C.render(view)
    if tamper:
        part = part.replace("incidentally", "incidentallY")
    return P.ReviewOrder(rnd, art, part, digest, cand, R.CRITERIA)


@pytest.fixture
def task(tmp_path):
    repo, base = R.create_task_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return repo, base, run_dir


GOOD = "def add(a, b):\n    return a + b\n"


def test_the_artifact_must_be_the_view_identity_not_a_substring(task):
    repo, base, run_dir = task
    art = artifact(repo, base, GOOD)
    v = R.Verifier(repo, base, lambda: run_dir)
    out = v(order("f" * 40, art))                                     # named only incidentally in the view text
    assert out.decision == "uncertain" and json.loads(out.evidence)["why"] == "artifact_not_bound_in_view"
    tampered = v(order(art, art, tamper=True, rnd=2))
    assert (tampered.decision, json.loads(tampered.evidence)["why"]) == ("uncertain", "view_unverifiable")
    bad_cand = order(art, art, rnd=3)
    bad_cand = v(P.ReviewOrder(3, art, bad_cand.view_part, bad_cand.view_digest, "d" * 64, R.CRITERIA))
    assert (bad_cand.decision, json.loads(bad_cand.evidence)["why"]) == ("uncertain", "view_candidate_mismatch")
    ok = v(order(art, art, rnd=4))
    assert ok.decision == "accepted" and json.loads(ok.evidence)["treeClean"] is True


def test_verifier_output_is_streamed_bounded_and_digested(task):
    repo, base, run_dir = task
    noisy = GOOD + "import sys\nsys.stdout.write('N' * (3 << 20))\nsys.stdout.flush()\n"
    art = artifact(repo, base, noisy)
    v = R.Verifier(repo, base, lambda: run_dir, keep=4096)
    out = json.loads(v(order(art, art)).evidence)
    assert out["decision"] == "accepted" and out["outputBytes"] >= 3 << 20 and out["outputKeptBytes"] == 4096
    assert len(out["outputSha256"]) == 64 and out["drained"] is True


def test_a_hanging_test_is_tree_killed_and_rejected(task):
    repo, base, run_dir = task
    art = artifact(repo, base, GOOD + "import time\ntime.sleep(300)\n")
    v = R.Verifier(repo, base, lambda: run_dir, timeout_s=3)
    out = json.loads(v(order(art, art)).evidence)
    assert (out["decision"], out["why"], out["timedOut"], out["killVerified"], out["drained"]) == (
        "rejected", "tests_timeout", True, True, True)


def test_an_unconfirmed_kill_or_undrained_reader_is_uncertain_not_rejected(task, monkeypatch):
    repo, base, run_dir = task
    art = artifact(repo, base, GOOD)
    for rnd, kv, dr, why in ((1, False, True, "verify_kill_unconfirmed"), (2, True, False, "verify_not_drained")):
        monkeypatch.setattr(R, "run_bounded", lambda *a, kv=kv, dr=dr, **kw: R.Bounded(None, True, kv, dr, b"", 0, "0" * 64))
        out = R.Verifier(repo, base, lambda: run_dir)(order(art, art, rnd=rnd))
        assert (out.decision, json.loads(out.evidence)["why"]) == ("uncertain", why)


def test_verifier_git_and_spawn_failures_are_uncertain(task, monkeypatch):
    repo, base, run_dir = task
    art = artifact(repo, base, GOOD)
    (run_dir / "verify-r1").mkdir()                                  # the worktree path is taken
    out = R.Verifier(repo, base, lambda: run_dir)(order(art, art))
    assert (out.decision, json.loads(out.evidence)["why"]) == ("uncertain", "verify_worktree_failed")

    def boom(*a, **kw):
        raise OSError("spawn")
    monkeypatch.setattr(R, "run_bounded", boom)
    out = R.Verifier(repo, base, lambda: run_dir)(order(art, art, rnd=2))
    assert (out.decision, json.loads(out.evidence)["why"]) == ("uncertain", "verify_spawn_failed")


def odd_artifact(repo: Path, base: str, mode: str) -> str:
    """A candidate where calc.py is a symlink (120000), a gitlink (160000) or executable (100755)."""
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach", base], check=True)
    if mode == "100755":
        (repo / "calc.py").write_text(GOOD, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "calc.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "update-index", "--chmod=+x", "calc.py"], check=True)
    else:
        blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input="test_calc.py", capture_output=True,
                              text=True, check=True).stdout.strip()
        target = blob if mode == "120000" else git(repo, "rev-parse", "HEAD")
        subprocess.run(["git", "-C", str(repo), "update-index", "--cacheinfo", f"{mode},{target},calc.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "odd"], check=True)
    sha = git(repo, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-f", "main"], check=True)
    return sha


@pytest.mark.parametrize("mode", ["120000", "160000", "100755"])
def test_calc_py_must_stay_a_regular_100644_file(task, mode):
    repo, base, run_dir = task
    art = odd_artifact(repo, base, mode)
    out = json.loads(R.Verifier(repo, base, lambda: run_dir)(order(art, art)).evidence)
    assert (out["decision"], out["why"]) == ("rejected", "diff_outside_allowlist") and "rc" not in out


def test_a_whitespace_name_next_to_a_correct_calc_py_is_rejected(task):
    """Repro B (msg 1548): NUL-delimited records keep "calc.py calc.py" one path (never two calc.py)."""
    repo, base, run_dir = task
    art = artifact(repo, base, GOOD, {"calc.py calc.py": "x = 1\n"})
    out = json.loads(R.Verifier(repo, base, lambda: run_dir)(order(art, art)).evidence)
    assert (out["decision"], out["why"]) == ("rejected", "diff_outside_allowlist")
    assert sorted(out["files"]) == ["calc.py", "calc.py calc.py"] and "rc" not in out


def test_an_ignored_leftover_counts_as_a_dirty_tree(task):
    repo, base, run_dir = task
    with open(repo / ".git" / "info" / "exclude", "a", encoding="utf-8") as f:     # shared by every worktree
        f.write("*.tmp\n")
    art = artifact(repo, base, GOOD + "open('leftover.tmp', 'w').write('x')\n")
    out = json.loads(R.Verifier(repo, base, lambda: run_dir)(order(art, art)).evidence)
    assert (out["decision"], out["why"], out["treeClean"]) == ("rejected", "verify_tree_dirty", False)


def test_a_test_run_that_writes_files_leaves_a_dirty_tree_and_is_rejected(task):
    repo, base, run_dir = task
    art = artifact(repo, base, GOOD + "open('side_effect.txt', 'w').write('x')\n")
    out = json.loads(R.Verifier(repo, base, lambda: run_dir)(order(art, art)).evidence)
    assert (out["decision"], out["why"], out["treeClean"]) == ("rejected", "verify_tree_dirty", False)


def test_no_bytecode_is_written_by_a_clean_run(task):
    repo, base, run_dir = task
    art = artifact(repo, base, GOOD)
    out = json.loads(R.Verifier(repo, base, lambda: run_dir)(order(art, art)).evidence)
    assert out["decision"] == "accepted" and not list((run_dir / "verify-r1").rglob("__pycache__"))


def test_the_task_repo_starts_failing_and_is_never_reused(tmp_path):
    repo, base = R.create_task_repo(tmp_path)
    assert subprocess.run([str(PY), *R.TEST_ARGV], cwd=repo, capture_output=True).returncode != 0      # add() is unimplemented
    with pytest.raises(R.RunnerRefused):
        R.create_task_repo(tmp_path)


# ------------------------------------------------------------------------ plan and the operator entrypoint

def test_plan_reports_the_reviewable_launch_parameters():
    p = R.plan(params())
    assert p["executableSha256"] == p["expectedSha256"] == R.sha256_file(FAKE)
    assert "--dangerously-skip-permissions" not in p["argv"] and p["argv"][p["argv"].index("--max-budget-usd") + 1] == "0.50"
    assert p["verifier"]["testArgv"] == ["<abs python>", "-B", "-m", "unittest", "-q"] and p["verifier"]["diffAllowlist"] == ["calc.py"]
    assert p["verifier"]["requiresCleanTree"] is True and "CLI-REPORTED" in p["runtimeModelProvenance"]
    assert p["toolSurface"] == {"exposed": "--tools Read,Glob,Grep,Edit,Write,Bash",
                                "autoApproved": ["Read", "Glob", "Grep", "Edit", "Write", "Bash(python -B -m unittest -q)"],
                                "restricted": True}
    assert "--restricted" in p["argv"] and p["argv"][p["argv"].index("--tools") + 1] == "Read,Glob,Grep,Edit,Write,Bash"


def test_main_plan_only_launches_nothing(tmp_path, capsys):
    assert R.main(["--exe", str(PY), "--exe-sha256", R.sha256_file(PY), "--run-root", str(tmp_path / "r")]) == 0
    assert "PLAN ONLY" in capsys.readouterr().out and not (tmp_path / "r").exists()


def test_main_refuses_a_launch_without_a_root_go_or_with_bad_params(tmp_path, capsys):
    base = ["--exe", str(PY), "--exe-arg", str(FAKE), "--exe-sha256", R.sha256_file(FAKE), "--run-root", str(tmp_path / "r")]
    assert R.main(base + ["--launch-real-model"]) == 2 and "root_go_required" in capsys.readouterr().out
    assert R.main(base + ["--budget", "lots"]) == 2 and "config_budget" in capsys.readouterr().out
    wrong = ["--exe", str(PY), "--exe-arg", str(FAKE), "--exe-sha256", "0" * 64, "--run-root", str(tmp_path / "r")]
    assert R.main(wrong) == 2 and "REFUSED exe_hash_mismatch" in capsys.readouterr().out
    assert not (tmp_path / "r").exists()


class SessionHarness:
    """Adapts the test session's harness to main(): start resets the journal schema; stop is a no-op."""

    def __init__(self, h):
        self.h = h
        self.stopped = False

    def __getattr__(self, name):
        return getattr(self.h, name)

    def start(self):
        reset_schema(self.h.dsn)

    def stop(self, *, keep_work=False):
        self.stopped = True
        return []


def test_main_runs_one_operator_pilot_end_to_end_with_the_fake(harness, tmp_path, capsys, monkeypatch):
    sh = SessionHarness(harness)
    root = tmp_path / "r"
    argv = ["--exe", str(PY), "--exe-arg", str(FAKE), "--exe-sha256", R.sha256_file(FAKE), "--run-root", str(root),
            "--model", "fake-model", "--budget", "0.50", "--launch-real-model", "--root-go", "test-only"]
    real_run = R.run
    monkeypatch.setattr(R, "run", lambda *a, **kw: real_run(*a, **dict(kw, task_suffix="\nFAKE-SCENARIO: calc_ok\n")))
    assert R.main(argv, harness_factory=lambda: sh) == 0 and sh.stopped
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["outcome"] == "accepted" and Path(out["evidence"]).is_file()
    ev = json.loads(Path(out["evidence"]).read_text(encoding="utf-8"))
    assert ev["rootGo"] == "test-only" and ev["verifier"][0]["decision"] == "accepted"
