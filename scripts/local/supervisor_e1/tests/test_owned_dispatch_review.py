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
