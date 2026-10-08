"""P2 minimal persistent LOCAL mode (plan 043 rev 3; root msg 1878). Every coordinator database here is created and
dropped BY THE TEST (disposable); no existing database is touched and no dedicated database is activated."""

import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from e1 import local_store as LS
from e1 import operator_acts as OP
from e1 import plan_import as PI
from e1 import plan_run as PR
from task_support import sha_file
from test_plan_run import FAKE, PYEXE, doc_bytes, fx, npm_cache  # noqa: F401

PORT = 5119


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    s = LS.LocalStore.create(tmp_path_factory.mktemp("coord") / "state", api_port=PORT)
    yield s
    s.drop_for_test()


# ------------------------------------------------------------------------ create / open / never adopt

def test_create_makes_a_new_marked_database_and_verifies_it(store):
    loc = json.loads((store.state_dir / LS.LOCATOR).read_text(encoding="utf-8"))
    assert LS.COORD_RE.match(loc["db"]) and loc["db"] == store.loc.db and loc["apiPort"] == PORT
    assert store.psql("SELECT purpose FROM hekate_local_coordinator") == LS.PURPOSE
    store.verify()                                                         # complete before any dispatch


def test_create_never_adopts_or_recreates_in_place(store):
    with pytest.raises(LS.LocalStoreRefused) as e:
        LS.LocalStore.create(store.state_dir, api_port=PORT + 1)
    assert e.value.code == "locator_exists"


@pytest.mark.parametrize("mutate, code", [
    (lambda d: d.update(marker="0" * 64), "marker_mismatch"),             # a database the locator does not own
    (lambda d: d.update(db="hekate_coord_20000101000000_deadbeef"), "db_missing"),
    (lambda d: d.update(db="code_storage"), "locator_invalid"),           # never a non-coordinator database name
])
def test_open_refuses_any_database_the_locator_and_marker_do_not_name(store, tmp_path, mutate, code):
    d = json.loads((store.state_dir / LS.LOCATOR).read_text(encoding="utf-8"))
    mutate(d)
    (tmp_path / LS.LOCATOR).write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(LS.LocalStoreRefused) as e:
        LS.LocalStore.open(tmp_path)
    assert e.value.code == code


def test_open_refuses_an_unreadable_locator(tmp_path):
    (tmp_path / LS.LOCATOR).write_text("{not json", encoding="utf-8")
    with pytest.raises(LS.LocalStoreRefused) as e:
        LS.LocalStore.open(tmp_path)
    assert e.value.code == "locator_unreadable"


def test_verify_refuses_a_partial_schema_or_mismatched_bounds(store):
    store.psql("ALTER TABLE supervisor_journal.takeovers RENAME TO takeovers_hidden")
    try:
        with pytest.raises(LS.LocalStoreRefused) as e:
            store.verify()
        assert e.value.code == "schema_incomplete" and "supervisor_journal.takeovers" in e.value.detail
    finally:
        store.psql("ALTER TABLE supervisor_journal.takeovers_hidden RENAME TO takeovers")
    store.psql("ALTER TABLE supervisor_journal.global_usage DISABLE TRIGGER USER; "
               "UPDATE supervisor_journal.global_usage SET per_stream = per_stream + 1 WHERE id = 1")
    try:
        with pytest.raises(LS.LocalStoreRefused) as e:
            store.verify()
        assert e.value.code == "bounds_mismatch"
    finally:
        store.psql("UPDATE supervisor_journal.global_usage SET per_stream = per_stream - 1 WHERE id = 1; "
                   "ALTER TABLE supervisor_journal.global_usage ENABLE TRIGGER USER")
    store.verify()


# ------------------------------------------------------------------------ persistence across a process restart

def test_a_plan_run_persists_across_stop_and_reopen_with_a_real_clock(store, fx):
    raw = doc_bytes([{"key": "a", "name": "set value", "spec": {"path": fx.a_path, "sha256": fx.a_spec.sha256}, "after": []}],
                    title=f"local {uuid.uuid4().hex[:6]}")
    setup, client, aj = store.session(actor="operator:test", plan_bytes=raw)
    t0 = time.time()
    try:
        plan = PI.import_plan(setup, store.loc.project_id, raw)
        r1 = PR.run_plan(plan, fx.tmp / "rr1", setup=setup, client=client, aj=aj, executable=(PYEXE, FAKE),
                         executable_sha256=sha_file(FAKE), execution_kind="fake-cli", root_go="test-only", timeouts=(120, 60, 60),
                         task_suffix=lambda k: "\nFAKE-SCENARIO: value_ok\n")
    finally:
        aj.close()
    assert (r1.outcome, [(s.key, s.outcome) for s in r1.steps]) == ("all_done", [("a", "accepted")])
    at = [float(x) for x in store.psql("SELECT at_json FROM supervisor_journal.records").split()]
    assert at and all(t0 - 5 <= t <= time.time() + 5 for t in at)                    # real UTC, not the fixture counter
    acts = [json.loads(x)["act"] for x in (store.state_dir / "operator-acts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "create_plan" in acts and "decide" in acts
    store.stop()                                                                 # the Api process ends; the database stays
    again = LS.LocalStore.open(store.state_dir)                                   # a NEW Api process on the SAME database
    try:
        setup2, client2, aj2 = again.session(actor="operator:test", plan_bytes=raw)
        try:
            plan2 = PI.import_plan(setup2, again.loc.project_id, raw)             # deterministic ids: a no-op
            assert plan2.applied == () and plan2.root == plan.root
            r2 = PR.run_plan(plan2, fx.tmp / "rr2", setup=setup2, client=client2, aj=aj2, executable=(PYEXE, FAKE),
                             executable_sha256=sha_file(FAKE), execution_kind="fake-cli", root_go="test-only")
        finally:
            aj2.close()
        assert (r2.outcome, r2.steps) == ("all_done", [])                         # the accepted state persisted
    finally:
        again.stop()
    store.api = None
    store._start_api()                                                           # leave the module store running for later tests


# ------------------------------------------------------------------------ the clock rule (pure)

def test_the_clock_rule_compares_only_recorded_utc():
    LS.clock_check(None, 100.0)
    LS.clock_check(100.0, 100.0)
    LS.clock_check(100.0, 100.0 + LS.MAX_GAP_S)
    with pytest.raises(LS.LocalStoreRefused) as e:
        LS.clock_check(100.0, 99.0)
    assert e.value.code == "clock_backwards"
    with pytest.raises(LS.LocalStoreRefused) as e:
        LS.clock_check(100.0, 101.0 + LS.MAX_GAP_S)
    assert e.value.code == "clock_gap_implausible"


def test_the_live_journal_clock_is_sampled_stable_and_never_backwards():
    j = LS.LiveClockJournal.__new__(LS.LiveClockJournal)
    j.now = 1000.0                                                               # the fixture value is ignored: a real sample
    first = j.now
    assert abs(first - time.time()) < 5
    time.sleep(0.01)
    assert j.now == first                                                        # stable between ticks (size and `at` agree)
    j.now = first + 10                                                           # a tick: re-sample the wall clock
    assert first <= j.now <= time.time()
    j._live = time.time() + 3600                                                 # a wall clock that went backwards in-process
    later = j.now
    j.now = 0
    assert j.now == later                                                        # never backwards within the process


# ------------------------------------------------------------------------ the operator surface (pure)

ROOT = str(uuid.uuid4())


class FakeSetup:
    def __init__(self, project):
        self.project, self.calls = project, []

    def _resp(self, body=None):
        return SimpleNamespace(status=200, code=None, body=body or {})

    def plan(self, root):
        self.calls.append(("plan", root))
        return self._resp({"projectId": self.project, "nodes": [{"id": root}, {"id": "n1"}, {"id": "n2"}]})

    def __getattr__(self, name):
        def call(*a, **kw):
            self.calls.append((name, a, kw))
            return self._resp()
        return call


def test_operator_acts_are_named_policy_bound_and_logged(tmp_path):
    project = str(uuid.uuid4())
    fake = FakeSetup(project)
    s = OP.OperatorSurface(fake, OP.OperatorPolicy("operator:alice", project, frozenset({ROOT})), tmp_path / "acts.jsonl")
    assert not hasattr(s, "events") and not hasattr(s, "_send")                    # no general passthrough
    with pytest.raises(OP.OperatorRefused) as e:
        s.create_plan(ROOT, str(uuid.uuid4()), "x", "k")
    assert e.value.code == "project_outside_policy"
    with pytest.raises(OP.OperatorRefused) as e:
        s.decide("n1", {"decision": "accepted"})                                   # not seen in a policy plan view
    assert e.value.code == "node_outside_policy"
    s.plan(ROOT)
    s.decide("n1", {"decision": "accepted", "actor": "someone-else"})
    assert fake.calls[-1][1][1]["actor"] == "operator:alice"                       # the configured label, always
    with pytest.raises(OP.OperatorRefused) as e:
        s.transition("n1", {"to": "done"})
    assert e.value.code == "transition_not_an_operator_act"
    s.transition("n1", {"to": "cancelled"})
    other = FakeSetup(str(uuid.uuid4()))
    with pytest.raises(OP.OperatorRefused) as e:
        OP.OperatorSurface(other, OP.OperatorPolicy("operator:alice", project, frozenset({ROOT})), tmp_path / "x.jsonl").plan(ROOT)
    assert e.value.code == "plan_outside_policy"
    log = [json.loads(x) for x in (tmp_path / "acts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(x["act"], x["phase"], x["actor"]) for x in log] == [
        ("decide", "intent", "operator:alice"), ("decide", "outcome", "operator:alice"),
        ("transition", "intent", "operator:alice"), ("transition", "outcome", "operator:alice")]
    assert s.uncertain_acts() == []


def test_a_crash_between_intent_and_outcome_is_an_uncertain_act(tmp_path):
    """Review 1894 D7: the intent is written BEFORE the call; a call that never returns leaves it uncertain."""
    project = str(uuid.uuid4())
    fake = FakeSetup(project)
    s = OP.OperatorSurface(fake, OP.OperatorPolicy("operator:alice", project, frozenset({ROOT})), tmp_path / "acts.jsonl")
    s.plan(ROOT)

    def boom(*a, **kw):
        raise ConnectionError("coordinator died mid-call")
    fake.decide = boom
    with pytest.raises(ConnectionError):
        s.decide("n1", {"decision": "accepted"})
    left = s.uncertain_acts()
    assert [(x["act"], x["target"], x["phase"]) for x in left] == [("decide", "n1", "intent")]


def test_a_second_coordinator_on_the_same_database_is_refused(store):
    """Review 1894 D3: one coordinator per database, by an advisory lock taken BEFORE its Api starts."""
    if getattr(store, "_lock_conn", None) is None:
        store._acquire_lock()                                                   # the module store holds the lock
    with pytest.raises(LS.LocalStoreRefused) as e:
        LS.LocalStore.open(store.state_dir)
    assert e.value.code == "store_in_use"


def test_a_busy_api_port_is_refused_and_the_listener_is_left_alone(store, tmp_path):
    """Review 1894 S2: a port in use refuses the start; the other listener is never killed or adopted."""
    import socket
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(16)                                                              # room for the start probe and our check
    port = busy.getsockname()[1]
    try:
        d = json.loads((store.state_dir / LS.LOCATOR).read_text(encoding="utf-8"))
        s = LS.LocalStore(tmp_path, LS.Locator(d["db"], d["marker"], d["projectId"], port))
        s._container()
        with pytest.raises(LS.LocalStoreRefused) as e:
            s._start_api()
        assert e.value.code == "api_port_in_use"
        probe = socket.create_connection(("127.0.0.1", port), timeout=2)          # still listening
        probe.close()
    finally:
        busy.close()


# ------------------------------------------------------------------------ the CLI's --store local

def _plan_file(fx, title):
    a = json.loads(json.dumps(fx.a_doc))
    a["task"]["text"] += "\nFAKE-SCENARIO: value_ok\n"                          # the CLI has no per-node suffix
    from task_support import write_spec
    spec = write_spec(a, fx.tmp / f"spec-{title}.json")
    p = fx.tmp / f"plan-{title}.json"
    p.write_bytes(doc_bytes([{"key": "a", "name": "set value", "spec": {"path": (fx.tmp / f"spec-{title}.json").as_posix(),
                                                                         "sha256": spec.sha256}, "after": []}], title=f"cli local {title}"))
    return p


def _cli(plan, run_root, *extra):
    from e1 import plan_cli as CLI
    return CLI.main(["run", "--plan", str(plan), "--run-root", str(run_root), "--exe", str(PYEXE), "--exe-arg", str(FAKE),
                     "--exe-sha256", sha_file(FAKE), "--launch-real-model", "--root-go", "test-only", *extra])


def _last_json(capsys):
    out = capsys.readouterr().out
    return json.loads(out[out.rindex("\n{") + 1:] if "\n{" in out else out)


def test_cli_store_local_runs_and_a_rerun_finds_the_persisted_state(store, fx, capsys):
    plan = _plan_file(fx, uuid.uuid4().hex[:6])
    store.stop()                                                                 # the CLI opens its own Api process
    try:
        assert _cli(plan, fx.tmp / "cli-rr1", "--store", "local", "--state-dir", str(store.state_dir)) == 0
        out = _last_json(capsys)
        assert (out["outcome"], [(s["key"], s["outcome"]) for s in out["steps"]]) == ("all_done", [("a", "accepted")])
        assert out["store"] == {"kind": "local", "db": store.loc.db, "projectId": store.loc.project_id}
        assert _cli(plan, fx.tmp / "cli-rr2", "--store", "local", "--state-dir", str(store.state_dir)) == 0
        out2 = _last_json(capsys)
        assert (out2["outcome"], out2["steps"]) == ("all_done", [])               # persisted: nothing re-run
    finally:
        store.api = None
        store._start_api()


def test_cli_store_local_refusals_happen_before_any_effect(fx, tmp_path, capsys):
    plan = _plan_file(fx, "refusal")
    assert _cli(plan, tmp_path / "rr-a", "--store", "local") == 2
    assert _last_json(capsys)["refused"] == "state_dir_required" and not (tmp_path / "rr-a").exists()
    assert _cli(plan, tmp_path / "rr-b", "--store", "local", "--state-dir", str(tmp_path / "no-coordinator")) == 2
    assert _last_json(capsys)["refused"] == "locator_unreadable" and not (tmp_path / "rr-b").exists()


def test_in_flight_work_survives_a_restart_and_stops_without_a_duplicate_claim(store, fx):
    """Root msg 1892: a node claimed (in flight) before a restart is still in flight after it; the re-run classifies
    it and stops, with NO new claim (the attempt epoch is unchanged) and no node run root."""
    raw = doc_bytes([{"key": "a", "name": "set value", "spec": {"path": fx.a_path, "sha256": fx.a_spec.sha256}, "after": []}],
                    title=f"local inflight {uuid.uuid4().hex[:6]}")
    setup, client, aj = store.session(actor="operator:test", plan_bytes=raw)
    aj.close()
    plan = PI.import_plan(setup, store.loc.project_id, raw)
    c = client.claim(plan.root, f"elsewhere-{uuid.uuid4().hex[:8]}", "other-attempt", None, "someone-else")
    assert c.body["receipt"]["nodeId"] == plan.node_ids["a"]
    store.stop()
    again = LS.LocalStore.open(store.state_dir)                                   # restart: a new Api process, same database
    try:
        setup2, client2, aj2 = again.session(actor="operator:test", plan_bytes=raw)
        try:
            r = PR.run_plan(plan, fx.tmp / "rr-inflight", setup=setup2, client=client2, aj=aj2, executable=(PYEXE, FAKE),
                            executable_sha256=sha_file(FAKE), execution_kind="fake-cli", root_go="test-only")
        finally:
            aj2.close()
        assert (r.outcome, r.reason, r.steps) == ("needs_operator", "inflight", [])
        assert r.nodes["a"]["work"] == "in_progress" and r.nodes["a"]["attemptEpoch"] == 1        # no duplicate claim
        assert not (fx.tmp / "rr-inflight" / "a").exists()
    finally:
        again.stop()
        store.api = None
        store._start_api()


def test_a_root_outside_the_derived_allowlist_is_refused_before_any_call(tmp_path):
    """Root msg 1895 (D6): the allowed roots are derived from the validated plan file; any other root is refused
    LOCALLY, before any HTTP call, even inside the right project."""
    project = str(uuid.uuid4())
    fake = FakeSetup(project)
    s = OP.OperatorSurface(fake, OP.OperatorPolicy("operator:alice", project, frozenset({ROOT})), tmp_path / "acts.jsonl")
    other_root = str(uuid.uuid4())
    for call in (lambda: s.plan(other_root), lambda: s.create_plan(other_root, project, "x", "k")):
        with pytest.raises(OP.OperatorRefused) as e:
            call()
        assert e.value.code == "root_outside_policy"
    assert fake.calls == []                                                       # zero HTTP calls
    with pytest.raises(OP.OperatorRefused) as e:
        OP.OperatorPolicy("operator:alice", project, frozenset())
    assert e.value.code == "policy_roots"


# ------------------------------------------------------------------------ the two-process CLI continuation (root msg 1901)

def test_two_cli_processes_continue_a_bound_plan_across_an_operator_pin(store, fx, capsys):
    """Root 1901's required result: run 1 (a accepted, b pending) stops spec_pending; the operator integrates a's
    artifact and PINS b's spec; run 2 with the SAME plan file in the SAME run root attaches (no re-import), skips the
    accepted a, runs b and ends all_done. An edited plan file is plan_changed."""
    from test_plan_run import b_spec_doc, integrate_and_prepare_b
    from task_support import write_spec
    a = json.loads(json.dumps(fx.a_doc))
    a["task"]["text"] += "\nFAKE-SCENARIO: value_ok\n"
    a_spec = write_spec(a, fx.tmp / "spec-a-cli2.json")
    raw = doc_bytes([{"key": "a", "name": "set value", "spec": {"path": (fx.tmp / "spec-a-cli2.json").as_posix(), "sha256": a_spec.sha256},
                      "after": []}, {"key": "b", "name": "set other", "spec": None, "after": ["a"]}], title=f"two process {uuid.uuid4().hex[:6]}")
    plan_file = fx.tmp / "plan-two-process.json"
    plan_file.write_bytes(raw)
    rr = fx.tmp / "rr-two-process"
    store.stop()
    try:
        assert _cli(plan_file, rr, "--store", "local", "--state-dir", str(store.state_dir)) == 1
        out1 = _last_json(capsys)
        assert (out1["outcome"], out1["reason"], out1["resumable"]) == ("needs_operator", "spec_pending", True)
        bind = json.loads((rr / "plan.binding.json").read_text(encoding="utf-8"))
        assert bind["marker"] == store.loc.marker and (rr / "plan.import.json").read_bytes() == raw
        # OPERATOR: forward a's accepted artifact, freeze b's spec on it, pin it through the operator surface
        ev = json.loads(next((rr / "a").glob("pilot-*/evidence.json")).read_text(encoding="utf-8"))
        anchor, base = integrate_and_prepare_b(fx, ev["ownedRefs"][0].split()[1], rr / "a" / "repo")
        b = b_spec_doc(fx, anchor, base)
        b["task"]["text"] += "\nFAKE-SCENARIO: other_ok\n"
        write_spec(b, fx.tmp / "spec-b-cli2.json")
        op = LS.LocalStore.open(store.state_dir)
        try:
            setup, _client, aj = op.session(actor="operator:test", plan_bytes=raw)
            aj.close()
            PI.pin_spec(setup, PI.attach_plan(setup, op.loc.project_id, raw), "b", (fx.tmp / "spec-b-cli2.json").as_posix())
        finally:
            op.stop()
        edited = fx.tmp / "plan-two-process-edited.json"
        edited.write_bytes(raw + b" ")
        assert _cli(edited, rr, "--store", "local", "--state-dir", str(store.state_dir)) == 2
        assert _last_json(capsys)["refused"] == "plan_changed"
        assert _cli(plan_file, rr, "--store", "local", "--state-dir", str(store.state_dir)) == 0
        out2 = _last_json(capsys)
        assert (out2["outcome"], [(s["key"], s["outcome"]) for s in out2["steps"]]) == ("all_done", [("b", "accepted")])
        assert {k: (v["work"], v["acceptance"]) for k, v in out2["nodes"].items()} == {"a": ("done", "accepted"), "b": ("done", "accepted")}
    finally:
        store.api = None
        store._start_api()


def test_an_existing_unbound_run_root_is_refused_before_any_effect(store, fx, tmp_path, capsys):
    plan = _plan_file(fx, "unbound")
    (tmp_path / "rr-unbound").mkdir()
    assert _cli(plan, tmp_path / "rr-unbound", "--store", "local", "--state-dir", str(store.state_dir)) == 2
    assert _last_json(capsys)["refused"] == "run_root_unbound"


def test_uncertain_or_unparseable_operator_acts_stop_before_any_dispatch(store):
    """Root msg 1904: an intent without an outcome, or a partial line, is surfaced on the next session and stops it."""
    log = store.state_dir / "operator-acts.jsonl"
    before = log.read_bytes() if log.exists() else b""
    raw = doc_bytes([{"key": "a", "name": "x", "spec": None, "after": []}], title="uncertain acts")
    try:
        with open(log, "ab") as f:
            f.write(json.dumps({"phase": "intent", "id": "deadbeef", "act": "decide", "target": "n1"}).encode() + b"\n")
            f.write(b'{"phase": "outc')
        with pytest.raises(LS.LocalStoreRefused) as e:
            store.session(actor="operator:test", plan_bytes=raw)
        assert e.value.code == "uncertain_operator_acts"
        assert [x["phase"] for x in e.value.detail] == ["unparseable", "intent"]
    finally:
        log.write_bytes(before)


# ------------------------------------------------------------------------ review 1907 F1/F3 (root msg 1910)

def test_a_missing_marker_table_is_a_typed_refusal(store):
    """F1: a database the locator names whose marker table is gone is marker_missing, never a raw RuntimeError."""
    store.psql("ALTER TABLE hekate_local_coordinator RENAME TO hekate_local_coordinator_hidden")
    try:
        with pytest.raises(LS.LocalStoreRefused) as e:
            LS.LocalStore.open(store.state_dir)
        assert e.value.code == "marker_missing"
    finally:
        store.psql("ALTER TABLE hekate_local_coordinator_hidden RENAME TO hekate_local_coordinator")


def test_a_failed_create_stops_its_api_and_frees_the_port(tmp_path, monkeypatch):
    """F3: a create that fails after the Api started stops only its own Api and lock; the partially initialized owned
    database and its locator are KEPT for operator inspection (root msg 1910)."""
    port = 5141

    def boom(dsn):
        raise RuntimeError("installer failed")
    monkeypatch.setattr(LS, "install_acts", boom)
    with pytest.raises(RuntimeError):
        LS.LocalStore.create(tmp_path / "state", api_port=port)
    from e1 import harness as HZ
    assert HZ._port_free(port)                                                   # our Api child was stopped
    loc = LS.Locator.read(tmp_path / "state")                                    # the locator is kept
    s = LS.LocalStore(tmp_path / "state", loc)
    s._container()
    try:
        assert s.psql(f"SELECT 1 FROM pg_database WHERE datname = '{loc.db}'", db="postgres") == "1"   # the DB is kept
        assert s.psql("SELECT purpose FROM hekate_local_coordinator") == LS.PURPOSE
    finally:
        s.drop_for_test()                                                        # disposable: this test made it
