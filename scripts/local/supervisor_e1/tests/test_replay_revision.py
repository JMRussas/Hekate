"""Plan 041 §4a (root msg 1748): the out-of-bundle revised-consumer replay check accepts ONLY the one declared
source drift (30ca084a -> aea15fa4) with every frozen case ok, and rejects anything else. Offline."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from e1 import replay_revision as RR

OLD, NEW = RR.REVISION_FROM, RR.REVISION_TO


def strict_out(bundle: RR.Bundle, *, new=NEW, old=OLD, ok=None, extra=(), bad=0, verdict="REPLAY FAIL", drift=True) -> str:
    lines = [f"ok case-{i}: composed" for i in range(bundle.cases if ok is None else ok)]
    lines += [f"BAD case-x-{i}: refused" for i in range(bad)]
    lines += ([bundle.drift_line.format(new=new, old=old)] if drift else []) + list(extra) + [verdict]
    return "\n".join(lines) + "\n"


@pytest.fixture(params=sorted(RR.BUNDLES))
def bundle(request) -> RR.Bundle:
    return RR.BUNDLES[request.param]


def test_the_exact_declared_drift_with_every_case_ok_is_accepted(bundle):
    assert RR.evaluate(bundle, strict_out(bundle), imported=NEW, producer_pin=OLD) == []


@pytest.mark.parametrize("case, kw", [
    ("wrong imported consumer", dict(imported="1" * 64)),
    ("frozen consumer imported (no revision)", dict(imported=OLD)),
    ("producer pin not the declared old consumer", dict(producer_pin="2" * 64)),
])
def test_any_other_consumer_or_producer_hash_is_rejected(bundle, case, kw):
    args = {"imported": NEW, "producer_pin": OLD, **kw}
    assert RR.evaluate(bundle, strict_out(bundle, new=args["imported"], old=args["producer_pin"]), **args)


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
    assert RR.evaluate(bundle, strict_out(bundle, **mutate), imported=NEW, producer_pin=OLD)


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
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(fixtures.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts}
    assert after == before
    repo = RR.PROJECT.parents[2]
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", str(fixtures)], capture_output=True, text=True)
    assert st.stdout == ""                                                         # no bundle file edited or added
