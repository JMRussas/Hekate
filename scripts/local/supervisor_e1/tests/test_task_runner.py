"""Operator task runner v0 (e1/task_runner.py) OFFLINE: plan/preflight/run on a fixture anchor/base repo
with a FAKE pinned node (the base Python), fake npm/vitest/tsc and the FAKE worker CLI. No real npm,
Node or model; the end-to-end cases use the disposable harness database."""

import json
import subprocess
from pathlib import Path

import pytest

from e1 import consumer as C
from e1 import pilot as P
from e1 import pilot_real as R
from e1 import task_runner as TR
from e1.acts import E2C_BOUNDS
from e1.acts_durable import ActsJournal, install_acts
from e1.durable import install
from e1.handoff_durable import install_handoff
from e2b_support import reset_schema
from helpers import key
from task_support import (FIRST, ORACLE_A, commit, edited, git, make_repo, make_spec, pinned_node, sha_file,
                          source_state, write_spec)

FAKE = Path(__file__).resolve().parent / "fake_cli.py"
PY = Path(R.sys.executable).resolve()


@pytest.fixture
def fx(tmp_path, tmp_path_factory):
    f = make_repo(tmp_path)
    doc = make_spec(f, pinned_node(tmp_path_factory.getbasetemp()))
    return f, doc


def spec_of(tmp_path, doc, name="spec.json"):
    return write_spec(doc, tmp_path / name)


def refusal(fn, *a, **kw) -> str:
    with pytest.raises(TR.PreflightRefused) as e:
        fn(*a, **kw)
    return e.value.code


# ------------------------------------------------------------------------ plan: no effects at all

def test_plan_only_clones_installs_and_spawns_nothing(fx, tmp_path, capsys, monkeypatch):
    f, doc = fx
    spec_of(tmp_path, doc)
    before = source_state(f["repo"])

    def boom(*a, **kw):
        raise AssertionError("plan must not spawn")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    assert TR.main(["plan", "--spec", str(tmp_path / "spec.json"), "--run-root", str(tmp_path / "rr")]) == 0
    out = capsys.readouterr().out
    assert "PLAN ONLY" in out and not (tmp_path / "rr").exists()
    monkeypatch.undo()
    assert source_state(f["repo"]) == before


def test_plan_on_a_malformed_spec_is_a_typed_refusal(tmp_path, capsys):
    (tmp_path / "s.json").write_text("{}", encoding="utf-8")
    assert TR.main(["plan", "--spec", str(tmp_path / "s.json")]) == 2 and "REFUSED spec_shape" in capsys.readouterr().out


def test_run_without_the_launch_flags_is_refused_before_any_harness(fx, tmp_path, capsys):
    f, doc = fx
    spec_of(tmp_path, doc)
    started = []
    rc = TR.main(["run", "--spec", str(tmp_path / "spec.json"), "--run-root", str(tmp_path / "rr")],
                 harness_factory=lambda: started.append(1))
    assert rc == 2 and started == [] and "REFUSED run_needs" in capsys.readouterr().out


# ------------------------------------------------------------------------ preflight

def test_preflight_proves_the_baseline_in_an_owned_clone_and_never_touches_the_source(fx, tmp_path):
    f, doc = fx
    spec = spec_of(tmp_path, doc)
    before = source_state(f["repo"])
    rec = TR.preflight(spec, tmp_path / "rr")
    assert rec["ok"] and rec["specSha256"] == spec.sha256 and rec["baseline"]["cases"] == 3 and rec["baseline"]["failed"] == 2
    assert rec["baseline"]["rc"] == 1 and rec["baseline"]["deps"]["rc"] == 0
    assert "ExperimentalWarning" in rec["baseline"]["stderrHead"]           # F1: stderr is evidence, not the report
    assert json.loads((tmp_path / "rr" / TR.PREFLIGHT).read_text(encoding="utf-8"))["ok"] is True
    clone = Path(rec["repo"])
    assert git(clone, "rev-parse", "HEAD") == f["base"] and clone.resolve() != Path(f["repo"]).resolve()
    assert source_state(f["repo"]) == before                            # read only: no worktree, ref or file added
    assert (tmp_path / "rr" / "baseline" / "node_modules").is_dir() and not (Path(f["repo"]) / "node_modules").exists()


def test_an_existing_run_root_is_refused(fx, tmp_path):
    f, doc = fx
    (tmp_path / "rr").mkdir()
    assert refusal(TR.preflight, spec_of(tmp_path, doc), tmp_path / "rr") == "run_root_exists"
    assert list((tmp_path / "rr").iterdir()) == []


def pre_refusal(tmp_path, doc) -> tuple[str, dict]:
    code = refusal(TR.preflight, spec_of(tmp_path, doc), tmp_path / "rr")
    rec = json.loads((tmp_path / "rr" / TR.PREFLIGHT).read_text(encoding="utf-8"))
    assert rec["ok"] is False and rec["refused"] == code
    return code, rec


def test_a_pinned_tool_hash_mismatch_is_refused_before_the_clone(fx, tmp_path):
    f, doc = fx
    for k, code in (("pinnedNodeExe", "node_hash_mismatch"), ("npmCli", "npm_hash_mismatch")):
        d = edited(doc, lambda d: d["hashes"][k].update(sha256="0" * 64))
        root = tmp_path / k
        root.mkdir()
        assert pre_refusal(root, d)[0] == code and not (root / "rr" / "repo").exists()


@pytest.mark.parametrize("mutate, code", [
    (lambda d, f: d["source"].update(anchorCommit=f["side"]), "anchor_not_ancestor"),
    (lambda d, f: d["source"].update(anchorCommit=f["c0"]), "anchor_diff_not_oracle"),
    (lambda d, f: d["source"].update(taskBaseCommit="1" * 40), "base_missing"),
    (lambda d, f: d["source"].update(repo=d["source"]["repo"] + "-missing"), "clone_failed"),
    (lambda d, f: d["oracle"]["files"][0].update(sha256="0" * 64), "oracle_hash_mismatch"),
    (lambda d, f: d["hashes"].update(packageLock="0" * 64), "lock_mismatch"),
    (lambda d, f: d["hashes"].update(vitestEntry="0" * 64), "deps_entry_mismatch"),
    (lambda d, f: d["oracle"]["baseline"]["cases"][0].update(failureFirstLine=FIRST + "!"), "spec_baseline_mismatch"),
    (lambda d, f: d["oracle"]["baseline"]["cases"].pop(), "spec_baseline_mismatch"),
    (lambda d, f: d["oracle"]["baseline"]["cases"][2].update(fullName="renamed"), "spec_baseline_mismatch"),
    (lambda d, f: d["oracle"]["baseline"].update(expectedExit=0), "spec_baseline_mismatch"),
    (lambda d, f: d["oracle"]["baseline"].update(reportMaxBytes=64), "baseline_report_truncated"),
])
def test_preflight_refusals_are_typed_and_recorded(fx, tmp_path, mutate, code):
    f, doc = fx
    got, rec = pre_refusal(tmp_path, edited(doc, lambda d: mutate(d, f)))
    assert got == code
    if code == "spec_baseline_mismatch":
        assert rec["detail"]["reason"] in ("cases", "exit")


def test_a_baseline_case_mismatch_names_the_missing_and_extra_cases(fx, tmp_path):
    f, doc = fx
    _, rec = pre_refusal(tmp_path, edited(doc, lambda d: d["oracle"]["baseline"]["cases"][2].update(fullName="renamed")))
    assert rec["detail"]["missing"] == [[ORACLE_A, "renamed", "passed", None]]
    assert rec["detail"]["extra"] == [[ORACLE_A, "value file loads", "passed", None]]


def test_a_failed_or_hung_install_is_refused(fx, tmp_path, monkeypatch):
    f, doc = fx
    real = R.run_bounded
    monkeypatch.setattr(R, "run_bounded", lambda argv, **kw: R.Bounded(None, True, True, True, b"", 0, "0" * 64)
                        if argv[1].endswith("npm_cli.py") else real(argv, **kw))
    assert pre_refusal(tmp_path, doc)[0] == "deps_install_failed"


def test_the_recorded_ca012_baseline_report_is_exactly_the_frozen_specs_cases():
    """Offline: the real vitest JSON recorded at the CA012 task base (63cfadae) against the frozen spec (b919504a)."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    spec = TR.T.load(fixtures / "ca012-spec-v0.json")
    report = json.loads((fixtures / "ca012-baseline-report-1f75576.json").read_bytes())
    wt = Path(report["testResults"][0]["name"]).parents[2]                  # the probe clone's root
    got, counts = TR.report_cases(report, wt)
    b0 = spec.doc["oracle"]["baseline"]
    assert got == {(c["file"], c["fullName"], c["status"], c["failureFirstLine"]) for c in b0["cases"]}
    assert (counts["numFailedTests"], counts["numPassedTests"]) == (14, 1) == (
        sum(c["status"] == "failed" for c in b0["cases"]), sum(c["status"] == "passed" for c in b0["cases"]))


def test_report_cases_refuses_a_suite_level_error_and_foreign_files(tmp_path):
    rep = {"testResults": [{"name": str(tmp_path / "t.test.ts"), "message": "SyntaxError", "assertionResults": []}]}
    assert refusal(TR.report_cases, rep, tmp_path) == "baseline_suite_error"
    rep = {"testResults": [{"name": str(tmp_path.parent / "x.test.ts"), "message": "", "assertionResults": []}]}
    assert refusal(TR.report_cases, rep, tmp_path) == "baseline_report_shape"


# ------------------------------------------------------------------------ the spec verifier, directly

@pytest.fixture
def ver(fx, tmp_path):
    """A passing preflight; the verifier on its owned clone; candidates committed there on a detached HEAD."""
    f, doc = fx

    def make(d=None, **_):
        spec = spec_of(tmp_path, d or doc, f"spec-{key()[:6]}.json")
        root = tmp_path / f"rr-{key()[:6]}"
        rec = TR.preflight(spec, root)
        run_dir = root / "run"
        run_dir.mkdir()
        return TR.SpecVerifier(spec, Path(rec["repo"]), lambda: run_dir), Path(rec["repo"])
    return make, f


def candidate(repo: Path, base: str, files: dict[str, str], chmod: str | None = None) -> str:
    git(repo, "checkout", "-q", "--detach", base)
    sha = commit(repo, files, "candidate") if not chmod else None
    if chmod:
        for rel, text in files.items():
            (repo / rel).write_text(text, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "update-index", f"--chmod={chmod}", next(iter(files)))
        git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "candidate")
        sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach", base)
    return sha


def review(v: TR.SpecVerifier, art: str, *, art_in_view: str | None = None, tamper=False, rnd=1) -> tuple[str, dict]:
    view = {"candidateDigest": "c" * 64, "mandatory": {"state": {"mandatory": {"identity": {"artifactRef": art_in_view or art}}}}}
    part, digest = C.render(view)
    if tamper:
        part = part.replace("c" * 64, "d" * 64, 1)
    out = v(P.ReviewOrder(rnd, art, part, digest, "c" * 64, "criteria"))
    return out.decision, json.loads(out.evidence)


def test_a_correct_artifact_is_accepted_with_fresh_deps_every_step_and_a_clean_tree(ver):
    make, f = ver
    v, repo = make()
    art = candidate(repo, f["base"], {"src/value.txt": "42\n"})
    dec, rep = review(v, art)
    assert (dec, rep["why"]) == ("accepted", "all_steps_pass")
    assert [s["name"] for s in rep["steps"]] == ["tsc", "oracle"] and all(s["rc"] == 0 for s in rep["steps"])
    assert rep["status"] == TR.CLEAN_STATUS and rep["deps"]["rc"] == 0
    assert rep["diffRecords"] == [["100644", "100644", "M", "src/value.txt"]]
    assert git(Path(rep["worktree"]), "rev-parse", "HEAD") == art                 # the verified tree IS the artifact
    oracle = rep["steps"][1]                                  # F1: a Node warning on stderr never corrupts the report
    assert "ExperimentalWarning" in oracle["stderrHead"] and oracle["stderrBytes"] > 0 and len(oracle["stderrSha256"]) == 64
    assert "ExperimentalWarning" not in oracle["outputTail"]


@pytest.mark.parametrize("files, chmod, why", [
    ({"src/value.txt": "42\n", "src/other.txt": "y\n"}, None, "diff_outside_allowlist"),
    ({"src/value.txt": "42\n", ORACLE_A: '{"cases": []}\n'}, None, "diff_outside_allowlist"),
    ({"src/value.txt": "42\n", "package-lock.json": "{}\n"}, None, "diff_outside_allowlist"),
    ({"src/value.txt": "42\n", "src/new.txt": "n\n"}, None, "diff_outside_allowlist"),
    ({"src/value.txt": "42\n"}, "+x", "diff_outside_allowlist"),
])
def test_the_diff_is_checked_before_any_install_or_artifact_code(ver, files, chmod, why):
    make, f = ver
    v, repo = make()
    dec, rep = review(v, candidate(repo, f["base"], files, chmod))
    assert (dec, rep["why"]) == ("rejected", why) and "worktree" not in rep and "steps" not in rep


@pytest.mark.parametrize("value, decision, why", [
    ("41\n", "rejected", "step_failed:oracle"),
    ("42 tserror\n", "rejected", "step_failed:tsc"),
    ("42 dirty\n", "rejected", "verify_tree_dirty"),
    ("crash\n", "uncertain", "oracle_report_unusable"),
    ("42 garbage\n", "uncertain", "oracle_report_unusable"),          # junk on STDOUT is not a report
    ("42 extra\n", "rejected", "oracle_case_set_not_declared"),       # F4: exactly the declared identities
])
def test_artifact_outcomes(ver, value, decision, why):
    make, f = ver
    v, repo = make()
    dec, rep = review(v, candidate(repo, f["base"], {"src/value.txt": value}))
    assert (dec, rep["why"]) == (decision, why)


def test_a_hanging_step_is_tree_killed_and_rejected(ver, fx):
    make, f = ver
    _, doc = fx
    v, repo = make(edited(doc, lambda d: d["verify"]["steps"][0].update(timeoutS=2)))
    dec, rep = review(v, candidate(repo, f["base"], {"src/value.txt": "42 hang\n"}))
    assert (dec, rep["why"]) == ("rejected", "step_timeout:tsc") and rep["steps"][0]["killVerified"]


def test_a_truncated_oracle_report_is_uncertain_not_a_decision(ver, fx):
    make, f = ver
    _, doc = fx
    v, repo = make(edited(doc, lambda d: d["verify"]["steps"][1].update(outputKeepBytes=32)))
    dec, rep = review(v, candidate(repo, f["base"], {"src/value.txt": "42\n"}))
    assert (dec, rep["why"]) == ("uncertain", "oracle_report_truncated")


def test_view_binding_parent_and_worktree_problems_are_uncertain(ver):
    make, f = ver
    v, repo = make()
    art = candidate(repo, f["base"], {"src/value.txt": "42\n"})
    other = candidate(repo, f["base"], {"src/value.txt": "42\n\n"})
    assert review(v, art, art_in_view=other)[1]["why"] == "artifact_not_bound_in_view"
    assert review(v, art, tamper=True)[1]["why"] in ("view_unverifiable", "view_candidate_mismatch")
    git(repo, "checkout", "-q", "--detach", art)
    stacked = commit(repo, {"src/value.txt": "42\n\n\n"}, "stacked")
    assert review(v, stacked) == ("uncertain", review(v, stacked)[1]) and review(v, stacked)[1]["why"] == "artifact_parent_not_base"
    (v.run_dir_of() / "verify-r1").mkdir()
    assert review(v, art)[1]["why"] == "verify_worktree_failed"


def test_deps_kill_and_drain_failures_are_uncertain(ver, monkeypatch):
    make, f = ver
    v, repo = make()
    art = candidate(repo, f["base"], {"src/value.txt": "42\n"})
    real_install = TR.install_deps
    monkeypatch.setattr(TR, "install_deps", lambda *a: (_ for _ in ()).throw(TR.PreflightRefused("deps_install_failed")))
    assert review(v, art, rnd=1) [1]["why"] == "verify_deps_failed"
    monkeypatch.setattr(TR, "install_deps", real_install)
    real = R.run_bounded
    for rnd, (bounded, why) in enumerate([(R.Bounded(None, True, False, True, b"", 0, "0" * 64), "verify_kill_unconfirmed"),
                                          (R.Bounded(0, False, True, False, b"", 0, "0" * 64), "verify_not_drained")], start=2):
        monkeypatch.setattr(R, "run_bounded", lambda argv, b=bounded, **kw: real(argv, **kw) if argv[1].endswith("npm_cli.py") else b)
        dec, rep = review(v, art, rnd=rnd)
        assert (dec, rep["why"]) == ("uncertain", why)


def test_pinned_tools_and_immutable_inputs_are_rechecked_before_every_spawn(ver, monkeypatch):
    """A step that rewrites an oracle file (or a tool that changes) is caught before the NEXT spawn."""
    make, f = ver
    v, repo = make()
    art = candidate(repo, f["base"], {"src/value.txt": "42\n"})
    real = R.run_bounded

    def tamper(argv, **kw):
        out = real(argv, **kw)
        if argv[1].endswith("tsc"):
            (kw["cwd"] / ORACLE_A).write_text('{"cases": []}\n', encoding="utf-8")
        return out
    monkeypatch.setattr(R, "run_bounded", tamper)
    dec, rep = review(v, art, rnd=1)
    assert (dec, rep["why"]) == ("uncertain", "integrity_before:oracle") and rep["integrity"] == ["tree_integrity", ORACLE_A]
    monkeypatch.setattr(R, "run_bounded", real)
    calls = []
    real_pins = TR.check_pins

    def pins(spec):
        calls.append(1)
        if len(calls) == 3:                                             # install, step 1 ... then the tool changes
            raise TR.PreflightRefused("node_hash_mismatch", "x")
        return real_pins(spec)
    monkeypatch.setattr(TR, "check_pins", pins)
    dec, rep = review(v, art, rnd=2)
    assert (dec, rep["why"]) == ("uncertain", "integrity_before:oracle") and rep["integrity"][0] == "node_hash_mismatch"


# ------------------------------------------------------------------------ run: preflight gate and the full loop

def test_run_requires_a_passing_preflight_of_the_same_spec(fx, tmp_path):
    f, doc = fx
    spec = spec_of(tmp_path, doc)
    kw = dict(executable=(PY, FAKE), executable_sha256=sha_file(FAKE), setup=None, client=None, aj=None, project_id="p",
              execution_kind="fake-cli", root_go="test")
    assert refusal(TR.run, spec, tmp_path / "none", **kw) == "preflight_missing"
    TR.preflight(spec, tmp_path / "rr")
    other = spec_of(tmp_path, edited(doc, lambda d: d["metadata"].update(title="other")), "other.json")
    assert refusal(TR.run, other, tmp_path / "rr", **kw) == "preflight_not_passing_for_this_spec"
    pre = tmp_path / "rr" / TR.PREFLIGHT
    rec = json.loads(pre.read_text(encoding="utf-8"))
    pre.write_text(json.dumps(dict(rec, repo=f["repo"])), encoding="utf-8")         # F3: a recorded path is never trusted
    assert refusal(TR.run, spec, tmp_path / "rr", **kw) == "preflight_repo_not_owned"
    pre.write_text(json.dumps(rec), encoding="utf-8")
    git(Path(rec["repo"]), "checkout", "-q", "--detach", f["anchor"])
    assert refusal(TR.run, spec, tmp_path / "rr", **kw) == "clone_moved"


@pytest.fixture
def taskrun(harness, setup, client, fx, tmp_path):
    f, doc = fx
    opened = []

    def go(scenario: str, d=None):
        spec = spec_of(tmp_path, d or doc)
        TR.preflight(spec, tmp_path / "rr")
        reset_schema(harness.dsn)
        install(harness.dsn, E2C_BOUNDS)
        install_acts(harness.dsn)
        install_handoff(harness.dsn)
        aj = ActsJournal(harness.dsn, f"task-runner#{key()[:8]}", now=1000.0).open()
        opened.append(aj)
        return TR.run(spec, tmp_path / "rr", executable=(PY, FAKE), executable_sha256=sha_file(FAKE), setup=setup, client=client,
                      aj=aj, project_id=harness.project_id, execution_kind="fake-cli", root_go="test-only",
                      task_suffix=f"\nFAKE-SCENARIO: {scenario}\n", timeouts=(120, 60, 60))
    yield go, f
    for aj in opened:
        aj.close()


def launches(ev) -> int:
    return sum(1 for j in ev["journal"] if j["kind"] == "launch_intent")


def test_correct_work_is_accepted_end_to_end_and_the_source_is_untouched(taskrun):
    go, f = taskrun
    before = source_state(f["repo"])
    res, ev = go("value_ok")
    assert (res.outcome, len(res.rounds)) == ("accepted", 1) and launches(ev) == 1
    v = ev["verifier"][0]
    assert v["decision"] == "accepted" and v["artifactRef"] == res.rounds[0].artifact_ref
    wt = Path(ev["adapter"]["1"]["worktree"])
    assert (wt / "node_modules" / "vitest" / "vitest.mjs").is_file()          # the worker had its OWN deps (prepare hook)
    assert Path(v["worktree"]) != wt and (Path(v["worktree"]) / "node_modules").is_dir()
    assert any(r.endswith(f"refs/hekate-pilot/{Path(ev['runDir']).name}/r1") or "refs/hekate-pilot/" in r for r in ev["ownedRefs"])
    assert source_state(f["repo"]) == before
    assert json.loads((Path(ev["runDir"]) / "evidence.json").read_text(encoding="utf-8"))["rootGo"] == "test-only"


def test_a_failing_first_round_gets_one_fix_round(taskrun):
    go, _ = taskrun
    res, ev = go("value_bad_then_ok")
    assert (res.outcome, len(res.rounds)) == ("accepted", 2) and [r["why"] for r in ev["verifier"]] == ["step_failed:oracle", "all_steps_pass"]


@pytest.mark.parametrize("scenario", ["value_touch_oracle", "value_touch_lock"])
def test_touching_the_oracle_or_the_lock_is_rejected_before_any_artifact_code(taskrun, scenario):
    go, _ = taskrun
    res, ev = go(scenario)
    assert res.outcome != "accepted" and all(r["why"] == "diff_outside_allowlist" and "steps" not in r for r in ev["verifier"])


def test_a_failed_prepare_journals_nothing_and_spawns_nothing(taskrun, monkeypatch):
    go, _ = taskrun

    def install_fails(spec, wt):
        if Path(wt).name.startswith("wt-r"):
            raise TR.PreflightRefused("deps_install_failed")
        return real(spec, wt)
    real = TR.install_deps
    monkeypatch.setattr(TR, "install_deps", install_fails)
    res, ev = go("value_ok")
    assert res.outcome != "accepted" and launches(ev) == 0 and ev["verifier"] == []
    assert "prepare_failed" in json.dumps(ev["records"]) + str(res.reason) + str(res.detail)


def test_evidence_is_best_effort_and_always_written(taskrun, monkeypatch):
    go, _ = taskrun

    def boom(self, claim_key):
        raise RuntimeError("records unavailable")
    monkeypatch.setattr(P.Pilot, "records_of", boom)
    res, ev = go("value_ok")
    assert res.outcome == "accepted" and {"part": "records", "type": "RuntimeError"} in ev["errors"]
    on_disk = json.loads((Path(ev["runDir"]) / "evidence.json").read_text(encoding="utf-8"))
    assert on_disk["errors"] == ev["errors"] and on_disk["ownedRefs"] and on_disk["journal"]


class SessionHarness:
    def __init__(self, h):
        self.h, self.stopped = h, False

    def __getattr__(self, name):
        return getattr(self.h, name)

    def start(self):
        reset_schema(self.h.dsn)

    def stop(self, *, keep_work=False):
        self.stopped = True
        return []


def test_main_preflight_then_run_end_to_end_with_the_fake(harness, fx, tmp_path, capsys, monkeypatch):
    f, doc = fx
    spec_of(tmp_path, doc)
    base = ["--spec", str(tmp_path / "spec.json"), "--run-root", str(tmp_path / "rr")]
    assert TR.main(["preflight", *base]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"preflight": "ok", "baseline": 3, "failed": 2}
    real_run = TR.run
    monkeypatch.setattr(TR, "run", lambda *a, **kw: real_run(*a, **dict(kw, task_suffix="\nFAKE-SCENARIO: value_ok\n",
                                                                        timeouts=(120, 60, 60))))
    sh = SessionHarness(harness)
    argv = ["run", *base, "--exe", str(PY), "--exe-arg", str(FAKE), "--exe-sha256", sha_file(FAKE), "--launch-real-model",
            "--root-go", "test-only"]
    assert TR.main(argv, harness_factory=lambda: sh) == 0 and sh.stopped
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["outcome"] == "accepted" and Path(out["evidence"]).is_file()
