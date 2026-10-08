"""HK-ISSUE-001 + HK-ISSUE-002 (plan 041): the reference consumer gives TYPED refusals for malformed input.
Before this change, `strict_loads` let `RecursionError` escape on deep nesting, and `review_identity` /
`revalidate` let `KeyError` / `AttributeError` escape on a missing identity or proof field. Offline, Python only."""

import copy
import json
from types import SimpleNamespace

import pytest

from e1 import acts as A
from e1 import consumer as C
from test_e2e_model import compose, delivery, fr_for, pkg  # noqa: F401 (pkg is a fixture)


def nested(kind: str, depth: int) -> bytes:
    return (b"[" * depth + b"]" * depth) if kind == "array" else (b'{"a":' * depth + b"1" + b"}" * depth)


# ------------------------------------------------------------------------ HK-ISSUE-001: excessive nesting

@pytest.mark.parametrize("kind", ["array", "object"])
@pytest.mark.parametrize("depth", [2000, 100_000])
def test_excessive_nesting_is_a_typed_strict_json_refusal(kind, depth):
    with pytest.raises(C.Refused) as e:
        C.strict_loads(nested(kind, depth), "probe")
    assert e.value.code == "strict_json" and "probe: nesting too deep" in str(e.value)


def test_a_scan_overflow_after_parsing_is_also_typed(monkeypatch):
    """A document can just fit the parser and still overflow the recursive scalar scan."""
    def overflow(v):
        raise RecursionError
    monkeypatch.setattr(C, "_scalars_ok", overflow)
    with pytest.raises(C.Refused) as e:
        C.strict_loads(b'{"a":[1]}', "probe")
    assert e.value.code == "strict_json" and "nesting too deep" in str(e.value)


@pytest.mark.parametrize("kind", ["array", "object"])
def test_reasonable_nesting_still_parses(kind):
    assert C.strict_loads(nested(kind, 50), "probe") is not None


def test_a_deeply_nested_delivery_field_refuses_before_any_effect(pkg):
    m, rid, p, t = pkg
    calls = []
    d = C.Delivery(**{**delivery(p, t).__dict__, "receipt": nested("array", 2000)})      # 4000 bytes: inside the receipt cap
    with pytest.raises(C.Refused) as e:
        compose(d, t, h1=lambda o: calls.append(o), retriever=lambda q: calls.append(q), wanted=[{"seq": 1, "kind": "x"}])
    assert e.value.code == "strict_json" and calls == []


# ------------------------------------------------------------------------ HK-ISSUE-002: missing identity / proof fields

def without_identity_field(manifest, name):
    m = copy.deepcopy(manifest)
    del m["state"]["mandatory"]["identity"][name]
    return m


@pytest.mark.parametrize("name", [*A.ATTEMPT_FIELDS, "artifactRef"])
def test_review_identity_missing_a_field_is_a_typed_delivery_mismatch(pkg, name):
    m, rid, p, t = pkg
    with pytest.raises(C.Refused) as e:
        C.review_identity(without_identity_field(p.manifest, name))
    assert (e.value.code, e.value.detail) == ("delivery_mismatch", "manifest identity")


@pytest.mark.parametrize("broken", [{"state": {}}, {"state": {"mandatory": {"identity": None}}}, [], None])
def test_review_identity_of_a_malformed_manifest_is_typed(broken):
    with pytest.raises(C.Refused) as e:
        C.review_identity(broken)
    assert e.value.code == "delivery_mismatch"


def test_a_consistent_delivery_whose_identity_lacks_attempt_id_is_refused_at_verification(pkg):
    """Re-signed end to end (manifest bytes, the envelope's copy, the digest and the receipt) so only the
    missing identity field is wrong: verify_delivery refuses it, before any revalidation or composition."""
    m, rid, p, t = pkg
    man = without_identity_field(p.manifest, "attemptId")
    env = copy.deepcopy(p.envelope)
    env["manifest"] = man
    raw = A.canonical(man).encode()
    d = delivery(p, t)
    receipt = dict(json.loads(d.receipt), candidateDigest=C.sha(raw))
    d = C.Delivery(**{**d.__dict__, "candidate_digest": C.sha(raw), "manifest": raw, "envelope": A.canonical(env).encode(),
                      "receipt": json.dumps(receipt).encode()})
    with pytest.raises(C.Refused) as e:
        C.verify_delivery(d)
    assert (e.value.code, e.value.detail) == ("delivery_mismatch", "manifest identity")


@pytest.mark.parametrize("drop", ["candidate_digest", "review_identity", "package_ref", "receipt_status", "pending", "basis"])
def test_a_fresh_proof_missing_a_field_is_a_typed_fresh_mismatch(pkg, drop):
    m, rid, p, t = pkg
    d = delivery(p, t)
    v = C.verify_delivery(d)
    proof = SimpleNamespace(**{k: getattr(fr_for(d, t), k) for k in C.FRESH_FIELDS if k != drop})
    with pytest.raises(C.Refused) as e:
        C.revalidate(v, proof)
    assert e.value.code == "fresh_mismatch" and drop in str(e.value.detail)


@pytest.mark.parametrize("over", [{"pending": None}, {"queue": "not-a-list"}])
def test_a_fresh_proof_with_malformed_lists_is_a_typed_fresh_mismatch(pkg, over):
    m, rid, p, t = pkg
    d = delivery(p, t)
    with pytest.raises(C.Refused) as e:
        C.revalidate(C.verify_delivery(d), fr_for(d, t, **over))
    assert e.value.code == "fresh_mismatch"


@pytest.mark.parametrize("break_v", [
    lambda v: C.Verified(without_identity_field(v.manifest, "attemptId"), v.envelope, v.task, v.receipt, v.h1_input),
    lambda v: C.Verified(v.manifest, v.envelope, {k: x for k, x in v.task.items() if k != "packageRef"}, v.receipt, v.h1_input),
    lambda v: C.Verified(v.manifest, v.envelope, v.task, {k: x for k, x in v.receipt.items() if k != "recordId"}, v.h1_input),
])
def test_a_verified_missing_a_bound_field_is_a_typed_delivery_mismatch(pkg, break_v):
    m, rid, p, t = pkg
    d = delivery(p, t)
    v = C.verify_delivery(d)
    with pytest.raises(C.Refused) as e:
        C.revalidate(break_v(v), fr_for(d, t))
    assert e.value.code == "delivery_mismatch"


def test_a_well_formed_proof_still_revalidates(pkg):
    m, rid, p, t = pkg
    d = delivery(p, t)
    out = C.revalidate(C.verify_delivery(d), fr_for(d, t))
    assert out["asOf"] == "revalidation" and out["pending"] == [] and out["queue"] == []
