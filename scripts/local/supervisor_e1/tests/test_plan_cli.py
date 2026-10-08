"""Prepared-plan CLI (root msg 1847) OFFLINE: refusals before any harness or spawn, a validate with no effects, and the
run outcomes and exit codes over the disposable harness with the FAKE CLI. No model, no live database."""

import json
import subprocess
from pathlib import Path

import pytest

from e1 import plan_cli as CLI
from e1 import plan_run as PR
from task_support import sha_file
from test_plan_run import FAKE, PYEXE, doc_bytes, fx, npm_cache, two_nodes  # noqa: F401 -- fixtures
from test_task_runner import SessionHarness


def plan_file(fx, raw: bytes, name="plan.json") -> Path:
    p = fx.tmp / name
    p.write_bytes(raw)
    return p


def one_node(fx) -> bytes:
    return doc_bytes([{"key": "a", "name": "set value", "spec": {"path": fx.a_path, "sha256": fx.a_spec.sha256}, "after": []}])


def run_argv(plan: Path, run_root: Path, **over) -> list[str]:
    args = {"--plan": str(plan), "--run-root": str(run_root), "--exe": str(PYEXE), "--exe-arg": str(FAKE),
            "--exe-sha256": sha_file(FAKE), "--root-go": "test-only"}
    args.update(over)
    argv = ["run", "--launch-real-model"]
    for k, v in args.items():
        if v is not None:
            argv += [k, v]
    return argv


def last_json(capsys) -> dict:
    out = capsys.readouterr().out
    start = out.rfind("\n{")
    return json.loads(out[start + 1:] if start >= 0 else out)


@pytest.fixture
def no_effects(monkeypatch):
    """Any spawn or harness start fails the test."""
    def boom(*a, **kw):
        raise AssertionError("no effect may happen before the inputs are validated")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    return boom


# ------------------------------------------------------------------------ validate: no effects

def test_validate_reports_every_node_and_has_no_effects(fx, capsys, no_effects):
    plan = plan_file(fx, two_nodes(fx))
    assert CLI.main(["validate", "--plan", str(plan)], harness_factory=no_effects) == 0
    out = last_json(capsys)
    assert out["validate"] == "ok" and out["pendingSpec"] == ["b"] and "NEW run root" in out["note"]
    a, b = out["nodes"]
    assert (a["key"], a["after"], a["spec"]["sha256"]) == ("a", [], fx.a_spec.sha256)
    assert a["spec"]["bounds"] == {k: fx.a_doc["worker"][k] for k in ("model", "budgetUsd", "maxRounds", "maxTurns")}
    assert a["spec"]["taskBaseCommit"] == fx.a_doc["source"]["taskBaseCommit"]
    assert (b["key"], b["after"], b["spec"]) == ("b", ["a"], "pending")


@pytest.mark.parametrize("mutate, code", [
    (lambda d: d.update(version="plan-import.v1"), "import_shape"),
    (lambda d: d["nodes"][1].update(after=["zz"]), "import_graph"),
    (lambda d: d["nodes"][0]["spec"].update(sha256="0" * 64), "spec_sha_mismatch"),
    (lambda d: d["nodes"][0]["spec"].update(path="C:/no/such/spec.json"), "spec_invalid"),
])
def test_an_invalid_plan_is_refused_by_validate_and_run_before_any_effect(fx, tmp_path, capsys, no_effects, mutate, code):
    d = json.loads(two_nodes(fx))
    mutate(d)
    plan = plan_file(fx, json.dumps(d).encode("utf-8"))
    for argv in (["validate", "--plan", str(plan)], run_argv(plan, tmp_path / "rr")):
        assert CLI.main(argv, harness_factory=no_effects) == 2
        assert last_json(capsys)["refused"] == code
    assert not (tmp_path / "rr").exists()


def test_an_unreadable_plan_is_refused(fx, capsys, no_effects):
    assert CLI.main(["validate", "--plan", str(fx.tmp / "missing.json")], harness_factory=no_effects) == 2
    assert last_json(capsys)["refused"] == "plan_unreadable"


# ------------------------------------------------------------------------ run: every input before the harness

@pytest.mark.parametrize("over, code", [
    ({"--run-root": None}, "run_root_required"),
    ({"--root-go": None}, "run_needs"),
    ({"--root-go": "  "}, "run_needs"),
    ({"--exe-sha256": None}, "run_needs"),
    ({"--exe-sha256": "0" * 64}, "executable_hash_mismatch"),
    ({"--exe-arg": "C:/no/such/fake_cli.py"}, "executable_missing"),
])
def test_run_inputs_are_refused_before_any_harness_or_spawn(fx, tmp_path, capsys, no_effects, over, code):
    plan = plan_file(fx, one_node(fx))
    assert CLI.main(run_argv(plan, tmp_path / "rr", **over), harness_factory=no_effects) == 2
    assert last_json(capsys)["refused"] == code
    assert not (tmp_path / "rr").exists()


def test_run_without_the_launch_acknowledgement_is_refused(fx, tmp_path, capsys, no_effects):
    argv = [x for x in run_argv(plan_file(fx, one_node(fx)), tmp_path / "rr") if x != "--launch-real-model"]
    assert CLI.main(argv, harness_factory=no_effects) == 2
    assert last_json(capsys)["refused"] == "run_needs"


def test_an_existing_run_root_is_an_earlier_attempt_and_is_refused(fx, tmp_path, capsys, no_effects):
    (tmp_path / "rr").mkdir()
    assert CLI.main(run_argv(plan_file(fx, one_node(fx)), tmp_path / "rr"), harness_factory=no_effects) == 2
    assert last_json(capsys)["refused"] == "run_root_exists"
    assert list((tmp_path / "rr").iterdir()) == []


# ------------------------------------------------------------------------ run: outcomes over the disposable harness (FAKE CLI)

@pytest.fixture
def scenarios(monkeypatch):
    """The FAKE CLI picks its behaviour from the task text; the test hook adds it per node."""
    real = PR.run_plan
    monkeypatch.setattr(PR, "run_plan", lambda *a, **kw: real(*a, **dict(
        kw, timeouts=(120, 60, 60), task_suffix=lambda k: "\nFAKE-SCENARIO: " + {"a": "value_ok", "b": "other_ok"}[k] + "\n")))


def test_a_fully_specified_plan_runs_to_all_done_exit_0_labelled_fake(harness, fx, tmp_path, capsys, scenarios):
    sh = SessionHarness(harness)
    rr = tmp_path / "rr"
    assert CLI.main(run_argv(plan_file(fx, one_node(fx)), rr), harness_factory=lambda: sh) == 0 and sh.stopped
    out = last_json(capsys)
    assert (out["outcome"], out["reason"], out["executionKind"], out["rootGo"]) == ("all_done", None, "fake-cli", "test-only")
    assert "no model ran" in out["fake"] and "resumable" not in out
    assert out["nodes"]["a"]["acceptance"] == "accepted" and out["nodes"]["a"]["artifactRef"]
    (step,) = out["steps"]
    assert (step["key"], step["action"], step["outcome"]) == ("a", "ran", "accepted") and Path(step["evidence"]).is_file()
    assert json.loads(Path(out["planRunLog"]).read_text(encoding="utf-8"))["outcome"] == "all_done"


def test_a_pending_spec_stops_needs_operator_exit_1_and_says_it_is_not_resumable(harness, fx, tmp_path, capsys, scenarios):
    sh = SessionHarness(harness)
    assert CLI.main(run_argv(plan_file(fx, two_nodes(fx)), tmp_path / "rr"), harness_factory=lambda: sh) == 1 and sh.stopped
    out = last_json(capsys)
    assert (out["outcome"], out["reason"], out["detail"]) == ("needs_operator", "spec_pending", {"node": "b"})
    assert out["resumable"] is False and "NEW run root" in out["note"]
    assert [(s["key"], s["action"], s["outcome"]) for s in out["steps"]] == [("a", "ran", "accepted"), ("b", "stopped", None)]
    assert (out["nodes"]["a"]["acceptance"], out["nodes"]["b"]["work"]) == ("accepted", "todo")
    assert Path(out["planRunLog"]).is_file()


class Busy:
    def start(self):
        raise RuntimeError("port 5108 is in use; refusing\nsecond line " + "x" * 500)


def no_harness():
    raise OSError("docker not found")


@pytest.mark.parametrize("factory, detail", [
    (Busy, "RuntimeError: port 5108 is in use; refusing"),           # start fails: one bounded line, nothing after it
    (no_harness, "OSError: docker not found"),                        # the factory itself fails
])
def test_a_harness_that_cannot_start_is_a_typed_result_not_a_traceback(fx, tmp_path, capsys, factory, detail):
    assert CLI.main(run_argv(plan_file(fx, one_node(fx)), tmp_path / "rr"), harness_factory=factory) == 1
    assert last_json(capsys) == {"outcome": "needs_operator", "reason": "harness_unavailable", "detail": detail}
    assert not (tmp_path / "rr").exists()


def test_a_failure_after_the_harness_started_is_a_typed_stop_and_the_harness_is_stopped(harness, fx, tmp_path, capsys, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("driver failed")
    monkeypatch.setattr(PR, "run_plan", broken)
    sh = SessionHarness(harness)
    assert CLI.main(run_argv(plan_file(fx, one_node(fx)), tmp_path / "rr"), harness_factory=lambda: sh) == 1 and sh.stopped
    assert last_json(capsys) == {"outcome": "needs_operator", "reason": "unexpected_error", "detail": "RuntimeError"}


def test_a_recipe_chain_runs_to_all_done_in_one_cli_run(harness, fx, tmp_path, capsys, scenarios):
    from test_plan_run_d3 import chain, recipe_doc, write_recipe
    path, rsha = write_recipe(fx, recipe_doc(fx))
    plan = plan_file(fx, chain(fx, path, rsha, "cli d3 chain"))
    assert CLI.main(["validate", "--plan", str(plan)]) == 0
    assert last_json(capsys)["nodes"][1]["spec"] == {"recipe": {"path": path, "sha256": rsha}}
    sh = SessionHarness(harness)
    assert CLI.main(run_argv(plan, tmp_path / "rr"), harness_factory=lambda: sh) == 0 and sh.stopped
    out = last_json(capsys)
    assert (out["outcome"], [(s["key"], s["outcome"]) for s in out["steps"]]) == ("all_done", [("a", "accepted"), ("b", "accepted")])
    assert (tmp_path / "rr" / "b.integration" / "provenance.json").is_file()
