"""Managed role host (e1/role_supervisor.py): one injected read-only role under the EXISTING Supervisor, offline.
Fake BaseChatModel + a fake SupervisorClient serving the real claim fixture + the in-memory ModelJournal + tmp_path files.
No network, Docker, services or paid model. Needs the optional `roles` group (see ROLE-SUPERVISOR.md)."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from e1 import role_evidence as RE  # noqa: E402
from e1 import role_supervisor as RS  # noqa: E402
from e1 import role_worker as RW  # noqa: E402
from e1.evidence import RECORD_MAX_BYTES, JournalRefused, ModelJournal  # noqa: E402
from e1.supervisor import Outcome  # noqa: E402
from e1.wire import Response, SupervisorClient  # noqa: E402
from test_role_worker import GOOD, SNAPSHOT, FakeChat  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[4] / "context-store" / "plans" / "fixtures" / "023" / "claim-claimed.json"
KEY, ATT, EXEC = "claim-1", "k-att", "run-k"
SRC = "a" * 40
OBSERVED = "2026-10-08T12:00:00Z"
FULL = ["claim_intent", "claimed", "launch_intent", "launched", "exited", "result_captured", "finish_intent", "finish_outcome"]


def claim_doc(**receipt) -> dict[str, Any]:
    doc = json.loads(FIXTURE.read_bytes())
    doc["receipt"]["claimKey"] = KEY       # the fixture key contains "~", which the evidence identity does not admit
    doc["receipt"].update(receipt)
    return doc


ROOT, NODE = claim_doc()["receipt"]["rootId"], claim_doc()["receipt"]["nodeId"]


def resp(doc, status=200) -> Response:
    raw = json.dumps(doc).encode()
    return Response(status, raw, doc)


class FakePlan(SupervisorClient):
    """Serves the real claim fixture; records every call. It has NO decide/accept method (the real client has none)."""

    def __init__(self, *, replayed=False, reject_finish=False, doc=None):
        super().__init__("http://127.0.0.1:1")
        self.doc = doc or claim_doc()
        self.doc["replayed"] = replayed
        self.reject_finish = reject_finish
        self.node = {"id": NODE, "work": "in_progress", "attemptId": ATT, "attemptEpoch": 1, "stateRevision": 5}
        self.calls: list[str] = []
        self.finishes: list[dict[str, Any]] = []
        self.reread: dict[str, Any] | None = None     # a different receipt served to the pre-finish re-read

    def claim(self, *a, **kw):
        self.calls.append("claim")
        return resp(self.doc)

    def get_claim(self, root, claim_key):
        self.calls.append("get_claim")
        return resp(self.reread or self.doc)

    def get_plan(self, root):
        self.calls.append("get_plan")
        return resp({"rootId": ROOT, "nodes": [dict(self.node)]})

    def transition(self, node, payload):
        self.calls.append("transition:" + payload["to"])
        self.finishes.append(payload)
        if self.reject_finish:
            return resp({"code": "stale_revision"}, 409)
        self.node.update(work="done")
        return resp({"outcome": "applied"})


class HookChat(FakeChat):
    on_call: Any = None

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.on_call:
            self.on_call()
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


class Hook:
    """The journal hook already bound to (root, claimKey), as a host would pass it."""

    def __init__(self, refuse=(), degrade=()):
        self.mj, self.refuse, self.degrade = ModelJournal("w"), set(refuse), set(degrade)

    def __call__(self, kind, data):
        if kind in self.refuse:
            raise JournalRefused("injected")
        rec = self.mj.append(ROOT, KEY, kind, data)
        return SimpleNamespace(degraded=True) if kind in self.degrade else rec

    @property
    def records(self):
        s = self.mj.streams.get((ROOT, KEY))
        return s.records if s else []

    def kinds(self):
        return [r.kind for r in self.records]

    def one(self, kind):
        (rec,) = [r for r in self.records if r.kind == kind]
        return rec.payload


def host(tmp_path, *, reply=None, model=None, **over):
    tmp_path.mkdir(parents=True, exist_ok=True)
    model = model or HookChat(reply=json.dumps(GOOD) if reply is None else reply)
    kw: dict[str, Any] = dict(client=FakePlan(), journal=Hook(), role=RW.athena_role(), model=model,
                              snapshot=SNAPSHOT, task_id=NODE, source_revision=SRC, observed_at=OBSERVED,
                              run_dir=tmp_path / "run", model_identity="fake-model-1")
    kw.update(over)
    return RS.RoleSupervisor(**kw), kw["client"], kw["journal"], model


def go(h):
    return h.run(ROOT, KEY, ATT, EXEC)


def no_effect(r, client, model):
    assert r.outcome is Outcome.NEEDS_OPERATOR and model.calls == []
    assert not [c for c in client.calls if c.startswith("transition")]


def test_success_native_trace_exact_manifest_and_one_finish_to_done(tmp_path):
    snap = json.loads(json.dumps(SNAPSHOT))
    h, client, hook, model = host(tmp_path, snapshot=snap)
    snap["evidence"][0]["text"] = "mutated after construction"
    r = go(h)
    assert (r.outcome, r.reason) == (Outcome.FINISHED, "finished")
    assert hook.kinds() == FULL                                     # exactly one of each, no duplicate model-only record
    assert all(rec.size <= RECORD_MAX_BYTES for rec in hook.records)
    assert client.calls.count("claim") == 1 and [c for c in client.calls if c.startswith("transition")] == ["transition:done"]
    # one model call with the exact prompt: system prompt + canonical snapshot incl. the reserved receipt item
    assert len(model.calls) == 1
    sys_msg, user_msg = model.calls[0]
    run_dir = tmp_path / "run"
    prompt = (run_dir / "attempt-r1.prompt.txt").read_bytes()
    assert prompt == b"[system]\n" + sys_msg.content.encode() + b"\n\n[user]\n" + user_msg.content.encode()
    shown = json.loads(user_msg.content)
    assert [e["id"] for e in shown["evidence"]] == ["ev-1", "ev-2", RS.RESERVED_EVIDENCE_ID]
    assert shown["evidence"][0]["text"] == SNAPSHOT["evidence"][0]["text"]               # the immutable copy
    pinned = shown["evidence"][2]
    assert pinned["pinnedContent"] == r.envelope.receipt.content_snapshot_norm and pinned["taskId"] == NODE
    # native launch linkage: trace ref BEFORE the model, honest in-process label, no pid / CLI facts
    launch, exited = hook.one("launch_intent"), hook.one("exited")
    assert launch["executionKind"] == "langchain-role" and launch["inProcess"] is True and "modelOnly" not in launch
    assert launch["trace"] == {"version": "hekate-attempt-trace.v0", "executionKind": "langchain-role", "runDir": str(run_dir),
                               "prompt": "attempt-r1.prompt.txt", "trace": "attempt-r1.trace.jsonl"}
    assert "pid" not in json.dumps(hook.one("launched")) and hook.one("launched")["inProcess"] is True
    trace_bytes = (run_dir / "attempt-r1.trace.jsonl").read_bytes()
    assert exited["exitCode"] == 0 and exited["inProcess"] is True and exited["trace"]["complete"] is True
    assert exited["trace"]["trace"]["sha256"] == hashlib.sha256(trace_bytes).hexdigest()
    assert exited["trace"]["prompt"]["sha256"] == hashlib.sha256(prompt).hexdigest()
    records = [json.loads(line) for line in trace_bytes.decode("ascii").splitlines()]
    assert [json.loads(x["text"]) for x in records if x["stream"] == "stdout"] == [GOOD]     # validated output only
    assert {x["stream"] for x in records} == {"stdout", "hekate"}
    # exact manifest, host-asserted, bound to the receipt, the plan node's stateRevision (5, never eventSeq 4) and files
    manifest = (run_dir / "role-manifest.json").read_bytes()
    digest = hashlib.sha256(manifest).hexdigest()
    assert client.finishes[0]["artifactRef"] == f"role-manifest:sha256:{digest}" == r.result.artifact_ref
    ident = json.loads(manifest)["identity"]
    assert (ident["linkage"], ident["stateRevision"], ident["claimKey"], ident["runId"]) == ("host_asserted", 5, KEY, r.run.run_id)
    assert ident["source"]["snapshotSha256"] == hashlib.sha256(user_msg.content.encode()).hexdigest()
    files = {p.name: p.read_bytes() for p in run_dir.iterdir() if p.name != "role-manifest.json"}
    assert set(files) == {"attempt-r1.prompt.txt", "attempt-r1.trace.jsonl", "role-result.json"}
    assert RE.verify_manifest(manifest, files, expected=ident, max_file_bytes=RS.MAX_FILE_BYTES,
                              max_total_bytes=RS.MAX_TOTAL_BYTES, expected_digest=digest).ok
    result_doc = json.loads(files["role-result.json"])
    assert (result_doc["reviewStatus"], result_doc["accepted"], result_doc["output"]) == ("pending", False, GOOD)
    # no review/accept write exists: the only write is the finish, and the journal has no review record
    assert not {"review_requested", "review_assigned"} & set(hook.kinds()) and not hasattr(client, "decide")


def test_replayed_claim_never_calls_the_model_again(tmp_path):
    h, client, hook, model = host(tmp_path, client=FakePlan(replayed=True))
    r = go(h)
    assert r.reason == "replayed_claim"
    no_effect(r, client, model)
    assert hook.kinds() == ["claim_intent", "claimed"]
    again, *_ = host(tmp_path, client=client, journal=Hook(), model=model, run_dir=tmp_path / "run2")
    assert go(again).reason == "replayed_claim" and model.calls == []


def test_an_instance_runs_once(tmp_path):
    h, client, hook, model = host(tmp_path)
    assert go(h).outcome is Outcome.FINISHED
    again = go(h)
    assert again.reason == "host_refused:already_used" and len(model.calls) == 1 and client.calls.count("claim") == 1


def test_stale_receipt_attempt_state_or_rejected_finish_never_finishes(tmp_path):
    h, client, _, model = host(tmp_path / "a")
    client.reread = claim_doc(contentDigest="0" * 64)
    assert go(h).reason == "receipt_changed" and len(model.calls) == 1 and client.finishes == []

    client = FakePlan()
    h, *_ = host(tmp_path / "b", client=client, model=HookChat(reply=json.dumps(GOOD), on_call=lambda: client.node.update(attemptEpoch=2)))
    assert go(h).reason == "attempt_not_current" and client.finishes == []

    client = FakePlan()
    h, _, hook, _ = host(tmp_path / "c", client=client, model=HookChat(reply=json.dumps(GOOD), on_call=lambda: client.node.update(stateRevision=6)))
    r = go(h)
    assert r.outcome is Outcome.NEEDS_OPERATOR and r.reason == "non_success:nonzero_exit" and client.finishes == []
    assert r.result.structured_result == {"status": "failed", "code": "state_changed"}
    assert hook.one("exited")["reason"] == "state_changed" and hook.one("result_captured")["candidate"] is False

    h, client, hook, _ = host(tmp_path / "d", client=FakePlan(reject_finish=True))
    r = go(h)
    assert (r.outcome, r.reason) == (Outcome.NEEDS_OPERATOR, "finish_rejected:409:stale_revision") and len(client.finishes) == 1
    assert hook.kinds() == FULL                                      # evidence kept; nothing retried or released


def test_invalid_host_config_is_refused_before_any_claim(tmp_path):
    bad = [dict(source_revision="main"), dict(journal=None), dict(observed_at="2026-10-08"), dict(model_identity="a b"),
           dict(model_identity="vendor/model"), dict(task_id="nope"), dict(run_dir=Path("relative")), dict(model=object()),
           dict(binding_id="other"), dict(snapshot={"evidence": []}), dict(client=object())]
    for over in bad:
        with pytest.raises(RS.RoleHostRefused):
            host(tmp_path, **over)
    (tmp_path / "taken").mkdir()
    with pytest.raises(RS.RoleHostRefused) as e:
        host(tmp_path, run_dir=tmp_path / "taken")
    assert e.value.code == "run_dir_not_fresh"


def test_run_arguments_and_fresh_dir_are_checked_before_the_claim(tmp_path):
    h, client, hook, model = host(tmp_path)
    r = h.run(ROOT, "key~with-tilde", ATT, EXEC)                    # valid for PlanStore, not an admissible evidence id
    assert r.reason == "host_refused:identity_invalid" and client.calls == [] and hook.kinds() == []
    h, client, hook, model = host(tmp_path, run_dir=tmp_path / "late")
    (tmp_path / "late").mkdir()                                     # appears between construction and run
    r = go(h)
    assert r.reason == "host_refused:run_dir_not_fresh"
    no_effect(r, client, model)
    assert client.calls == [] and hook.kinds() == [] and list((tmp_path / "late").iterdir()) == []


def test_symlink_parent_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    try:
        os.symlink(real, tmp_path / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(RS.RoleHostRefused) as e:
        host(tmp_path, run_dir=tmp_path / "link" / "run")
    assert e.value.code == "run_dir_link"


def test_wrong_target_or_reserved_id_collision_stops_before_the_model(tmp_path):
    other = "11111111-1111-4111-8111-111111111111"
    h, client, hook, model = host(tmp_path / "a", task_id=other)
    r = go(h)
    assert r.reason == "package_refused:wrong_target"
    no_effect(r, client, model)
    assert hook.kinds() == ["claim_intent", "claimed"]
    snap = {"evidence": [{"id": RS.RESERVED_EVIDENCE_ID}]}
    h, client, hook, model = host(tmp_path / "b", snapshot=snap)
    r = go(h)
    assert r.reason == "package_refused:evidence_id_collision"
    no_effect(r, client, model)


def test_plan_drift_before_the_model_is_not_released(tmp_path):
    client = FakePlan()
    client.node["work"] = "todo"
    h, _, hook, model = host(tmp_path, client=client)
    r = go(h)
    assert r.reason == "package_refused:plan_drift"
    no_effect(r, client, model)
    assert "transition:todo" not in client.calls


@pytest.mark.parametrize("over,reason,claims", [
    (dict(refuse={"claim_intent"}), "journal_refused:claim_intent", 0),
    (dict(refuse={"launch_intent"}), "journal_refused:launch_intent", 1),
    (dict(degrade={"launch_intent"}), "journal_refused:launch_intent", 1),
    (dict(refuse={"launched"}), "outcome_unrecorded:launched", 1),
    (dict(degrade={"launched"}), "outcome_unrecorded:launched", 1),
])
def test_journal_refusal_or_degradation_prevents_model_and_finish(tmp_path, over, reason, claims):
    h, client, hook, model = host(tmp_path, journal=Hook(**over))
    r = go(h)
    assert r.reason.startswith(reason)
    no_effect(r, client, model)
    assert client.calls.count("claim") == claims
    assert "exited" not in hook.kinds()


@pytest.mark.parametrize("kind", ["exited", "result_captured", "finish_intent"])
def test_later_journal_failure_never_finishes(tmp_path, kind):
    h, client, hook, model = host(tmp_path, journal=Hook(refuse={kind}))
    r = go(h)
    assert r.outcome is Outcome.NEEDS_OPERATOR and client.finishes == []
    if kind == "finish_intent":
        assert r.reason == "journal_refused:finish_intent"


def test_degraded_exited_record_stops_before_finish(tmp_path):
    h, client, _, _ = host(tmp_path, journal=Hook(degrade={"exited"}))
    r = go(h)
    assert r.reason.startswith("outcome_unrecorded:exited") and client.finishes == []


def test_result_file_failure_is_not_a_success(tmp_path):
    run_dir = tmp_path / "run"
    h, client, hook, model = host(tmp_path, model=HookChat(reply=json.dumps(GOOD), on_call=lambda: (run_dir / "role-result.json").write_bytes(b"x")))
    r = go(h)
    assert r.outcome is Outcome.NEEDS_OPERATOR and client.finishes == []
    assert (run_dir / "role-result.json").read_bytes() == b"x"        # never overwritten
    assert hook.one("exited")["exitCode"] == RS.EXIT_HOST_FAILED and hook.one("exited")["trace"]["complete"] is False
    assert not (run_dir / "role-manifest.json").exists()


@pytest.mark.parametrize("model_kw,code", [
    (dict(reply="not json at all"), RW.MALFORMED_OUTPUT),
    (dict(reply=json.dumps({**GOOD, "steps": [{"title": "t", "acceptance": "a", "evidence": ["ev-404"]}]})), RW.INVENTED_EVIDENCE),
    (dict(reply="x", boom=True), RW.UNAVAILABLE),
])
def test_invalid_output_or_outage_keeps_evidence_and_never_finishes(tmp_path, model_kw, code):
    h, client, hook, model = host(tmp_path, model=HookChat(**model_kw))
    r = go(h)
    assert r.outcome is Outcome.NEEDS_OPERATOR and r.reason == "non_success:nonzero_exit" and client.finishes == []
    assert r.result.structured_result == {"status": "failed", "code": code}
    exited = hook.one("exited")
    assert exited["exitCode"] == RS.EXIT_ROLE_FAILED and exited["reason"] == code and exited["trace"]["complete"] is True
    assert hook.one("result_captured")["candidate"] is False and hook.one("result_captured")["artifactRef"] is None
    run_dir = tmp_path / "run"
    assert not (run_dir / "role-result.json").exists() and not (run_dir / "role-manifest.json").exists()
    retained = " ".join(p.read_text(encoding="utf-8") for p in run_dir.iterdir()) + json.dumps([x.data for x in hook.records])
    assert "sk-SECRET" not in retained and f"role_failed:{code}" in retained


def test_interruption_closes_the_trace_and_leaves_the_attempt_in_progress(tmp_path):
    def stop():
        raise asyncio.CancelledError()
    h, client, hook, _ = host(tmp_path, model=HookChat(reply="x", on_call=stop))
    with pytest.raises(asyncio.CancelledError):
        go(h)
    assert client.finishes == [] and "transition:todo" not in client.calls
    assert hook.kinds() == ["claim_intent", "claimed", "launch_intent", "launched"]
    final = h._trace.final
    assert final["complete"] is False
    assert "trace_incomplete:interrupted" in (tmp_path / "run" / "attempt-r1.trace.jsonl").read_text(encoding="ascii")


def test_trace_cap_or_write_error_cannot_succeed(tmp_path, monkeypatch):
    class Capped(RS.AttemptTrace):
        def stdout_line(self, raw):
            self.capped.add("stdout")
    monkeypatch.setattr(RS, "AttemptTrace", Capped)
    h, client, hook, _ = host(tmp_path)
    r = go(h)
    assert r.outcome is Outcome.NEEDS_OPERATOR and client.finishes == []
    assert r.result.structured_result["code"] == "trace_incomplete" and hook.one("exited")["exitCode"] == RS.EXIT_HOST_FAILED
    assert hook.one("exited")["trace"]["trace"]["capped"] is True


def test_corrupted_manifest_is_refused_by_own_verification(tmp_path, monkeypatch):
    real = RS._write_new

    def corrupt(path, data):
        real(path, data + b" " if path.name == RS.MANIFEST_FILE else data)
    monkeypatch.setattr(RS, "_write_new", corrupt)
    h, client, hook, _ = host(tmp_path)
    r = go(h)
    assert r.outcome is Outcome.NEEDS_OPERATOR and client.finishes == []
    assert r.result.structured_result["code"].startswith("manifest_unverified")
    assert (tmp_path / "run" / "role-manifest.json").exists()        # evidence preserved


def test_journal_record_sizes_fit_with_a_long_run_dir(tmp_path):
    deep = tmp_path / ("d" * 16) / ("e" * 16)
    deep.mkdir(parents=True)
    h, client, hook, _ = host(tmp_path, run_dir=deep / "run")
    r = go(h)
    assert r.outcome is Outcome.FINISHED and all(x.size <= RECORD_MAX_BYTES for x in hook.records)
