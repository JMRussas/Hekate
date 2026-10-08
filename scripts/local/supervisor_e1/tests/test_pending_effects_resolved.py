"""Latent defect (root msgs 2228/2232): handoff.pending_effects on a stream CLOSED by an EXISTING terminal operator
resolution (abandoned_no_effects). resolve() clears the stream's `outstanding`, but the records still hold the open
dispatch_intent, so the listing's cross-check raises instead of listing the closed intent. Retained as a strict
xfail regression: it documents today's behaviour and must flip when the defect is fixed. No vocabulary change."""

import pytest

from e1 import handoff as H
from e1.evidence import ModelJournal

ROOT, CK = "root-1", "ck-1"


def closed_prelaunch_stream():
    m = ModelJournal("w#1", now=1.0)
    m.append(ROOT, CK, "claim_intent", {"attemptId": "a1"})
    m.append(ROOT, CK, "claimed", {"outcome": "claimed"})
    m.append(ROOT, CK, "package_ref", {"packageSha256": "p" * 64, "jcs": "{}"})
    m.append(ROOT, CK, "dispatch_intent", {"exec": "e" * 64, "key": {}})
    m.append(ROOT, CK, "operator_resolution", {"decision": "abandoned_no_effects", "reconciliationRef": "recon-1"})
    m.resolve(ROOT, CK)
    s = m.streams[(ROOT, CK)]
    return s.records, s.outstanding


def test_the_fixture_is_a_resolved_stream_with_an_unanswered_dispatch_intent():
    records, outstanding = closed_prelaunch_stream()
    kinds = [r.kind for r in records]
    assert kinds[-2:] == ["dispatch_intent", "operator_resolution"] and "dispatch_outcome" not in kinds
    assert outstanding == []                                             # resolve() cleared the reservation


@pytest.mark.xfail(strict=True, raises=H.Refused, reason="latent defect: a terminally resolved stream's closed intent "
                   "is unlistable (pending_unlistable) instead of listed; fix separately (root 2232)")
def test_pending_effects_lists_an_intent_closed_by_a_terminal_resolution():
    records, outstanding = closed_prelaunch_stream()
    items = H.pending_effects(records, None, outstanding)
    assert any(i["id"].startswith("dispatch_intent@") for i in items)    # listed, never omitted


def test_an_unresolved_mismatch_still_refuses():
    records, _ = closed_prelaunch_stream()
    with pytest.raises(H.Refused) as e:
        H.pending_effects(records[:-1], None, [])                       # no resolution, outstanding disagrees
    assert e.value.code == "pending_unlistable"
