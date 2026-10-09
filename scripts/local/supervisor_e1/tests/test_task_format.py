"""supervised-task-spec.v1 + the trusted supervisor formatter (plan 051). OFFLINE: the fake pinned "node" (Python), fake npm
and the fake prettier in tests/fake_node/ (strip trailing blanks, one final newline; its pinned config picks a misbehaviour)."""

import json
from pathlib import Path

import pytest

from e1 import cli_worker as W
from e1 import consumer as C
from e1 import pilot as P
from e1 import pilot_real as R
from e1 import task_author as A
from e1 import task_format as TF
from e1 import task_runner as TR
from e1 import task_spec as T
from e1.acts import E2C_BOUNDS
from e1.acts_durable import ActsJournal, install_acts
from e1.durable import install
from e1.handoff_durable import install_handoff
from e2b_support import reset_schema
from helpers import key
from task_support import (FAKE_NODE, FIRST, NPM_CLI, ORACLE_A, ORACLE_B, VITEST, edited, git, make_repo, make_spec, pinned_node, sha,
                          sha_file, write_spec)

FAKE = Path(__file__).resolve().parent / "fake_cli.py"
PY = Path(R.sys.executable).resolve()
PRETTIER = "node_modules/prettier/bin/prettier.cjs"
CFG = ".prettierrc.json"
PACKAGE = '{"name": "fx", "private": true, "devDependencies": {"prettier": "3.0.0"}}\n'
UNFORMATTED = "42   \n"


@pytest.fixture(autouse=True)
def npm_cache(tmp_path, monkeypatch) -> Path:
    cache = tmp_path / "npm-cache"
    (cache / "_cacache").mkdir(parents=True)
    monkeypatch.setenv("npm_config_cache", str(cache))
    return cache


def cfg_text(mode: str) -> str:
    return json.dumps({"mode": mode}) + "\n"


def fmt_step(node: Path) -> dict:
    return {"name": "fmtcheck", "timeoutS": 30, "outputKeepBytes": 4096,
            "argv": [node.as_posix(), PRETTIER, "--check", "--config", CFG, "--no-editorconfig", "src/value.txt"]}


def formatter(mode: str = "ok", timeout: int = 20) -> dict:
    return {"prettierEntry": {"path": PRETTIER, "version": "3.0.0", "sha256": sha_file(FAKE_NODE / "prettier.py")},
            "config": {"path": CFG, "sha256": sha(cfg_text(mode).encode())}, "paths": ["src/value.txt"],
            "timeoutS": timeout, "outputKeepBytes": 4096}


def v0_doc(f, node, mode="ok") -> dict:
    doc = make_spec(f, node)
    doc["verify"]["steps"].append(fmt_step(node))
    return doc


def v1_doc(f, node, mode="ok", timeout=20) -> dict:
    doc = v0_doc(f, node)
    doc["specVersion"] = T.SPEC_VERSION_V1
    doc["formatter"] = formatter(mode, timeout)
    return doc


def fmt_repo(tmp_path, mode="ok") -> dict:
    return make_repo(tmp_path, extra={CFG: cfg_text(mode), "package.json": PACKAGE})


def refused(doc) -> str:
    with pytest.raises(T.SpecRefused) as e:
        T.parse(json.dumps(doc).encode("utf-8"))
    return e.value.code


# ------------------------------------------------------------------------ the closed v1 spec; v0 untouched

def test_v1_parses_and_a_v0_spec_keeps_its_shape_and_has_no_formatter(tmp_path, tmp_path_factory):
    f = fmt_repo(tmp_path)
    node = pinned_node(tmp_path_factory.getbasetemp())
    v1 = T.parse(json.dumps(v1_doc(f, node)).encode("utf-8"))
    assert v1.formatter["paths"] == ["src/value.txt"] and v1.doc["specVersion"] == T.SPEC_VERSION_V1
    v0 = T.parse(json.dumps(make_spec(f, node)).encode("utf-8"))
    assert v0.formatter is None and v0.doc["specVersion"] == T.SPEC_VERSION == "supervised-task-spec.v0"
    assert refused(edited(make_spec(f, node), lambda d: d.update(formatter=formatter()))) == "spec_shape"      # v0 never takes one
    assert refused(edited(v1_doc(f, node), lambda d: d.pop("formatter"))) == "spec_shape"                      # v1 requires one


def test_step_slots_v0_stays_4_and_v1_allows_exactly_5(tmp_path, tmp_path_factory):
    f = fmt_repo(tmp_path)
    node = pinned_node(tmp_path_factory.getbasetemp())
    extra = lambda n: {"name": f"extra{n}", "argv": [node.as_posix(), "tools/check.mjs"], "timeoutS": 5, "outputKeepBytes": 64}

    def with_steps(doc, n):
        return edited(doc, lambda d: d["verify"]["steps"].extend(extra(i) for i in range(n)))
    assert len(T.parse(json.dumps(with_steps(v0_doc(f, node), 1)).encode()).doc["verify"]["steps"]) == 4
    assert refused(with_steps(v0_doc(f, node), 2)) == "spec_value"                                          # v0: 5 steps refused
    assert len(T.parse(json.dumps(with_steps(v1_doc(f, node), 2)).encode()).doc["verify"]["steps"]) == 5
    assert refused(with_steps(v1_doc(f, node), 3)) == "spec_value"                                          # v1: 6 refused


@pytest.mark.parametrize("mutate, code", [
    (lambda d: d["formatter"]["paths"].append("src/other.txt"), "spec_formatter"),               # not an allow path
    (lambda d: d["formatter"].update(paths=["src/value.txt", "src/value.txt"]), "spec_formatter"),
    (lambda d: d["formatter"].update(paths=[]), "spec_value"),
    (lambda d: d["formatter"].update(paths=["src/../value.txt"]), "spec_value"),
    (lambda d: d["formatter"].update(paths=["src/*.txt"]), "spec_value"),
    (lambda d: d["formatter"].update(paths=["src/va lue.txt"]), "spec_value"),
    (lambda d: (d["allow"].append({"path": "-w", "status": "M", "mode": "100644"}), d["formatter"].update(paths=["-w"])), "spec_formatter"),
    (lambda d: d["formatter"]["config"].update(path="src/value.txt"), "spec_formatter"),         # config is an allow path
    (lambda d: d["formatter"]["config"].update(path="node_modules/x/c.json"), "spec_formatter"),
    (lambda d: d["formatter"]["config"].update(path="../c.json"), "spec_value"),
    (lambda d: d["formatter"]["prettierEntry"].update(path="node_modules/other/bin.js"), "spec_value"),
    (lambda d: d["formatter"]["prettierEntry"].update(path="node_modules/prettier/../../x.js"), "spec_value"),
    (lambda d: d["formatter"]["prettierEntry"].update(path="/abs/prettier.cjs"), "spec_value"),
    (lambda d: d["formatter"]["prettierEntry"].update(sha256="abc"), "spec_value"),
    (lambda d: d["formatter"]["prettierEntry"].update(version="latest"), "spec_value"),
    (lambda d: d["formatter"].update(timeoutS=0), "spec_value"),
    (lambda d: d["formatter"].update(timeoutS=121), "spec_value"),
    (lambda d: d["formatter"].update(outputKeepBytes=(64 << 10) + 1), "spec_value"),
    (lambda d: d["formatter"].update(argv=["--evil"]), "spec_shape"),                            # no free-form argv
])
def test_unsafe_formatter_descriptors_are_refused_before_anything_runs(tmp_path, tmp_path_factory, mutate, code):
    f = fmt_repo(tmp_path)
    assert refused(edited(v1_doc(f, pinned_node(tmp_path_factory.getbasetemp())), mutate)) == code


# ------------------------------------------------------------------------ the formatter itself

def prepared(tmp_path, tmp_path_factory, mode="ok", timeout=20, value=UNFORMATTED):
    f = fmt_repo(tmp_path, mode)
    node = pinned_node(tmp_path_factory.getbasetemp())
    spec = write_spec(v1_doc(f, node, mode, timeout), tmp_path / "spec.json")
    root = tmp_path / "rr"
    repo = Path(TR.preflight(spec, root)["repo"])
    run_dir = root / "run"
    run_dir.mkdir()
    wt = root / "cand"
    git(repo, "worktree", "add", "--detach", str(wt), f["base"])
    (wt / "src/value.txt").write_bytes(value.encode("utf-8"))
    return spec, repo, root, run_dir, wt, f


def commit_wt(wt: Path) -> str:
    git(wt, "add", "-A")
    git(wt, "-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false", "commit", "-qm", "candidate")
    return git(wt, "rev-parse", "HEAD")


def review(v: TR.SpecVerifier, art: str, rnd: int) -> tuple[str, dict]:
    view = {"candidateDigest": "c" * 64, "mandatory": {"state": {"mandatory": {"identity": {"artifactRef": art}}}}}
    part, digest = C.render(view)
    out = v(P.ReviewOrder(rnd, art, part, digest, "c" * 64, "criteria"))
    return out.decision, json.loads(out.evidence)


def test_a_correct_but_unformatted_candidate_is_formatted_with_real_pre_and_post_evidence_and_then_accepted(tmp_path, tmp_path_factory):
    spec, repo, root, run_dir, wt, f = prepared(tmp_path, tmp_path_factory)
    ev = TF.format_candidate(spec, repo, root, run_dir, wt, 1)
    assert (wt / "src/value.txt").read_bytes() == b"42\n"
    [file] = ev["files"]
    assert file["changed"] and file["preSha256"] == sha(UNFORMATTED.encode()) and file["postSha256"] == sha(b"42\n")
    assert Path(file["preFile"]).read_bytes() == UNFORMATTED.encode() and Path(file["postFile"]).read_bytes() == b"42\n"
    assert (file["preBytes"], file["postBytes"]) == (len(UNFORMATTED), 3) and ev["idempotent"] is True
    assert [r["run"] for r in ev["runs"]] == ["format", "idempotence"] and all(r["rc"] == 0 for r in ev["runs"])
    assert ev["runs"][0]["argv"][2:] == ["--write", "--config", CFG, "--no-editorconfig", "src/value.txt"]
    assert ev["kind"] == "host-derivation" and ev["usage"].startswith("none") and ev["formatter"] == spec.formatter
    art = commit_wt(wt)
    v = TR.SpecVerifier(spec, repo, lambda: run_dir, root)
    dec, rep = review(v, art, 1)
    assert (dec, rep["why"]) == ("accepted", "all_steps_pass") and [s["name"] for s in rep["steps"]] == ["tsc", "oracle", "fmtcheck"]


def test_the_verifier_does_not_normalize_an_unformatted_candidate_with_or_without_a_formatter(tmp_path, tmp_path_factory):
    spec, repo, root, run_dir, wt, f = prepared(tmp_path, tmp_path_factory)
    art = commit_wt(wt)                                                           # NOT formatted
    dec, rep = review(TR.SpecVerifier(spec, repo, lambda: run_dir, root), art, 1)
    assert (dec, rep["why"]) == ("rejected", "step_failed:fmtcheck")
    assert (Path(rep["worktree"]) / "src/value.txt").read_bytes() == UNFORMATTED.encode()                  # never rewritten
    v0 = write_spec(v0_doc(f, pinned_node(tmp_path_factory.getbasetemp())), tmp_path / "v0.json")
    dec, rep = review(TR.SpecVerifier(v0, repo, lambda: run_dir, root), art, 2)
    assert (dec, rep["why"]) == ("rejected", "step_failed:fmtcheck")


def test_an_already_formatted_candidate_is_byte_identical_and_recorded_as_unchanged(tmp_path, tmp_path_factory):
    spec, repo, root, run_dir, wt, _ = prepared(tmp_path, tmp_path_factory, value="42\n")
    ev = TF.format_candidate(spec, repo, root, run_dir, wt, 1)
    assert ev["files"][0]["changed"] is False and (wt / "src/value.txt").read_bytes() == b"42\n"


@pytest.mark.parametrize("mode, timeout, reason", [
    ("nonidem", 20, "format_not_idempotent"), ("touch", 20, "format_out_of_scope"),
    ("fail", 20, "format_failed"), ("hang", 1, "format_timeout"),
])
def test_formatter_misbehaviour_is_uncertain_keeps_pre_evidence_and_leaves_the_candidate_alone(tmp_path, tmp_path_factory, mode, timeout, reason):
    spec, repo, root, run_dir, wt, _ = prepared(tmp_path, tmp_path_factory, mode, timeout)
    with pytest.raises(W.FinalizeRefused) as e:
        TF.format_candidate(spec, repo, root, run_dir, wt, 1)
    assert (e.value.status, e.value.reason) == ("unknown", reason)                # infrastructure doubt: no model retry
    assert e.value.evidence["preSha256"] == {"src/value.txt": sha(UNFORMATTED.encode())} and e.value.evidence["refused"] == reason
    assert (wt / "src/value.txt").read_bytes() == UNFORMATTED.encode()
    assert Path(e.value.evidence["dir"], "pre", "src/value.txt").read_bytes() == UNFORMATTED.encode()


@pytest.mark.parametrize("extra", [{"src/other.txt": "y\n"}, {ORACLE_A: '{"cases": []}\n'}, {"package-lock.json": "{}\n"}, {"src/new.txt": "n\n"}])
def test_an_unsafe_candidate_diff_is_refused_before_any_spawn(tmp_path, tmp_path_factory, monkeypatch, extra):
    spec, repo, root, run_dir, wt, _ = prepared(tmp_path, tmp_path_factory)
    for rel, text in extra.items():
        (wt / rel).write_bytes(text.encode())
    monkeypatch.setattr(TF.R, "run_bounded", lambda *a, **k: pytest.fail("spawned"))
    monkeypatch.setattr(TR, "install_deps", lambda *a, **k: pytest.fail("installed"))
    with pytest.raises(W.FinalizeRefused) as e:
        TF.format_candidate(spec, repo, root, run_dir, wt, 1)
    assert (e.value.status, e.value.reason) == ("failed", "diff_outside_allowlist")
    assert (wt / "src/value.txt").read_bytes() == UNFORMATTED.encode() and not (run_dir / "format-r1").exists()


def test_a_changed_formatter_pin_is_refused_at_preflight_and_right_before_the_spawn(tmp_path, tmp_path_factory, monkeypatch):
    f = fmt_repo(tmp_path)
    node = pinned_node(tmp_path_factory.getbasetemp())
    for n, mutate in enumerate((lambda d: d["formatter"]["prettierEntry"].update(sha256="0" * 64),
                                lambda d: d["formatter"]["config"].update(sha256="1" * 64))):
        spec = write_spec(edited(v1_doc(f, node), mutate), tmp_path / f"bad{n}.json")
        with pytest.raises(TR.PreflightRefused) as e:
            TR.preflight(spec, tmp_path / f"rr-bad{n}")
        assert e.value.code == "formatter_pin_mismatch"
    spec, repo, root, run_dir, wt, _ = prepared(tmp_path / "before-spawn", tmp_path_factory)
    real = TR.install_deps

    def tamper(s, w, r):
        out = real(s, w, r)
        (Path(w) / PRETTIER).write_bytes(b"import sys; sys.exit(0)\n")                  # a swapped entry after the install
        return out
    monkeypatch.setattr(TR, "install_deps", tamper)
    real_run = TF.R.run_bounded

    def guard_spawn(argv, **kwargs):
        if argv[1] == PRETTIER:
            pytest.fail("spawned a tampered formatter")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(TF.R, "run_bounded", guard_spawn)
    with pytest.raises(W.FinalizeRefused) as e:
        TF.format_candidate(spec, repo, root, run_dir, wt, 1)
    assert (e.value.status, e.value.reason) == ("unknown", "formatter_pin_mismatch_before")


def test_a_changed_oracle_in_the_format_tree_is_an_integrity_refusal_before_the_spawn(tmp_path, tmp_path_factory, monkeypatch):
    spec, repo, root, run_dir, wt, _ = prepared(tmp_path, tmp_path_factory)
    real = TR.install_deps

    def tamper(s, w, r):
        out = real(s, w, r)
        (Path(w) / ORACLE_B).write_bytes(b"{}\n")
        return out
    monkeypatch.setattr(TR, "install_deps", tamper)
    with pytest.raises(W.FinalizeRefused) as e:
        TF.format_candidate(spec, repo, root, run_dir, wt, 1)
    assert (e.value.status, e.value.reason) == ("unknown", "integrity_before_format")


# ------------------------------------------------------------------------ the hook inside the real run

@pytest.fixture
def taskrun(harness, setup, client, tmp_path, tmp_path_factory):
    opened = []
    node = pinned_node(tmp_path_factory.getbasetemp())

    def go(scenario: str, version: int, mode: str = "ok"):
        f = fmt_repo(tmp_path, mode)
        spec = write_spec(v1_doc(f, node, mode) if version == 1 else v0_doc(f, node), tmp_path / "spec.json")
        TR.preflight(spec, tmp_path / "rr")
        reset_schema(harness.dsn)
        install(harness.dsn, E2C_BOUNDS)
        install_acts(harness.dsn)
        install_handoff(harness.dsn)
        aj = ActsJournal(harness.dsn, f"task-format#{key()[:8]}", now=1000.0).open()
        opened.append(aj)
        res, ev = TR.run(spec, tmp_path / "rr", executable=(PY, FAKE), executable_sha256=sha_file(FAKE), setup=setup, client=client,
                         aj=aj, project_id=harness.project_id, execution_kind="fake-cli", root_go="test-only",
                         task_suffix=f"\nFAKE-SCENARIO: {scenario}\n", timeouts=(120, 60, 60))
        return res, ev, f
    yield go
    for aj in opened:
        aj.close()


def launches(ev) -> int:
    return sum(1 for j in ev["journal"] if j["kind"] == "launch_intent")


def test_v1_run_accepts_otherwise_correct_unformatted_work_after_trusted_formatting(taskrun):
    res, ev, f = taskrun("value_unformatted", 1)
    assert (res.outcome, len(res.rounds)) == ("accepted", 1) and launches(ev) == 1
    fin = ev["adapter"]["1"]["finalize"]
    assert fin["files"][0]["preSha256"] == sha(UNFORMATTED.encode()) and fin["files"][0]["postSha256"] == sha(b"42\n")
    assert fin["kind"] == "host-derivation" and "files" not in (ev["adapter"]["1"]["reported_usage"] or {})   # not provider usage
    assert git(Path(ev["preflight"]["repo"]), "show", f"{res.rounds[0].artifact_ref}:src/value.txt") == "42"


def test_a_v0_run_is_unchanged_has_no_hook_and_its_verifier_rejects_the_unformatted_artifact(taskrun):
    res, ev, _ = taskrun("value_unformatted", 0)
    assert res.outcome != "accepted" and all(r["why"] == "step_failed:fmtcheck" for r in ev["verifier"]) and ev["verifier"]
    assert "finalize" not in ev["adapter"]["1"]


def test_a_formatter_failure_stops_without_a_verdict_or_a_second_model_round(taskrun):
    res, ev, _ = taskrun("value_unformatted", 1, mode="fail")
    assert res.outcome != "accepted" and launches(ev) == 1 and ev["verifier"] == []
    assert ev["adapter"]["1"]["finalize"]["refused"] == "format_failed"


# ------------------------------------------------------------------------ profile.v1 / draft.v1 authoring

def profile_v1(f, node) -> dict:
    h = make_spec(f, node)["hashes"]
    return {"version": A.PROFILE_VERSION_V1, "repo": Path(f["repo"]).as_posix(),
            "tools": {"pinnedNodeExe": h["pinnedNodeExe"], "npmCli": h["npmCli"]},
            "lock": {k: h[k] for k in ("packageLock", "vitestEntry", "tscEntry")},
            "deps": {"kind": "npm-ci", "network": "offline", "timeoutS": 60, "outputKeepBytes": 4096},
            "oracleRunner": {"argv": [node.as_posix(), VITEST, "run", "--reporter=json", A.ORACLE_TOKEN], "timeoutS": 60,
                             "reportMaxBytes": 65536, "outputKeepBytes": 65536},
            "steps": [fmt_step(node)], "worker": {"model": "fake-model", "budgetUsd": "0.50", "maxRounds": 2, "maxTurns": 5},
            "formatter": {k: v for k, v in formatter().items() if k != "paths"}}


def draft_v1(f) -> dict:
    return {"version": A.DRAFT_VERSION_V1, "source": {"anchorCommit": f["anchor"], "taskBaseCommit": f["base"]},
            "task": {"text": "Set src/value.txt to 42.", "criteria": "Every oracle case passes."}, "allow": ["src/value.txt"],
            "oracle": [ORACLE_A, ORACLE_B], "worker": {}, "metadata": {"issue": "FX-2", "title": "t", "notes": []},
            "formatPaths": ["src/value.txt"]}


def test_profile_v1_and_draft_v1_compose_a_spec_the_unchanged_v1_parser_accepts(tmp_path, tmp_path_factory):
    f = fmt_repo(tmp_path)
    node = pinned_node(tmp_path_factory.getbasetemp())
    profile, draft = A.parse_profile(json.dumps(profile_v1(f, node)).encode()), A.parse_draft(json.dumps(draft_v1(f)).encode(), True)
    oracle = [{"path": p, "sha256": "a" * 64} for p in (ORACLE_A, ORACLE_B)]
    cases = [{"file": ORACLE_A, "fullName": "x", "status": "failed", "failureFirstLine": FIRST}]
    spec = T.parse(A.compose(profile, draft, oracle, cases, 1))
    assert spec.doc["specVersion"] == T.SPEC_VERSION_V1 and spec.formatter["paths"] == ["src/value.txt"]
    assert [s["name"] for s in spec.doc["verify"]["steps"]] == ["oracle", "fmtcheck"]


@pytest.mark.parametrize("mutate, code", [
    (lambda p: p.pop("formatter"), "profile_invalid"),                                           # v1 requires the formatter
    (lambda p: p["steps"].extend(p["steps"] * 4), "profile_invalid"),                             # 5 profile steps: over the v1 limit
    (lambda p: p.update(version="hekate-task-profile.v2"), "profile_invalid"),
    (lambda p: p["formatter"].update(argv=["x"]), "profile_invalid"),
])
def test_profile_v1_shape_is_closed(tmp_path, tmp_path_factory, mutate, code):
    f = fmt_repo(tmp_path)
    p = edited(profile_v1(f, pinned_node(tmp_path_factory.getbasetemp())), mutate)
    with pytest.raises(A.AuthorRefused) as e:
        A.parse_profile(json.dumps(p).encode())
    assert e.value.code == code


def test_draft_versions_do_not_cross_and_format_paths_must_be_allow_paths(tmp_path):
    f = fmt_repo(tmp_path)
    for raw, v1, code in ((edited(draft_v1(f), lambda d: d.pop("formatPaths")), True, "draft_invalid"),
                          (edited(draft_v1(f), lambda d: d.update(formatPaths=["src/other.txt"])), True, "draft_invalid"),
                          (edited(draft_v1(f), lambda d: d.update(version=A.DRAFT_VERSION)), True, "draft_version"),
                          (draft_v1(f), False, "draft_invalid")):
        with pytest.raises(A.AuthorRefused) as e:
            A.parse_draft(json.dumps(raw).encode(), v1)
        assert e.value.code == code


def test_a_v0_profile_still_caps_profile_steps_at_three(tmp_path, tmp_path_factory):
    f = fmt_repo(tmp_path)
    node = pinned_node(tmp_path_factory.getbasetemp())
    p = edited(profile_v1(f, node), lambda d: (d.pop("formatter"), d.update(version=A.PROFILE_VERSION), d["steps"].extend(d["steps"] * 3)))
    with pytest.raises(A.AuthorRefused) as e:
        A.parse_profile(json.dumps(p).encode())
    assert e.value.code == "profile_invalid"
