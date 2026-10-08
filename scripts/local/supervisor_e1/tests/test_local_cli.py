"""Local coordinator CLI (root msgs 1915/1926): `create` refuses before any effect, creates a NEW owned coordinator database
that open() accepts, stops its Api and keeps the database, and keeps a partial database on a later failure. Every
coordinator database here is created and dropped BY THE TEST (disposable); no existing database is touched."""

import json
import socket
from pathlib import Path

import pytest

from e1 import harness as HZ
from e1 import local_cli as CLI
from e1 import local_store as LS

PORT = 5143


def last_json(capsys) -> dict:
    out = capsys.readouterr().out
    start = out.rfind("\n{")
    return json.loads(out[start + 1:] if start >= 0 else out)


def never(*a, **kw):
    raise AssertionError("no database may be created before the inputs are checked")


def drop_if_created(state: Path) -> None:
    """Cleanup that never masks the test's own failure: only a database the locator names, created by this test."""
    if (state / LS.LOCATOR).exists():
        s = LS.LocalStore(state, LS.Locator.read(state))
        s._container()
        s.drop_for_test()


@pytest.mark.parametrize("argv, code", [
    (["create"], "state_dir_required"),
    (["create", "--state-dir", "{tmp}/s", "--api-port", "5108"], "api_port_reserved"),
    (["create", "--state-dir", "{tmp}/s", "--api-port", "80"], "api_port_invalid"),
    (["create", "--state-dir", "{repo}/coordinator-state"], "state_dir_in_repo"),
])
def test_create_refuses_bad_inputs_before_any_effect(tmp_path, capsys, argv, code):
    repo = Path(__file__).resolve().parents[1]                     # inside this checkout: a tracked-tree risk
    argv = [x.format(tmp=tmp_path.as_posix(), repo=repo.as_posix()) for x in argv]
    assert CLI.main(argv, creator=never) == 2
    assert last_json(capsys)["refused"] == code
    assert not (tmp_path / "s").exists() and not (repo / "coordinator-state").exists()


def test_an_existing_locator_is_refused_and_left_untouched(tmp_path, capsys):
    (tmp_path / LS.LOCATOR).write_text("{}", encoding="utf-8")
    assert CLI.main(["create", "--state-dir", str(tmp_path), "--api-port", str(PORT)], creator=never) == 2
    assert last_json(capsys)["refused"] == "locator_exists"
    assert (tmp_path / LS.LOCATOR).read_text(encoding="utf-8") == "{}"


def test_a_busy_port_is_refused_before_any_database_and_the_listener_is_left_alone(tmp_path, capsys):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(16)
        port = s.getsockname()[1]
        assert CLI.main(["create", "--state-dir", str(tmp_path / "s"), "--api-port", str(port)], creator=never) == 2
        assert last_json(capsys) == {"refused": "api_port_in_use", "detail": port}
        assert not HZ._port_free(port)                              # still listening: never killed or adopted


def test_create_makes_a_new_store_that_open_accepts_and_keeps_it_after_stop(tmp_path, capsys):
    state = tmp_path / "state"
    assert CLI.main(["create", "--state-dir", str(state), "--api-port", str(PORT)]) == 0
    out = last_json(capsys)
    try:
        assert out["outcome"] == "created" and LS.COORD_RE.match(out["db"]) and out["apiPort"] == PORT
        assert HZ._port_free(PORT)                                  # the CLI stopped its Api before exit
        loc = LS.Locator.read(state)
        assert (loc.db, loc.project_id, loc.api_port) == (out["db"], out["projectId"], PORT)
        again = LS.LocalStore.open(state)                           # the kept database opens and verifies
        again.stop()
        assert CLI.main(["create", "--state-dir", str(state), "--api-port", str(PORT)], creator=never) == 2
        assert last_json(capsys)["refused"] == "locator_exists"     # never re-created in place
    finally:
        drop_if_created(state)


def test_a_failure_after_the_database_exists_keeps_it_and_its_locator(tmp_path, capsys, monkeypatch):
    def boom(dsn):
        raise RuntimeError("installer failed\nsecond line")
    monkeypatch.setattr(LS, "install_acts", boom)
    state = tmp_path / "state"
    assert CLI.main(["create", "--state-dir", str(state), "--api-port", str(PORT)]) == 1
    out = last_json(capsys)
    try:
        assert (out["outcome"], out["code"], out["detail"], out["locatorKept"]) == ("create_failed", "RuntimeError",
                                                                                    "installer failed", True)
        assert HZ._port_free(PORT)                                  # its own Api was stopped
        s = LS.LocalStore(state, LS.Locator.read(state))
        s._container()
        assert s.psql(f"SELECT 1 FROM pg_database WHERE datname = '{s.loc.db}'", db="postgres") == "1"   # kept
    finally:
        drop_if_created(state)


def test_no_owned_container_is_a_refusal_before_any_effect(tmp_path, capsys):
    """Review 1932 R2: the store refuses before createdb (no owned container): exit 2, nothing created."""
    def no_container(state, api_port):
        raise LS.LocalStoreRefused("container", "expected exactly one running owned hekate-local container, found 0")
    assert CLI.main(["create", "--state-dir", str(tmp_path / "s"), "--api-port", str(PORT)], creator=no_container) == 2
    assert last_json(capsys)["refused"] == "container"


def test_a_failure_without_a_locator_warns_that_an_unnamed_database_may_exist(tmp_path, capsys):
    """Review 1932 R1: createdb runs before the marker and the locator, so a failure with no locator kept must say a
    hekate_coord_* database may exist unnamed."""
    def fails_before_locator(state, api_port):
        raise RuntimeError("docker exec ... failed (1)")
    assert CLI.main(["create", "--state-dir", str(tmp_path / "s"), "--api-port", str(PORT)], creator=fails_before_locator) == 1
    out = last_json(capsys)
    assert (out["outcome"], out["locatorKept"], out["locator"]) == ("create_failed", False, None)
    assert "hekate_coord_*" in out["note"] and "WITHOUT a locator" in out["note"]
