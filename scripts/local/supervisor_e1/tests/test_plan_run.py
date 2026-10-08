"""plan-run v0 (plan 042; root msg 1793) OFFLINE: plan-import.v0 into the disposable harness PlanStore, and the
driver over two dependent nodes with the FAKE CLI, fake node tools and temporary repos. No model, no live DB."""

import copy
import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from e1 import pilot as P
from e1 import plan_import as PI
from e1 import plan_run as PR
from e1 import task_runner as TR
from e1.acts import E2C_BOUNDS
from e1.acts_durable import ActsJournal, install_acts
from e1.durable import install
from e1.handoff_durable import install_handoff
from e2b_support import reset_schema
from helpers import key
from task_support import FAKE_NODE, commit, git, make_repo, make_spec, pinned_node, sha, sha_file, write_spec

FAKE = Path(__file__).resolve().parent / "fake_cli.py"
PY = Path(P.__file__).resolve().parents[1]
PYEXE = Path(__import__("sys").executable).resolve()
OTHER_TEST = "tests/unit/other.test.ts"
OTHER_CASES = '{"cases": [{"name": "other is y", "expect": "y", "source": "src/other.txt"}, ' \
              '{"name": "other loads", "expect": "*", "source": "src/other.txt"}]}\n'


@pytest.fixture(autouse=True)
def npm_cache(tmp_path, monkeypatch):
    cache = tmp_path / "npm-cache"
    (cache / "_cacache").mkdir(parents=True)
    monkeypatch.setenv("npm_config_cache", str(cache))


@pytest.fixture
def fx(tmp_path, tmp_path_factory):
    f = make_repo(tmp_path)
    node = pinned_node(tmp_path_factory.getbasetemp())
    a_doc = make_spec(f, node)
    a_spec = write_spec(a_doc, tmp_path / "spec-a.json")
    return SimpleNamespace(f=f, node=node, a_doc=a_doc, a_spec=a_spec, a_path=(tmp_path / "spec-a.json").as_posix(), tmp=tmp_path)


def doc_bytes(nodes, title="fixture roadmap slice") -> bytes:
    return json.dumps({"version": PI.VERSION, "title": title, "nodes": nodes}).encode("utf-8")


def two_nodes(fx, b_spec=None) -> bytes:
    return doc_bytes([{"key": "a", "name": "set value", "spec": {"path": fx.a_path, "sha256": fx.a_spec.sha256}, "after": []},
                      {"key": "b", "name": "set other", "spec": b_spec, "after": ["a"]}])


def b_spec_doc(fx, anchor: str, base: str) -> dict:
    """The successor: src/other.txt -> y, oracle tests/unit/other.test.ts, on an operator-prepared base."""
    d = copy.deepcopy(fx.a_doc)
    node = fx.node.as_posix()
    argv = [node, "node_modules/vitest/vitest.mjs", "run", "--reporter=json", OTHER_TEST]
    d["source"].update(anchorCommit=anchor, taskBaseCommit=base)
    d["task"] = {"text": "Set src/other.txt to y.", "criteria": "The other oracle passes."}
    d["allow"] = [{"path": "src/other.txt", "status": "M", "mode": "100644"}]
    d["oracle"]["files"] = [{"path": OTHER_TEST, "sha256": sha(OTHER_CASES.encode("utf-8"))}]
    d["oracle"]["baseline"].update(argv=argv, cases=[
        {"file": OTHER_TEST, "fullName": "other is y", "status": "failed", "failureFirstLine": "AssertionError: expected 'x' to be 'y'"},
        {"file": OTHER_TEST, "fullName": "other loads", "status": "passed", "failureFirstLine": None}])
    d["verify"]["steps"][1]["argv"] = argv
    d["worker"]["testCommand"] = " ".join(argv)
    return d


def integrate_and_prepare_b(fx, a_ref: str, a_clone: Path, *, on: str | None = None) -> tuple[str, str]:
    """OPERATOR step (D3 v0): bring A's accepted artifact into the source repo, then commit B's oracle on top.
    `on` overrides the parent (a base that is NOT chained, for the negative case)."""
    src = Path(fx.f["repo"])
    git(src, "fetch", "-q", str(a_clone), f"{a_ref}:{a_ref}")
    art = git(src, "rev-parse", a_ref)
    git(src, "checkout", "-q", "--detach", on or art)
    base = commit(src, {OTHER_TEST: OTHER_CASES}, "oracle for b")
    git(src, "branch", "-f", "task/b-base", base)              # a task base lives on a branch (clones copy branches)
    git(src, "checkout", "-q", "main")
    return (on or art), base


@pytest.fixture
def driver(harness, setup, client, tmp_path):
    opened = []

    def new_aj():
        reset_schema(harness.dsn)
        install(harness.dsn, E2C_BOUNDS)
        install_acts(harness.dsn)
        install_handoff(harness.dsn)
        aj = ActsJournal(harness.dsn, f"plan-run#{key()[:8]}", now=1000.0).open()
        opened.append(aj)
        return aj

    def go(plan, *, scenarios=None, run_root=None):
        scenarios = scenarios or {"a": "value_ok", "b": "other_ok"}
        return PR.run_plan(plan, run_root or tmp_path / "plan-run", setup=setup, client=client, aj=new_aj(),
                           executable=(PYEXE, FAKE), executable_sha256=sha_file(FAKE), execution_kind="fake-cli",
                           root_go="test-only", timeouts=(120, 60, 60),
                           task_suffix=lambda k: f"\nFAKE-SCENARIO: {scenarios[k]}\n")
    yield go
    for aj in opened:
        aj.close()


def view(setup, plan):
    return setup.plan(plan.root).body


# ------------------------------------------------------------------------ import: validation before any write

class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*a, **kw):
            self.calls.append(name)
            raise AssertionError(f"no write may happen: {name}")
        return call


@pytest.mark.parametrize("mutate, code", [
    (lambda d: d.update(version="plan-import.v1"), "import_shape"),
    (lambda d: d.update(extra=1), "import_shape"),
    (lambda d: d.update(title=""), "import_value"),
    (lambda d: d["nodes"][0].update(key="A"), "import_value"),
    (lambda d: d["nodes"][1].update(key="a"), "import_value"),
    (lambda d: d["nodes"][1].update(after=["zz"]), "import_graph"),
    (lambda d: d["nodes"][0].update(after=["a"]), "import_graph"),
    (lambda d: d["nodes"][0].update(after=["b"]), "import_graph"),                      # a <- b <- a: a cycle
    (lambda d: d["nodes"][0].update(extra=1), "import_shape"),
    (lambda d: d["nodes"][0]["spec"].update(sha256="0" * 64), "spec_sha_mismatch"),
    (lambda d: d["nodes"][0]["spec"].update(path="relative/spec.json"), "import_value"),
    (lambda d: d["nodes"][0]["spec"].update(path="C:/no/such/spec.json"), "spec_invalid"),
    (lambda d: d["nodes"].extend({"key": f"n{i}", "name": "x", "spec": None, "after": []} for i in range(7)), "import_value"),
])
def test_every_input_is_validated_before_any_write(fx, mutate, code):
    d = json.loads(two_nodes(fx))
    mutate(d)
    rec = Recorder()
    with pytest.raises(PI.ImportRefused) as e:
        PI.import_plan(rec, str(uuid.uuid4()), json.dumps(d).encode("utf-8"))
    assert e.value.code == code and rec.calls == []


def test_an_invalid_spec_file_is_refused_before_any_write(fx):
    bad = fx.tmp / "bad-spec.json"
    bad.write_text("{}", encoding="utf-8")
    d = json.loads(two_nodes(fx))
    d["nodes"][0]["spec"] = {"path": bad.as_posix(), "sha256": sha_file(bad)}
    rec = Recorder()
    with pytest.raises(PI.ImportRefused) as e:
        PI.import_plan(rec, str(uuid.uuid4()), json.dumps(d).encode("utf-8"))
    assert e.value.code == "spec_invalid" and rec.calls == []


# ------------------------------------------------------------------------ import: identities, re-import, partial, conflict

def test_import_creates_the_chain_deterministically_and_a_reimport_is_a_noop(fx, harness, setup):
    raw = doc_bytes([{"key": "a", "name": "A", "spec": {"path": fx.a_path, "sha256": fx.a_spec.sha256}, "after": []},
                     {"key": "b", "name": "B", "spec": None, "after": ["a"]},
                     {"key": "c", "name": "C", "spec": None, "after": ["b"]}])
    plan = PI.import_plan(setup, harness.project_id, raw)
    assert plan.applied == ("root", "node:a", "node:b", "node:c", "edge:b<-a", "edge:c<-b")
    assert plan.root == PI.identities(PI.parse(raw), harness.project_id).root
    v = view(setup, plan)
    ready = {plan.key_of(x["nodeId"]): x["ready"] for x in v["readiness"]["leaves"]}
    assert ready == {"a": True, "b": False, "c": False}
    by_id = {n["id"]: n for n in v["nodes"]}
    assert by_id[plan.node_ids["a"]]["value"] == PI.spec_value(fx.a_path, fx.a_spec.sha256)
    assert by_id[plan.node_ids["b"]]["value"] == PI.PENDING
    again = PI.import_plan(setup, harness.project_id, raw)
    assert again.applied == () and again.node_ids == plan.node_ids
    assert view(setup, plan) == v                                              # nothing changed


def test_a_partial_import_completes_on_resume_without_duplicates(fx, harness, setup):
    raw = two_nodes(fx)

    class Crash:
        def __init__(self, inner):
            self.inner, self.children = inner, 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def add_child(self, *a, **kw):
            self.children += 1
            if self.children == 2:
                raise ConnectionError("supervisor died mid-import")
            return self.inner.add_child(*a, **kw)
    with pytest.raises(ConnectionError):
        PI.import_plan(Crash(setup), harness.project_id, raw)
    plan = PI.import_plan(setup, harness.project_id, raw)
    assert plan.applied == ("node:b", "edge:b<-a")
    v = view(setup, plan)
    assert len(v["nodes"]) == 3 and len(v["dependencies"]) == 1


def test_a_conflicting_existing_plan_is_refused_without_writing(fx, harness, setup):
    raw = two_nodes(fx)
    plan = PI.import_plan(setup, harness.project_id, raw)
    other = write_spec(fx.a_doc, fx.tmp / "spec-a-copy.json")
    PI.pin_spec(setup, plan, "b", (fx.tmp / "spec-a-copy.json").as_posix())    # b's content now differs from the document
    before = view(setup, plan)
    with pytest.raises(PI.ImportRefused) as e:
        PI.import_plan(setup, harness.project_id, raw)
    assert (e.value.code, e.value.detail) == ("import_conflict", {"node": "b"}) and view(setup, plan) == before
    assert other.sha256 == fx.a_spec.sha256


# ------------------------------------------------------------------------ the driver

def test_two_dependent_nodes_end_to_end_with_an_operator_prepared_base(fx, harness, setup, driver, tmp_path):
    plan = PI.import_plan(setup, harness.project_id, two_nodes(fx))
    r1 = driver(plan)
    assert (r1.outcome, r1.reason, r1.detail) == ("needs_operator", "spec_pending", {"node": "b"})
    assert [(s.key, s.action, s.outcome) for s in r1.steps] == [("a", "ran", "accepted"), ("b", "stopped", None)]
    assert r1.nodes["a"]["acceptance"] == "accepted" and r1.nodes["b"]["work"] == "todo"
    a_art = r1.nodes["a"]["artifactRef"]
    a_ev = json.loads(Path(r1.steps[0].evidence).read_text(encoding="utf-8"))
    a_ref = next(r.split()[1] for r in a_ev["ownedRefs"] if r.startswith(a_art))
    anchor, base = integrate_and_prepare_b(fx, a_ref, tmp_path / "plan-run" / "a" / "repo")
    b_path = (fx.tmp / "spec-b.json")
    write_spec(b_spec_doc(fx, anchor, base), b_path)
    PI.pin_spec(setup, plan, "b", b_path.as_posix())
    r2 = driver(plan)
    assert (r2.outcome, r2.reason) == ("all_done", None)
    assert [(s.key, s.action, s.outcome) for s in r2.steps] == [("b", "ran", "accepted")]
    assert {k: (s["work"], s["acceptance"]) for k, s in r2.nodes.items()} == {"a": ("done", "accepted"), "b": ("done", "accepted")}
    logs = sorted((tmp_path / "plan-run").glob("plan-run-*.json"))
    assert [json.loads(p.read_text(encoding="utf-8"))["outcome"] for p in logs] == ["needs_operator", "all_done"]
    r3 = driver(plan)                                                           # nothing left: done, nothing claimed
    assert (r3.outcome, r3.steps) == ("all_done", [])


def test_a_successor_base_not_chained_to_the_accepted_artifact_stops_before_any_clone(fx, harness, setup, driver, tmp_path):
    plan = PI.import_plan(setup, harness.project_id, two_nodes(fx))
    r1 = driver(plan)
    a_art = r1.nodes["a"]["artifactRef"]
    a_ev = json.loads(Path(r1.steps[0].evidence).read_text(encoding="utf-8"))
    a_ref = next(r.split()[1] for r in a_ev["ownedRefs"] if r.startswith(a_art))
    anchor, base = integrate_and_prepare_b(fx, a_ref, tmp_path / "plan-run" / "a" / "repo", on=fx.f["base"])
    write_spec(b_spec_doc(fx, anchor, base), fx.tmp / "spec-b.json")
    PI.pin_spec(setup, plan, "b", (fx.tmp / "spec-b.json").as_posix())
    r2 = driver(plan)
    assert (r2.outcome, r2.reason) == ("needs_operator", "base_not_chained")
    assert r2.detail == {"node": "b", "predecessor": "a", "artifactRef": a_art, "taskBaseCommit": base}
    assert not (tmp_path / "plan-run" / "b").exists() and r2.nodes["b"]["work"] == "todo"


def test_a_pinned_spec_file_that_changed_stops_before_preflight(fx, harness, setup, driver, tmp_path):
    raw = doc_bytes([{"key": "a", "name": "A", "spec": {"path": fx.a_path, "sha256": fx.a_spec.sha256}, "after": []}])
    plan = PI.import_plan(setup, harness.project_id, raw)
    Path(fx.a_path).write_bytes(Path(fx.a_path).read_bytes() + b"\n")
    r = driver(plan)
    assert (r.outcome, r.reason) == ("needs_operator", "spec_mismatch") and r.detail["code"] == "spec_sha_mismatch"
    assert not (tmp_path / "plan-run" / "a").exists() and r.nodes["a"]["work"] == "todo"


def test_a_node_in_flight_stops_the_rerun_without_a_new_claim(fx, harness, setup, client, driver, tmp_path):
    plan = PI.import_plan(setup, harness.project_id, two_nodes(fx))
    c = client.claim(plan.root, f"elsewhere-{key()[:8]}", "other-attempt", None, "someone-else")
    assert c.body["receipt"]["nodeId"] == plan.node_ids["a"]
    r = driver(plan)
    assert (r.outcome, r.reason) == ("needs_operator", "inflight_ownership")
    assert r.detail == {"a": {"work": "in_progress", "acceptance": "none"}} and r.steps == []
    assert r.nodes["a"]["attemptEpoch"] == 1 and not (tmp_path / "plan-run" / "a").exists()


def test_a_rejected_node_stops_and_its_successor_stays_blocked(fx, harness, setup, driver):
    plan = PI.import_plan(setup, harness.project_id, two_nodes(fx))
    r1 = driver(plan, scenarios={"a": "value_bad", "b": "other_ok"})
    assert (r1.outcome, r1.reason) == ("needs_operator", "node_not_accepted") and r1.detail["reason"] == "max_rounds"
    assert r1.nodes["b"]["work"] == "todo" and r1.nodes["b"]["ready"] is False
    r2 = driver(plan, run_root=None)
    assert (r2.outcome, r2.reason, r2.detail) == ("needs_operator", "node_rejected", ["a"]) and r2.steps == []


def test_an_existing_node_run_root_is_an_earlier_attempt_and_stops(fx, harness, setup, driver, tmp_path):
    plan = PI.import_plan(setup, harness.project_id, two_nodes(fx))
    (tmp_path / "plan-run" / "a").mkdir(parents=True)
    r = driver(plan)
    assert (r.outcome, r.reason) == ("needs_operator", "node_run_root_exists") and r.nodes["a"]["work"] == "todo"


# ------------------------------------------------------------------------ pure rules

def st(**over):
    base = {"nodeId": "n", "work": "todo", "acceptance": "none", "ready": False, "blockers": [], "value": PI.PENDING,
            "contentRevision": 1, "siblingOrder": 0, "artifactRef": None, "attemptEpoch": 0}
    return dict(base, **over)


def test_no_ready_work_is_never_done_and_names_the_blockers():
    state = {"a": st(work="done", acceptance="accepted"),
             "b": st(siblingOrder=1, blockers=[{"reason": "predecessor_not_completed", "predecessor": "x"}])}
    assert PR.classify(state) == ("stop", ("no_ready_work", {"b": [{"reason": "predecessor_not_completed", "predecessor": "x"}]}))
    assert PR.classify({"a": st(work="done", acceptance="accepted")}) == ("done", None)
    assert PR.classify({"a": st(work="done", acceptance="none")})[1][0] == "inflight_ownership"         # awaiting review
    assert PR.classify({"a": st(work="done", acceptance="stale")})[1][0] == "inflight_ownership"
    assert PR.classify({"a": st(work="cancelled")})[1] == ("node_cancelled", ["a"])
    assert PR.classify({"b": st(siblingOrder=1, ready=True), "a": st(ready=True)}) == ("next", "a")       # PlanStore's order


def test_the_claim_check_pins_node_content_and_predecessor_artifacts():
    check = PR.make_claim_check("n", "v", 3, {"p": "art"})
    good = {"nodeId": "n", "contentRevision": 3, "contentSnapshot": {"value": "v"},
            "prereqSnapshot": {"nodes": [{"id": "p", "artifactRef": "art", "acceptance": {"decision": "accepted"}}]}}
    assert check(good) is None
    for mutate, why in [(lambda r: r.update(nodeId="m"), "node"), (lambda r: r.update(contentRevision=4), "content_pin"),
                        (lambda r: r["contentSnapshot"].update(value="w"), "content_pin"),
                        (lambda r: r["prereqSnapshot"]["nodes"][0].update(artifactRef="other"), "prerequisite_pin"),
                        (lambda r: r["prereqSnapshot"]["nodes"][0].update(acceptance={"decision": "rejected"}), "prerequisite_pin"),
                        (lambda r: r["prereqSnapshot"].update(nodes=[]), "prerequisite_pin")]:
        r = copy.deepcopy(good)
        mutate(r)
        assert check(r)["why"] == why


def test_an_attached_pilot_never_dispatches_another_node(fx, harness, setup, client):
    """Two independent ready nodes; attached to the SECOND, the claim returns the first: claim_mismatch, no dispatch."""
    raw = doc_bytes([{"key": "a", "name": "A", "spec": None, "after": []}, {"key": "b", "name": "B", "spec": None, "after": []}])
    plan = PI.import_plan(setup, harness.project_id, raw)
    reset_schema(harness.dsn)
    install(harness.dsn, E2C_BOUNDS)
    install_acts(harness.dsn)
    install_handoff(harness.dsn)
    aj = ActsJournal(harness.dsn, f"attach#{key()[:8]}", now=1000.0).open()
    calls = []
    try:
        cfg = P.PilotConfig(repo=Path(fx.f["repo"]), base=fx.f["base"], workspace=fx.tmp, project_id=harness.project_id,
                            task_text="t", criteria="c")
        pilot = P.Pilot(P.resolve(cfg), setup, client, aj, worker=lambda o: calls.append(o), reviewer=lambda o: None,
                        attach=(plan.root, plan.node_ids["b"]))
        res = pilot.run()
    finally:
        aj.close()
    assert (res.outcome, res.reason) == ("needs_operator", "claim_mismatch")
    assert res.detail == {"outcome": "claimed", "nodeId": plan.node_ids["a"]} and calls == []
