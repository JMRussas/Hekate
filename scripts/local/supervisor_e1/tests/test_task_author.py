"""Task authoring v0 (e1/task_author.py) OFFLINE, on the task-runner fixture repo with the FAKE pinned node and fake
npm/vitest/tsc. No real npm, Node, model, harness or PlanStore."""

import json
import subprocess
from pathlib import Path

import pytest

from e1 import task_author as TA
from e1 import task_runner as TR
from e1 import task_spec as T
from task_support import (FAKE_NODE, FIRST, LOCK, NPM_CLI, ORACLE_A, ORACLE_B, TSC, VITEST, edited, make_repo, pinned_node, sha,
                          sha_file, source_state)

PATCH_42 = "diff --git a/src/value.txt b/src/value.txt\n--- a/src/value.txt\n+++ b/src/value.txt\n@@ -1 +1 @@\n-0\n+42\n"
PATCH_41 = PATCH_42.replace("+42", "+41")
PATCH_OUTSIDE = PATCH_42 + ("diff --git a/src/other.txt b/src/other.txt\n--- a/src/other.txt\n+++ b/src/other.txt\n"
                            "@@ -1 +1 @@\n-x\n+y\n")


@pytest.fixture(autouse=True)
def npm_cache(tmp_path, monkeypatch) -> Path:
    cache = tmp_path / "npm-cache"
    (cache / "_cacache").mkdir(parents=True)
    monkeypatch.setenv("npm_config_cache", str(cache))
    return cache


def profile_of(repo: str, node: Path) -> dict:
    return {
        "version": TA.PROFILE_VERSION,
        "repo": Path(repo).as_posix(),
        "tools": {"pinnedNodeExe": {"path": node.as_posix(), "version": "v24.0.0", "sha256": sha_file(node)},
                  "npmCli": {"path": NPM_CLI.as_posix(), "version": "11.0.0", "sha256": sha_file(NPM_CLI)}},
        "lock": {"packageLock": sha(LOCK.encode("utf-8")), "vitestEntry": sha_file(FAKE_NODE / "vitest.py"),
                 "tscEntry": sha_file(FAKE_NODE / "tsc.py")},
        "deps": {"kind": "npm-ci", "network": "offline", "timeoutS": 60, "outputKeepBytes": 4096},
        "oracleRunner": {"argv": [node.as_posix(), VITEST, "run", "--reporter=json", "{oracle}"], "timeoutS": 60,
                         "reportMaxBytes": 65536, "outputKeepBytes": 65536},
        "steps": [{"name": "tsc", "argv": [node.as_posix(), TSC, "--noEmit"], "timeoutS": 60, "outputKeepBytes": 4096}],
        "worker": {"model": "fake-model", "budgetUsd": "0.50", "maxRounds": 2, "maxTurns": 5},
    }


def draft_of(f: dict) -> dict:
    return {"version": TA.DRAFT_VERSION, "source": {"anchorCommit": f["anchor"], "taskBaseCommit": f["base"]},
            "task": {"text": "Set src/value.txt to 42.", "criteria": "Every oracle case passes."},
            "allow": ["src/value.txt"], "oracle": [ORACLE_A, ORACLE_B], "worker": {},
            "metadata": {"issue": "FX-1", "title": "fixture task", "notes": []}}


@pytest.fixture
def ax(tmp_path, tmp_path_factory):
    f = make_repo(tmp_path)
    node = pinned_node(tmp_path_factory.getbasetemp())
    return f, profile_of(f["repo"], node), draft_of(f)


def files(tmp_path: Path, profile: dict, draft: dict, patch: str = PATCH_42) -> tuple[Path, Path, Path]:
    p, d, r = tmp_path / "profile.json", tmp_path / "draft.json", tmp_path / "reference.patch"
    p.write_text(json.dumps(profile), encoding="utf-8")
    d.write_text(json.dumps(draft), encoding="utf-8")
    r.write_bytes(patch.encode("utf-8"))
    return d, p, r


def author(tmp_path, profile, draft, patch=PATCH_42, out="out"):
    d, p, r = files(tmp_path, profile, draft, patch)
    return TA.draft(d, p, r, tmp_path / out)


# ------------------------------------------------------------------------ ready

def test_a_draft_becomes_a_ready_package_proven_by_preflight_and_the_reference(ax, tmp_path):
    f, profile, draft = ax
    before = source_state(f["repo"])
    code, rec = author(tmp_path, profile, draft)
    out = tmp_path / "out"
    assert code == 0 and rec["ready"] is True, rec.get("notReady")
    assert sorted(p.name for p in (out / "package").iterdir()) == ["spec.json"]          # the package is the spec only
    spec = T.load(out / "package" / "spec.json")
    assert (out / "package" / "spec.json").read_bytes() == (out / "evidence" / "spec.json").read_bytes()
    assert {(c["file"], c["fullName"], c["status"], c["failureFirstLine"]) for c in spec.doc["oracle"]["baseline"]["cases"]} == {
        (ORACLE_B, "value is still 42", "failed", FIRST), (ORACLE_A, "value is 42", "failed", FIRST),
        (ORACLE_A, "value file loads", "passed", None)}
    assert spec.doc["verify"]["steps"][0]["name"] == "oracle" and spec.doc["deps"]["network"] == "offline"
    assert spec.doc["worker"]["testCommand"] == " ".join(spec.doc["verify"]["steps"][0]["argv"])
    assert TA.PROVISIONAL not in (out / "package" / "spec.json").read_text(encoding="utf-8")
    saved = json.loads((out / "authoring.json").read_text(encoding="utf-8"))
    assert saved["ready"] is True and saved["package"]["sha256"] == spec.sha256
    assert saved["inputs"]["profile"]["sha256"] == sha((tmp_path / "profile.json").read_bytes())   # review 1989 R-b
    assert saved["preflight"] == dict(saved["preflight"], ok=True, cases=3, failed=2)
    assert (saved["reference"]["decision"], saved["reference"]["why"]) == ("accepted", "all_steps_pass")
    assert saved["captureSpec"]["label"].startswith("PROVISIONAL")
    assert source_state(f["repo"]) == before                                              # the source is never touched


def test_the_cli_prints_the_record_and_exits_by_readiness(ax, tmp_path, capsys):
    f, profile, draft = ax
    d, p, r = files(tmp_path, profile, draft)
    assert TA.main(["draft", "--draft", str(d), "--profile", str(p), "--reference", str(r), "--out", str(tmp_path / "o")]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is True
    assert TA.main(["draft", "--draft", str(d), "--profile", str(p), "--reference", str(r), "--out", str(tmp_path / "o")]) == 2
    assert json.loads(capsys.readouterr().out)["refused"] == "out_exists"


# ------------------------------------------------------------------------ refused before any effect (exit 2, no DIR)

@pytest.mark.parametrize("which, mutate, code", [
    ("profile", lambda p: p.update(version="v9"), "profile_version"),
    ("profile", lambda p: p["deps"].update(network="prefer-offline"), "profile_not_offline"),
    ("profile", lambda p: p["oracleRunner"].update(argv=p["oracleRunner"]["argv"][:-1]), "profile_invalid"),
    ("profile", lambda p: p["steps"].append(dict(p["steps"][0], name="oracle")), "profile_invalid"),
    ("profile", lambda p: p.update(extra=1), "profile_invalid"),
    ("profile", lambda p: p["lock"].update(packageLock="0" * 64), "profile_lock_mismatch"),
    ("draft", lambda d: d.update(allow=[ORACLE_A]), "draft_allow_overlaps_oracle"),
    ("draft", lambda d: d.update(oracle=[ORACLE_A, ORACLE_B, "tests/unit/absent.test.ts"]), "oracle_missing_at_base"),
    ("draft", lambda d: d["worker"].update(effort="max"), "draft_invalid"),
    ("draft", lambda d: d.update(allow=["../x"]), "draft_invalid"),
    ("draft", lambda d: d["worker"].update(maxRounds=9), "draft_spec_invalid"),     # the UNCHANGED spec validation
])
def test_bad_inputs_are_refused_before_any_effect(ax, tmp_path, which, mutate, code):
    f, profile, draft = ax
    profile, draft = (edited(profile, mutate), draft) if which == "profile" else (profile, edited(draft, mutate))
    with pytest.raises(TA.AuthorRefused) as e:
        author(tmp_path, profile, draft)
    assert e.value.code == code and not (tmp_path / "out").exists()


def test_a_tool_pin_mismatch_is_refused_before_any_subprocess(ax, tmp_path, monkeypatch):
    f, profile, draft = ax
    profile["tools"]["npmCli"]["sha256"] = "0" * 64

    def no_git(*a, **kw):
        raise AssertionError("no subprocess before the tool pins hold")
    monkeypatch.setattr(TR, "blob_sha256", no_git)
    monkeypatch.setattr(TR, "_git", no_git)
    with pytest.raises(TA.AuthorRefused) as e:
        author(tmp_path, profile, draft)
    assert e.value.code == "profile_tool_mismatch" and not (tmp_path / "out").exists()


def test_an_oversized_reference_is_refused(ax, tmp_path):
    f, profile, draft = ax
    with pytest.raises(TA.AuthorRefused) as e:
        author(tmp_path, profile, draft, patch="x" * (T.SPEC_MAX + 1))
    assert e.value.code == "reference_size"


# ------------------------------------------------------------------------ not ready (exit 1, evidence kept, no package)

def not_ready(tmp_path, profile, draft, patch=PATCH_42) -> dict:
    code, rec = author(tmp_path, profile, draft, patch)
    out = tmp_path / "out"
    assert code == 1 and rec["ready"] is False and not (out / "package").exists()
    assert json.loads((out / "authoring.json").read_text(encoding="utf-8")) == json.loads(json.dumps(rec, default=str))
    return rec["notReady"]


def test_a_reference_outside_the_allow_list_is_not_ready(ax, tmp_path):
    f, profile, draft = ax
    nr = not_ready(tmp_path, profile, draft, PATCH_OUTSIDE)
    assert (nr["stage"], nr["code"], nr["detail"]) == ("reference", "reference_rejected", "diff_outside_allowlist")
    assert (tmp_path / "out" / "evidence" / "reference.json").is_file()


def test_a_reference_that_fails_the_oracle_is_not_ready(ax, tmp_path):
    f, profile, draft = ax
    nr = not_ready(tmp_path, profile, draft, PATCH_41)
    assert (nr["stage"], nr["code"]) == ("reference", "reference_rejected")


def test_an_oracle_that_never_fails_at_the_base_is_not_ready(tmp_path, tmp_path_factory):
    f = make_repo(tmp_path, extra={"src/value.txt": "42\n"})
    nr = not_ready(tmp_path, profile_of(f["repo"], pinned_node(tmp_path_factory.getbasetemp())), draft_of(f))
    assert (nr["stage"], nr["code"]) == ("capture", "oracle_never_fails")


def test_a_crashing_oracle_run_is_not_ready(tmp_path, tmp_path_factory):
    f = make_repo(tmp_path, extra={"src/value.txt": "crash\n"})
    nr = not_ready(tmp_path, profile_of(f["repo"], pinned_node(tmp_path_factory.getbasetemp())), draft_of(f))
    assert (nr["stage"], nr["code"]) == ("capture", "capture_report_not_json")


def test_a_failure_that_is_not_an_assertion_is_not_ready(ax, tmp_path, monkeypatch):
    f, profile, draft = ax
    real = TR.report_cases

    def runtime_error(report, wt):
        got, counts = real(report, wt)
        return {(fl, n, s, "TypeError: x is not a function" if s == "failed" else m) for fl, n, s, m in got}, counts
    monkeypatch.setattr(TR, "report_cases", runtime_error)
    nr = not_ready(tmp_path, profile, draft)
    assert (nr["stage"], nr["code"]) == ("capture", "baseline_not_assertion")


def test_the_helper_imports_no_launch_path():
    imports = [l for l in Path(TA.__file__).read_text(encoding="utf-8").splitlines() if l.startswith(("import ", "from "))]
    assert sorted(l for l in imports if "e1" in l) == ["from e1 import consumer as C", "from e1 import pilot as P",
                                                      "from e1 import task_runner as TR", "from e1 import task_spec as T"]
    assert not hasattr(TA, "run") and "launch_real_model" not in Path(TA.__file__).read_text(encoding="utf-8")


# ------------------------------------------------------------------------ review 2002 F1/F2

@pytest.mark.parametrize("key, code", [("anchorCommit", "anchor_not_found"), ("taskBaseCommit", "base_not_found")])
def test_an_unknown_commit_is_named_before_the_lock_check(ax, tmp_path, key, code):
    f, profile, draft = ax
    draft["source"][key] = "f" * 40
    with pytest.raises(TA.AuthorRefused) as e:
        author(tmp_path, profile, draft)
    assert e.value.code == code and not (tmp_path / "out").exists()


def test_an_unexpected_error_after_effects_is_a_typed_not_ready(ax, tmp_path, monkeypatch, capsys):
    f, profile, draft = ax

    def boom(*a, **kw):
        raise RuntimeError("something unforeseen")
    monkeypatch.setattr(TR, "run_baseline", boom)
    d, p, r = files(tmp_path, profile, draft)
    assert TA.main(["draft", "--draft", str(d), "--profile", str(p), "--reference", str(r), "--out", str(tmp_path / "out")]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["notReady"] == {"stage": "capture", "code": "unexpected_error", "detail": "RuntimeError"}
    assert json.loads((tmp_path / "out" / "authoring.json").read_text(encoding="utf-8"))["notReady"]["code"] == "unexpected_error"
    assert not (tmp_path / "out" / "package").exists()


def test_an_unforeseen_error_before_any_effect_is_a_typed_refusal_with_no_folder(ax, tmp_path, monkeypatch, capsys):
    """Review 2005: e.g. a git read timing out after the tool pins: exit 2, the type name only, nothing created."""
    f, profile, draft = ax

    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired(["git"], 300)
    monkeypatch.setattr(TR, "_git", timeout)
    d, p, r = files(tmp_path, profile, draft)
    assert TA.main(["draft", "--draft", str(d), "--profile", str(p), "--reference", str(r), "--out", str(tmp_path / "out")]) == 2
    assert json.loads(capsys.readouterr().out) == {"refused": "pre_effect_error", "detail": "TimeoutExpired", "effects": "none"}
    assert not (tmp_path / "out").exists()


def test_a_record_that_cannot_be_written_is_not_reported_as_no_effects(ax, tmp_path, monkeypatch, capsys):
    """Review 2005: the folder exists, so a failed authoring.json is exit 1 with recordFailed, never a pre-effect refusal."""
    f, profile, draft = ax
    real = TA._write_new

    def no_record(path, data):
        if Path(path).name == "authoring.json":
            raise PermissionError("denied")
        real(path, data)
    monkeypatch.setattr(TA, "_write_new", no_record)
    d, p, r = files(tmp_path, profile, draft)
    assert TA.main(["draft", "--draft", str(d), "--profile", str(p), "--reference", str(r), "--out", str(tmp_path / "out")]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["ready"] is False and printed["recordFailed"]["code"] == TA.RECORD_FAILED
    assert printed["recordFailed"]["detail"] == "PermissionError" and "refused" not in printed
    assert (tmp_path / "out").is_dir() and not (tmp_path / "out" / "authoring.json").exists()
