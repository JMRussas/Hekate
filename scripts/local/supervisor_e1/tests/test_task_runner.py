"""Operator task runner v0 (e1/task_runner.py) OFFLINE: plan/preflight/run on a fixture anchor/base repo
with a FAKE pinned node (the base Python), fake npm/vitest/tsc and the FAKE worker CLI. No real npm,
Node or model; the end-to-end cases use the disposable harness database."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from e1 import cli_worker as W
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
from task_support import (FIRST, NPM_CLI, ORACLE_A, commit, edited, git, make_repo, make_spec, pinned_node, sha_file,
                          source_state, write_spec)

FAKE = Path(__file__).resolve().parent / "fake_cli.py"
PY = Path(R.sys.executable).resolve()


@pytest.fixture(autouse=True)
def npm_cache(tmp_path, monkeypatch) -> Path:
    """A HERMETIC warm cache: npm_config_cache -> <tmp>/npm-cache with _cacache (never the operator's)."""
    cache = tmp_path / "npm-cache"
    (cache / "_cacache").mkdir(parents=True)
    monkeypatch.setenv("npm_config_cache", str(cache))
    return cache


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


# ------------------------------------------------------------------------ npm's scoped environment (msgs 1675-1691)

def test_npm_gets_only_the_scoped_cache_and_the_owned_empty_configs(fx, tmp_path, npm_cache):
    f, doc = fx
    rec = TR.preflight(spec_of(tmp_path, doc), tmp_path / "rr")
    deps = rec["baseline"]["deps"]
    user, glob = (tmp_path / "rr" / TR.NPMRCS[r] for r in ("user", "global"))
    empty = hashlib.sha256(b"").hexdigest()
    assert deps["npm"] == {"npmCache": str(npm_cache), "cacachePresent": True,
                           "userconfig": {"path": str(user), "sha256": empty}, "globalconfig": {"path": str(glob), "sha256": empty},
                           "builtinNpmrc": str(Path(doc["hashes"]["npmCli"]["path"]).parents[1] / "npmrc"),
                           "builtinNpmrcSha256": None}                     # the fake npm has no builtin npmrc
    assert "added 2 packages" in deps["outputHead"] and deps["outputBytes"] == len(deps["outputHead"].encode())
    for rc in (user, glob):
        assert rc.is_file() and rc.stat().st_size == 0 and not (tmp_path / "rr" / "baseline" / rc.name).exists()
    assert not any(k.lower().startswith("npm_config") for k in W.worker_env())      # the worker env is NOT widened


def test_a_builtin_npmrc_is_recorded_by_hash_only(fx, tmp_path, monkeypatch):
    f, doc = fx
    tools = tmp_path / "npm" / "node_modules" / "npm"
    (tools / "bin").mkdir(parents=True)
    cli = tools / "bin" / "npm-cli.js"
    for src in (NPM_CLI, NPM_CLI.parent / "vitest.py", NPM_CLI.parent / "tsc.py"):   # the fake copies its siblings
        (cli.parent / (cli.name if src == NPM_CLI else src.name)).write_bytes(src.read_bytes())
    (tools / "npmrc").write_text("cache=D:/somewhere/else\n", encoding="utf-8")
    d = edited(doc, lambda d: d["hashes"]["npmCli"].update(path=cli.as_posix(), sha256=sha_file(cli)))
    rec = TR.preflight(spec_of(tmp_path, d), tmp_path / "rr")
    npm = rec["baseline"]["deps"]["npm"]
    assert npm["builtinNpmrcSha256"] == sha_file(tools / "npmrc") and "somewhere" not in json.dumps(rec)


def test_an_absent_cache_fails_closed_before_any_npm_spawn(fx, tmp_path, monkeypatch):
    f, doc = fx
    monkeypatch.delenv("npm_config_cache")
    code, rec = pre_refusal(tmp_path, doc)
    assert code == "npm_cache_required" and not (tmp_path / "rr" / "baseline" / "node_modules").exists()


@pytest.mark.parametrize("raw", ["relative/cache", "C:relative-to-drive", "missing-abs", "a-file", "unc"])
def test_an_invalid_cache_fails_closed_before_any_npm_spawn(fx, tmp_path, monkeypatch, raw):
    f, doc = fx
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    value = {"missing-abs": str(tmp_path / "no-such-cache"), "a-file": str(tmp_path / "a-file"),
             "unc": r"\\localhost\hekate-no-such-share-1675\npm-cache"}.get(raw, raw)
    monkeypatch.setenv("npm_config_cache", value)
    code, rec = pre_refusal(tmp_path, doc)
    assert (code, rec["detail"]) == ("npm_cache_invalid", value)
    assert not (tmp_path / "rr" / "baseline" / "node_modules").exists()


def test_a_cold_cache_is_refused_with_npms_own_error_in_the_evidence(fx, tmp_path, monkeypatch):
    """The 1675 failure, offline: the cache exists but holds nothing -> npm's ENOTCACHED text is recorded."""
    f, doc = fx
    cold = tmp_path / "cold-cache"
    cold.mkdir()
    monkeypatch.setenv("npm_config_cache", str(cold))
    code, rec = pre_refusal(tmp_path, doc)
    assert code == "deps_install_failed" and rec["detail"]["rc"] == 1
    assert "ENOTCACHED" in rec["detail"]["outputHead"] and rec["detail"]["npm"]["cacachePresent"] is False


@pytest.mark.parametrize("role", ["user", "global"])
def test_each_empty_npmrc_is_rechecked_before_every_npm_spawn(fx, tmp_path, role):
    f, doc = fx
    spec = spec_of(tmp_path, doc)
    TR.preflight(spec, tmp_path / "rr")
    rc = tmp_path / "rr" / TR.NPMRCS[role]
    rc.write_text("registry=https://example.invalid/\n", encoding="utf-8")
    assert refusal(TR.npm_env, spec, tmp_path / "rr") == "npmrc_not_empty"
    rc.unlink()
    assert refusal(TR.npm_env, spec, tmp_path / "rr") == "npmrc_missing"
    rc.mkdir()
    assert refusal(TR.npm_env, spec, tmp_path / "rr") == "npmrc_not_empty"


def test_the_fake_npm_refuses_one_file_as_both_configs_like_real_npm(fx, tmp_path, monkeypatch):
    """The 1689 defect would now fail the suite: one shared file -> npm's double-loading error."""
    f, doc = fx
    real = TR.npm_env

    def shared(spec, run_root):
        env, ev = real(spec, run_root)
        return dict(env, npm_config_globalconfig=env["npm_config_userconfig"]), ev
    monkeypatch.setattr(TR, "npm_env", shared)
    code, rec = pre_refusal(tmp_path, doc)
    assert code == "deps_install_failed" and "double-loading config" in rec["detail"]["outputHead"]


@pytest.mark.skipif(TR.os.environ.get("HEKATE_E1_INTEROP_LIVE") != "1", reason="opt-in interop_live: the REAL pinned npm")
def test_interop_live_real_npm_reads_the_forwarded_cache_under_npm_env(tmp_path, monkeypatch):
    """The REAL pinned node + npm-cli of the frozen CA012 spec, `npm config get cache` under npm_env(): no
    network, no install, no model. It must print the forwarded cache; both owned configs stay empty."""
    spec = TR.T.load(Path(__file__).resolve().parent / "fixtures" / "ca012-spec-v0.json")
    cache = TR.os.environ.get("HEKATE_E1_NPM_CACHE", "D:/caches/npm")
    if not Path(cache).is_dir():
        pytest.skip(f"no warm npm cache at {cache}")
    monkeypatch.setenv("npm_config_cache", str(Path(cache)))
    TR.check_pins(spec)
    root = tmp_path / "rr"
    root.mkdir()
    TR.make_npmrc(root)
    env, ev = TR.npm_env(spec, root)
    h = spec.doc["hashes"]
    p = subprocess.run([h["pinnedNodeExe"]["path"], h["npmCli"]["path"], "config", "get", "cache"], cwd=root, env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert Path(p.stdout.strip()) == Path(cache) and ev["cacachePresent"] is True
    assert all((root / n).stat().st_size == 0 for n in TR.NPMRCS.values())


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
        return TR.SpecVerifier(spec, Path(rec["repo"]), lambda: run_dir, root), Path(rec["repo"])
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
    assert review(v, art)[1]["why"] == "verify_worktree_exists"


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

    def install_fails(spec, wt, root):
        if Path(wt).name.startswith("wt-r"):
            raise TR.PreflightRefused("deps_install_failed")
        return real(spec, wt, root)
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


# ------------------------------------------------------------------------ Windows long paths (msgs 1702-1704 D1/D2)

DEEP = "docs/" + "/".join(["d" * 20] * 5) + "/" + "f" * 18 + ".txt"        # 130 chars, never read by any step


@pytest.mark.skipif(R.sys.platform != "win32", reason="MAX_PATH is a Windows limit")
def test_a_verify_worktree_past_max_path_works_with_the_clone_local_longpaths(tmp_path, tmp_path_factory):
    """The CA012 pilot's failure, reproduced: <run_dir>/verify-r1 + the deepest tracked path > 260 chars.
    The owned clone carries core.longpaths=true in its OWN config, so the worktree is created; with it off,
    the verifier stops uncertain and the git stderr says why."""
    f = make_repo(tmp_path, extra={DEEP: "deep\n"})
    doc = make_spec(f, pinned_node(tmp_path_factory.getbasetemp()))
    spec = spec_of(tmp_path, doc)
    rr = tmp_path / "rr"
    rec = TR.preflight(spec, rr)
    repo = Path(rec["repo"])
    assert git(repo, "config", "--local", "--get", "core.longpaths") == "true"
    assert subprocess.run(["git", "-C", f["repo"], "config", "--local", "--get", "core.longpaths"],
                          capture_output=True, env=W.git_env()).returncode == 1          # the source repo is untouched
    room = 260 - len(DEEP) - len("/verify-r1/") - len(str(tmp_path)) + 8            # run_dir + DEEP lands over 260
    long_dir = tmp_path / ("L" * room)
    long_dir.mkdir()
    assert len(str(long_dir / "verify-r1" / DEEP)) > 260
    art = candidate(repo, f["base"], {"src/value.txt": "42\n"})
    git(repo, "config", "--local", "core.longpaths", "false")                       # the pilot's condition
    v = TR.SpecVerifier(spec, repo, lambda: long_dir, rr)
    dec, rep = review(v, art, rnd=1)
    assert (dec, rep["why"]) == ("uncertain", "verify_worktree_failed")
    assert "too long" in rep["gitStderr"].lower()                                    # D2: the cause is recorded
    git(repo, "worktree", "prune")
    git(repo, "config", "--local", "core.longpaths", "true")                        # what the fixed clone carries
    dec, rep = review(v, art, rnd=2)
    assert (dec, rep["why"]) == ("accepted", "all_steps_pass") and len(str(Path(rep["worktree"]) / DEEP)) > 260


def test_preflight_git_failures_carry_bounded_stderr(fx, tmp_path):
    f, doc = fx
    code, rec = pre_refusal(tmp_path, edited(doc, lambda d: d["source"].update(taskBaseCommit="1" * 40)))
    assert code == "base_missing" and isinstance(rec["detail"], str) and rec["detail"] and len(rec["detail"]) <= TR.ERR_KEEP


# ------------------------------------------------------------------------ verifier-only re-check (msg 1704 option 1)

@pytest.fixture
def stopped(taskrun, monkeypatch):
    """A run whose verifier could not create its worktree: needs_operator / review_uncertain, artifact kept."""
    go, f = taskrun
    real = TR.SpecVerifier.check_artifact

    def no_worktree(self, art, rnd, rep, verdict):                 # as the CA012 pilot: binding and diff passed first
        rep["diffRecords"] = [["100644", "100644", "M", "src/value.txt"]]
        rep["gitStderr"] = "fatal: simulated: Filename too long"
        return verdict("uncertain", "verify_worktree_failed")
    monkeypatch.setattr(TR.SpecVerifier, "check_artifact", no_worktree)
    res, ev = go("value_ok")
    monkeypatch.setattr(TR.SpecVerifier, "check_artifact", real)
    assert (res.outcome, res.reason) == ("needs_operator", "review_uncertain")
    return res, ev, f


def test_verify_only_rechecks_the_same_artifact_freshly_and_never_touches_the_original(stopped, fx, tmp_path):
    res, ev, f = stopped
    _, doc = fx
    spec = TR.T.load(tmp_path / "spec.json")
    pilot = Path(ev["runDir"])
    before = {n: (pilot / n).read_bytes() for n in ("run.json", "evidence.json")}
    rec = TR.verify_only(spec, tmp_path / "rr", pilot, tmp_path / "rr" / "reverify-1", root_go="test-only")
    assert (rec["decision"], rec["why"]) == ("accepted", "all_steps_pass") and rec["mode"] == "verifier-only"
    assert "NOT a replay" in rec["label"] and "were not preserved" in rec["trustBasis"]          # msg 1714: honest label
    b = rec["bound"]
    assert b["artifactRef"] == res.rounds[0].artifact_ref and all(b["checks"].values())
    assert b["runJsonSha256"] == hashlib.sha256(before["run.json"]).hexdigest()
    assert b["evidenceJsonSha256"] == hashlib.sha256(before["evidence.json"]).hexdigest()
    assert rec["originalOutcome"] == {"outcome": "needs_operator", "reason": "review_uncertain", "unchanged": True}
    assert {n: (pilot / n).read_bytes() for n in before} == before                  # the original run is never changed
    assert Path(rec["report"]["worktree"]).parent == (tmp_path / "rr" / "reverify-1").resolve()
    on_disk = json.loads((tmp_path / "rr" / "reverify-1" / TR.VERIFY_EVIDENCE).read_text(encoding="utf-8"))
    assert on_disk["decision"] == "accepted" and on_disk["rootGo"] == "test-only"
    assert refusal(TR.verify_only, spec, tmp_path / "rr", pilot, tmp_path / "rr" / "reverify-1",
                   root_go="x") == "verify_out_exists"


def test_verify_only_refuses_a_broken_binding_before_any_effect(stopped, tmp_path):
    res, ev, f = stopped
    spec = TR.T.load(tmp_path / "spec.json")
    pilot = Path(ev["runDir"])
    copy = tmp_path / "rr" / "pilot-tampered"
    copy.mkdir()
    run = json.loads((pilot / "run.json").read_text(encoding="utf-8"))
    run["rounds"][-1]["view_digest"] = "0" * 64
    (copy / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (copy / "evidence.json").write_bytes((pilot / "evidence.json").read_bytes())
    with pytest.raises(TR.PreflightRefused) as e:
        TR.verify_only(spec, tmp_path / "rr", copy, tmp_path / "rr" / "reverify-x", root_go="x")
    assert e.value.code == "verify_binding_failed" and e.value.detail["prior_review_binds_round"] is False
    assert not (tmp_path / "rr" / "reverify-x").exists()
    assert refusal(TR.verify_only, spec, tmp_path / "rr", pilot, tmp_path / "elsewhere", root_go="x") == "verify_paths_not_in_run_root"


@pytest.mark.parametrize("why, keep_diff", [
    ("view_unverifiable", True),           # a PRE-binding stop with matching digests (msg 1718): refused by the checkpoint
    ("artifact_not_bound_in_view", True),
    ("verify_deps_failed", True),          # a later stop is not the supported checkpoint either
    ("verify_worktree_failed", False),     # the right reason but no recorded diff
])
def test_verify_only_refuses_any_checkpoint_but_a_post_binding_worktree_failure(stopped, tmp_path, why, keep_diff):
    """Only the CA012 case is supported: stopped at verify_worktree_failed after the binding and the diff read."""
    res, ev, f = stopped
    spec = TR.T.load(tmp_path / "spec.json")
    pilot = Path(ev["runDir"])
    copy = tmp_path / "rr" / "pilot-early-stop"
    copy.mkdir()
    (copy / "run.json").write_bytes((pilot / "run.json").read_bytes())
    e2 = json.loads((pilot / "evidence.json").read_text(encoding="utf-8"))
    if not keep_diff:
        del e2["verifier"][-1]["diffRecords"]
    e2["verifier"][-1]["why"] = why
    (copy / "evidence.json").write_text(json.dumps(e2), encoding="utf-8")
    with pytest.raises(TR.PreflightRefused) as e:
        TR.verify_only(spec, tmp_path / "rr", copy, tmp_path / "rr" / "reverify-y", root_go="x")
    assert e.value.code == "verify_binding_failed" and e.value.detail["prior_review_passed_binding"] is False
    assert not (tmp_path / "rr" / "reverify-y").exists()


def test_verify_only_refuses_a_run_that_already_has_a_decision(taskrun, tmp_path):
    go, f = taskrun
    res, ev = go("value_ok")
    assert res.outcome == "accepted"
    spec = TR.T.load(tmp_path / "spec.json")
    with pytest.raises(TR.PreflightRefused) as e:
        TR.verify_only(spec, tmp_path / "rr", Path(ev["runDir"]), tmp_path / "rr" / "reverify-1", root_go="x")
    assert e.value.code == "verify_binding_failed" and e.value.detail["run_stopped_without_decision"] is False


# ------------------------------------------------------------------------ per-step bounded logs (msgs 1775/1780)

def test_a_failed_step_names_its_failure_from_retained_output_not_the_tail(ver):
    """The CA013 gap: the failing test was printed EARLY; the 400-char tail lost it. The step log keeps the
    retained output beside the worktree, and failureLines names the failure."""
    make, f = ver
    v, repo = make()
    dec, rep = review(v, candidate(repo, f["base"], {"src/value.txt": "42 tsdetail\n"}))
    assert (dec, rep["why"]) == ("rejected", "step_failed:tsc")
    step = rep["steps"][0]
    assert "keeps the old contract" not in step["outputTail"]                       # the old evidence lost it
    log = Path(step["log"]["path"])
    data = log.read_bytes()
    assert log.parent == v.run_dir_of() and log.name == "verify-r1-tsc.log"           # beside, never inside, the worktree
    assert Path(rep["worktree"]) not in log.parents
    assert b"keeps the old contract" in data and step["log"] == {"path": str(log), "sha256": hashlib.sha256(data).hexdigest(),
                                                                   "bytes": len(data)}
    assert step["logTruncated"] is False and step["outputSha256"] == hashlib.sha256(data).hexdigest()
    assert step["failureLines"] == ["FAIL  tests/unit/value.test.ts > value > keeps the old contract"]


def test_the_log_holds_only_the_retained_prefix_and_says_so(ver, fx):
    make, f = ver
    _, doc = fx
    v, repo = make(edited(doc, lambda d: d["verify"]["steps"][0].update(outputKeepBytes=64)))
    dec, rep = review(v, candidate(repo, f["base"], {"src/value.txt": "42 tsdetail\n"}))
    step = rep["steps"][0]
    data = Path(step["log"]["path"]).read_bytes()
    assert len(data) == 64 == step["log"]["bytes"] and step["logTruncated"] is True        # the output cap is unchanged
    assert step["outputBytes"] > 64 and step["outputSha256"] != step["log"]["sha256"]      # digest/count of the WHOLE stream
    assert step["failureLines"] == ["FAIL  tests/unit/value.test.ts > value > keeps the old contract"]


def test_an_accepted_run_logs_every_step_and_the_oracle_streams_separately(ver):
    make, f = ver
    v, repo = make()
    dec, rep = review(v, candidate(repo, f["base"], {"src/value.txt": "42\n"}))
    assert (dec, rep["why"]) == ("accepted", "all_steps_pass") and rep["status"] == TR.CLEAN_STATUS
    tsc, oracle = rep["steps"]
    for s in (tsc, oracle):
        assert Path(s["log"]["path"]).is_file() and s["failureLines"] == [] and s["logTruncated"] is False
    assert json.loads(Path(oracle["log"]["path"]).read_bytes())["numPassedTests"] == 3                # stdout = the report
    assert b"ExperimentalWarning" in Path(oracle["stderrLog"]["path"]).read_bytes() and "stderrLog" not in tsc


def test_a_log_that_cannot_be_written_is_recorded_and_never_changes_the_verdict(ver):
    make, f = ver
    v, repo = make()
    (v.run_dir_of() / "verify-r1-tsc.log").write_text("pre-existing", encoding="utf-8")   # exclusive create fails
    dec, rep = review(v, candidate(repo, f["base"], {"src/value.txt": "42\n"}))
    assert (dec, rep["why"]) == ("accepted", "all_steps_pass")
    assert rep["steps"][0]["logError"] == [{"part": "log", "type": "FileExistsError"}] and "log" not in rep["steps"][0]
    assert (v.run_dir_of() / "verify-r1-tsc.log").read_text(encoding="utf-8") == "pre-existing"   # never overwritten


def test_failure_lines_strip_ansi_dedupe_and_bound():
    text = "\x1b[31m FAIL \x1b[39m tests/a.test.ts > x\n × y\n FAIL  tests/a.test.ts > x\n" + "".join(f" FAIL  t{i}\n" for i in range(40))
    got = TR.failure_lines(text)
    assert got[:2] == ["FAIL  tests/a.test.ts > x", "× y"] and len(got) == TR.FAILURE_LINES_MAX
    assert TR.failure_lines("FAIL " + "z" * 1000)[0] == ("FAIL " + "z" * 1000)[:300]
    assert TR.failure_lines("all good\nTests 3 passed\n") == []


# --- ownedRefs: a failed read is never an empty ref set (check-002, root msg 2080) ---------------------------

def test_owned_refs_distinguishes_a_failed_read_from_a_truly_empty_set(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    sha = commit(repo, {"a.txt": "a\n"}, "c0")
    assert TR.owned_refs(repo) == []                                    # git succeeded and none exist
    git(repo, "update-ref", f"{W.REF_ROOT}/run1/r1", sha)
    assert TR.owned_refs(repo) == [f"{sha} {W.REF_ROOT}/run1/r1"]
    not_git = tmp_path / "plain"
    not_git.mkdir()
    with pytest.raises(TR.GitReadFailed) as e:
        TR.owned_refs(not_git)
    assert e.value.detail["rc"] not in (0, None) and "not a git repository" in e.value.detail["stderr"]
    rec = TR.error_record("ownedRefs", e.value)
    assert rec == {"part": "ownedRefs", "type": "GitReadFailed", "detail": e.value.detail}
    assert TR.error_record("journal", ValueError("x")) == {"part": "journal", "type": "ValueError"}


def test_a_failed_git_read_keeps_only_the_bounded_tail_of_stderr():
    long = "y" * (TR.ERR_KEEP + 50) + "fatal: why"
    detail = TR.GitReadFailed(subprocess.CompletedProcess(["git"], 128, "", long)).detail
    assert detail == {"rc": 128, "stderr": long[-TR.ERR_KEEP:]} and detail["stderr"].endswith("fatal: why")


# --- runner-source provenance (HK-ISSUE-016, root msgs 2135/2144): before the claim, never changes the run -------

def test_provenance_is_persisted_before_the_first_claim_and_bound_in_the_evidence(taskrun, client, tmp_path, monkeypatch):
    go, _ = taskrun
    seen = []
    real_claim = client.claim

    def claim(*a, **kw):
        seen.append(sorted(p.name for p in (tmp_path / "rr").glob("provenance-*.json")))
        return real_claim(*a, **kw)
    monkeypatch.setattr(client, "claim", claim)
    res, ev = go("value_ok")
    assert (res.outcome, len(res.rounds)) == ("accepted", 1) and launches(ev) == 1
    assert seen and all(len(names) == 1 for names in seen)                   # the file existed at EVERY claim
    f = Path(ev["provenanceFile"])
    assert f.name == seen[0][0] and hashlib.sha256(f.read_bytes()).hexdigest() == ev["provenanceSha256"]
    assert json.loads(f.read_text(encoding="utf-8")) == ev["provenance"] and ev["errors"] == []
    assert {"e1/task_runner.py", "e1/cli_worker.py", "e1/pilot.py", "e1/durable.py", "e1/acts_durable.py"} <= set(ev["provenance"]["modules"])


def test_a_failed_provenance_write_changes_nothing_but_the_evidence(taskrun, monkeypatch):
    go, _ = taskrun
    monkeypatch.setattr(TR, "PROVENANCE", "no-such-dir/provenance-{run_id}.json")
    res, ev = go("value_ok")
    assert (res.outcome, len(res.rounds)) == ("accepted", 1) and launches(ev) == 1
    assert ev["provenance"]["schema"] == "hekate-run-provenance.v0"                # the observation is still embedded
    assert (ev["provenanceFile"], ev["provenanceSha256"]) == (None, None)        # ...but no file and no hash is claimed
    assert ev["errors"] == [{"part": "provenanceFile", "type": "FileNotFoundError"}]


def test_a_failed_provenance_observation_changes_nothing_but_the_evidence(taskrun, monkeypatch):
    go, _ = taskrun

    def boom(*a, **kw):
        raise RuntimeError("observe failed")
    monkeypatch.setattr(TR.PV, "observe", boom)
    res2, ev2 = go("value_ok")
    assert (res2.outcome, len(res2.rounds)) == ("accepted", 1) and launches(ev2) == 1
    assert (ev2["provenance"], ev2["provenanceFile"], ev2["provenanceSha256"]) == (None, None, None)
    assert ev2["errors"] == [{"part": "provenance", "type": "RuntimeError"}]


def test_an_existing_provenance_file_is_never_overwritten(tmp_path):
    prov, f, errs = TR.record_provenance(tmp_path, "abc123")
    assert errs == [] and Path(f["provenanceFile"]).is_file()
    before = Path(f["provenanceFile"]).read_bytes()
    prov2, f2, errs2 = TR.record_provenance(tmp_path, "abc123")
    assert errs2 == [{"part": "provenanceFile", "type": "exists"}] and f2 == {"provenanceFile": None, "provenanceSha256": None}
    assert prov2 is not None and Path(f["provenanceFile"]).read_bytes() == before
