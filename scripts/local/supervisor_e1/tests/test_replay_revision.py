"""Plan 041 §4a (root msg 1748): the out-of-bundle revised-consumer replay check accepts ONLY the one declared
source drift (30ca084a -> aea15fa4) with every frozen case ok, and rejects anything else. Offline."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from e1 import replay_revision as RR

OLD, NEW = RR.REVISION_FROM, RR.REVISION_TO
HOLD, HNEW = RR.HANDOFF_FROM, RR.HANDOFF_TO


def handoff_lines(bundle, hnew=HNEW, hold=HOLD):
    return [bundle.handoff_drift_line.format(new=hnew, old=hold)] if bundle.handoff_drift_line else []


def strict_out(bundle: RR.Bundle, *, new=NEW, old=OLD, ok=None, extra=(), bad=0, verdict="REPLAY FAIL", drift=True,
               handoff=None) -> str:
    lines = [f"ok case-{i}: composed" for i in range(bundle.cases if ok is None else ok)]
    lines += [f"BAD case-x-{i}: refused" for i in range(bad)]
    drifts = ([bundle.drift_line.format(new=new, old=old)] if drift else []) + (handoff_lines(bundle) if handoff is None else handoff)
    lines += drifts + list(extra) + [verdict]
    return "\n".join(lines) + "\n"


def hk(bundle, **over):
    """The handoff keyword arguments a bundle needs: the declared pair, or none for the consumer-only bundle."""
    base = dict(imported_handoff=HNEW, producer_handoff=HOLD) if bundle.handoff_drift_line else {}
    return {**base, **over}


@pytest.fixture(params=sorted(RR.BUNDLES))
def bundle(request) -> RR.Bundle:
    return RR.BUNDLES[request.param]


def test_the_exact_declared_drift_with_every_case_ok_is_accepted(bundle):
    assert RR.evaluate(bundle, strict_out(bundle), imported=NEW, producer_pin=OLD, **hk(bundle)) == []


@pytest.mark.parametrize("case, kw", [
    ("wrong imported consumer", dict(imported="1" * 64)),
    ("frozen consumer imported (no revision)", dict(imported=OLD)),
    ("producer pin not the declared old consumer", dict(producer_pin="2" * 64)),
])
def test_any_other_consumer_or_producer_hash_is_rejected(bundle, case, kw):
    args = {"imported": NEW, "producer_pin": OLD, **kw}
    assert RR.evaluate(bundle, strict_out(bundle, new=args["imported"], old=args["producer_pin"]), **args, **hk(bundle))


@pytest.mark.parametrize("mutate", [
    dict(extra=["PROBLEM hash mismatch: delivery/manifest.bin"]),               # an index/bundle problem
    dict(extra=["PROBLEM implementation drift: e1.handoff 3 != producer handoffSha256 4"]),
    dict(new="3" * 64),                                                        # a drift line naming another hash
    dict(drift=False, verdict="REPLAY PASS"),                                  # no drift at all: not this mode
    dict(drift=False),                                                         # FAIL without the declared drift
    dict(ok=16),                                                               # a case missing
    dict(ok=53),                                                               # an unexpected extra case
    dict(bad=1),                                                               # a case that failed
    dict(verdict="REPLAY PASS"),                                               # the strict verdict relabelled
])
def test_an_extra_problem_or_unexpected_count_or_verdict_is_rejected(bundle, mutate):
    assert RR.evaluate(bundle, strict_out(bundle, **mutate), imported=NEW, producer_pin=OLD, **hk(bundle))


def test_both_frozen_bundles_pass_under_the_declared_revision_and_stay_byte_unchanged():
    """Live: each bundle's own strict replay, unchanged, run against the revised consumer."""
    fixtures = RR.PROJECT / "fixtures"
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(fixtures.rglob("*"))
              if p.is_file() and "__pycache__" not in p.parts}
    for name, b in RR.BUNDLES.items():
        r = RR.run(name)
        assert r["revisedResult"] == "PASS", r["reasons"]
        assert (r["strict"]["exit"], r["strict"]["verdict"]) == (1, "FAIL")           # the strict result stays truthful
        assert r["cases"] == {"ok": b.cases, "expected": b.cases}
        assert r["revision"] == {"from": OLD, "to": NEW, "importedConsumer": NEW, "producerPin": OLD}
        if b.handoff_drift_line:
            assert r["handoffRevision"] == {"from": HOLD, "to": HNEW, "importedHandoff": HNEW, "producerHandoffPin": HOLD}
            assert r["strict"]["problems"] == [b.drift_line.format(new=NEW, old=OLD), b.handoff_drift_line.format(new=HNEW, old=HOLD)]
        else:
            assert "handoffRevision" not in r and r["strict"]["problems"] == [b.drift_line.format(new=NEW, old=OLD)]
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(fixtures.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts}
    assert after == before
    repo = RR.PROJECT.parents[2]
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", str(fixtures)], capture_output=True, text=True)
    assert st.stdout == ""                                                         # no bundle file edited or added


# --- the declared handoff revision (supplement bundle only; root msgs 2299/2300) ---------------------------------------

BC = RR.BUNDLES["e2e-byte-compat-v0"]
GOLDEN = RR.BUNDLES["e2e-consumer-v0"]
PENDING_FIX_HANDOFF = "cfa1a52a654a311b6d1d9b915ba350184160108265d96fac015337d0027c1a47"


@pytest.mark.parametrize("case, kw", [
    ("wrong imported handoff", dict(imported_handoff="3" * 64)),
    ("the intermediate pending-fix handoff imported", dict(imported_handoff=PENDING_FIX_HANDOFF)),
    ("frozen handoff imported (no revision)", dict(imported_handoff=HOLD)),
    ("producer handoff pin not the declared old handoff", dict(producer_handoff="4" * 64)),
    ("producer handoff pin missing", dict(producer_handoff=None)),
])
def test_any_other_handoff_hash_is_rejected(case, kw):
    args = hk(BC, **kw)
    out = strict_out(BC, handoff=[BC.handoff_drift_line.format(new=args["imported_handoff"], old=args["producer_handoff"])])
    assert RR.evaluate(BC, out, imported=NEW, producer_pin=OLD, **args)


@pytest.mark.parametrize("case, lines", [
    ("handoff drift omitted", []),
    ("handoff drift duplicated", [BC.handoff_drift_line.format(new=HNEW, old=HOLD)] * 2),
    ("handoff drift naming another hash", [BC.handoff_drift_line.format(new="5" * 64, old=HOLD)]),
])
def test_an_omitted_extra_or_wrong_handoff_drift_line_is_rejected(case, lines):
    assert RR.evaluate(BC, strict_out(BC, handoff=lines), imported=NEW, producer_pin=OLD, **hk(BC))


def test_a_reordered_drift_list_is_rejected():
    out = strict_out(BC, drift=False, handoff=handoff_lines(BC) + [BC.drift_line.format(new=NEW, old=OLD)])
    assert RR.evaluate(BC, out, imported=NEW, producer_pin=OLD, **hk(BC))


def test_the_golden_bundle_still_accepts_exactly_one_consumer_drift():
    assert GOLDEN.handoff_drift_line is None
    out = strict_out(GOLDEN, handoff=[BC.handoff_drift_line.format(new=HNEW, old=HOLD)])     # a handoff drift there: reject
    assert RR.evaluate(GOLDEN, out, imported=NEW, producer_pin=OLD)
