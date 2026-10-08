"""handoff.pending_effects on streams closed by an EXISTING terminal operator resolution (root msgs 2228/2232/2240/2243).

The defect (retained first as a strict xfail at 321edfd): resolve() clears the stream's `outstanding`, but the records
still hold the unanswered intent, so the listing's cross-check raised instead of listing the closed intent. The fix
lists an intent recorded BEFORE a final terminal resolution with status "closed:<decision>" (kind and keys unchanged),
and accepts a released (empty) `outstanding` only when nothing after that resolution is open. No vocabulary change."""

import pytest

from e1 import handoff as H
from e1.evidence import ModelJournal

ROOT, CK = "root-1", "ck-1"
TERMINAL = {"decision": "abandoned_no_effects", "reconciliationRef": "recon-1"}


def stream(*extra, resolution=TERMINAL, resolve=True):
    """claim -> claimed -> package -> dispatch_intent (unanswered) -> resolution -> extra records -> resolve()."""
    m = ModelJournal("w#1", now=1.0)
    m.append(ROOT, CK, "claim_intent", {"attemptId": "a1"})
    m.append(ROOT, CK, "claimed", {"outcome": "claimed"})
    m.append(ROOT, CK, "package_ref", {"packageSha256": "p" * 64, "jcs": "{}"})
    m.append(ROOT, CK, "dispatch_intent", {"exec": "e" * 64, "key": {}})
    if resolution is not None:
        m.append(ROOT, CK, "operator_resolution", resolution)
    for kind, data in extra:
        m.append(ROOT, CK, kind, data)
    if resolve:
        m.resolve(ROOT, CK)
    s = m.streams[(ROOT, CK)]
    return s.records, s.outstanding


def listing(records, outstanding):
    return [(i["id"].split("@")[0], i["status"], i["awaiting"]) for i in H.pending_effects(records, None, outstanding)]


def test_the_fixture_is_a_resolved_stream_with_an_unanswered_dispatch_intent():
    records, outstanding = stream()
    kinds = [r.kind for r in records]
    assert kinds[-2:] == ["dispatch_intent", "operator_resolution"] and "dispatch_outcome" not in kinds
    assert outstanding == []                                             # resolve() cleared the reservation


def test_pending_effects_lists_an_intent_closed_by_a_terminal_resolution():
    # the 321edfd regression, now passing: listed, never omitted, kind/keys unchanged
    records, outstanding = stream()
    items = H.pending_effects(records, None, outstanding)
    assert items == [{"kind": "open_intent", "id": items[0]["id"], "status": "closed:abandoned_no_effects",
                      "awaiting": ["dispatch_outcome"]}] and items[0]["id"].startswith("dispatch_intent@")


def test_before_resolve_the_same_listing_holds_with_the_reservation_still_held():
    records, outstanding = stream(resolve=False)
    assert outstanding == [["dispatch_outcome"]]
    assert listing(records, outstanding) == [("dispatch_intent", "closed:abandoned_no_effects", ["dispatch_outcome"])]


@pytest.mark.parametrize("decision", ["confirmed_released", "confirmed_finished"])
def test_every_existing_terminal_decision_closes_the_same_way(decision):
    records, outstanding = stream(resolution={"decision": decision, "reconciliationRef": "recon-1"})
    assert listing(records, outstanding) == [("dispatch_intent", f"closed:{decision}", ["dispatch_outcome"])]


NOTIFY = ("notify_intent", {"n": 1})


def test_an_intent_after_the_terminal_resolution_stays_unknown_before_resolve():
    records, outstanding = stream(NOTIFY, resolve=False)
    assert listing(records, outstanding) == [("dispatch_intent", "closed:abandoned_no_effects", ["dispatch_outcome"]),
                                             ("notify_intent", "unknown", ["notify_outcome"])]


def test_an_intent_after_the_terminal_resolution_released_by_resolve_refuses():
    records, outstanding = stream(NOTIFY)                                # resolve() released the post-resolution intent too
    assert outstanding == []
    with pytest.raises(H.Refused) as e:
        H.pending_effects(records, None, outstanding)
    assert e.value.code == "pending_unlistable"


def test_a_partial_release_is_never_accepted():
    records, _ = stream(NOTIFY, resolve=False)
    for partial in ([["notify_outcome"]], [["dispatch_outcome"]]):       # neither the closed-only nor the open-only subset
        with pytest.raises(H.Refused) as e:
            H.pending_effects(records, None, partial)
        assert e.value.code == "pending_unlistable"


@pytest.mark.parametrize("resolution", [
    {"decision": "release", "reconciliationRef": "recon-1"},                       # permission, not terminal
    {"decision": "abandoned_no_effects", "reconciliationRef": "  "},               # invalid: blank ref
    {"decision": "abandoned_no_effects", "reconciliationRef": "r", "invalidPayload": True},
    {"decision": "confirm_act", "observationId": "o1", "reconciliationRef": "r"},  # acts confirmation, not terminal
])
def test_non_terminal_or_invalid_resolutions_never_discharge(resolution):
    records, outstanding = stream(resolution=resolution, resolve=False)
    assert listing(records, outstanding) == [("dispatch_intent", "unknown", ["dispatch_outcome"])]
    with pytest.raises(H.Refused):
        H.pending_effects(records, None, [])                             # an emptied outstanding is NOT accepted


def test_a_later_non_terminal_resolution_cancels_the_discharge():
    records, outstanding = stream(("operator_resolution", {"decision": "release", "reconciliationRef": "r2"}), resolve=False)
    assert listing(records, outstanding) == [("dispatch_intent", "unknown", ["dispatch_outcome"])]
    with pytest.raises(H.Refused):
        H.pending_effects(records, None, [])


def test_an_unresolved_mismatch_still_refuses_and_corruption_and_unconfirmed_are_unchanged():
    records, _ = stream(resolution=None, resolve=False)
    with pytest.raises(H.Refused) as e:
        H.pending_effects(records, None, [])
    assert e.value.code == "pending_unlistable"
    with pytest.raises(H.Refused) as e:
        H.pending_effects(records, "chain:3", [["dispatch_outcome"]])
    assert e.value.code == "pending_unlistable"
    items = H.pending_effects(records, None, [["dispatch_outcome"]], [{"recordId": "r9", "kind": "dispatch_outcome"}])
    assert items == [{"kind": "open_intent", "id": items[0]["id"], "status": "unknown", "awaiting": ["dispatch_outcome"]},
                     {"kind": "commit_unknown", "id": "r9", "status": "unknown", "awaiting": ["dispatch_outcome"]}]
