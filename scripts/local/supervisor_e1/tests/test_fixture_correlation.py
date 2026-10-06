"""Offline: whole-document validation of the REAL raw claim fixtures (context-store/plans/fixtures/023).

Mutated documents below are derived test inputs, never described as captured responses.
"""

import ast
import json
from pathlib import Path

import pytest

from e1.exact import WireError, counter, loads_exact
from e1.seam import OPAQUE_PREFIX, opaque_package, parse_claim_envelope
from e1.supervisor import CORRELATION, WorkResult, correlation_fields

FIXTURES = Path(__file__).resolve().parents[4] / "context-store" / "plans" / "fixtures" / "023"


def raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def mutated(name: str, edit) -> bytes:
    doc = json.loads(raw(name))
    edit(doc)
    return json.dumps(doc).encode()


def test_claimed_fixture_validates_whole_document():
    env = parse_claim_envelope(raw("claim-claimed.json"))
    r = env.receipt
    assert (env.replayed, env.still_current, r.outcome) == (False, True, "claimed")
    assert env.current is not None and (env.current.work, env.current.attempt_id, env.current.attempt_epoch) == ("in_progress", r.attempt_id, r.attempt_epoch)
    assert r.content_digest == r.content_digest.upper() and len(r.content_digest) == 64   # structured digest, uppercase
    assert r.prereq_digest == r.prereq_digest.lower() and len(r.prereq_digest) == 64      # hekate-prereq/v1, lowercase
    assert type(r.attempt_epoch) is int and type(r.event_seq) is int and type(r.content_revision) is int
    assert env.raw == raw("claim-claimed.json")                                          # raw bytes kept verbatim


def test_replayed_fixture_correlates_with_the_claimed_one():
    a = parse_claim_envelope(raw("claim-claimed.json"))
    b = parse_claim_envelope(raw("claim-replayed.json"))
    assert b.replayed is True and a.replayed is False
    assert b.receipt == a.receipt                     # identical receipt, every field
    assert opaque_package(b) == opaque_package(a)     # same opaque package identities


def test_no_ready_work_fixture_yields_no_package():
    env = parse_claim_envelope(raw("claim-no-ready-work.json"))
    assert (env.receipt.outcome, env.still_current, env.current) == ("no_ready_work", False, None)
    assert env.receipt.node_id is None and env.receipt.content_digest is None
    with pytest.raises(WireError) as e:
        opaque_package(env)
    assert e.value.code == "no_package"


def test_opaque_package_is_explicitly_opaque_and_verbatim():
    env = parse_claim_envelope(raw("claim-claimed.json"))
    pkg = opaque_package(env)
    r = env.receipt
    assert pkg.token == OPAQUE_PREFIX + r.claim_key   # not a rendered context, not a digest
    assert (pkg.content_digest, pkg.prereq_digest, pkg.attempt_epoch, pkg.event_seq) == (r.content_digest, r.prereq_digest, r.attempt_epoch, r.event_seq)


@pytest.mark.parametrize("name,edit,code", [
    ("claim-claimed.json", lambda d: d.update(contractVersion="plan-contract/v9"), "unsupported_contract_version"),
    ("claim-claimed.json", lambda d: d["receipt"].update(outcome="granted"), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"].update(contentDigest=d["receipt"]["contentDigest"].lower()), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"].update(prereqDigest=d["receipt"]["prereqDigest"].upper()), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"].update(digest="0" * 64), "correlation_mismatch"),
    ("claim-claimed.json", lambda d: d["receipt"].update(attemptEpoch=True), "unexpected_shape"),          # bool-as-int
    ("claim-claimed.json", lambda d: d["receipt"].update(attemptEpoch=0), "unexpected_shape"),             # epoch >= 1
    ("claim-claimed.json", lambda d: d["receipt"].update(eventSeq=2**63), "integer_out_of_range"),         # raw token > Int64
    ("claim-claimed.json", lambda d: d["receipt"].update(eventSeq=0), "unexpected_shape"),                 # semantic counter: seq >= 1
    ("claim-claimed.json", lambda d: d["receipt"].update(contentRevision=-1), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["current"].update(attemptEpoch=2), "correlation_mismatch"),         # stillCurrent lies
    ("claim-claimed.json", lambda d: d["receipt"].pop("executorRef"), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"].update(claimKey=".."), "unexpected_shape"),
    # nested snapshot validation (derived inputs)
    ("claim-claimed.json", lambda d: d["receipt"]["contentSnapshot"].update(value=5), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["contentSnapshot"].update(attributes={"priority": "high"}), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["contentSnapshot"].update(attributes={"scope": 3}), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"].update(chain="not-a-list"), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"].update(chain=["string-owner"]), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["chain"].reverse(), "correlation_mismatch"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["nodes"][0].update(attemptEpoch=True), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["nodes"][0].update(attemptEpoch=-1), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["nodes"][0].update(contentRevision=2**63), "integer_out_of_range"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["nodes"][0].update(contentRevision=0), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["nodes"][0].update(work="paused"), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["nodes"][0].update(kind="widget"), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["nodes"][0]["acceptance"].update(decision="maybe"), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["nodes"][0]["acceptance"].update(attemptEpoch="1"), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["nodes"][0].update(pinnedPrereqDigest="A" * 64), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["nodes"][0].pop("acceptance"), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["declared"][0].update(gate="maybe"), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"]["prereqSnapshot"]["declared"][0].update(predecessorId="00000000-0000-4000-8000-000000000009"), "unexpected_shape"),
    ("claim-claimed.json", lambda d: d["receipt"].update(rootId=d["receipt"]["rootId"] + "\n"), "unexpected_shape"),   # fullmatch, no trailing newline
    ("claim-no-ready-work.json", lambda d: d["receipt"].update(nodeId="00000000-0000-4000-8000-000000000001"), "unexpected_shape"),
    ("claim-no-ready-work.json", lambda d: d.update(stillCurrent=True), "unexpected_shape"),
])
def test_derived_invalid_documents_fail_closed(name, edit, code):
    with pytest.raises(WireError) as e:
        parse_claim_envelope(mutated(name, edit))
    assert e.value.code == code


def test_snapshot_equality_is_semantic_not_property_order():
    a = parse_claim_envelope(raw("claim-claimed.json"))
    reordered = mutated("claim-claimed.json", lambda d: d["receipt"].update(
        prereqSnapshot=dict(reversed(list(d["receipt"]["prereqSnapshot"].items()))),
        contentSnapshot=dict(reversed(list(d["receipt"]["contentSnapshot"].items())))))
    assert parse_claim_envelope(reordered).receipt == a.receipt


def test_requested_identity_must_match():
    env = parse_claim_envelope(raw("claim-claimed.json"))
    with pytest.raises(WireError) as e:
        parse_claim_envelope(raw("claim-claimed.json"), expected_key=env.receipt.claim_key + "x")
    assert e.value.code == "correlation_mismatch"


@pytest.mark.parametrize("text,code", [
    ('{"a": 1.5}', "unsupported_number"),
    ('{"a": 9.007199254740993e15}', "unsupported_number"),
    ('{"a": 1e3}', "unsupported_number"),
    ('{"a": NaN}', "unsupported_number"),
    ('{"a": Infinity}', "unsupported_number"),
    ('{"a": 1', "malformed_json"),
    # every token is checked, even one shadowed by a later duplicate (duplicates are rejected anyway)
    ('{"a": 9223372036854775808, "b": 1}', "integer_out_of_range"),
    ('{"a": -9223372036854775809}', "integer_out_of_range"),
    ('[1, [2, {"deep": 123456789012345678901234567890}]]', "integer_out_of_range"),
    ('{"a": 1.5, "a": 2}', "unsupported_number"),
    ('{"a": 9223372036854775808, "a": 2}', "integer_out_of_range"),
    ('{"a": 1, "a": 2}', "duplicate_key"),
    ('{"o": {"x": null, "x": 3}}', "duplicate_key"),
])
def test_float_and_constant_tokens_are_rejected_not_rounded(text, code):
    with pytest.raises(WireError) as e:
        loads_exact(text)
    assert e.value.code == code


def test_large_integers_stay_exact_and_counters_are_bounded():
    assert loads_exact('{"a": 9007199254740993}')["a"] == 9007199254740993
    assert loads_exact("[9223372036854775807, -9223372036854775808]") == [2**63 - 1, -(2**63)]   # Int64 bounds parse
    assert counter(2**63 - 1, "x") == 2**63 - 1
    for bad in (True, False, -1, 2**63, "1", None):
        with pytest.raises(WireError):
            counter(bad, "x")


def test_seam_modules_are_pure_by_import_list():
    allowed = {"__future__", "re", "json", "dataclasses", "typing"}
    for module in ("seam.py", "exact.py"):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "e1" / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert {a.name.split(".")[0] for a in node.names} <= allowed, (module, ast.dump(node))
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert node.module.split(".")[0] in allowed, (module, node.module)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("open", "eval", "exec", "__import__"):
                pytest.fail(f"{module} performs I/O or dynamic execution")


def test_a_fresh_claim_that_is_not_current_is_never_dispatched():
    """Derived input: a fresh (replayed=false) claimed receipt whose attempt is no longer current."""
    from e1.supervisor import FakeWorker, Outcome, Supervisor
    from e1.wire import Response, SupervisorClient

    def not_current(d):
        d["stillCurrent"] = False
        d["current"] = {"work": "todo", "attemptId": None, "attemptEpoch": 1}
    body = mutated("claim-claimed.json", not_current)
    rec = json.loads(body)["receipt"]

    class StubClient(SupervisorClient):
        def claim(self, root, claim_key, attempt_id, executor_ref, actor):
            return Response(200, body, json.loads(body))

    worker = FakeWorker(lambda pkg, run: pytest.fail("must not dispatch"))
    r = Supervisor(StubClient("http://127.0.0.1:1"), worker).run(rec["rootId"], rec["claimKey"], rec["attemptId"], rec["executorRef"])
    assert (r.outcome, r.reason, worker.dispatches) == (Outcome.NEEDS_OPERATOR, "not_current_at_dispatch", [])


def test_work_result_carries_every_correlation_field():
    assert correlation_fields() == CORRELATION
    assert set(CORRELATION) == {"run_id", "package_token", "root_id", "node_id", "claim_key", "attempt_id", "attempt_epoch",
                                "executor_ref", "content_digest", "prereq_digest", "supplied_sha256",
                                "system_instruction", "role_fast", "role_deep"}
    assert {f for f in WorkResult.__dataclass_fields__} >= set(CORRELATION)
