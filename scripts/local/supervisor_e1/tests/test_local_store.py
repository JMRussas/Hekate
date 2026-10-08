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
    setup, client, aj = store.session(actor="operator:test")
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
        setup2, client2, aj2 = again.session(actor="operator:test")
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
    s = OP.OperatorSurface(fake, OP.OperatorPolicy("operator:alice", project), tmp_path / "acts.jsonl")
    assert not hasattr(s, "events") and not hasattr(s, "_send")                    # no general passthrough
    with pytest.raises(OP.OperatorRefused) as e:
        s.create_plan("r", str(uuid.uuid4()), "x", "k")
    assert e.value.code == "project_outside_policy"
    with pytest.raises(OP.OperatorRefused) as e:
        s.decide("n1", {"decision": "accepted"})                                   # not seen in a policy plan view
    assert e.value.code == "node_outside_policy"
    s.plan("r")
    s.decide("n1", {"decision": "accepted", "actor": "someone-else"})
    assert fake.calls[-1][1][1]["actor"] == "operator:alice"                       # the configured label, always
    with pytest.raises(OP.OperatorRefused) as e:
        s.transition("n1", {"to": "done"})
    assert e.value.code == "transition_not_an_operator_act"
    s.transition("n1", {"to": "cancelled"})
    other = FakeSetup(str(uuid.uuid4()))
    with pytest.raises(OP.OperatorRefused) as e:
        OP.OperatorSurface(other, OP.OperatorPolicy("operator:alice", project), tmp_path / "x.jsonl").plan("r")
    assert e.value.code == "plan_outside_policy"
    log = [json.loads(x) for x in (tmp_path / "acts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(x["act"], x["actor"]) for x in log] == [("decide", "operator:alice"), ("transition", "operator:alice")]
