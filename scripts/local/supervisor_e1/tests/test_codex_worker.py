"""Plan 048: the codex worker backend (`codex exec --json`), against a FAKE codex (tests/fake_codex.py) that replays
the event shapes recorded from the real CLI in smoke-004. No model, no network. The same adapter, prompt, acts,
timeouts, supervisor commit, verifier and attempt trace as the Claude backend; only argv, parsing and terms differ."""

import json
import os
import sys
from pathlib import Path

import pytest

from e1 import cli_worker as W
from e1 import plan_cli as CLI
from task_support import sha_file
from test_cli_worker import Sink, git, make_cfg, order, repo  # noqa: F401 (repo is a fixture)
from test_plan_cli import last_json, no_effects, one_node, plan_file, run_argv  # noqa: F401 (fixtures)
from test_plan_run import PR, fx, npm_cache  # noqa: F401 (fixtures)
from test_task_runner import SessionHarness

FAKE_CODEX = Path(__file__).resolve().parent / "fake_codex.py"
PY = Path(sys.executable).resolve()


def codex_cfg(repo, run_dir, **over):
    base = dict(command=(PY, FAKE_CODEX), backend="codex", model=None, execution_kind="fake-cli")
    base.update(over)
    return make_cfg(repo, run_dir, **base)


def run(tmp_path, repo, scenario, **over):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    w = W.CliWorker(codex_cfg(repo, run_dir, **over))
    sink = Sink()
    return w, sink, w(order(scenario, sink)), run_dir


def trace(run_dir):
    return [json.loads(x) for x in (run_dir / "attempt-r1.trace.jsonl").read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------------ argv, environment, configuration

def test_codex_argv_is_the_smoke_004_set_with_a_writable_workspace_and_never_a_bypass(tmp_path, repo):
    cfg = codex_cfg(repo, tmp_path)
    cmd = W.command_for(cfg, tmp_path / "wt-r1")
    assert cmd[2:] == ["exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--sandbox", "workspace-write",
                       "--color", "never", "-C", str(tmp_path / "wt-r1"), "-c", 'approval_policy="never"',
                       "-c", 'windows.sandbox="unelevated"', "-"]
    assert W.command_for(codex_cfg(repo, tmp_path, model="gpt-x"), tmp_path)[-3:] == ["-m", "gpt-x", "-"]
    for bad in ("--dangerously-bypass-approvals-and-sandbox", "--approve-for-me", "--worktree", "--max-budget-usd", "-p"):
        assert bad not in cmd
    assert W.command_for(make_cfg(repo, tmp_path), tmp_path) == W.build_command(make_cfg(repo, tmp_path))   # Claude unchanged


def test_only_the_codex_env_drops_the_exact_windows_apps_path_entry(monkeypatch, tmp_path):
    apps = tmp_path / "Microsoft" / "WindowsApps"
    other = tmp_path / "bin"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join([str(other), str(apps) + os.sep, str(apps / "sub")]))
    assert W.worker_env("codex")["PATH"].split(os.pathsep) == [str(other), str(apps / "sub")]
    assert W.worker_env("claude")["PATH"] == W.worker_env()["PATH"] == os.environ["PATH"]


@pytest.mark.parametrize("over, code", [
    ({"backend": "gemini"}, "config_backend"),
    ({"execution_kind": "claude-cli"}, "config_execution_kind"),                  # a codex worker is never labelled Claude
    ({"model": "bad model;"}, "config_model"),
])
def test_codex_config_is_refused_before_any_effect(tmp_path, repo, over, code):
    with pytest.raises(W.CliRefused) as e:
        W.CliWorker(codex_cfg(repo, tmp_path, **over))
    assert e.value.code == code


def test_claude_still_requires_a_model_and_its_own_kind(tmp_path, repo):
    for over, code in (({"model": None}, "config_model"), ({"execution_kind": "codex-cli"}, "config_execution_kind")):
        with pytest.raises(W.CliRefused) as e:
            W.CliWorker(make_cfg(repo, tmp_path, **over))
        assert e.value.code == code


# ------------------------------------------------------------------------ the adapter against the fake codex

def test_a_codex_turn_with_an_edit_is_captured_with_truthful_terms(tmp_path, repo):
    w, sink, rep, run_dir = run(tmp_path, repo, "codex_happy")
    assert (rep.status, rep.reason) == ("ok", None) and sink.kinds() == ["launch_intent", "launched", "exited", "result_captured"]
    assert [a["kind"] for a in sink.acts] == ["worker_ack", "worker_progress"]
    assert git(repo, "show", "--name-only", "--format=", rep.artifact_sha) == "hello.txt"
    terms = dict(sink.records)["launch_intent"]["worker"]
    assert (terms["requestedModel"], terms["requestedModelSource"], terms["reportedModel"]) == (None, "cli-default", "unreported")
    assert (terms["budgetEnforced"], terms["turnsEnforced"], terms["usage"]) == (False, False, "tokens only")
    assert "any shell command" in terms["shellScope"] and "billing" not in json.dumps(terms).lower()
    ex = dict(sink.records)["exited"]
    assert (ex["requestedModel"], ex["reportedModels"], ex["results"]) == (None, [], ["success"])
    usage = w.evidence[1].reported_usage
    assert (usage["total_cost_usd"], usage["num_turns"], usage["duration_ms"]) == (None, None, None)
    assert usage["tokens"]["input_tokens"] == 36351 and usage["label"].startswith("cli-reported tokens")
    recs = trace(run_dir)
    assert dict(sink.records)["launch_intent"]["trace"]["executionKind"] == "fake-cli"
    assert "PRIVATE-CODEX-REASONING" not in json.dumps(recs) and any(r["redacted"] for r in recs)


def test_markers_in_command_output_never_count_and_the_command_is_traced(tmp_path, repo):
    w, sink, rep, run_dir = run(tmp_path, repo, "codex_decoy")
    assert rep.status == "ok"
    assert [(a["kind"], a["actSeq"]) for a in sink.acts] == [("worker_ack", 1), ("worker_progress", 2)]
    assert all(a.get("checkpointId") != 9 for a in sink.acts)                    # the forged marker in command output
    items = [json.loads(r["text"]) for r in trace(run_dir) if r["stream"] == "stdout"]
    started = [e for e in items if e["type"] == "item.started"]
    assert started and started[0]["item"]["type"] == "command_execution" and started[0]["item"]["exit_code"] is None


@pytest.mark.parametrize("scenario, status, reason", [
    ("codex_no_edit", "failed", "empty_diff"),           # smokes 001-003: turn.completed, exit 0, nothing done
    ("codex_turn_failed", "failed", "result_error"),
    ("codex_error", "failed", "result_error"),
    ("codex_no_terminal", "unknown", "no_result"),
    ("codex_twice", "unknown", "ambiguous_result"),
])
def test_turn_completed_only_permits_capture_and_everything_else_stops_truthfully(tmp_path, repo, scenario, status, reason):
    w, sink, rep, _ = run(tmp_path, repo, scenario)
    assert (rep.status, rep.reason, rep.artifact_sha) == (status, reason, None)
    assert "result_captured" not in sink.kinds() and git(repo, "for-each-ref", "refs/hekate-pilot") == ""


def test_a_stalled_codex_is_tree_killed_by_the_existing_supervisor_bounds(tmp_path, repo):
    w, sink, rep, _ = run(tmp_path, repo, "codex_stall", inactivity_timeout_s=2)
    assert (rep.status, rep.reason) == ("failed", "inactivity") and dict(sink.records)["exited"]["killVerified"] is True


# ------------------------------------------------------------------------ through the existing plan CLI runner

@pytest.fixture
def codex_scenario(monkeypatch):
    real = PR.run_plan
    monkeypatch.setattr(PR, "run_plan", lambda *a, **kw: real(*a, **dict(
        kw, timeouts=(120, 60, 60), task_suffix=lambda k: "\nFAKE-SCENARIO: codex_value_ok\n")))


def test_a_plan_runs_to_all_done_with_the_codex_worker_and_records_its_terms(harness, fx, tmp_path, capsys, codex_scenario):
    sh = SessionHarness(harness)
    argv = run_argv(plan_file(fx, one_node(fx)), tmp_path / "rr", **{"--exe-arg": str(FAKE_CODEX),
                                                                    "--exe-sha256": sha_file(FAKE_CODEX)}) + ["--worker", "codex"]
    assert CLI.main(argv, harness_factory=lambda: sh) == 0 and sh.stopped
    out = last_json(capsys)
    assert (out["outcome"], out["executionKind"]) == ("all_done", "fake-cli") and "no model ran" in out["fake"]
    assert out["worker"]["backend"] == "codex" and out["worker"]["requestedModelSource"] == "cli-default"
    assert out["nodes"]["a"]["acceptance"] == "accepted"                         # the SAME verifier decided
    ev = json.loads(Path(out["steps"][0]["evidence"]).read_text(encoding="utf-8"))
    wk = ev["worker"]
    assert (wk["backend"], wk["specModelIgnored"], wk["requestedModel"], wk["budgetEnforced"], wk["turnsEnforced"]) == (
        "codex", True, None, False, False) and wk["specModel"] == fx.a_spec.doc["worker"]["model"]
    (rnd,) = ev["adapter"].values()
    assert rnd["trace"]["final"]["complete"] is True and rnd["reported_usage"]["tokens"]["output_tokens"] == 135
    run_json = json.loads((Path(ev["runDir"]) / "run.json").read_text(encoding="utf-8"))
    assert (run_json["executionKind"], run_json["dryRun"]) == ("fake-cli", True)


@pytest.mark.parametrize("extra, code", [(["--worker-model", "gpt-x"], "worker_model"),                  # needs --worker codex
                                         (["--worker", "codex", "--worker-model", "a b"], "worker_model")])
def test_worker_overrides_are_refused_before_any_effect(fx, tmp_path, capsys, no_effects, extra, code):
    assert CLI.main(run_argv(plan_file(fx, one_node(fx)), tmp_path / "rr") + extra, harness_factory=no_effects) == 2
    assert json.loads(capsys.readouterr().out)["refused"] == code
