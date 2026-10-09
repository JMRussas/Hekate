"""Fenced owned stop (plan 053): offline tests. No database, no Api, no model, no real process or port: process identity,
clock and the PlanStore/runner are injected fakes; the real code under test is request_stop, the stop-file reader, the
StopGate and the dispatcher loop wiring."""

import json
import os
import threading
from types import SimpleNamespace

import pytest

from e1 import owned_dispatch as OD
from e1 import plan_import as PI
from e1 import plan_run as PR

A, B = "a" * 32, "b" * 32
UP = lambda pid: True  # noqa: E731


@pytest.fixture(autouse=True)
def process_identity_fixture(monkeypatch):
    monkeypatch.setattr(OD, "process_birth", lambda pid: "test-birth")


def write_status(state_dir, launch_id=A, **over):
    d = OD.dispatch_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"schema": OD.SCHEMA, "phase": "waiting", "state": "blocked", "limits": {"heartbeat_s": 15},
           "owner": {"pid": 4242, "processBirth": "test-birth", "launchId": launch_id}, "heartbeat": {"epoch": 1000.0, "intervalS": 15}}
    doc.update(over)
    (d / OD.STATUS).write_text(json.dumps(doc), encoding="utf-8")


def stop_path(state_dir):
    return OD.dispatch_dir(state_dir) / OD.STOP


def names(state_dir):
    return sorted(p.name for p in OD.dispatch_dir(state_dir).iterdir() if p.name.startswith(OD.STOP))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def gate(state_dir, launch_id, clock=None, events=None):
    note = (lambda kind, **d: events.append((kind, d))) if events is not None else None
    return OD.stop_flag(OD.dispatch_dir(state_dir), threading.Event(), launch_id, clock=clock or Clock(), note=note)


# --- request_stop: validation and refusals happen before any stop-file effect ---------------------------------------

@pytest.mark.parametrize("bad", ["", "A" * 32, "a" * 31, "a" * 33, "g" * 32, " " + "a" * 31, "a" * 32 + "\n", "../" + "a" * 29])
def test_an_invalid_expected_id_is_refused_before_any_effect(tmp_path, bad):
    write_status(tmp_path)
    code, out = OD.request_stop(tmp_path, "op", 1010.0, UP, expected_launch_id=bad)
    assert (code, out) == (OD.EXIT_REFUSED, {"refused": "invalid_expected_launch_id"})
    assert names(tmp_path) == []


def test_an_invalid_expected_id_is_refused_even_without_an_owner(tmp_path):
    assert OD.request_stop(tmp_path / "none", "op", 1010.0, UP, expected_launch_id="x")[1] == {"refused": "invalid_expected_launch_id"}
    assert not (tmp_path / "none").exists()


def test_a_wrong_expected_owner_is_refused_before_the_stop_file_exists(tmp_path):
    write_status(tmp_path, launch_id=B)
    code, out = OD.request_stop(tmp_path, "op", 1010.0, UP, expected_launch_id=A)
    assert code == OD.EXIT_REFUSED and out["refused"] == "owner_changed"
    assert names(tmp_path) == []


def test_an_owner_without_a_launch_id_cannot_satisfy_a_fence(tmp_path):
    write_status(tmp_path)
    doc = json.loads((OD.dispatch_dir(tmp_path) / OD.STATUS).read_text())
    del doc["owner"]["launchId"]
    (OD.dispatch_dir(tmp_path) / OD.STATUS).write_text(json.dumps(doc))
    assert OD.request_stop(tmp_path, "op", 1010.0, UP, expected_launch_id=A)[1]["refused"] == "owner_changed"
    assert names(tmp_path) == []


def test_no_live_owner_is_still_refused_when_fenced(tmp_path):
    write_status(tmp_path)
    assert OD.request_stop(tmp_path, "op", 1010.0, lambda p: False, expected_launch_id=A)[1]["refused"] == "no_live_owner"
    assert names(tmp_path) == []


def test_an_accepted_fenced_request_echoes_the_target_and_stores_it_without_claiming_delivery(tmp_path):
    write_status(tmp_path)
    code, out = OD.request_stop(tmp_path, "op", 1010.0, UP, expected_launch_id=A)
    assert code == OD.EXIT_OK and out["stopRequested"] is True and out["targetLaunchId"] == A
    assert "not confirmed" in out["note"] and "stopped" not in out and "delivered" not in out
    body = json.loads(stop_path(tmp_path).read_bytes())
    assert body["targetLaunchId"] == A and set(body) == {"requestedBy", "requestedAt", "id", "targetLaunchId"}
    assert OD.request_stop(tmp_path, "op", 1010.0, UP, expected_launch_id=A)[1] == {"refused": "stop_already_requested"}


def test_the_default_is_the_unchanged_legacy_unfenced_request(tmp_path):
    write_status(tmp_path)
    code, out = OD.request_stop(tmp_path, "op", 1010.0, UP)
    assert code == OD.EXIT_OK and "targetLaunchId" not in out
    assert set(json.loads(stop_path(tmp_path).read_bytes())) == {"requestedBy", "requestedAt", "id"}
    assert gate(tmp_path, B)() is True                                   # legacy: whichever dispatcher polls it honours it


# --- the race: owner B replaces A between request_stop's read and its write -------------------------------------------

def test_a_replacement_owner_between_the_precheck_and_the_write_is_not_stopped(tmp_path, monkeypatch):
    write_status(tmp_path, launch_id=A)
    real_report = OD.report

    def report_then_replace(*a, **k):
        rep = real_report(*a, **k)                                       # request_stop sees A ...
        write_status(tmp_path, launch_id=B)                              # ... then B takes over before the write
        return rep

    monkeypatch.setattr(OD, "report", report_then_replace)
    code, out = OD.request_stop(tmp_path, "op", 1010.0, UP, expected_launch_id=A)
    assert code == OD.EXIT_OK and out["targetLaunchId"] == A             # accepted for A, nothing more claimed
    events = []
    b = gate(tmp_path, B, events=events)
    assert b() is False and b.honored is False                           # B does not stop
    assert [k for k, _ in events] == ["foreign_stop_request_ignored"]
    assert not stop_path(tmp_path).exists() and len(names(tmp_path)) == 1  # retained, renamed, not deleted
    retained = json.loads((OD.dispatch_dir(tmp_path) / names(tmp_path)[0]).read_bytes())
    assert retained["targetLaunchId"] == A


def test_the_old_unfenced_guard_would_have_stopped_the_replacement(tmp_path):
    """Negative control: the same late request, judged by the pre-fence rule (the file merely exists)."""
    write_status(tmp_path, launch_id=A)
    assert OD.request_stop(tmp_path, "op", 1010.0, UP, expected_launch_id=A)[0] == OD.EXIT_OK
    old_guard = lambda: stop_path(tmp_path).exists()  # noqa: E731 -- stop_flag before plan 053
    assert old_guard() is True
    assert gate(tmp_path, B)() is False


def test_a_foreign_request_arriving_after_b_started_is_ignored_and_retained(tmp_path):
    """B's startup archival has already run; A's late request lands afterwards."""
    write_status(tmp_path, launch_id=B)
    stop_path(tmp_path).write_text(json.dumps({"targetLaunchId": A, "id": "late"}))
    events = []
    b = gate(tmp_path, B, events=events)
    assert b() is False and b() is False
    assert b.ignored == 1 and len(events) == 1 and events[0][1]["target"] == A
    assert names(tmp_path) == [events[0][1]["retained"]]
    # the slot is free again: a new, correctly fenced request for B is accepted and honoured
    assert OD.request_stop(tmp_path, "op", 1010.0, UP, expected_launch_id=B)[0] == OD.EXIT_OK
    assert b() is True and b.honored is True


def test_a_foreground_dispatcher_without_a_launch_id_never_matches_a_fence(tmp_path):
    OD.dispatch_dir(tmp_path).mkdir(parents=True)
    stop_path(tmp_path).write_text(json.dumps({"targetLaunchId": A}))
    assert gate(tmp_path, None)() is False


def test_the_matching_fenced_request_is_honoured_and_latches(tmp_path):
    write_status(tmp_path, launch_id=A)
    OD.request_stop(tmp_path, "op", 1010.0, UP, expected_launch_id=A)
    g = gate(tmp_path, A)
    assert g() is True
    stop_path(tmp_path).unlink()
    assert g() is True                                                   # latched: a stop once honoured is not forgotten


def test_the_halt_event_still_stops(tmp_path):
    halt = threading.Event()
    g = OD.stop_flag(OD.dispatch_dir(tmp_path), halt, A)
    assert g() is False
    halt.set()
    assert g() is True


# --- unknown content: bounded read, never trusted, never deleted ------------------------------------------------------

def test_malformed_content_is_unknown_not_a_stop_and_is_retained_after_the_grace(tmp_path):
    OD.dispatch_dir(tmp_path).mkdir(parents=True)
    stop_path(tmp_path).write_text("{not json")
    clk, events = Clock(), []
    g = gate(tmp_path, A, clk, events)
    assert g() is False and stop_path(tmp_path).exists()                 # within the grace: left alone (may be half-written)
    clk.t += OD.STOP_UNKNOWN_GRACE_S
    assert g() is False and not stop_path(tmp_path).exists()
    assert events[0][0] == "unverified_stop_request_ignored" and events[0][1]["reason"] == "malformed"
    assert names(tmp_path) == [events[0][1]["retained"]]


@pytest.mark.parametrize("content", ["", "[]", '"x"', '{"targetLaunchId": 7}', '{"targetLaunchId": "' + "A" * 32 + '"}',
                                     '{"targetLaunchId": null}', '{"targetLaunchId": "' + A + '\\n"}'])
def test_other_unestablishable_requests_are_not_honoured(tmp_path, content):
    OD.dispatch_dir(tmp_path).mkdir(parents=True)
    stop_path(tmp_path).write_text(content)
    assert OD.read_stop_request(stop_path(tmp_path))[0] == "unknown"
    assert gate(tmp_path, A)() is False


class OsWithRead:
    """`os` as seen by owned_dispatch, with only `read` replaced (so no global patching of the real module)."""

    def __init__(self, read):
        self.read = read

    def __getattr__(self, name):
        return getattr(os, name)


def test_an_oversized_file_is_refused_without_being_read(tmp_path, monkeypatch):
    OD.dispatch_dir(tmp_path).mkdir(parents=True)
    stop_path(tmp_path).write_text(json.dumps({"targetLaunchId": A, "pad": "x" * OD.STOP_READ_MAX}))
    monkeypatch.setattr(OD, "os", OsWithRead(lambda *a: pytest.fail("an oversized stop file must not be read")))
    assert OD.read_stop_request(stop_path(tmp_path)) == ("unknown", "oversized")


def test_the_read_itself_is_capped(tmp_path, monkeypatch):
    """A file that grows after the size check is still read at most STOP_READ_MAX + 1 bytes."""
    OD.dispatch_dir(tmp_path).mkdir(parents=True)
    stop_path(tmp_path).write_text("{}")
    asked = []
    monkeypatch.setattr(OD, "os", OsWithRead(lambda fd, n: asked.append(n) or b"x" * n))
    assert OD.read_stop_request(stop_path(tmp_path)) == ("unknown", "oversized")
    assert asked == [OD.STOP_READ_MAX + 1]


def test_a_directory_or_link_at_the_stop_path_is_not_followed_and_not_deleted(tmp_path):
    OD.dispatch_dir(tmp_path).mkdir(parents=True)
    stop_path(tmp_path).mkdir()
    assert OD.read_stop_request(stop_path(tmp_path)) == ("unknown", "not_regular_file")
    stop_path(tmp_path).rmdir()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"requestedBy": "x"}))                  # would be a valid legacy stop if followed
    try:
        stop_path(tmp_path).symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert OD.read_stop_request(stop_path(tmp_path)) == ("unknown", "not_regular_file")
    clk = Clock()
    g = gate(tmp_path, A, clk)
    assert g() is False
    clk.t += OD.STOP_UNKNOWN_GRACE_S
    assert g() is False
    assert target.exists() and json.loads(target.read_text()) == {"requestedBy": "x"}   # the link target is untouched
    assert len(names(tmp_path)) == 1


def test_retained_evidence_is_bounded(tmp_path):
    OD.dispatch_dir(tmp_path).mkdir(parents=True)
    g = gate(tmp_path, B)
    for _ in range(OD.STOP_RETAINED_MAX + 3):
        stop_path(tmp_path).write_text(json.dumps({"targetLaunchId": A}))
        assert g() is False
    assert len(names(tmp_path)) == OD.STOP_RETAINED_MAX + 1              # the cap, plus the last one left in place
    assert stop_path(tmp_path).exists()


# --- the loop: a matching request ends after the node in flight, with no next claim -----------------------------------

def make_plan(*keys):
    ids = {k: f"id-{k}" for k in keys}
    return SimpleNamespace(root="root", node_ids=ids, after={k: () for k in keys}, project_id="p",
                           doc=SimpleNamespace(sha256="b" * 64), key_of=lambda nid: next((k for k, v in ids.items() if v == nid), None))


def make_view(plan, states):
    nodes = [{"id": plan.node_ids[k], "work": w, "effectiveAcceptance": acc, "value": "task-spec.v0 sha256=" + "a" * 64 + f" path=D:/x/{k}.json",
              "contentRevision": 1, "siblingOrder": i, "artifactRef": None, "attemptEpoch": 0} for i, (k, (w, acc, _)) in enumerate(states.items())]
    leaves = [{"nodeId": plan.node_ids[k], "ready": r, "blockers": []} for k, (_, _, r) in states.items()]
    return {"nodes": nodes, "readiness": {"leaves": leaves, "containers": [{"nodeId": plan.root, "completion": "incomplete", "acceptance": "none"}]}}


def run_loop(tmp_path, launch_id, in_node):
    plan = make_plan("a", "b")
    clk = Clock()
    lim = OD.Limits(max_duration_s=60, poll_s=5, max_poll_s=20, heartbeat_s=5, max_nodes=5).validate()
    sdir = tmp_path / "loop-status"                                      # apart from the owner status request_stop reads
    sdir.mkdir()
    status = OD.StatusFile(sdir, {"limits": {"heartbeat_s": 5}}, clk)
    view, calls = make_view(plan, {"a": ("todo", "none", True), "b": ("todo", "none", False)}), []

    def dispatch(n):
        calls.append(n)
        in_node()
        step = PR.NodeStep("a", "id-a", "ran", "accepted", None, "/runs/a", "/runs/a/evidence.json")
        return PR.PlanRunResult("needs_operator", "review_pending", "root", "b" * 64, [step], {}, None)

    d = OD.Dispatcher(lim, plan, status, read_view=lambda root: view, dispatch=dispatch,
                      stop_requested=gate(tmp_path, launch_id, clk), run_root=tmp_path / "runs", clock=clk, sleep=lambda s: setattr(clk, "t", clk.t + s),
                      load_spec=lambda path, sha: object())
    return d, status, calls


def test_a_matching_request_made_during_a_node_is_honoured_after_it_with_no_next_claim(tmp_path):
    write_status(tmp_path, launch_id=A)
    d, status, calls = run_loop(tmp_path, A, lambda: OD.request_stop(tmp_path, "op", 1010.0, UP, expected_launch_id=A))
    assert d.run() == OD.EXIT_OK
    assert calls == [5]                                                  # the node in flight finished; nothing was claimed after it
    assert (status.doc["state"], status.doc["stopReason"], status.doc["counters"]["dispatched"]) == ("stopped", "stop_requested", 1)


def test_a_foreign_request_made_during_a_node_does_not_stop_the_dispatcher(tmp_path):
    write_status(tmp_path, launch_id=B)
    d, status, calls = run_loop(tmp_path, B, lambda: stop_path(tmp_path).write_text(json.dumps({"targetLaunchId": A})))
    assert d.run() == OD.EXIT_OK
    assert status.doc["stopReason"] == "node_limit"                      # it ran to its own node budget: the foreign request stopped nothing
    assert calls == [5, 4, 3, 2, 1]


# --- CLI: the flag is for stop only, and never becomes the new owner's launch id ---------------------------------------

@pytest.mark.parametrize("command", ["launch", "run", "status"])
def test_other_commands_reject_the_flag_before_any_effect(tmp_path, capsys, command):
    code = OD.main([command, "--state-dir", str(tmp_path / "s"), "--expected-launch-id", A])
    assert code == OD.EXIT_REFUSED and json.loads(capsys.readouterr().out)["refused"] == "unsupported_flag"
    assert not (tmp_path / "s").exists()


def test_the_stop_command_passes_the_flag_and_the_default_is_none(tmp_path, capsys, monkeypatch):
    write_status(tmp_path, launch_id=B)
    seen, real = [], OD.request_stop
    monkeypatch.setattr(OD, "request_stop", lambda *a, **k: seen.append(k) or real(*a, **k))
    assert OD.main(["stop", "--state-dir", str(tmp_path)]) == OD.EXIT_REFUSED      # real clock: the 1000.0 heartbeat is stale
    assert OD.main(["stop", "--state-dir", str(tmp_path), "--expected-launch-id", "bad"]) == OD.EXIT_REFUSED
    assert [k["expected_launch_id"] for k in seen] == [None, "bad"]
    assert names(tmp_path) == []
    capsys.readouterr()


def test_the_expected_id_is_not_rebuilt_into_the_child_argv():
    ns = SimpleNamespace(plan="p", plan_sha256="h", state_dir="s", run_root="r", exe="e", exe_sha256="x", root_go="g", worker="claude", actor="op",
                         max_duration_s=60, poll_s=5, max_poll_s=20, heartbeat_s=5, max_nodes=1, launch_id=B, launch_real_model=False,
                         exe_arg=None, worker_model=None, observe_only=False, expected_launch_id=A)
    argv = OD.run_argv(ns)
    assert A not in argv and "--expected-launch-id" not in argv and argv[argv.index("--launch-id") + 1] == B
