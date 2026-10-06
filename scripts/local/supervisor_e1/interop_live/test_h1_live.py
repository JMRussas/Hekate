"""One live case: a REAL raw claim response from the disposable-DB Api, through ChatAgent's
actual H1 package, to a fake worker and a finish. Proves non-fixture bytes end to end."""

import hashlib

from e1.h1_package import h1_builder
from e1.supervisor import FakeWorker, Outcome, Supervisor, echo
from test_supervisor_live import events, key, make_plan, node_state

RULES = [{"path": "AGENTS.md", "revision": "live-rev", "text": "Live rule: do the task exactly as specified."}]


def test_real_claim_bytes_through_h1_to_finish(harness, setup, client):
    p = make_plan(harness, setup)
    seen = []
    b = h1_builder(rules=RULES, system_instruction="LIVE-SYSTEM", role_fast="F", role_deep="D",
                   budget={"windowTokens": 200_000, "maxHistoryTurns": 0, "safetyTokens": 0,
                           "fastOutputTokens": 1000, "deepOutputTokens": 1000},
                   captured_at_iso="2026-10-06T12:00:00Z")
    s = Supervisor(client, FakeWorker(lambda pkg, run: (seen.append(pkg), echo(pkg, run, artifact_ref="sha-live"))[1]),
                   package_builder=b)
    ck = key()
    r = s.run(p.root, ck, "att-live", "e1:live")
    assert (r.outcome, r.reason) == (Outcome.FINISHED, "finished"), r.reason
    pkg = seen[0]
    assert pkg.supplied_sha256 == hashlib.sha256(pkg.text.encode("utf-8")).hexdigest()
    assert (pkg.node_id, pkg.claim_key, pkg.attempt_epoch) == (p.target, ck, 1)
    assert "target spec" in pkg.text and "tests pass" in pkg.text and RULES[0]["text"] in pkg.text
    assert pkg.content_digest == r.envelope.receipt.content_digest and pkg.content_digest.isupper()
    assert '"version": "v24.21.0"' in pkg.runtime
    assert node_state(setup, p.root, p.target)["work"] == "done"
    assert [e["kind"] for e in events(setup, p.target)] == ["attempt_started", "attempt_finished"]
