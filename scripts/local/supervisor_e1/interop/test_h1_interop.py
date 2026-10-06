"""E1b pure interop: ChatAgent's ACTUAL buildPlanTaskContext (pinned H1) over the real raw claim
fixtures and derived inputs. No HTTP, no container, no provider.
"""

import hashlib
import json
from pathlib import Path

import pytest

from e1.h1_bridge import H1Unavailable, build, verify_checkout
from e1.h1_package import H1ProvenanceError, h1_builder, package_from, semantic_identity
from e1.seam import parse_claim_envelope
from e1.supervisor import FakeWorker, Outcome, Supervisor, echo
from e1.wire import Response, SupervisorClient

FIXTURES = Path(__file__).resolve().parents[4] / "context-store" / "plans" / "fixtures" / "023"
SYSTEM = "SYSTEM-INSTRUCTION-MARKER: you are a careful coding worker."
FAST, DEEP = "ROLE-FAST-MARKER", "ROLE-DEEP-MARKER"
RULES = [{"path": "AGENTS.md", "revision": "rev-1", "text": "Rule one: never commit secrets.\nSecond line."},
         {"path": "docs/style.md", "revision": "rev-2", "text": "Rule two: keep functions small."}]
BUDGET = {"windowTokens": 200_000, "maxHistoryTurns": 0, "safetyTokens": 0, "fastOutputTokens": 1000, "deepOutputTokens": 1000}
AT = "2026-10-06T12:00:00Z"


def raw(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("utf-8")


def derived(name: str, edit) -> str:
    doc = json.loads(raw(name))
    edit(doc)
    return json.dumps(doc)


def opts(response: str, **over):
    o = {"response": response, "rules": [dict(r) for r in RULES], "systemInstruction": SYSTEM,
         "roleInstructions": {"fast": FAST, "deep": DEEP}, "budget": dict(BUDGET), "capturedAtIso": AT}
    o.update(over)
    return o


def ok(r):
    assert r["ok"] is True, r
    return r


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- fixtures, identities, provenance

def test_claimed_fixture_declared_ids_and_digests_are_preserved_verbatim():
    r = ok(build(opts(raw("claim-claimed.json"))))
    fixture = json.loads(raw("claim-claimed.json"))["receipt"]
    src = r["source"]
    for k in ("rootId", "nodeId", "claimKey", "attemptId", "attemptEpoch", "executorRef", "contentRevision",
              "contentDigest", "prereqDigest", "eventSeq", "actor", "createdAt"):
        assert src[k] == fixture[k] and type(src[k]) is type(fixture[k]), k
    assert src["contentDigest"] == src["contentDigest"].upper() and src["prereqDigest"] == src["prereqDigest"].lower()
    assert (src["replayed"], src["stillCurrent"]) == (False, True)
    assert r["_runtime"]["version"] == "v24.21.0" and r["_runtime"]["h1Commit"].startswith("5255daa")


def test_replay_keeps_provenance_facts_while_the_mandatory_text_and_hashes_are_stable():
    a = ok(build(opts(raw("claim-claimed.json"))))
    b = ok(build(opts(raw("claim-replayed.json"))))
    assert b["text"] == a["text"] and b["suppliedSha256"] == a["suppliedSha256"]
    assert (b["source"]["replayed"], a["source"]["replayed"]) == (True, False)       # preserved, not hidden
    strip = lambda s: {k: v for k, v in s.items() if k not in ("replayed", "stillCurrent")}
    assert strip(b["source"]) == strip(a["source"])
    ea = parse_claim_envelope(raw("claim-claimed.json").encode())
    eb = parse_claim_envelope(raw("claim-replayed.json").encode())
    pa = package_from(a, ea, system_instruction=SYSTEM, role_fast=FAST, role_deep=DEEP)
    pb = package_from(b, eb, system_instruction=SYSTEM, role_fast=FAST, role_deep=DEEP)
    assert semantic_identity(pa) == semantic_identity(pb) and (pa.replayed, pb.replayed) == (False, True)


def test_no_ready_work_is_a_typed_refusal():
    r = build(opts(raw("claim-no-ready-work.json")))
    assert r == {"ok": False, "kind": "PlanTaskError", "code": "NO_WORK", "_runtime": r["_runtime"]}


def test_supplied_sha256_is_independently_exact_and_distinct_from_hekate_digests():
    r = ok(build(opts(raw("claim-claimed.json"))))
    assert r["suppliedSha256"] == sha(r["text"])                                     # recomputed here, from the exact text
    s = r["source"]["supplied"]
    assert [x["sha256"] for x in s["rules"]] == [sha(x["text"]) for x in RULES]
    assert [(x["path"], x["revision"]) for x in s["rules"]] == [(x["path"], x["revision"]) for x in RULES]
    hekate = {r["source"]["contentDigest"].lower(), r["source"]["prereqDigest"].lower()}
    for h in (r["suppliedSha256"], s["requirementSha256"], s["prerequisitesSha256"]):
        assert h not in hekate                                                        # never a relabelled Hekate digest


def test_typed_provenance_check_rejects_a_mismatched_or_mislabelled_result():
    env = parse_claim_envelope(raw("claim-claimed.json").encode())
    good = ok(build(opts(raw("claim-claimed.json"))))
    package_from(good, env, system_instruction=SYSTEM, role_fast=FAST, role_deep=DEEP)
    for edit in (lambda r: r["source"].update(attemptEpoch=True),
                 lambda r: r["source"].update(contentDigest=r["source"]["contentDigest"].lower()),
                 lambda r: r["source"].update(replayed=True),
                 lambda r: r.update(suppliedSha256=r["source"]["supplied"]["requirementSha256"]),
                 lambda r: r["context"].update(systemInstruction="other")):
        bad = json.loads(json.dumps(good))
        edit(bad)
        with pytest.raises(H1ProvenanceError):
            package_from(bad, env, system_instruction=SYSTEM, role_fast=FAST, role_deep=DEEP)


# --------------------------------------------------------------------------- stability, separation, completeness

def test_two_builds_are_identical_except_the_random_snapshot_id():
    a = ok(build(opts(raw("claim-claimed.json"))))
    b = ok(build(opts(raw("claim-claimed.json"))))
    assert (a["text"], a["suppliedSha256"], a["source"]) == (b["text"], b["suppliedSha256"], b["source"])
    assert a["context"]["snapshotId"] != b["context"]["snapshotId"]


def test_system_and_role_instructions_are_captured_separately_from_the_hashed_text():
    r = ok(build(opts(raw("claim-claimed.json"))))
    c = r["context"]
    assert (c["systemInstruction"], c["roleInstructions"]) == (SYSTEM, {"fast": FAST, "deep": DEEP})
    assert c["messages"] == [{"role": "user", "content": r["text"]}]
    assert c["memoryIsNull"] is True and c["optionalCounts"] == [0, 0, 0, 0, 0, 0]
    for marker in (SYSTEM, FAST, DEEP):
        assert marker not in r["text"]                                               # not part of the hashed text


def test_requirement_attributes_and_every_rule_appear_whole():
    resp = derived("claim-claimed.json", lambda d: d["receipt"]["contentSnapshot"].update(
        value="Build the exporter.\nLine two.", attributes={"acceptance_criteria": "All tests pass.", "scope": "exporter/"}))
    r = ok(build(opts(resp)))
    t = r["text"]
    for piece in ("Build the exporter.\nLine two.", "All tests pass.", "exporter/", RULES[0]["text"], RULES[1]["text"],
                  RULES[0]["path"], RULES[1]["revision"]):
        assert piece in t
    assert f"({len('Build the exporter.\nLine two.'.encode())} bytes):\nBuild the exporter.\nLine two.\n" in t   # framed whole


# --------------------------------------------------------------------------- bounded refusals, no truncation

def test_context_budget_overflow_is_a_bounded_typed_refusal_without_echo():
    r = build(opts(raw("claim-claimed.json"), budget=dict(BUDGET, windowTokens=50)))
    assert (r["ok"], r["kind"], r["code"]) == (False, "ContextBudgetError", "CONTEXT_TOO_LARGE")
    assert type(r["estimatedInputTokens"]) is int and r["estimatedInputTokens"] > r["availableInputTokens"]
    assert set(r) == {"ok", "kind", "code", "estimatedInputTokens", "availableInputTokens", "_runtime"}


@pytest.mark.parametrize("over,code", [
    (dict(limits={"maxResponseBytes": 1_048_576, "maxPrerequisiteBytes": 262_144, "maxRules": 32,
                  "maxRuleBytes": 262_144, "maxPackageBytes": 64}), "PACKAGE_TOO_LARGE"),
    (dict(budget={"windowTokens": 1000}), "INVALID_OPTIONS"),
    (dict(capturedAtIso="yesterday"), "INVALID_OPTIONS"),
    (dict(rules=[RULES[0], dict(RULES[0])]), "INVALID_RULES"),                         # duplicate path
    (dict(rules=[{"path": "a\nb", "revision": "r", "text": "t"}]), "INVALID_RULES"),
])
def test_invalid_inputs_are_typed_refusals_that_echo_nothing(over, code):
    r = build(opts(raw("claim-claimed.json"), **over))
    assert (r["ok"], r["kind"], r["code"]) == (False, "PlanTaskError", code)
    assert set(r) == {"ok", "kind", "code", "_runtime"}


def test_unsafe_number_is_refused_by_h1_even_though_it_fits_int64():
    resp = derived("claim-claimed.json", lambda d: d["receipt"].update(eventSeq=9007199254740993))
    parse_claim_envelope(resp.encode())                                                # valid Int64 for the envelope
    r = build(opts(resp))
    assert (r["ok"], r["code"]) == (False, "INVALID_NUMBER")


# --------------------------------------------------------------------------- frozen / copy semantics

def test_result_is_frozen_and_unaffected_by_later_input_mutation():
    p = ok(build(opts(raw("claim-claimed.json"))))["probes"]
    assert p["sourceFrozen"] is True and p["assignmentRejectedOrIgnored"] is True and p["unchangedAfterInputMutation"] is True


def test_each_rule_field_is_read_exactly_once():
    r = ok(build(opts(raw("claim-claimed.json")), probe={"getterRules": True}))
    reads = r["probes"]["ruleFieldReads"]
    assert sorted(reads) == sorted(f"{i}.{k}" for i in range(len(RULES)) for k in ("path", "revision", "text"))
    assert "-SECOND-READ" not in r["text"]
    assert [x["sha256"] for x in r["source"]["supplied"]["rules"]] == [sha(x["text"]) for x in RULES]


# --------------------------------------------------------------------------- supervisor with the H1 builder (stub client)

class StubClient(SupervisorClient):
    """Serves the real fixture bytes; records writes. No HTTP."""

    def __init__(self, claim_body: str):
        super().__init__("http://127.0.0.1:1")
        self.claim_body = claim_body.encode()
        self.rec = json.loads(claim_body)["receipt"]
        self.writes: list[dict] = []

    def claim(self, *a, **kw):
        return Response(200, self.claim_body, json.loads(self.claim_body))

    def get_claim(self, root, key):
        return Response(200, self.claim_body, json.loads(self.claim_body))

    def get_plan(self, root):
        r = self.rec
        body = {"rootId": root, "nodes": [{"id": r["nodeId"], "work": "in_progress", "attemptId": r["attemptId"],
                                           "attemptEpoch": r["attemptEpoch"], "stateRevision": 1}]}
        return Response(200, json.dumps(body).encode(), body)

    def transition(self, node, payload):
        self.writes.append(dict(payload))
        body = {"outcome": "applied"}
        return Response(200, json.dumps(body).encode(), body)


def builder(**over):
    kw = dict(rules=RULES, system_instruction=SYSTEM, role_fast=FAST, role_deep=DEEP, budget=BUDGET, captured_at_iso=AT)
    kw.update(over)
    return h1_builder(**kw)


def run_with(stub, worker, b=None):
    rec = stub.rec
    return Supervisor(stub, worker, package_builder=b or builder()).run(rec["rootId"], rec["claimKey"], rec["attemptId"], rec["executorRef"])


def test_supervisor_with_h1_package_correlates_supplied_sha256_and_finishes():
    stub = StubClient(raw("claim-claimed.json"))
    seen = []
    r = run_with(stub, FakeWorker(lambda pkg, run: (seen.append(pkg), echo(pkg, run))[1]))
    assert (r.outcome, r.reason) == (Outcome.FINISHED, "finished")
    pkg = seen[0]
    assert pkg.supplied_sha256 == sha(pkg.text) and pkg.token == f"h1:{stub.rec['claimKey']}"
    assert (pkg.system_instruction, pkg.role_fast, pkg.role_deep) == (SYSTEM, FAST, DEEP)
    assert len(stub.writes) == 1 and stub.writes[0]["to"] == "done"
    assert "supplied" not in json.dumps(stub.writes[0])                               # nothing H1 is written to PlanStore


@pytest.mark.parametrize("field,builder_kw", [
    ("system_instruction", dict(system_instruction=SYSTEM + " (changed)")),
    ("role_fast", dict(role_fast=FAST + "-changed")),
    ("role_deep", dict(role_deep=DEEP + "-changed")),
])
def test_changed_instructions_change_semantic_identity_but_not_the_task_text_hash(field, builder_kw):
    env = parse_claim_envelope(raw("claim-claimed.json").encode())
    base = builder()(env)
    changed = builder(**builder_kw)(env)
    assert (changed.text, changed.supplied_sha256) == (base.text, base.supplied_sha256)   # hash scope: task text only
    assert semantic_identity(changed) != semantic_identity(base)                         # but identity differs
    assert getattr(changed, field) != getattr(base, field)


@pytest.mark.parametrize("field", ["system_instruction", "role_fast", "role_deep", "supplied_sha256"])
def test_a_result_for_different_supplied_context_never_finishes(field):
    stub = StubClient(raw("claim-claimed.json"))
    other = "0" * 64 if field == "supplied_sha256" else "a different instruction"
    r = run_with(stub, FakeWorker(lambda pkg, run: echo(pkg, run, **{field: other})))
    assert r.reason == f"result_correlation_mismatch:{field}" and stub.writes == []


def test_supplied_sha256_mismatch_never_finishes():
    stub = StubClient(raw("claim-claimed.json"))
    r = run_with(stub, FakeWorker(lambda pkg, run: echo(pkg, run, supplied_sha256="0" * 64)))
    assert r.reason == "result_correlation_mismatch:supplied_sha256" and stub.writes == []


@pytest.mark.parametrize("over,reason", [
    (dict(budget=dict(BUDGET, windowTokens=50)), "package_overflow:CONTEXT_TOO_LARGE"),
    (dict(limits={"maxResponseBytes": 1_048_576, "maxPrerequisiteBytes": 262_144, "maxRules": 32,
                  "maxRuleBytes": 262_144, "maxPackageBytes": 64}), "package_overflow:PACKAGE_TOO_LARGE"),
    (dict(rules=[RULES[0], dict(RULES[0])]), "package_refused:INVALID_RULES"),
])
def test_h1_refusals_stop_before_dispatch(over, reason):
    stub = StubClient(raw("claim-claimed.json"))
    worker = FakeWorker(lambda pkg, run: pytest.fail("must not dispatch"))
    r = run_with(stub, worker, builder(**over))
    assert (r.outcome, r.reason, worker.dispatches, stub.writes) == (Outcome.NEEDS_OPERATOR, reason, [], [])


def test_h1_number_refusal_stops_before_dispatch():
    stub = StubClient(derived("claim-claimed.json", lambda d: d["receipt"].update(eventSeq=9007199254740993)))
    worker = FakeWorker(lambda pkg, run: pytest.fail("must not dispatch"))
    r = run_with(stub, worker)
    assert (r.reason, worker.dispatches, stub.writes) == ("package_refused:INVALID_NUMBER", [], [])


def test_a_wrong_checkout_fails_explicitly(tmp_path):
    with pytest.raises(H1Unavailable):
        verify_checkout(tmp_path)
    stub = StubClient(raw("claim-claimed.json"))
    r = run_with(stub, FakeWorker(lambda pkg, run: pytest.fail("must not dispatch")), builder(repo=tmp_path))
    assert (r.reason, r.error, stub.writes) == ("uncertain:package_build", "H1Unavailable", [])
