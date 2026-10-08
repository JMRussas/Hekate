"""Independent regression checks for process identity, public evidence and launch fences."""
import json
import os
import time
from e1 import owned_dispatch as OD
from test_owned_dispatch import prepared


def doc(birth):
    return {"phase": "dispatching", "owner": {"pid": os.getpid(), "processBirth": birth},
            "heartbeat": {"epoch": time.time(), "intervalS": 15}}


def test_native_process_identity_is_available_and_alive():
    birth = OD.process_birth(os.getpid())
    assert birth is not None and OD.pid_alive(os.getpid())
    assert OD.liveness(doc(birth), time.time()) == "running"


def test_reused_pid_with_fresh_heartbeat_is_not_current_owner(monkeypatch):
    monkeypatch.setattr(OD, "process_birth", lambda pid: "new-native-creation")
    assert OD.liveness(doc("old-native-creation"), time.time()) == "owner_gone"


def test_future_or_nonfinite_heartbeat_never_establishes_liveness():
    for epoch in (time.time() + 3600, float("nan"), float("inf"), "now", True):
        value = doc(OD.process_birth(os.getpid()))
        value["heartbeat"]["epoch"] = epoch
        assert OD.liveness(value, time.time()) == "unresponsive"


def test_missing_process_identity_is_explicitly_unverified():
    assert OD.liveness(doc(None), time.time()) == "owner_unverified"


def test_public_detail_drops_credentials_in_arbitrary_error_values(tmp_path):
    status = OD.StatusFile(tmp_path, {"limits": {"heartbeat_s": 5}})
    status.update(detail={"message": "https://user:secret@host/raw prompt", "error": "Bearer secret value",
                          "node": "task-one", "stderr": "private text"})
    text = (tmp_path / OD.STATUS).read_text()
    assert "secret" not in text and "private text" not in text and "raw prompt" not in text
    assert json.loads(text)["detail"]["node"] == "task-one"


def test_launch_checks_plan_pin_before_spawning(tmp_path, capsys):
    _, argv = prepared(tmp_path)
    argv[0] = "launch"
    argv[argv.index("--plan-sha256") + 1] = "0" * 64
    def never(*args, **kwargs):
        raise AssertionError("bad plan pin spawned a child")
    assert OD.launch(OD.build_parser().parse_args(argv), popen=never) == OD.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["refused"] == "plan_pin_mismatch"


def test_nonpositive_pid_never_probes_process_group():
    value = doc("birth")
    for pid in (0, -1):
        value["owner"]["pid"] = pid
        def never(_):
            raise AssertionError("nonpositive pid probed")
        assert OD.liveness(value, time.time(), never) == "owner_gone"


def test_failure_during_owner_setup_stops_acquired_store(tmp_path, monkeypatch):
    from types import SimpleNamespace
    _, argv = prepared(tmp_path)
    stopped = []
    store = SimpleNamespace(loc=SimpleNamespace(db="test-owned"), stop=lambda: stopped.append(True))
    def broken(_):
        raise OSError("creation identity unavailable")
    monkeypatch.setattr(OD, "process_birth", broken)
    import pytest
    with pytest.raises(OSError):
        OD.serve(OD.build_parser().parse_args(argv), opener=lambda _: store)
    assert stopped == [True]


def test_launch_correlates_virtualenv_launcher_with_runtime_owner(tmp_path, capsys):
    from types import SimpleNamespace
    state, argv = prepared(tmp_path)
    argv[0] = "launch"
    def wrapper_spawn(command, **kwargs):
        launch_id = command[command.index("--launch-id") + 1]
        value = doc(OD.process_birth(os.getpid()))
        value.update(schema=OD.SCHEMA, state="running")
        value["owner"]["launchId"] = launch_id
        (OD.dispatch_dir(state) / OD.STATUS).write_text(json.dumps(value))
        return SimpleNamespace(pid=7777, poll=lambda: None)
    assert OD.launch(OD.build_parser().parse_args(argv), popen=wrapper_spawn) == OD.EXIT_OK
    result = json.loads(capsys.readouterr().out)
    assert result["launched"] and result["pid"] == os.getpid() and result["launcherPid"] == 7777
