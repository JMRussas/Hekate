"""HK-ISSUE-007 real-worker adapter (e1/cli_worker.py) against a FAKE CLI (tests/fake_cli.py): no model,
no network. Unit cases use a throwaway git repo and an in-memory journal; the integration cases run the
pilot driver end to end against the disposable harness database."""

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from e1 import cli_worker as W
from e1 import pilot as P
from test_pilot_dryrun import pilot  # noqa: F401 (fixture)

FAKE = Path(__file__).resolve().parent / "fake_cli.py"
PY = Path(sys.executable).resolve()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "README.md").write_text("pilot\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "base")
    return r


def make_cfg(repo: Path, run_dir: Path, **over) -> W.CliConfig:
    base = dict(command=(PY, FAKE), repo=repo, base_sha=git(repo, "rev-parse", "HEAD"), run_id="t1", run_dir=run_dir,
                model="fake-model", max_budget_usd="0.50", max_turns=5, total_timeout_s=60, first_output_timeout_s=30,
                inactivity_timeout_s=30, kill_wait_s=20)
    base.update(over)
    return W.CliConfig(**base)


class Sink:
    """An in-memory stand-in for the journal stream: records kinds in order and accepts acts."""

    def __init__(self):
        self.records, self.acts, self.delivered_n = [], [], 0

    def journal(self, kind, data):
        json.dumps(data)
        self.records.append((kind, data))

    def act(self, raw):
        self.acts.append(json.loads(raw))
        return SimpleNamespace(outcome="accepted")

    def delivered(self):
        self.delivered_n += 1

    def kinds(self):
        return [k for k, _ in self.records]


KEY = {"rootId": "r", "nodeId": "n", "attemptId": "a", "attemptEpoch": 1, "claimKey": "c", "runId": "x", "packageRef": "{}",
       "workerSession": "hkw1:worker-a:00000000-0000-4000-8000-000000000000"}


def order(scenario: str, sink: Sink, rnd: int = 1) -> P.WorkOrder:
    return P.WorkOrder(rnd, dict(KEY), f"Task.\nFAKE-SCENARIO: {scenario}\n", "base", journal=sink.journal, act=sink.act,
                       delivered=sink.delivered)


def run(tmp_path, repo, scenario, **over):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    w = W.CliWorker(make_cfg(repo, run_dir, **over))
    sink = Sink()
    rep = w(order(scenario, sink))
    return w, sink, rep, run_dir


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    import os
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------------ configuration (before any git/spawn)

@pytest.mark.parametrize("over, code", [
    ({"command": (Path("claude"),)}, "config_command"), ({"command": (PY, FAKE, FAKE)}, "config_command"),
    ({"base_sha": "HEAD"}, "config_base"), ({"run_id": "../x"}, "config_run_id"), ({"run_id": "a b"}, "config_run_id"),
    ({"model": "m; rm -rf"}, "config_model"), ({"max_budget_usd": "0.00"}, "config_budget"), ({"max_budget_usd": "1"}, "config_budget"),
    ({"max_budget_usd": "-1.00"}, "config_budget"), ({"max_turns": 0}, "config_turns"), ({"max_turns": True}, "config_turns"),
    ({"first_output_timeout_s": 61}, "config_timeouts"), ({"permission_mode": "bypassPermissions"}, "config_permission"),
    ({"allowed_tools": ("Read", "Bash")}, "config_tools"), ({"allowed_tools": ()}, "config_tools"),
    ({"test_command": "pytest; curl x"}, "config_test_command"), ({"test_command": "pytest && rm"}, "config_test_command"),
    ({"restricted": "yes"}, "config_restricted"),
])
def test_config_refused_before_any_effect(tmp_path, repo, over, code):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(W.CliRefused) as e:
        W.CliWorker(make_cfg(repo, run_dir, **over))
    assert e.value.code == code
    assert list(run_dir.iterdir()) == [] and git(repo, "worktree", "list").count("\n") == 0       # no worktree, no spawn


def test_command_snapshot_has_every_bound_and_never_skips_permissions(tmp_path, repo):
    cfg = make_cfg(repo, tmp_path, command=(Path(sys.executable).resolve(),), test_command="uv run pytest -q")
    cmd = W.build_command(cfg)
    assert "--dangerously-skip-permissions" not in cmd and "bypassPermissions" not in cmd
    pairs = {cmd[i]: cmd[i + 1] for i in range(len(cmd) - 1)}
    assert (pairs["--max-budget-usd"], pairs["--max-turns"], pairs["--permission-mode"], pairs["--mcp-config"]) == (
        "0.50", "5", "acceptEdits", '{"mcpServers":{}}')
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--allowedTools") + 1:] == ["Read", "Glob", "Grep", "Edit", "Write",
                                                                                       "Bash(uv run pytest -q)"]
    # the EXPOSED tool surface is separate from auto-approval (msgs 1501, 1526); --restricted on by default
    assert pairs["--tools"] == "Read,Glob,Grep,Edit,Write,Bash" and "--restricted" in cmd
    no_bash = W.build_command(make_cfg(repo, tmp_path, command=(Path(sys.executable).resolve(),)))
    assert no_bash[no_bash.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write" and not any(a.startswith("Bash") for a in no_bash)
    off = W.build_command(make_cfg(repo, tmp_path, command=(Path(sys.executable).resolve(),), restricted=False))
    assert "--restricted" not in off


def test_env_is_an_allowlist_plus_controlled_values():
    env = W.worker_env()
    assert set(env) <= set(W.ENV_ALLOW) | {"PYTHONDONTWRITEBYTECODE"} and env["PYTHONDONTWRITEBYTECODE"] == "1"
    g = W.git_env()
    assert (g["GIT_CONFIG_NOSYSTEM"], g["GIT_CONFIG_GLOBAL"], g["GIT_TERMINAL_PROMPT"]) == ("1", os.devnull, "0")


def test_a_hostile_global_git_config_cannot_reach_the_supervisor_commit(tmp_path, repo, monkeypatch):
    """A global core.hooksPath with a failing pre-commit hook would block the supervisor's commit; the
    controlled git environment disables global config, so the run is unaffected (msg 1545)."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    cfg = tmp_path / "hostile.gitconfig"
    cfg.write_text(f"[core]\n\thooksPath = {hooks.as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    w, sink, rep, _ = run(tmp_path, repo, "happy")
    assert rep.status == "ok" and "result_captured" in sink.kinds()


# ------------------------------------------------------------------------ the happy path and correlation

def test_happy_path_launch_records_acts_commit_and_ref(tmp_path, repo):
    base, branch = git(repo, "rev-parse", "HEAD"), git(repo, "branch", "--show-current")
    w, sink, rep, run_dir = run(tmp_path, repo, "happy")
    assert (rep.status, rep.reason, rep.attested) == ("ok", None, True)
    assert sink.kinds() == ["launch_intent", "launched", "exited", "result_captured"]          # intent BEFORE the process
    assert sink.records[1][1]["observedBy"] == "supervisor" and sink.delivered_n == 1
    assert [a["kind"] for a in sink.acts] == ["worker_ack", "worker_progress"]                 # worker-authored only
    assert sink.acts[0] == {"kind": "worker_ack", "key": KEY, "actSeq": 1} and sink.acts[1]["actSeq"] == 2
    art = rep.artifact_sha
    assert git(repo, "rev-parse", f"{art}^") == base and git(repo, "rev-parse", "refs/hekate-pilot/t1/r1") == art
    assert git(repo, "show", "--name-only", "--format=", art) == "hello.txt"
    cap = sink.records[3][1]
    assert cap["artifactRef"] == art and cap["parentRef"] == base and cap["resultSha256"] and cap["filesChanged"] == 1
    assert git(repo, "rev-parse", "HEAD") == base and git(repo, "branch", "--show-current") == branch     # user checkout untouched
    ex = dict(sink.records)["exited"]                                           # CLI-REPORTED provenance, kept apart
    assert (ex["requestedModel"], ex["reportedModels"]) == ("fake-model", ["fake-model", "fake-runtime-model-1"])
    assert (cap["requestedModel"], cap["reportedModels"]) == ("fake-model", ["fake-model", "fake-runtime-model-1"])
    assert (run_dir / "wt-r1").is_dir()                                                         # retained until operator cleanup


def test_spawn_is_launched_but_never_an_ack(tmp_path, repo):
    w, sink, rep, _ = run(tmp_path, repo, "no_ack")
    assert rep.status == "ok" and "launched" in sink.kinds() and sink.acts == []


def test_markers_outside_assistant_text_never_count_and_malformed_ones_are_counted(tmp_path, repo):
    w, sink, rep, _ = run(tmp_path, repo, "quoted")
    assert sink.acts == []
    assert w.evidence[1].acts_refused == {"act_json": 1, "act_seq": 1, "act_kind": 1}


@pytest.mark.parametrize("scenario, reason", [("nonzero", "nonzero_exit"), ("empty", "empty_diff"), ("self_commit", "worker_moved_head")])
def test_bad_outcomes_fail_closed_without_an_artifact(tmp_path, repo, scenario, reason):
    w, sink, rep, _ = run(tmp_path, repo, scenario)
    assert (rep.status, rep.reason, rep.artifact_sha) == ("failed", reason, None)
    assert "result_captured" not in sink.kinds() and git(repo, "for-each-ref", "refs/hekate-pilot") == ""


# ------------------------------------------------------------------------ kills, bounds, stderr

@pytest.mark.parametrize("scenario, over, reason", [
    ("no_output", {"first_output_timeout_s": 2}, "first_output_timeout"),
    ("stall", {"inactivity_timeout_s": 2}, "inactivity"),
    ("child", {"total_timeout_s": 6, "first_output_timeout_s": 6, "inactivity_timeout_s": 6}, "total_timeout"),
    ("big_line", {"stdout_line_max": 1 << 16}, "stdout_line_cap"),
])
def test_timeouts_and_caps_tree_kill_with_a_verified_exit(tmp_path, repo, scenario, over, reason):
    t0 = time.monotonic()
    w, sink, rep, run_dir = run(tmp_path, repo, scenario, **over)
    assert (rep.status, rep.reason) == ("failed", reason) and time.monotonic() - t0 < 60
    ex = dict(sink.records)["exited"]
    assert ex["reason"] == reason and ex["killVerified"] is True and "result_captured" not in sink.kinds()
    assert not alive(w.evidence[1].pid)
    if scenario == "child":
        pid_file = run_dir / "wt-r1" / "child.pid"
        assert pid_file.exists()
        child = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not alive(child), "the worker's child process survived: not a tree kill"


def test_a_child_that_never_reads_a_large_prompt_is_tree_killed_and_never_delivered(tmp_path, repo):
    """Root review msg 1492: the deadline starts BEFORE the prompt is written; a bounded writer thread
    can be blocked by a child that never reads stdin, and the supervisor still kills the tree."""
    (repo / "FAKE_MODE").write_text("stdin_block\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "fake mode")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    w = W.CliWorker(make_cfg(repo, run_dir, first_output_timeout_s=3, inactivity_timeout_s=3, total_timeout_s=30))
    sink = Sink()
    big = P.WorkOrder(1, dict(KEY), "x" * (4 << 20), "base", journal=sink.journal, act=sink.act, delivered=sink.delivered)
    t0 = time.monotonic()
    rep = w(big)
    assert time.monotonic() - t0 < 60
    assert (rep.status, rep.reason) == ("unknown", "prompt_not_delivered") and sink.delivered_n == 0
    ex = dict(sink.records)["exited"]
    assert ex["reason"] == "prompt_not_delivered" and ex["killVerified"] is True and ex["drained"] is True and ex["promptDelivered"] is False
    assert "result_captured" not in sink.kinds() and git(repo, "for-each-ref", "refs/hekate-pilot") == ""
    assert not alive(w.evidence[1].pid)
    child = int((run_dir / "wt-r1" / "child.pid").read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while alive(child) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not alive(child)


@pytest.mark.parametrize("scenario, status, reason", [
    ("no_result", "unknown", "no_result"),                         # edits + ACK + exit 0, but no terminal result
    ("result_error", "failed", "result_error"),                    # e.g. the budget limit
    ("result_is_error", "failed", "result_error"),                 # subtype success but is_error true
    ("result_malformed", "unknown", "result_malformed"),
    ("result_twice", "unknown", "ambiguous_result"),
])
def test_only_one_well_formed_success_result_allows_capture(tmp_path, repo, scenario, status, reason):
    w, sink, rep, _ = run(tmp_path, repo, scenario)
    assert (rep.status, rep.reason, rep.artifact_sha) == (status, reason, None)
    assert "result_captured" not in sink.kinds() and git(repo, "for-each-ref", "refs/hekate-pilot") == ""
    assert sink.delivered_n == 1 and dict(sink.records)["exited"]["code"] == 0


@pytest.mark.parametrize("event, want", [
    ({"type": "system", "subtype": "init", "model": "claude-x-1"}, ["claude-x-1"]),
    ({"type": "assistant", "message": {"model": "claude-y"}}, ["claude-y"]),
    ({"type": "assistant", "message": {"model": "bad model; rm"}}, []), ({"type": "system", "subtype": "other", "model": "m"}, []),
    ({"type": "user", "message": {"model": "m"}}, []), ({"type": "system", "subtype": "init", "model": 7}, []),
])
def test_reported_model_extraction(event, want):
    assert W.reported_models(event) == want


@pytest.mark.parametrize("event, want", [
    ({"type": "result", "subtype": "success", "is_error": False}, "success"),
    ({"type": "result", "subtype": "success", "is_error": True}, "error:success"),
    ({"type": "result", "subtype": "error_max_turns", "is_error": True}, "error:error_max_turns"),
    ({"type": "result", "subtype": "error_max_budget_usd", "is_error": False}, "error:error_max_budget_usd"),
    ({"type": "result", "is_error": False}, "malformed"), ({"type": "result", "subtype": "success"}, "malformed"),
    ({"type": "result", "subtype": "success", "is_error": 0}, "malformed"),
])
def test_result_classification(event, want):
    assert W.result_class(event) == want


def test_stderr_is_bounded_but_fully_digested(tmp_path, repo):
    w, sink, rep, _ = run(tmp_path, repo, "stderr_flood", stderr_keep=4096)
    ev = w.evidence[1]
    import hashlib
    assert rep.status == "ok" and ev.stderr_bytes == 1 << 20 and ev.stderr_sha256 == hashlib.sha256(b"E" * (1 << 20)).hexdigest()
    assert len(ev.stderr_head) <= 512 and dict(sink.records)["exited"]["stderrBytes"] == 1 << 20


@pytest.mark.parametrize("drop", ["--strict-mcp-config", "--tools"])
def test_missing_flags_are_proven_by_the_fake(tmp_path, repo, monkeypatch, drop):
    real = W.build_command

    def without(cfg):
        cmd = real(cfg)
        i = cmd.index(drop)
        return cmd[:i] + cmd[i + (2 if drop == "--tools" else 1):]
    monkeypatch.setattr(W, "build_command", without)
    w, sink, rep, _ = run(tmp_path, repo, "happy")
    assert (rep.status, rep.reason) == ("failed", "nonzero_exit") and w.evidence[1].exit_code == 3


class FailingSink(Sink):
    """A journal that FAILS at one callback while the worker is alive (review finding 6, msgs 1561/1562)."""

    def __init__(self, at: str):
        super().__init__()
        self.at = at

    def journal(self, kind, data):
        if self.at == "launched" and kind == "launched":
            raise RuntimeError("journal append failed")
        super().journal(kind, data)

    def delivered(self):
        if self.at == "delivered":
            raise RuntimeError("dispatch_outcome append failed")
        super().delivered()

    def act(self, raw):
        if self.at == "act":
            raise RuntimeError("journal intake failed")
        return super().act(raw)


@pytest.mark.parametrize("at", ["launched", "delivered", "act"])
def test_a_callback_failure_never_orphans_the_live_worker(tmp_path, repo, at):
    """The fake `child` scenario stays alive (it and its own child sleep 300s). When a callback raises, the
    adapter tree-kills it, drains and joins its helpers, records supervisor_error and re-raises, without
    writing `exited` (the journal is what failed)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    w = W.CliWorker(make_cfg(repo, run_dir, total_timeout_s=120, first_output_timeout_s=60, inactivity_timeout_s=60))
    sink = FailingSink(at)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError):
        w(order("child", sink))
    assert time.monotonic() - t0 < 60
    ev = w.evidence[1]
    assert ev.kill_reason == "supervisor_error" and ev.drained is True and ev.exit_code is not None
    assert not alive(ev.pid)
    assert "exited" not in sink.kinds() and "result_captured" not in sink.kinds()
    pid_file = run_dir / "wt-r1" / "child.pid"
    if pid_file.exists():                                     # the grandchild exists once the fake got that far
        child = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not alive(child), "a descendant survived the abort-path tree kill"


def test_an_existing_worktree_path_is_never_reused(tmp_path, repo):
    run_dir = tmp_path / "run"
    (run_dir / "wt-r1").mkdir(parents=True)
    rep = W.CliWorker(make_cfg(repo, run_dir))(order("happy", Sink()))
    assert (rep.status, rep.reason) == ("failed", "worktree_exists")


# ------------------------------------------------------------------------ the optional prepare hook (msg 1632)

def test_prepare_runs_in_the_added_worktree_at_base_before_the_intent_and_the_spawn(tmp_path, repo):
    seen = []
    sink = Sink()

    def prepare(wt):
        seen.append((wt, git(wt, "rev-parse", "HEAD"), list(sink.kinds())))
        (wt / "prepared.txt").write_text("x", encoding="utf-8")         # visible to the worker, so part of the diff
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cfg = make_cfg(repo, run_dir, prepare=prepare)
    rep = W.CliWorker(cfg)(order("happy", sink))
    assert seen == [(run_dir / "wt-r1", cfg.base_sha, [])]                    # nothing journaled before it
    assert rep.status == "ok" and sink.kinds()[0] == "launch_intent"
    assert set(git(repo, "show", "--name-only", "--format=", rep.artifact_sha).split()) == {"hello.txt", "prepared.txt"}


def test_a_failing_prepare_journals_nothing_and_spawns_nothing(tmp_path, repo, monkeypatch):
    def prepare(wt):
        raise RuntimeError("deps failed")
    spawned = []
    real = subprocess.Popen
    monkeypatch.setattr(W.subprocess, "Popen", lambda *a, **kw: (a[0][0] != "git" and spawned.append(a)) or real(*a, **kw))
    w, sink, rep, _ = run(tmp_path, repo, "happy", prepare=prepare)
    assert (rep.status, rep.reason) == ("failed", "prepare_failed") and sink.records == [] and sink.acts == [] and spawned == []


def test_prepare_must_be_callable_and_defaults_to_none(tmp_path, repo):
    with pytest.raises(W.CliRefused) as e:
        W.validate(make_cfg(repo, tmp_path, prepare="npm ci"))
    assert e.value.code == "config_prepare" and make_cfg(repo, tmp_path).prepare is None


# ------------------------------------------------------------------------ explicit operator cleanup

def test_cleanup_is_explicit_refuses_dirty_or_unknown_and_keeps_the_ref_unless_asked(tmp_path, repo):
    w, sink, rep, run_dir = run(tmp_path, repo, "happy")
    cfg = w.cfg
    assert W.cleanup(cfg, 9) == "refused_not_owned_worktree"
    (run_dir / "wt-r1" / "stray.txt").write_text("x", encoding="utf-8")
    assert W.cleanup(cfg, 1) == "refused_dirty" and (run_dir / "wt-r1").is_dir()
    (run_dir / "wt-r1" / "stray.txt").unlink()
    assert W.cleanup(cfg, 1) == "removed" and not (run_dir / "wt-r1").exists()
    assert git(repo, "rev-parse", "refs/hekate-pilot/t1/r1") == rep.artifact_sha                # evidence kept
    assert W.cleanup(cfg, 1, remove_ref=True) == "refused_not_owned_worktree"


# ------------------------------------------------------------------------ the pilot driver with the real adapter (fake CLI)

def pilot_with_cli(pilot_fixture, tmp_path, repo, scenario, reviewer):
    holder = {}

    def worker(order_: P.WorkOrder) -> P.WorkReport:
        if "w" not in holder:
            run_dir = holder["p"].r.run_dir
            holder["w"] = W.CliWorker(make_cfg(repo, run_dir.resolve(), run_id=holder["p"].cfg.run_id))
        return holder["w"](replace(order_, task_text=order_.task_text + f"\nFAKE-SCENARIO: {scenario}\n"))

    p = pilot_fixture(reviewer, worker=worker, repo=repo, base=git(repo, "rev-parse", "HEAD"))
    holder["p"] = p
    return p, holder


def test_pilot_end_to_end_with_the_adapter(pilot, tmp_path, repo):
    from test_pilot_dryrun import Verdicts
    p, holder = pilot_with_cli(pilot, tmp_path, repo, "happy", Verdicts("accepted"))
    res = p.run()
    assert (res.outcome, res.reason, len(res.rounds)) == ("accepted", None, 1)
    art = res.rounds[0].artifact_ref
    assert git(repo, "rev-parse", f"{art}^") == git(repo, "rev-parse", "HEAD") and p._node()["artifactRef"] == art
    kinds = [k for k in p.records_of(res.rounds[0].claim_key)]
    assert kinds.index("launch_intent") < kinds.index("launched") < kinds.index("worker_ack") < kinds.index("exited") < kinds.index("result_captured")
    assert "dispatch_outcome" in kinds


def test_pilot_refuses_to_finish_without_a_worker_authored_ack(pilot, tmp_path, repo):
    from test_pilot_dryrun import Verdicts
    p, _ = pilot_with_cli(pilot, tmp_path, repo, "no_ack", Verdicts())
    res = p.run()
    assert (res.outcome, res.reason) == ("needs_operator", "no_worker_ack") and p._node()["work"] == "in_progress"
