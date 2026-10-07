"""E2e opt-in live (plan 034 rev 3 §4, §7): the PINNED real H1 (ChatAgent 5255daa, Node 24.21.0) on a
REAL claim response; its exact output is the E2d task AT PREPARE; after commit, the consumer re-runs
the same H1 with a reduced window and binds it. Selected with `uv run pytest interop_live`; a missing
pinned prerequisite or container ERRORS this suite (never skips) and never affects the default suite."""

import uuid

import pytest

from e1 import acts as A
from e1 import consumer as C
from e1 import consumer_durable as CD
from e1 import handoff as H
from e1.acts_durable import ActsJournal, install_acts
from e1.durable import install
from e1.h1_bridge import build as h1_build
from e1.handoff_durable import commit, install_handoff, prepare
from e1.wire import SupervisorClient
from e2b_support import W, reset_schema
from test_e2c_live import LS1, LS2, Live

RULES = [{"path": "AGENTS.md", "revision": "e2e-rev", "text": "E2e rule: verify, then report."}]
SYSTEM, FAST, DEEP = "E2E-SYSTEM", "E2E-FAST", "E2E-DEEP"
BUDGET = {"windowTokens": 200_000, "maxHistoryTurns": 0, "safetyTokens": 0, "fastOutputTokens": 1000, "deepOutputTokens": 1000}
POLICY = C.PolicyStub("lead-1", ({"id": "read", "action": "source_read", "match": {}, "allow": True},
                                 {"id": "use", "action": "destination_use", "match": {}, "allow": True}))


class Recording(SupervisorClient):
    """Keeps the RAW claim response bytes for H1 (nothing else changes)."""
    raw: bytes = b""

    def claim(self, *a, **kw):
        r = super().claim(*a, **kw)
        Recording.raw = r.raw
        return r


@pytest.fixture
def e2c(harness):
    from types import SimpleNamespace
    opened = []

    def make(bounds=A.E2C_BOUNDS):
        reset_schema(harness.dsn)
        install(harness.dsn, bounds)
        install_acts(harness.dsn)
        return harness.dsn

    def writer(now=100.0, **kw):
        aj = ActsJournal(harness.dsn, W, now=now, **kw)
        opened.append(aj)
        return aj.open()
    yield SimpleNamespace(make=make, writer=writer, dsn=harness.dsn)
    for aj in opened:
        aj.close()


def h1_options(raw: bytes, budget=BUDGET):
    return {"response": raw.decode("utf-8"), "rules": RULES, "systemInstruction": SYSTEM,
            "roleInstructions": {"fast": FAST, "deep": DEEP}, "budget": budget, "capturedAtIso": "2026-10-07T12:00:00Z"}


def handed_with_real_h1(harness, setup, e2c):
    client = Recording(harness.base_url)
    L = Live(harness, setup, client, e2c, now=1000.0)
    install_handoff(L.dsn)
    h1 = h1_build(h1_options(Recording.raw))                           # the REAL H1 on the REAL claim bytes, BEFORE prepare
    assert h1["ok"] is True and h1["context"]["messages"] == [{"role": "user", "content": h1["text"]}]
    assert h1["context"]["memoryIsNull"] is True and h1["context"]["optionalCounts"] == [0, 0, 0, 0, 0, 0]
    L.finish()
    rk = {**{k: L.key[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art", "lead": "lead-1", "leadSession": LS1}
    assert L.aj.request_review(L.p.root, L.ck, rk).outcome == "accepted"
    pkg = L.package(suppliedSha256=h1["suppliedSha256"],
                    instructions={"system": H.sha(SYSTEM), "fast": H.sha(FAST), "deep": H.sha(DEEP)})
    task = {"text": h1["text"], "instructions": {"system": SYSTEM, "fast": FAST, "deep": DEEP}, "packageRef": pkg}
    p = prepare(L.aj, L.p.root, L.ck, rk, prepare_id=str(uuid.uuid4()), target=LS2, gate="operator", gate_ref="recon-e2e",
                task=task, conversation_ref="conv-successor", now=L.aj.now)
    d, rec = commit(L.aj, p, now=L.aj.now)
    assert d.outcome == "accepted"
    return L, p, rec, h1


def compose(L, p, rec, budget=BUDGET):
    d = CD.delivery(L.dsn, p.transition["handoffId"], rec, h1_options(Recording.raw, budget))
    fr = CD.fresh(L.dsn, C.verify_delivery(d))
    return C.compose(d, fr, policy=POLICY, destination="conv-successor", h1=h1_build)


def test_real_h1_is_the_task_at_prepare_and_binds_at_the_consumer(harness, setup, e2c):
    L, p, rec, h1 = handed_with_real_h1(harness, setup, e2c)
    assert p.package.manifest["task"]["suppliedSha256"] == h1["suppliedSha256"]
    c = compose(L, p, rec)
    assert c.h1["text"] == h1["text"] and c.h1["context"]["messages"] == [{"role": "user", "content": h1["text"]}]
    assert c.h1["context"]["optionalCounts"] == [0, 0, 0, 0, 0, 0]          # H1 stays exactly one message, no optional context
    assert c.h1["_runtime"]["h1Commit"] == "5255daacfc670a4919f61439eb12adcb6a401920" and "v24.21.0" in c.h1["_runtime"]["version"]


def test_reduced_window_reaches_the_real_h1_boundary(harness, setup, e2c):
    L, p, rec, h1 = handed_with_real_h1(harness, setup, e2c)
    c = compose(L, p, rec)
    probe = h1_build(h1_options(Recording.raw, dict(BUDGET, windowTokens=1)))
    assert probe["ok"] is False and probe["code"] == "CONTEXT_TOO_LARGE"
    fixed = probe["estimatedInputTokens"]                                    # the real H1 fixed cost
    # The view carries the budget, so its cost depends on the digits of windowTokens: measure the view
    # AT the edge's own digit count, then step to the exact edge (msg: every emitted byte is charged).
    guess = c.view_cost + fixed + BUDGET["fastOutputTokens"]
    at_guess = compose(L, p, rec, dict(BUDGET, windowTokens=guess))
    edge = at_guess.view_cost + fixed + BUDGET["fastOutputTokens"]           # exactly enough for view + H1
    assert len(str(edge)) == len(str(edge - 1)) == len(str(guess))
    assert compose(L, p, rec, dict(BUDGET, windowTokens=edge)).h1["ok"] is True
    with pytest.raises(C.Refused) as e:
        compose(L, p, rec, dict(BUDGET, windowTokens=edge - 1))
    assert e.value.code == "CONTEXT_TOO_LARGE"


def test_an_h1_shaped_candidate_is_refused_by_the_real_h1(harness, setup, e2c):
    """A candidate prepared from H1-SHAPED data is never bound to real H1 output (task_mismatch)."""
    from test_e2d_live import Review
    client = Recording(harness.base_url)
    r = Review(harness, setup, client, e2c)
    p = prepare(r.L.aj, r.L.p.root, r.L.ck, r.rk, prepare_id=str(uuid.uuid4()), target=LS2, gate="operator", gate_ref="x",
                task=dict(r.task, instructions={"system": SYSTEM, "fast": FAST, "deep": DEEP}), conversation_ref="c", now=r.L.aj.now)
    d, rec = commit(r.L.aj, p, now=r.L.aj.now)
    dl = CD.delivery(r.L.dsn, p.transition["handoffId"], rec, h1_options(Recording.raw))
    with pytest.raises(C.Refused) as e:
        C.compose(dl, CD.fresh(r.L.dsn, C.verify_delivery(dl)), policy=POLICY, destination="c", h1=h1_build)
    assert e.value.code == "task_mismatch"

