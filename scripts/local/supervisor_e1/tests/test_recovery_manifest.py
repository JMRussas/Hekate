"""Pure recovery projection (e1/recovery_manifest.py; HK-ISSUE-015; root msgs 2309/2310/2311). Offline, no harness."""

import copy
import hashlib
import json

import pytest

from e1 import handoff as H
from e1 import recovery_manifest as RM
from e1.evidence import Record

HEX = "a" * 64
TERMINAL = {"decision": "abandoned_no_effects", "reconciliationRef": "recon-1"}
PRE = [("claim_intent", {}), ("claimed", {}), ("package_ref", {}), ("dispatch_intent", {})]


def stream(records, *, state="active", outstanding=None, root="root-1", ck="ck-1", corrupt=None, summary=None):
    recs = [{"seq": i + 1, "kind": k, "hash": f"{i:064x}", "fields": dict(f)} for i, (k, f) in enumerate(records)]
    return {"root": root, "claimKey": ck, "state": state, "outstanding": [] if outstanding is None else outstanding,
            "headHash": HEX, "resolvedAt": None if state == "active" else 5.0, "corrupt": corrupt, "records": recs,
            "summary": summary}


def obs(*streams, per_stream=64):
    return {"schema": RM.INPUT_SCHEMA, "writer": {"id": "coordinator#abc", "epoch": 1}, "bounds": {"perStream": per_stream},
            "streams": list(streams)}


def proj(*streams, **kw):
    raw, sha = RM.project(obs(*streams, **kw))
    assert hashlib.sha256(raw).hexdigest() == sha and sha.encode() not in raw          # hash beside the body, never inside
    return json.loads(raw)


def statuses(p, i=0):
    return [(x["id"], x["status"], x["effects"]) for x in p["streams"][i]["intents"]]


# --- per-status matrix ----------------------------------------------------------------------------------------------

def test_the_check_002_shape_is_open_unknown_and_never_complete_proof_of_safety():
    p = proj(stream(PRE, outstanding=[["dispatch_outcome"]]))
    assert statuses(p) == [("claim_intent@1", "answered", "observed_outcome_semantics_only"),
                           ("dispatch_intent@4", "open_unknown", "unknown")]
    assert p["integrity"] == "caller_observed_unverified" and p["authority"] == "none" and p["complete"] is True


def test_closed_before_and_after_resolve_and_another_existing_decision():
    before = proj(stream(PRE + [("operator_resolution", TERMINAL)], outstanding=[["dispatch_outcome"]]))
    after = proj(stream(PRE + [("operator_resolution", TERMINAL)], state="resolved"))
    for p in (before, after):
        assert statuses(p)[-1] == ("dispatch_intent@4", "closed:abandoned_no_effects", "operator_asserted_not_attested")
    assert after["streams"][0]["finalResolution"] == {"decision": "abandoned_no_effects", "problem": None, "afterIds": [], "afterCount": 0}
    rel = proj(stream(PRE + [("operator_resolution", {"decision": "confirmed_released", "reconciliationRef": "r"})], state="resolved"))
    assert statuses(rel)[-1][1] == "closed:confirmed_released"


def test_an_active_terminal_closes_only_earlier_intents_and_later_ones_stay_open():
    p = proj(stream(PRE + [("operator_resolution", TERMINAL), ("notify_intent", {})],
                    outstanding=[["dispatch_outcome"], ["notify_outcome"]]))
    assert statuses(p)[-2:] == [("dispatch_intent@4", "closed:abandoned_no_effects", "operator_asserted_not_attested"),
                                ("notify_intent@6", "open_unknown", "unknown")]


def test_a_legacy_resolved_post_terminal_intent_stays_visible_as_unknown():
    p = proj(stream(PRE + [("operator_resolution", TERMINAL), ("notify_intent", {})], state="resolved"))
    assert statuses(p)[-1] == ("notify_intent@6", "post_terminal_unknown", "unknown")
    assert p["streams"][0]["finalResolution"]["problem"] == "resolution_not_final"
    assert p["streams"][0]["finalResolution"]["afterIds"] == ["notify_intent@6"]


def test_a_confirmed_finish_resolved_stream_and_resolved_without_closure():
    done = [("claim_intent", {}), ("claimed", {}), ("finish_intent", {}), ("finish_outcome", {})]
    p = proj(stream(done, state="resolved"))
    assert p["streams"][0]["status"] == "listed" and all(s == "answered" for _, s, _ in statuses(p))
    bad = proj(stream(PRE, state="resolved"))                                            # resolved, dispatch unanswered
    assert (bad["streams"][0]["status"], bad["streams"][0]["reason"], bad["complete"]) == ("unlistable", "resolved_without_closure", False)


def test_compacted_and_corrupt_streams_are_never_enumerated_and_never_complete():
    comp = proj(stream([], state="compacted", summary={"kindsTail": ["dispatch_intent", "operator_resolution"], "resolution": TERMINAL}))
    s = comp["streams"][0]
    assert (s["status"], s["intents"], s["summary"]["resolution"], comp["complete"]) == (
        "compacted_no_enumeration", [], TERMINAL, False)
    bad = proj(stream([], corrupt="hash:3"))
    assert (bad["streams"][0]["status"], bad["streams"][0]["reason"], bad["complete"]) == ("unlistable", "hash:3", False)


def test_a_degraded_answer_has_unknown_effects():
    p = proj(stream([("claim_intent", {}), ("claimed", {"invalidPayload": True})]))
    assert statuses(p) == [("claim_intent@1", "answered", "unknown")]


def test_reservation_mismatches_are_unlistable():
    for outstanding, state in (([], "active"), ([["dispatch_outcome"], ["dispatch_outcome"]], "active"),
                               ([["notify_outcome"]], "active")):
        p = proj(stream(PRE, outstanding=outstanding, state=state))
        assert (p["streams"][0]["status"], p["streams"][0]["reason"]) == ("unlistable", "reservation_mismatch")
    p = proj(stream(PRE + [("operator_resolution", TERMINAL)], state="resolved", outstanding=[["dispatch_outcome"]]))
    assert p["streams"][0]["reason"] == "reservation_mismatch"


@pytest.mark.parametrize("resolution", [
    {"decision": "release", "reconciliationRef": "r"},
    {"decision": "abandoned_no_effects", "reconciliationRef": "r", "invalidPayload": True},
    {"decision": "abandoned_no_effects"},
])
def test_non_terminal_or_invalid_last_resolutions_close_nothing(resolution):
    p = proj(stream(PRE + [("operator_resolution", resolution)], outstanding=[["dispatch_outcome"]]))
    assert statuses(p)[-1] == ("dispatch_intent@4", "open_unknown", "unknown")


def test_an_invalid_last_resolution_does_not_revive_an_earlier_terminal_one():
    p = proj(stream(PRE + [("operator_resolution", TERMINAL), ("operator_resolution", {"decision": "release", "reconciliationRef": "r"})],
                    outstanding=[["dispatch_outcome"]]))
    assert statuses(p)[-1] == ("dispatch_intent@4", "open_unknown", "unknown")


def test_fifo_trap_a_later_outcome_answers_the_earlier_intent():
    p = proj(stream(PRE + [("operator_resolution", TERMINAL), ("dispatch_intent", {}), ("dispatch_outcome", {})],
                    outstanding=[["dispatch_outcome"]]))
    assert statuses(p)[-2:] == [("dispatch_intent@4", "answered", "observed_outcome_semantics_only"),
                                ("dispatch_intent@6", "open_unknown", "unknown")]


# --- agreement with handoff.pending_effects (listable, non-legacy streams) --------------------------------------------

@pytest.mark.parametrize("records, state, outstanding", [
    (PRE, "active", [["dispatch_outcome"]]),
    (PRE + [("operator_resolution", TERMINAL)], "active", [["dispatch_outcome"]]),
    (PRE + [("operator_resolution", TERMINAL)], "resolved", []),
    (PRE + [("operator_resolution", TERMINAL), ("notify_intent", {})], "active", [["dispatch_outcome"], ["notify_outcome"]]),
    (PRE + [("operator_resolution", {"decision": "release", "reconciliationRef": "r"})], "active", [["dispatch_outcome"]]),
])
def test_unanswered_statuses_agree_with_pending_effects(records, state, outstanding):
    s = stream(records, state=state, outstanding=outstanding)
    recs = [Record(r["seq"], r["kind"], "coordinator#abc", "root-1", "ck-1", 0.0, json.dumps(r["fields"], sort_keys=True))
            for r in s["records"]]
    pe = {i["id"]: i["status"] for i in H.pending_effects(recs, None, outstanding)}
    mine = {i: st for i, st, _ in statuses(proj(s)) if st != "answered"}
    assert {i: ("unknown" if st == "open_unknown" else st) for i, st in mine.items()} == pe


# --- determinism, strictness, bounds, purity -------------------------------------------------------------------------

def test_identical_input_gives_identical_bytes_and_order_does_not_matter():
    a, b = stream(PRE, outstanding=[["dispatch_outcome"]], root="r-a"), stream(PRE, outstanding=[["dispatch_outcome"]], root="r-b")
    raw1, sha1 = RM.project(obs(a, b))
    raw2, sha2 = RM.project(json.loads(json.dumps(obs(b, a), indent=3)))
    assert (raw1, sha1) == (raw2, sha2)
    c = copy.deepcopy(a)
    c["headHash"] = "b" + HEX[1:]
    assert RM.project(obs(c, b))[1] != sha1


def test_the_input_is_never_mutated():
    o = obs(stream(PRE + [("operator_resolution", TERMINAL)], state="resolved"))
    snapshot = copy.deepcopy(o)
    RM.project(o)
    assert o == snapshot


def test_an_empty_writer_is_permitted_but_not_complete():
    p = proj()
    assert (p["streams"], p["complete"], p["counts"]["streams"]) == ([], False, 0)


def mutate(path_fn):
    o = obs(stream(PRE + [("operator_resolution", TERMINAL)], outstanding=[["dispatch_outcome"]]))
    path_fn(o)
    with pytest.raises(RM.ProjectionRefused) as e:
        RM.project(o)
    return e.value.code


@pytest.mark.parametrize("fn, code", [
    (lambda o: o.update(extra=1), "input_schema"),
    (lambda o: o["streams"][0].update(note="x"), "input_schema"),
    (lambda o: o["streams"][0]["records"][0].update(text="x"), "input_schema"),
    (lambda o: o["streams"][0]["records"][0]["fields"].update(error="boom"), "input_schema"),
    (lambda o: o["streams"][0]["records"][0]["fields"].update(decision="x"), "input_schema"),       # not on that kind
    (lambda o: o["streams"][0]["records"][4]["fields"].update(reconciliationRef="has space"), "input_schema"),
    (lambda o: o["streams"][0]["records"][0].update(seq=True), "input_schema"),                    # bool is not an int
    (lambda o: o["writer"].update(epoch=1.0), "input_schema"),
    (lambda o: o["streams"][0].update(state="resolved", resolvedAt=float("nan")), "input_schema"),
    (lambda o: o["streams"][0]["records"][0].update(kind="nope"), "input_schema"),
    (lambda o: o["streams"][0]["records"][2].update(seq=4), "input_order"),                         # a gap hides an intent
    (lambda o: o["streams"].append(copy.deepcopy(o["streams"][0])), "input_order"),                 # duplicate stream
    (lambda o: o["streams"][0].update(resolvedAt=3.0), "input_state"),                              # active with resolvedAt
    (lambda o: o["streams"][0].update(corrupt="bad"), "input_state"),                               # corrupt with records
    (lambda o: o["streams"][0].update(outstanding=[["not_a_terminal"]]), "input_schema"),
    (lambda o: o["streams"][0].update(outstanding=[["dispatch_outcome"] * 4]), "input_schema"),
    (lambda o: o["streams"][0].update(root=""), "input_schema"),
])
def test_strict_input_refuses(fn, code):
    assert mutate(fn) == code


def test_bounds_refuse_the_whole_projection():
    with pytest.raises(RM.ProjectionRefused) as e:
        RM.project(obs(*[stream(PRE, outstanding=[["dispatch_outcome"]], root=f"r{i:03}") for i in range(65)]))
    assert e.value.code == "input_overflow"
    with pytest.raises(RM.ProjectionRefused) as e:
        RM.project(obs(stream(PRE, outstanding=[["dispatch_outcome"]]), per_stream=3))
    assert e.value.code == "input_overflow"
    many = [("notify_intent", {}) for _ in range(4000)]
    big = [stream(many, outstanding=[["notify_outcome"]] * 4000, root=f"r{i:02}") for i in range(3)]
    with pytest.raises(RM.ProjectionRefused) as e:
        RM.project(obs(*big, per_stream=4096))
    assert e.value.code == "projection_overflow"


def test_no_status_or_effect_ever_asserts_no_effects():
    fixtures = [obs(stream(PRE, outstanding=[["dispatch_outcome"]])),
                obs(stream(PRE + [("operator_resolution", TERMINAL)], state="resolved")),
                obs(stream(PRE + [("operator_resolution", TERMINAL), ("notify_intent", {})], state="resolved")),
                obs(stream([], state="compacted", summary={"kindsTail": [], "resolution": TERMINAL}))]
    for o in fixtures:
        p = json.loads(RM.project(o)[0])
        for s in p["streams"]:
            assert all(i["effects"] in ("unknown", "operator_asserted_not_attested", "observed_outcome_semantics_only")
                       for i in s["intents"])
        text = RM.project(o)[0].decode()
        assert text.count("no_effects") == text.count("abandoned_no_effects")   # only the echoed decision name
