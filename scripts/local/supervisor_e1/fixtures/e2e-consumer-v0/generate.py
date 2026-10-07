"""Generate the E2e golden consumer bundle (plan 034 rev 3 / 035; TEST-ONLY, offline fixture).

One REAL run on the disposable harness database with the PINNED real H1:
  real claim -> real H1 (the task) -> finish -> review requested, ACK + progress by the lead
  -> E2d prepare (task = the real H1 output) -> commit -> delivery -> one-snapshot Fresh
  -> compose (allow policy, all evidence pointers) and compose (default-deny policy).
It writes the exact bytes, the inputs, the recorded H1 calls and retrieval results, and the expected
outputs, then runs replay.py over the written bundle. Run (port 5108 free, owned container up):

  set HEKATE_E1_CHATAGENT_DIR=<pinned 5255daa checkout>
  uv run python fixtures/e2e-consumer-v0/generate.py

The bundle is a snapshot of ONE generation: its ids are random, so regenerating gives a different
(equally valid) bundle. It grants no authority and invokes nothing.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path[:0] = [str(PROJECT), str(PROJECT / "tests"), str(HERE)]

from e1 import acts as A                                    # noqa: E402
from e1 import consumer as C                                # noqa: E402
from e1 import consumer_durable as CD                       # noqa: E402
from e1 import handoff as H                                 # noqa: E402
from e1.acts_durable import ActsJournal, install_acts       # noqa: E402
from e1.durable import install                              # noqa: E402
from e1.h1_bridge import H1_COMMIT, build as h1_build, chatagent_dir, verify_checkout   # noqa: E402
from e1.handoff_durable import commit, install_handoff, prepare   # noqa: E402
from e1.harness import Harness                              # noqa: E402
from e1.wire import SetupClient, SupervisorClient           # noqa: E402

RULES = [{"path": "AGENTS.md", "revision": "golden-rev", "text": "Golden rule: verify, then report."}]
SYSTEM, FAST, DEEP = "GOLDEN-SYSTEM", "GOLDEN-FAST", "GOLDEN-DEEP"
BUDGET = {"windowTokens": 200000, "maxHistoryTurns": 0, "safetyTokens": 0, "fastOutputTokens": 1000, "deepOutputTokens": 1000}
DESTINATION = "conv-golden-successor"
ALLOW = {"principal": "lead-1", "rules": [{"id": "read-all", "action": "source_read", "match": {}, "allow": True},
                                          {"id": "use-all", "action": "destination_use", "match": {}, "allow": True}]}
DENY = {"principal": "lead-1", "rules": []}
WRONG_DESTINATION = "conv-golden-elsewhere"
# An explicitly SYNTHETIC prior conversation (fixture data, not a production source store): one
# user-stated and one assistant-claimed record, addressed as ChatAgent SourceRefs.
PRIOR_CONVERSATION = "conv-golden-predecessor"
PRIOR = [("e-user-1", "m-user-1", "user-stated", "Please keep the public API unchanged."),
         ("e-asst-1", "m-asst-1", "assistant-claimed", "I already updated the README section on retries.")]
NOTE = "Handing over: review pending; the retry README claim above is unverified."


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def jbytes(obj) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


class Recording(SupervisorClient):
    raw = b""

    def claim(self, *a, **kw):
        r = super().claim(*a, **kw)
        Recording.raw = r.raw
        return r


def main() -> None:
    from test_e2c_live import LS1, LS2, Live, ev          # noqa: E402 (needs the tests path)
    from e2b_support import W, reset_schema               # noqa: E402

    repo = verify_checkout()                              # the pinned H1 checkout, or this errors
    h = Harness()
    h.start()
    try:
        setup = SetupClient(h.base_url)
        opened = []

        def make(bounds=A.E2C_BOUNDS):
            reset_schema(h.dsn)
            install(h.dsn, bounds)
            install_acts(h.dsn)
            return h.dsn

        def writer(now=100.0, **kw):
            aj = ActsJournal(h.dsn, W, now=now, **kw)
            opened.append(aj)
            return aj.open()

        e2c = SimpleNamespace(make=make, writer=writer, dsn=h.dsn)
        L = Live(h, setup, Recording(h.base_url), e2c, now=1000.0)
        install_handoff(L.dsn)
        h1_in = {"response": Recording.raw.decode("utf-8"), "rules": RULES, "systemInstruction": SYSTEM,
                 "roleInstructions": {"fast": FAST, "deep": DEEP}, "budget": BUDGET, "capturedAtIso": "2026-10-07T12:00:00Z"}
        task_h1 = h1_build(h1_in)
        assert task_h1["ok"] is True
        L.finish()
        rk = {**{k: L.key[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art", "lead": "lead-1", "leadSession": LS1}
        assert L.aj.request_review(L.p.root, L.ck, rk).outcome == "accepted"
        L.aj.now = 1100.0
        assert L.intake(L.act("review_acknowledged", 1, key_=rk)).outcome == "accepted"
        L.aj.now = 1200.0
        assert L.intake(L.act("review_progress", 2, 1, ev(1), key_=rk)).outcome == "accepted"
        source_store = {(PRIOR_CONVERSATION, e): {"messageId": mid, "contentHash": H.sha(text), "text": text, "provenance": prov}
                        for e, mid, prov, text in PRIOR}
        imports = [{"ref": {"conversationId": PRIOR_CONVERSATION, "eventId": e, "messageId": mid, "contentHash": H.sha(text)}, "text": text}
                   for e, mid, prov, text in PRIOR]

        def prepare_authorize(principal, kind, target):       # the PREPARE-side stub decision, recorded in the manifest
            return f"golden-stub:{kind}:{principal}"
        pkg = L.package(suppliedSha256=task_h1["suppliedSha256"],
                        instructions={"system": H.sha(SYSTEM), "fast": H.sha(FAST), "deep": H.sha(DEEP)})
        task = {"text": task_h1["text"], "instructions": {"system": SYSTEM, "fast": FAST, "deep": DEEP}, "packageRef": pkg}
        p = prepare(L.aj, L.p.root, L.ck, rk, prepare_id=str(uuid.uuid4()), target=LS2, gate="operator", gate_ref="recon-golden",
                    task=task, conversation_ref="conv-golden-successor", now=L.aj.now, note={"text": NOTE, "author": LS1},
                    imports=imports, source_store=source_store, principal="lead-1", authorize=prepare_authorize,
                    destination=DESTINATION)
        d, receipt = commit(L.aj, p, now=L.aj.now)
        assert d.outcome == "accepted"

        delivery = CD.delivery(L.dsn, p.transition["handoffId"], receipt, h1_in)
        verified = C.verify_delivery(delivery)
        fresh = CD.fresh(L.dsn, verified)
        wanted = [{"seq": e["seq"], "kind": e["kind"]} for e in p.package.manifest["evidenceIndex"]]
        retrieval, h1_calls = [], []
        real_get = CD.retriever(L.dsn, verified)

        def rec_get(pointer):
            out = real_get(pointer)
            retrieval.append({"request": pointer, "result": out})
            return out

        def rec_h1(options):
            out = h1_build(options)
            out.pop("_runtime", None)
            h1_calls.append({"options": options, "result": out})
            return out

        allow = C.PolicyStub(ALLOW["principal"], tuple(ALLOW["rules"]))
        deny = C.PolicyStub(DENY["principal"], tuple(DENY["rules"]))
        valid = C.compose(delivery, fresh, policy=allow, destination=DESTINATION, h1=rec_h1, wanted=wanted, retriever=rec_get)
        denied = C.compose(delivery, fresh, policy=deny, destination=DESTINATION, h1=rec_h1, wanted=wanted, retriever=rec_get)
        elsewhere = C.compose(delivery, fresh, policy=allow, destination=WRONG_DESTINATION, h1=rec_h1, wanted=wanted, retriever=rec_get)
    finally:
        for aj in opened:
            aj.close()
        problems = h.stop()
        if problems:
            print("harness cleanup:", problems)

    files = {
        "delivery/manifest.bin": delivery.manifest, "delivery/envelope.bin": delivery.envelope, "delivery/task.bin": delivery.task,
        "delivery/receipt.json": delivery.receipt, "delivery/h1-input.json": delivery.h1_input,
        "delivery/wrapper.json": jbytes({"wrapper": delivery.wrapper, "codec": delivery.codec, "candidateDigest": delivery.candidate_digest}),
        "inputs/fresh.json": jbytes(fresh.__dict__),
        "inputs/policy-allow.json": jbytes(ALLOW), "inputs/policy-deny.json": jbytes(DENY),
        "inputs/request.json": jbytes({"destination": DESTINATION, "wanted": wanted}),
        "inputs/request-wrong-destination.json": jbytes({"destination": WRONG_DESTINATION, "wanted": wanted}),
        "inputs/prior-source-SYNTHETIC.json": jbytes({"synthetic": True, "note": "fixture data, not a production source store",
                                                      "conversationId": PRIOR_CONVERSATION,
                                                      "records": [{"eventId": e, "messageId": mid, "provenance": prov, "text": text,
                                                                   "contentHash": H.sha(text)} for e, mid, prov, text in PRIOR]}),
        "recorded/retrieval.json": jbytes(retrieval), "recorded/h1-calls.json": jbytes(h1_calls),
        "expected/h1-text.txt": task_h1["text"].encode("utf-8"),
    }
    for name, c in (("valid", valid), ("denied", denied), ("wrong-destination", elsewhere)):
        files[f"expected/{name}/view-part.txt"] = c.part.encode("utf-8")
        files[f"expected/{name}/expected.json"] = jbytes({"viewDigest": c.view_digest, "reservationTokens": c.view["reservationTokens"],
                                                          "viewCost": c.view_cost, "h1SuppliedSha256": c.h1["suppliedSha256"]})
    files["producer.json"] = jbytes({
        "plan": "034 rev 3 (17273a5194ccec681a7b9eca7089db84cf20fe3489e64f3729130059cd2ab9db)", "evidence": "035 (accepted, msg 1328)",
        "hekateBase": "d0ed671 + accepted E2c/E2d/E2e overlay", "consumerSha256": hashlib.sha256((PROJECT / "e1" / "consumer.py").read_bytes()).hexdigest(),
        "h1": {"chatagentCommit": H1_COMMIT, "node": "24.21.0", "checkout": str(chatagent_dir()), "bridge": "e1/h1_bridge.py"},
        "estimator": C.ESTIMATOR_ID, "codec": C.CODEC, "wrapper": C.WRAPPER, "generatedAt": "2026-10-07"})
    for rel, data in files.items():
        write(HERE / rel, data)
    import replay                                           # noqa: E402
    replay.write_index()
    sys.exit(replay.main())


if __name__ == "__main__":
    main()
