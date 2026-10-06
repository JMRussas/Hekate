"""Fault injection for the test supervisor (review msg 829). Live, real API.

Lost replies are simulated by performing the REAL call and then raising, so the write is
committed but the supervisor never sees the answer. Every fault must become a structured
needs_operator outcome that retains its evidence, with no automatic second write.
"""

import pytest

from helpers import key, ok
from e1.exact import loads_exact
from e1.supervisor import FakeWorker, Outcome, Supervisor, echo
from e1.wire import Response, SupervisorClient
from test_supervisor_live import db_snapshot, events, make_plan, node_state


class FaultyClient(SupervisorClient):
    def __init__(self, base_url, *, lose_claim_reply=False, lose_finish_reply=False, fail_finish_before_send=False,
                 malformed_claim_5xx=False, fail_plan_read=False):
        super().__init__(base_url)
        self.f = dict(lose_claim_reply=lose_claim_reply, lose_finish_reply=lose_finish_reply,
                      fail_finish_before_send=fail_finish_before_send, malformed_claim_5xx=malformed_claim_5xx,
                      fail_plan_read=fail_plan_read)
        self.calls: list[str] = []

    def claim(self, *a, **kw):
        self.calls.append("claim")
        if self.f["malformed_claim_5xx"]:
            loads_exact(b"<html><body>502 Bad Gateway</body></html>")   # what _send would raise on this body
        r = super().claim(*a, **kw)
        if self.f["lose_claim_reply"]:
            raise TimeoutError("reply lost after commit")
        return r

    def get_plan(self, root):
        self.calls.append("get_plan")
        if self.f["fail_plan_read"]:
            raise OSError("connection reset during read")
        return super().get_plan(root)

    def transition(self, node, payload):
        self.calls.append("transition:" + payload["to"])
        if self.f["fail_finish_before_send"] and payload["to"] == "done":
            raise ConnectionResetError("dropped before the request was sent")
        r = super().transition(node, payload)
        if self.f["lose_finish_reply"] and payload["to"] == "done":
            raise ConnectionResetError("reply lost after commit")
        return r


def counts(harness, root) -> tuple[int, int]:
    """(claim receipts, attempt events) for the plan; compared as deltas from a pre-run baseline."""
    a, b = harness.psql(f"SELECT (SELECT count(*) FROM plan_claim_receipts WHERE root_node_id = '{root}') || '/' || "
                        f"(SELECT count(*) FROM plan_attempt_events WHERE root_node_id = '{root}')").split("/")
    return int(a), int(b)


def test_claim_reply_lost_after_commit_is_uncertain_and_never_reclaimed(harness, setup):
    p = make_plan(harness, setup)
    c = FaultyClient(harness.base_url, lose_claim_reply=True)
    worker = FakeWorker(lambda pkg, run: pytest.fail("must not dispatch"))
    ck = key()
    base_receipts, base_events = counts(harness, p.root)
    r = Supervisor(c, worker).run(p.root, ck, "att", "e1:ref")
    assert (r.outcome, r.reason, r.error) == (Outcome.NEEDS_OPERATOR, "uncertain:claim_reply", "TimeoutError")
    assert r.claim_request == {"root": p.root, "claimKey": ck, "attemptId": "att", "executorRef": "e1:ref", "actor": "supervisor-e1"}
    assert c.calls == ["claim"] and worker.dispatches == []
    assert counts(harness, p.root) == (base_receipts + 1, base_events + 1)   # the claim DID commit; nothing else
    # A later run with the retained request sees a replay: still no dispatch, no second claim.
    again = Supervisor(SupervisorClient(harness.base_url), worker).run(p.root, ck, "att", "e1:ref")
    assert again.reason == "replayed_claim" and counts(harness, p.root) == (base_receipts + 1, base_events + 1)


def test_malformed_5xx_claim_reply_is_uncertain_with_request_retained(harness, setup):
    p = make_plan(harness, setup)
    c = FaultyClient(harness.base_url, malformed_claim_5xx=True)
    before = counts(harness, p.root)
    r = Supervisor(c, FakeWorker(lambda pkg, run: pytest.fail("must not dispatch"))).run(p.root, key(), "att", None)
    assert (r.outcome, r.reason, r.error) == (Outcome.NEEDS_OPERATOR, "uncertain:claim_reply", "WireError")
    assert r.claim_request is not None and r.envelope is None
    assert counts(harness, p.root) == before                            # nothing was sent


def test_finish_reply_lost_after_commit_keeps_the_held_finish_and_writes_nothing_more(harness, setup):
    p = make_plan(harness, setup)
    c = FaultyClient(harness.base_url, lose_finish_reply=True)
    s = Supervisor(c, FakeWorker(lambda pkg, run: echo(pkg, run, artifact_ref="sha-lost")))
    r = s.run(p.root, key(), "att", None)
    assert (r.outcome, r.reason, r.error) == (Outcome.NEEDS_OPERATOR, "uncertain:finish_reply", "ConnectionResetError")
    assert r.held_finish is not None and r.package is not None and r.run is not None and r.result is not None and r.finish is None
    assert c.calls.count("transition:done") == 1                        # no automatic second write
    assert node_state(setup, p.root, p.target)["work"] == "done"        # it had committed
    c.f["lose_finish_reply"] = False                                     # the network is back
    resp, why = s.replay_held_finish(r)                                  # operator-driven check: our finish is the last op
    assert why == "replayed" and ok(resp)["outcome"] == "unchanged"
    assert [e["kind"] for e in events(setup, p.target)] == ["attempt_started", "attempt_finished"]


def test_finish_dropped_before_send_is_unconfirmed_and_the_helper_refuses_to_resend(harness, setup):
    p = make_plan(harness, setup)
    c = FaultyClient(harness.base_url, fail_finish_before_send=True)
    s = Supervisor(c, FakeWorker(lambda pkg, run: echo(pkg, run)))
    r = s.run(p.root, key(), "att", None)
    assert (r.outcome, r.reason) == (Outcome.NEEDS_OPERATOR, "uncertain:finish_reply") and r.held_finish is not None
    before = db_snapshot(harness, p.root)
    resp, why = s.replay_held_finish(r)
    assert (resp, why) == (None, "reconcile:finish_unconfirmed")        # distinct from an intervening mutation
    assert node_state(setup, p.root, p.target)["work"] == "in_progress" and db_snapshot(harness, p.root) == before


def test_failed_precondition_read_is_needs_operator_without_finish(harness, setup):
    p = make_plan(harness, setup)
    c = FaultyClient(harness.base_url, fail_plan_read=True)
    r = Supervisor(c, FakeWorker(lambda pkg, run: echo(pkg, run))).run(p.root, key(), "att", None)
    assert (r.outcome, r.reason, r.error) == (Outcome.NEEDS_OPERATOR, "read_failed", "OSError")
    assert r.held_finish is None and not any(x.startswith("transition") for x in c.calls)
    assert node_state(setup, p.root, p.target)["work"] == "in_progress"


def test_worker_exception_retains_package_and_run(harness, setup):
    p = make_plan(harness, setup)

    def boom(pkg, run):
        raise ValueError("worker exploded")
    r = Supervisor(SupervisorClient(harness.base_url), FakeWorker(boom)).run(p.root, key(), "att", None)
    assert (r.outcome, r.reason, r.error) == (Outcome.NEEDS_OPERATOR, "uncertain:worker_raised", "ValueError")
    assert r.package is not None and r.run is not None and r.result is None
    assert [e["kind"] for e in events(setup, p.target)] == ["attempt_started"]
