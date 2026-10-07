"""E2e live (plan 034 rev 3 §7; TEST-ONLY, disposable database, DEFAULT suite = Python + Postgres only):
an accepted E2d candidate is delivered as its exact stored bytes, revalidated in ONE combined snapshot
and composed offline. The H1 builder is the H1-SHAPED stub (the real H1 is opt-in interop_live)."""

import hashlib

import psycopg
import pytest

from e1 import acts as A
from e1 import consumer as C
from e1 import consumer_durable as CD
from e2b_support import journal_digest, raw
from helpers import key, ok
from test_e2c_live import LS1, LS2, LS3, e2c  # noqa: F401 (fixture)
from test_e2d_live import R  # noqa: F401 (fixture)

POLICY = C.PolicyStub("lead-1", ({"id": "read", "action": "source_read", "match": {}, "allow": True},
                                 {"id": "use", "action": "destination_use", "match": {}, "allow": True}))
BUDGET = {"windowTokens": 200_000, "maxHistoryTurns": 0, "safetyTokens": 0, "fastOutputTokens": 1000, "deepOutputTokens": 1000}


def everything_digest(dsn):
    """Every journal and E2c/E2d table (no-write assertion)."""
    with psycopg.connect(dsn) as c:
        extra = c.execute("SELECT md5(coalesce((SELECT string_agg(t::text, '|' ORDER BY t::text) FROM supervisor_journal.e2c_counters t), '') "
                          "|| coalesce((SELECT string_agg(t::text, '|' ORDER BY t::text) FROM supervisor_journal.e2c_queue t), '') "
                          "|| coalesce((SELECT string_agg(t::text, '|' ORDER BY t::text) FROM supervisor_journal.e2c_runs t), '') "
                          "|| coalesce((SELECT string_agg(t::text, '|' ORDER BY t::text) FROM supervisor_journal.e2d_candidates t), '') "
                          "|| coalesce((SELECT string_agg(t::text, '|' ORDER BY t::text) FROM public.plan_node_state t), ''))").fetchone()[0]
    return journal_digest(dsn) + extra


class Handed:
    def __init__(self, r):
        self.r = r
        self.p = r.prep()
        d, self.rec = r.commit(self.p)
        assert d.outcome == "accepted"
        self.h1_in = {"response": "{\"claim\":\"live\"}", "rules": [], "systemInstruction": r.task["instructions"]["system"],
                      "roleInstructions": {"fast": r.task["instructions"]["fast"], "deep": r.task["instructions"]["deep"]},
                      "budget": BUDGET, "capturedAtIso": "2026-10-07T12:00:00Z"}

    def delivery(self):
        return CD.delivery(self.r.L.dsn, self.p.transition["handoffId"], self.rec, self.h1_in)

    def fresh(self, **kw):
        return CD.fresh(self.r.L.dsn, C.verify_delivery(self.delivery()), **kw)

    def compose(self, **kw):
        fr = kw.pop("fr", None) or self.fresh()
        return C.compose(self.delivery(), fr, policy=kw.pop("policy", POLICY), destination=kw.pop("destination", "conv-successor"),
                         h1=C.h1_stub(self.r.task["text"]), **kw)


@pytest.fixture
def handed(R):  # noqa: F811
    return lambda: Handed(R())


def test_exact_stored_bytes_compose_and_nothing_is_written(handed):
    h = handed()
    d = h.delivery()
    assert hashlib.sha256(d.manifest).hexdigest() == h.p.package.candidate_digest == h.rec["candidateDigest"]
    before = everything_digest(h.r.L.dsn)
    sel = [{"seq": e["seq"], "kind": e["kind"]} for e in h.p.package.manifest["evidenceIndex"]]
    c = h.compose(wanted=sel, retriever=CD.retriever(h.r.L.dsn, C.verify_delivery(h.delivery())))
    assert everything_digest(h.r.L.dsn) == before                                   # verify, retrieve, compose: no writes
    assert c.view["candidateDigest"] == h.p.package.candidate_digest and c.view_digest != h.p.package.candidate_digest
    got = [o for o in c.view["optional"] if o["kind"] == "retrieval"]
    assert got and all(o["status"] == "included" and o["label"] == "as-of" and o["basis"] for o in got)
    assert c.view["mandatory"]["revalidation"]["basis"]["snapshot"]


def test_receipt_and_facts_come_from_one_snapshot_and_refuse_when_stale(handed, setup):
    h = handed()
    assert h.fresh().receipt_status == "current"
    p3 = h.r.prep(target=LS3)                                                        # a later handoff supersedes the receipt
    assert h.r.commit(p3)[0].outcome == "accepted"
    fr = h.fresh()
    assert (fr.receipt_status, fr.current_binding) == ("superseded", p3.transition["linkId"])
    with pytest.raises(C.Refused) as e:
        h.compose(fr=fr)
    assert e.value.code == "receipt_not_current"


def test_a_decided_review_refuses(handed, setup):
    h = handed()
    L = h.r.L
    ok(setup.decide(L.p.target, {"decision": "accepted", "reviewedContentRevision": L.receipt["contentRevision"], "reviewedArtifactRef": "sha-art",
                                 "reviewedAttemptEpoch": L.receipt["attemptEpoch"], "evidenceRef": "ev-e2e", "operationKey": key(),
                                 "expectedStateRevision": L.state_rev(), "actor": "e1-verifier"}))
    with pytest.raises(C.Refused) as e:
        h.compose()
    assert e.value.code == "review_not_candidate"


def test_revised_content_refuses(handed, setup):
    h = handed()
    L = h.r.L
    node = next(n for n in L.setup.plan(L.p.root).body["nodes"] if n["id"] == L.p.target)
    ok(setup.revise(L.p.target, "revised after commit", node["contentRevision"], key(), node["stateRevision"]))
    with pytest.raises(C.Refused) as e:
        h.compose()
    assert e.value.code == "stale_content"


def test_new_uncertainty_after_commit_is_relisted(handed):
    h = handed()
    h.r.L.aj.append(h.r.L.p.root, h.r.L.ck, "notify_intent", {"purpose": "remind-lead"})   # an outcome-less intent appears
    c = h.compose()
    ids = [p["id"] for p in c.view["mandatory"]["revalidation"]["pending"]]
    assert any(i.startswith("notify_intent@") for i in ids)
    assert not any(p["id"].startswith("notify_intent@") for p in c.view["mandatory"]["authority"]["pending"])


def test_a_corrupt_stream_is_not_current(handed):
    h = handed()
    raw(h.r.L.dsn, ("UPDATE supervisor_journal.records SET record_hash = repeat('0', 64) WHERE record_id = %s",
                    (h.rec["recordId"],)), replica=True)
    with pytest.raises(C.Refused) as e:
        h.fresh()                                     # pending effects cannot be listed from an unverifiable chain
    assert e.value.code == "uncertainty_unlistable"


def test_a_tampered_stored_candidate_refuses_the_delivery(handed):
    h = handed()
    raw(h.r.L.dsn, ("UPDATE supervisor_journal.e2d_candidates SET envelope = replace(envelope, '\"payload\":{', '\"payload\":{\"x\":1,') "
                    "WHERE handoff_id = %s", (h.p.transition["handoffId"],)), replica=True)
    with pytest.raises(C.Refused) as e:
        h.compose()
    assert e.value.code == "delivery_mismatch"


def test_retrieved_evidence_must_match_the_full_pointer_in_a_validated_chain(handed):
    h = handed()
    h.r.L.intake(h.r.act("review_acknowledged", 1, LS2))
    get = CD.retriever(h.r.L.dsn, C.verify_delivery(h.delivery()))
    ptr = dict(next(e for e in h.p.package.manifest["evidenceIndex"] if e["kind"] == "binding"),
               root=h.r.L.p.root, claimKey=h.r.L.ck)
    assert get(dict(ptr, claimKey="another-stream")) is None                                   # a different source stream
    assert get(dict(ptr))["pointer"] == ptr
    assert get(dict(ptr, linkId="00000000-0000-0000-0000-000000000000")) is None              # same seq/kind, other identity
    raw(h.r.L.dsn, ("UPDATE supervisor_journal.records SET data = replace(data, 'lead-1', 'lead-X') WHERE root = %s AND claim_key = %s "
                    "AND seq = %s", (h.r.L.p.root, h.r.L.ck, ptr["seq"])), replica=True)            # altered content, same seq/kind
    assert get(dict(ptr)) is None


def test_default_deny_policy_still_composes_with_mandatory_content(handed):
    h = handed()
    sel = [{"seq": e["seq"], "kind": e["kind"]} for e in h.p.package.manifest["evidenceIndex"]][:2]
    calls = []
    c = h.compose(policy=C.PolicyStub("lead-1"), wanted=sel, retriever=lambda q: calls.append(q))
    assert calls == [] and all(o["status"] == "denied" for o in c.view["optional"] if o["kind"] == "retrieval")
    assert c.view["mandatory"]["authority"] == h.p.package.manifest["authority"]
