"""Owned dispatch (plan 050): offline unit tests. No database, no Api, no model: PlanStore views, the runner and the clock
are injected fakes; the real primitives under test are observe/classify wiring, the bounded loop, the status file,
liveness, the stop/launch policy and run_plan's two additive stops."""

import json
import os
from types import SimpleNamespace

import pytest

from e1 import local_store as LS
from e1 import owned_dispatch as OD
from e1 import plan_import as PI
from e1 import plan_run as PR

@pytest.fixture(autouse=True)
def process_identity_fixture(monkeypatch):
    monkeypatch.setattr(OD, "process_birth", lambda pid: "test-birth")


PINNED = "task-spec.v0 sha256=" + "a" * 64 + " path=D:/x/{key}.json"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_plan(*keys):
    ids = {k: f"id-{k}" for k in keys}
    return SimpleNamespace(root="root", node_ids=ids, after={k: () for k in keys}, project_id="p",
                           doc=SimpleNamespace(sha256="b" * 64), key_of=lambda nid: next((k for k, v in ids.items() if v == nid), None))


def make_view(plan, states, done=False):
    """states: key -> (work, acceptance, ready[, value])"""
    nodes, leaves = [], []
    for i, (k, st) in enumerate(states.items()):
        work, acc, ready = st[:3]
        value = st[3] if len(st) > 3 else PINNED.format(key=k)
        nodes.append({"id": plan.node_ids[k], "work": work, "effectiveAcceptance": acc, "value": value, "contentRevision": 1,
                      "siblingOrder": i, "artifactRef": None, "attemptEpoch": 0})
        leaves.append({"nodeId": plan.node_ids[k], "ready": ready, "blockers": []})
    containers = [{"nodeId": plan.root, "completion": "complete" if done else "incomplete", "acceptance": "accepted" if done else "none"}]
    return {"nodes": nodes, "readiness": {"leaves": leaves, "containers": containers}}


READY = ("todo", "none", True)
ok_spec = lambda path, sha: object()  # noqa: E731


def bad_spec(path, sha):
    raise PI.ImportRefused("spec_hash_mismatch")


# --- observe ---------------------------------------------------------------------------------------------------

def test_a_pending_spec_blocks_and_is_never_invented():
    plan = make_plan("a")
    obs = OD.observe(plan, make_view(plan, {"a": READY + (PI.PENDING,)}), ok_spec)
    assert (obs.kind, obs.reason, obs.next) == ("blocked", "spec_pending", "a")


def test_in_progress_is_blocked_inflight_never_running_or_ready():
    plan = make_plan("a", "b")
    obs = OD.observe(plan, make_view(plan, {"a": ("in_progress", "none", False), "b": READY}), ok_spec)
    assert (obs.kind, obs.reason) == ("blocked", "inflight")
    assert obs.next is None


def test_done_without_a_decision_is_review_pending_and_accepted_leaves_are_done():
    plan = make_plan("a")
    assert OD.observe(plan, make_view(plan, {"a": ("done", "none", False)}), ok_spec).reason == "review_pending"
    done = OD.observe(plan, make_view(plan, {"a": ("done", "accepted", False)}, done=True), ok_spec)
    assert done.kind == "done"


def test_a_ready_node_needs_a_hash_valid_pinned_spec():
    plan = make_plan("a")
    assert OD.observe(plan, make_view(plan, {"a": READY}), ok_spec).kind == "ready"
    bad = OD.observe(plan, make_view(plan, {"a": READY}), bad_spec)
    assert (bad.kind, bad.reason) == ("blocked", "spec_mismatch")


def test_plan_drift_is_a_block_not_an_exception():
    plan = make_plan("a")
    view = make_view(plan, {"a": READY})
    view["nodes"].append({"id": "stranger", "work": "todo", "effectiveAcceptance": "none", "value": "x", "contentRevision": 1,
                          "siblingOrder": 9, "artifactRef": None, "attemptEpoch": 0})
    assert OD.observe(plan, view, ok_spec).reason == "plan_drift"


# --- the bounded loop ------------------------------------------------------------------------------------------

def build(tmp_path, plan, views, dispatch=None, *, limits=None, stop=lambda: False, observe_only=False):
    clk = Clock()
    lim = limits or OD.Limits(max_duration_s=60, poll_s=5, max_poll_s=20, heartbeat_s=5, max_nodes=2).validate()
    status = OD.StatusFile(tmp_path, {"limits": {"heartbeat_s": lim.heartbeat_s}}, clk)
    reads, calls = [], []
    seq = iter(views)

    def read_view(root):
        reads.append(clk())
        return next(seq)

    def run_dispatch(n):
        calls.append(n)
        return dispatch(n)

    d = OD.Dispatcher(lim, plan, status, read_view=read_view, dispatch=run_dispatch, stop_requested=stop, observe_only=observe_only,
                      run_root=tmp_path / "runs", clock=clk, sleep=clk.sleep, load_spec=ok_spec)
    return d, status, reads, calls, clk


def result(outcome, reason=None, steps=(), nodes=None, detail=None):
    return PR.PlanRunResult(outcome, reason, "root", "b" * 64, list(steps), nodes or {}, detail)


def ran(key, outcome="accepted"):
    return PR.NodeStep(key, f"id-{key}", "ran", outcome, None, f"/runs/{key}", f"/runs/{key}/evidence.json")


def test_spec_pending_waits_with_capped_backoff_and_stops_at_the_duration_bound(tmp_path):
    plan = make_plan("a")
    blocked = make_view(plan, {"a": READY + (PI.PENDING,)})
    d, status, reads, calls, _ = build(tmp_path, plan, [blocked] * 20)
    assert d.run() == OD.EXIT_OK
    assert reads == [1000.0, 1005.0, 1015.0, 1035.0, 1055.0]          # 5, then doubled, capped at max_poll_s=20
    assert calls == []                                                 # nothing was ever dispatched
    assert (status.doc["state"], status.doc["stopReason"], status.doc["exitedCleanly"]) == ("stopped", "duration_elapsed", True)


def test_inflight_stops_immediately_and_dispatches_nothing(tmp_path):
    plan = make_plan("a", "b")
    d, status, _, calls, _ = build(tmp_path, plan, [make_view(plan, {"a": ("in_progress", "none", False), "b": READY})])
    assert d.run() == OD.EXIT_STOPPED
    assert calls == []
    assert (status.doc["state"], status.doc["stopReason"]) == ("blocked", "inflight")


def test_a_ready_node_is_dispatched_once_within_the_remaining_node_budget(tmp_path):
    plan = make_plan("a")
    d, status, _, calls, _ = build(tmp_path, plan, [make_view(plan, {"a": READY})],
                                   lambda n: result("all_done", steps=[ran("a")], nodes={"a": {"work": "done", "acceptance": "accepted", "ready": False}}))
    assert d.run() == OD.EXIT_OK
    assert calls == [2]
    assert status.doc["state"] == "done" and status.doc["counters"]["dispatched"] == 1
    assert status.doc["steps"][0]["node"] == "a"


def test_a_node_that_is_not_accepted_fails_and_is_never_retried(tmp_path):
    plan = make_plan("a")
    d, status, _, calls, _ = build(tmp_path, plan, [make_view(plan, {"a": READY})] * 3,
                                   lambda n: result("needs_operator", "node_not_accepted", [ran("a", "needs_operator")]))
    assert d.run() == OD.EXIT_STOPPED
    assert calls == [2]
    assert (status.doc["state"], status.doc["stopReason"]) == ("failed", "node_not_accepted")


def test_a_dispatch_exception_fails_without_retry_and_without_leaking_its_text(tmp_path):
    plan = make_plan("a")

    def boom(n):
        raise RuntimeError("sk-secret-value in a message")

    d, status, _, calls, _ = build(tmp_path, plan, [make_view(plan, {"a": READY})] * 3, boom)
    assert d.run() == OD.EXIT_STOPPED
    assert calls == [2]
    assert status.doc["stopReason"] == "dispatch_exception"
    assert "sk-secret" not in json.dumps(status.doc)


def test_the_node_limit_leaves_ready_work_idle_not_failed(tmp_path):
    plan = make_plan("a")
    d, status, _, _, _ = build(tmp_path, plan, [make_view(plan, {"a": READY})],
                               lambda n: result("needs_operator", "node_limit", [ran("a")]))
    assert d.run() == OD.EXIT_OK
    assert (status.doc["state"], status.doc["stopReason"]) == ("ready_idle", "node_limit")


def test_progress_then_spec_pending_keeps_waiting_for_the_operator(tmp_path):
    plan = make_plan("a", "b")
    views = [make_view(plan, {"a": READY, "b": ("todo", "none", False)}),
             make_view(plan, {"a": ("done", "accepted", False), "b": READY + (PI.PENDING,)})]
    d, status, reads, calls, _ = build(tmp_path, plan, views + [views[1]] * 30,
                                       lambda n: result("needs_operator", "spec_pending", [ran("a")]))
    assert d.run() == OD.EXIT_OK
    assert calls == [2] and len(reads) > 2                              # dispatched once, then observed and waited
    assert status.doc["stopReason"] == "duration_elapsed"


def test_observe_only_reports_ready_idle_and_never_dispatches(tmp_path):
    plan = make_plan("a")
    d, status, _, calls, _ = build(tmp_path, plan, [make_view(plan, {"a": READY})], observe_only=True)
    assert d.run() == OD.EXIT_OK
    assert calls == [] and status.doc["state"] == "ready_idle"


def test_a_stop_request_is_honoured_before_observing_and_during_a_wait(tmp_path):
    plan = make_plan("a")
    d, status, reads, _, _ = build(tmp_path, plan, [], stop=lambda: True)
    assert d.run() == OD.EXIT_OK and reads == [] and status.doc["stopReason"] == "stop_requested"
    blocked = make_view(plan, {"a": READY + (PI.PENDING,)})
    d, status, reads, calls, clk = build(tmp_path, plan, [blocked] * 5)
    d.stop_requested = lambda: clk.t >= 1003.0
    assert d.run() == OD.EXIT_OK
    assert len(reads) == 1 and status.doc["stopReason"] == "stop_requested"


def test_an_unreadable_plan_store_is_a_failed_stop(tmp_path):
    plan = make_plan("a")
    d, status, _, calls, _ = build(tmp_path, plan, [])               # next() raises StopIteration inside read_view
    assert d.run() == OD.EXIT_STOPPED
    assert (status.doc["state"], status.doc["stopReason"]) == ("failed", "observe_failed") and calls == []


# --- restart, liveness, status ---------------------------------------------------------------------------------

def write_status(state_dir, **over):
    d = OD.dispatch_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"schema": OD.SCHEMA, "phase": "dispatching", "state": "running", "owner": {"pid": 4242, "processBirth": "test-birth"}, "limits": {"heartbeat_s": 15},
           "heartbeat": {"epoch": 1000.0, "intervalS": 15}, "current": {"node": "a"}}
    doc.update(over)
    (d / OD.STATUS).write_text(json.dumps(doc), encoding="utf-8")


def test_liveness_needs_a_live_pid_and_a_fresh_heartbeat(tmp_path):
    write_status(tmp_path)
    up, down = (lambda p: True), (lambda p: False)
    assert OD.report(tmp_path, 1010.0, up)["liveness"] == "running"
    assert OD.report(tmp_path, 5000.0, up)["liveness"] == "unresponsive"      # a reused pid with an old heartbeat is not alive
    gone = OD.report(tmp_path, 1010.0, down)
    assert (gone["liveness"], gone["state"], gone["lastPhase"]) == ("owner_gone", "owner_gone", "dispatching")
    assert "do not reset" in gone["note"]
    write_status(tmp_path, phase="exited", state="done")
    assert OD.report(tmp_path, 9999.0, up)["state"] == "done"


def test_no_status_is_reported_as_such(tmp_path):
    assert OD.report(tmp_path)["state"] == "no_status"


def test_a_restart_records_a_dead_previous_owner_and_refuses_a_live_one(tmp_path):
    write_status(tmp_path)
    prev = OD.previous_owner(tmp_path, 1010.0, lambda p: False)
    assert (prev["liveness"], prev["wasDispatching"], prev["pid"]) == ("owner_gone", "a", 4242)
    with pytest.raises(OD.Refused) as e:
        OD.previous_owner(tmp_path, 1010.0, lambda p: True)
    assert e.value.code == "owner_running"
    assert OD.previous_owner(tmp_path / "none", 0.0) is None


def test_a_restart_with_an_in_flight_node_dispatches_nothing(tmp_path):
    write_status(tmp_path)                                           # the crashed owner was dispatching "a"
    assert OD.previous_owner(tmp_path, 1010.0, lambda p: False)["wasDispatching"] == "a"
    plan = make_plan("a", "b")
    (tmp_path / "next").mkdir()
    d, status, _, calls, _ = build(tmp_path / "next", plan, [make_view(plan, {"a": ("in_progress", "none", False), "b": READY})])
    assert d.run() == OD.EXIT_STOPPED and calls == [] and status.doc["stopReason"] == "inflight"


def test_status_is_atomic_bounded_and_secret_free(tmp_path):
    s = OD.StatusFile(tmp_path, {"limits": {"heartbeat_s": 5}}, Clock())
    s.update(detail={"apiKey": "sk-1", "prompt": "raw prompt text", "node": "a", "long": "x" * 5000, "nested": {"authToken": "t"}})
    s.beat()
    text = (tmp_path / OD.STATUS).read_text(encoding="utf-8")
    for leak in ("sk-1", "raw prompt text", '"t"'):
        assert leak not in text
    doc = json.loads(text)
    assert doc["detail"]["node"] == "a" and "long" not in doc["detail"] and doc["heartbeat"]["seq"] == 1
    assert not (tmp_path / (OD.STATUS + ".tmp")).exists()


def test_a_status_write_failure_is_counted_not_raised(tmp_path):
    s = OD.StatusFile(tmp_path / "missing", {"limits": {"heartbeat_s": 5}}, Clock())
    s.update(phase="x")
    s.beat()
    s.event("e")
    assert s.write_errors == 3


def test_limits_are_bounded():
    assert OD.Limits().validate()
    for bad in (dict(max_duration_s=10**6), dict(poll_s=0), dict(max_nodes=0), dict(max_nodes=21), dict(heartbeat_s=1),
                dict(poll_s=100, max_poll_s=50)):
        with pytest.raises(OD.Refused):
            OD.Limits(**bad).validate()


# --- stop and launch policy ------------------------------------------------------------------------------------

def test_stop_needs_a_live_owner_and_is_requested_once(tmp_path):
    code, out = OD.request_stop(tmp_path, "op", 1010.0, lambda p: True)
    assert code == OD.EXIT_REFUSED and out["refused"] == "no_live_owner"
    write_status(tmp_path)
    code, out = OD.request_stop(tmp_path, "op", 1010.0, lambda p: True)
    assert code == OD.EXIT_OK and out["stopRequested"]
    assert OD.request_stop(tmp_path, "op", 1010.0, lambda p: True)[1]["refused"] == "stop_already_requested"
    import threading
    assert OD.stop_flag(OD.dispatch_dir(tmp_path), threading.Event())()


def test_hidden_launch_policy_on_windows_and_elsewhere():
    kw = OD.hidden_popen_kwargs("win32")
    assert kw["creationflags"] & OD.CREATE_NO_WINDOW and kw["creationflags"] & OD.CREATE_NEW_PROCESS_GROUP
    assert kw["creationflags"] & OD.CREATE_BREAKAWAY_FROM_JOB
    assert not OD.hidden_popen_kwargs("win32", breakaway=False)["creationflags"] & OD.CREATE_BREAKAWAY_FROM_JOB
    if os.name == "nt":
        assert kw["startupinfo"].wShowWindow == 0                       # SW_HIDE
    assert OD.hidden_popen_kwargs("linux") == {"start_new_session": True}


def test_the_child_argv_is_rebuilt_from_pins_only():
    a = OD.build_parser().parse_args(["launch", "--state-dir", "S", "--plan", "P.json", "--plan-sha256", "c" * 64, "--run-root", "R",
                                      "--exe", "E", "--exe-sha256", "d" * 64, "--root-go", "go-1", "--launch-real-model"])
    argv = OD.run_argv(a)
    assert argv[0] == "run" and "--launch-mode" in argv and "--launch-real-model" in argv
    b = OD.build_parser().parse_args(argv)
    assert (b.plan_sha256, b.exe_sha256, b.max_nodes, b.launch_mode) == ("c" * 64, "d" * 64, 3, "hidden")


# --- serve refuses before any effect ---------------------------------------------------------------------------

def prepared(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / LS.LOCATOR).write_text(json.dumps({"db": "hekate_coord_20261008000000_abcdef01", "marker": "e" * 64,
                                                "projectId": "11111111-1111-4111-8111-111111111111", "apiPort": 5109}))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"version": "plan-import.v0", "title": "t", "nodes": [{"key": "a", "name": "A", "spec": None, "after": []}]}))
    exe = tmp_path / "exe.bin"
    exe.write_bytes(b"exe")
    import hashlib
    argv = ["run", "--state-dir", str(state), "--plan", str(plan), "--plan-sha256", hashlib.sha256(plan.read_bytes()).hexdigest(),
            "--run-root", str(tmp_path / "runs"), "--exe", str(exe), "--exe-sha256", hashlib.sha256(b"exe").hexdigest(),
            "--root-go", "go-1", "--launch-real-model"]
    return state, argv


def test_a_wrong_plan_pin_is_refused_before_the_store_is_touched(tmp_path, capsys):
    state, argv = prepared(tmp_path)
    argv[argv.index("--plan-sha256") + 1] = "0" * 64

    def never(_):
        raise AssertionError("the store must not be opened")

    assert OD.serve(OD.build_parser().parse_args(argv), opener=never) == OD.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["refused"] == "plan_pin_mismatch"
    assert not OD.dispatch_dir(state).exists()


def test_a_second_owner_is_refused_by_the_store_lock_and_writes_nothing(tmp_path, capsys):
    state, argv = prepared(tmp_path)

    def busy(_):
        raise LS.LocalStoreRefused("store_in_use", "db")

    assert OD.serve(OD.build_parser().parse_args(argv), opener=busy) == OD.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["refused"] == "store_in_use"
    assert not OD.dispatch_dir(state).exists()


def test_a_live_dispatcher_refuses_a_second_start_before_opening_the_store(tmp_path, capsys):
    import time
    state, argv = prepared(tmp_path)
    # this very test process is the "owner": a real live pid, with a heartbeat that stays fresh for the test's duration
    write_status(state, owner={"pid": os.getpid(), "processBirth": "test-birth"}, heartbeat={"epoch": time.time(), "intervalS": 15})
    assert OD.serve(OD.build_parser().parse_args(argv), opener=lambda _: pytest.fail("store opened")) == OD.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["refused"] == "owner_running"


# --- run_plan's two additive stops -----------------------------------------------------------------------------

class FakeSetup:
    def __init__(self, view):
        self.view = view

    def plan(self, root):
        return SimpleNamespace(status=200, body=self.view, code=None)


def run_plan_with(tmp_path, view, plan, **kw):
    return PR.run_plan(plan, tmp_path / "rr", setup=FakeSetup(view), client=None, aj=None, executable=(), executable_sha256="",
                       execution_kind="fake-cli", root_go=None, **kw)


def test_run_plan_stop_requested_and_node_limit_stop_before_anything_is_claimed(tmp_path):
    plan = make_plan("a")
    view = make_view(plan, {"a": READY})
    r = run_plan_with(tmp_path, view, plan, stop_requested=lambda: True)
    assert (r.outcome, r.reason, r.steps) == ("needs_operator", "stop_requested", [])
    r = run_plan_with(tmp_path, view, plan, max_nodes=0)
    assert (r.reason, r.steps) == ("node_limit", [])
    r = run_plan_with(tmp_path, make_view(plan, {"a": READY + (PI.PENDING,)}), plan)          # unchanged default behaviour
    assert r.reason == "spec_pending"
