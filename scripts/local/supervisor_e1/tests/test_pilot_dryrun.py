"""HK-ISSUE-006 pilot DRY RUN (TEST-ONLY, disposable harness database): the accepted primitives composed
into one supervised loop with a simulated worker and a simulated independent reviewer. No process,
CLI or model is launched; nothing is committed. Fix rounds use the HK-ISSUE-005 dry-run workaround."""

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from e1 import pilot as P
from e1.acts import E2C_BOUNDS
from e1.acts_durable import ActsJournal, install_acts
from e1.durable import install
from e1.handoff_durable import install_handoff
from e2b_support import reset_schema
from helpers import key, ok

PROJECT = Path(__file__).resolve().parents[1]
REPO = PROJECT.parents[2]                                   # the Hekate checkout: read-only git queries only

# The accepted, frozen modules this driver composes; it must not change any of them.
FROZEN = {
    "e1/acts_durable.py": "5ad500d623e8e85f8a107e7846bde1ac810cf8ce3242fa9b3402c70dce684a44",
    "e1/acts_schema.sql": "cbe901cba3970e08e4edc83eecc829ad40d130fa70f03e411fb056f378cccf7b",
    "e1/consumer.py": "30ca084a4712560aec2975b79e5d71d0febb107bc4868a154b0cbcc2c0e61a6e",
    "e1/consumer_durable.py": "d21fa3d15c1938646ff21240ee8bd0a52743b0253cae0b583404bfcdae60993e",
    "e1/handoff.py": "1e9036f6ed29983c1282dde0a3a256094984e2c6a9a3d8940a575beeea6faab2",
    "e1/handoff_durable.py": "b6c1d9c3095c4bfba5bae865d5cf1dc4646d4c53e452ab3a95ab4c3ca5006670",
    "e1/handoff_schema.sql": "affc532042b3399f24884358114b415847ab256f76d5eb183a05d2f14c223159",
    "e1/durable.py": "55eef956fc7ec3a90d9e2cc57749e25809b945bade111ed65aa939370c82a6b1",
    "e1/supervisor.py": "697b0bd31694431c26ba1b202717ed5b43f79351cf9d875e637d3eb4eda8e5f0",
}

# Formerly frozen modules intentionally revised by plan 038 (HK-ISSUE-012, root decision). Provenance:
# the accepted pre-038 hashes were acts.py 1d44e39c...80e5 and evidence.py f8affab9...cbde (recovery.py
# was not pinned here). The post-038 values MUST be filled from real sha256sum output after root V&V
# and copied into plan 038; None fails the check below instead of pinning a guessed value.
REVISED_038: dict[str, str | None] = {
    "e1/acts.py": "ffa11dd506b5195fc0db6e813ec74bb9a97ef91d6e7027082436b8989b6bf97b",
    "e1/evidence.py": "db8b7a791fd07a73b42b9a47613f3fb9c5689020c8cfedda37a4ddd3af0f74f7",
    "e1/recovery.py": "c1af35bd00ddc5d8f28d30fa228862db7c79a0822e68d2099c31faf0be849fc6",
}


def cfg(harness, tmp_path, **over) -> P.PilotConfig:
    base = dict(repo=REPO, base="HEAD", workspace=tmp_path, project_id=harness.project_id if harness else
                "00000000-0000-4000-8000-000000000000", task_text="Add a greeting function.", criteria="tests pass")
    base.update(over)
    return P.PilotConfig(**base)


class Verdicts:
    """A scripted INDEPENDENT reviewer: it only receives the ReviewOrder (the verified view)."""

    def __init__(self, *decisions):
        self.decisions, self.orders = list(decisions), []

    def __call__(self, order: P.ReviewOrder) -> P.Verdict:
        self.orders.append(order)
        d = self.decisions.pop(0)
        return P.Verdict(d, f"round {order.round}: {d} after checking {order.criteria} on {order.artifact_ref}")


class Recording:
    def __init__(self, inner=P.dry_worker):
        self.inner, self.orders = inner, []

    def __call__(self, order: P.WorkOrder) -> P.WorkReport:
        self.orders.append(order)
        return self.inner(order)


@pytest.fixture
def pilot(harness, setup, client, tmp_path):
    opened = []

    def make(reviewer, worker=P.dry_worker, **over):
        reset_schema(harness.dsn)
        install(harness.dsn, E2C_BOUNDS)
        install_acts(harness.dsn)
        install_handoff(harness.dsn)
        aj = ActsJournal(harness.dsn, f"pilot#{key()[:8]}", now=1000.0).open()
        opened.append(aj)
        return P.Pilot(P.resolve(cfg(harness, tmp_path, **over)), setup, client, aj, worker=worker, reviewer=reviewer)
    yield make
    for aj in opened:
        aj.close()


def node(p: P.Pilot) -> dict:
    return p._node()


# ------------------------------------------------------------------------ configuration (no writes)

@pytest.mark.parametrize("over, code", [
    ({"max_rounds": 0}, "config_rounds"), ({"max_rounds": 4}, "config_rounds"), ({"max_rounds": True}, "config_rounds"),
    ({"worker": "Bad Name"}, "config_principal"), ({"run_id": "UPPER"}, "config_run_id"), ({"criteria": " "}, "config_task"),
    ({"project_id": "nope"}, "config_project"), ({"base": "no-such-revision-xyz"}, "config_base"),
])
def test_config_is_refused_before_any_write(tmp_path, over, code):
    with pytest.raises(P.PilotRefused) as e:
        P.resolve(cfg(None, tmp_path, **over))
    assert e.value.code == code and list(tmp_path.iterdir()) == []


def test_repo_and_workspace_are_checked(tmp_path):
    with pytest.raises(P.PilotRefused) as e:
        P.resolve(cfg(None, tmp_path, repo=tmp_path))                     # not a git work tree
    assert e.value.code == "config_repo"
    with pytest.raises(P.PilotRefused) as e:
        P.resolve(cfg(None, tmp_path, workspace=tmp_path / "missing"))
    assert e.value.code == "config_workspace"
    (tmp_path / "pilot-taken").mkdir()
    with pytest.raises(P.PilotRefused) as e:
        P.resolve(cfg(None, tmp_path, run_id="taken"))                    # never reuse a run directory
    assert e.value.code == "config_workspace"


def test_base_resolves_to_a_full_commit(tmp_path):
    r = P.resolve(cfg(None, tmp_path))
    assert P.SHA40.fullmatch(r.base_sha) and r.run_dir.parent == tmp_path and not r.run_dir.exists()


# ------------------------------------------------------------------------ the loop

def test_single_round_accept(pilot):
    rv = Verdicts("accepted")
    p = pilot(rv)
    res = p.run()
    assert (res.outcome, res.reason, res.detail, len(res.rounds)) == ("accepted", None, None, 1)
    r = res.rounds[0]
    assert rv.orders[0].artifact_ref == r.artifact_ref and r.artifact_ref in rv.orders[0].view_part
    n = node(p)
    assert (n["work"], n["attemptEpoch"], n["artifactRef"]) == ("done", r.attempt_epoch, r.artifact_ref)
    assert n["acceptance"]["decision"] == "accepted" and n["acceptance"]["evidenceRef"] == r.evidence_ref


def test_reject_then_fix_round_then_accept(pilot, setup):
    """HK-ISSUE-012 fixed by plan 038 (review_state); fix rounds still use the HK-ISSUE-005 dry-run workaround."""
    rv, wk = Verdicts("rejected", "accepted"), Recording()
    p = pilot(rv, worker=wk)
    res = p.run()
    assert (res.outcome, res.reason, res.detail, len(res.rounds)) == ("accepted", None, None, 2)
    r1, r2 = res.rounds
    # a new claimKey (a new stream) and a new attempt epoch per round
    assert r1.claim_key != r2.claim_key and r2.attempt_epoch == r1.attempt_epoch + 1 and r1.artifact_ref != r2.artifact_ref
    # the explicit cross-round link, in the run record AND in the round-2 work package text
    assert r2.previous == {"claimKey": r1.claim_key, "attemptEpoch": r1.attempt_epoch, "artifactRef": r1.artifact_ref,
                           "decision": "rejected", "evidenceRef": r1.evidence_ref}
    assert r1.claim_key in wk.orders[1].task_text and r1.evidence_ref in wk.orders[1].task_text
    # round 2 got its own request, E2d prepare/commit, verified + revalidated (Fresh review_class candidate) view and decision
    assert r2.handoff_id and r2.handoff_id != r1.handoff_id and r2.view_digest and r2.decision == "accepted"
    assert [o.artifact_ref for o in rv.orders] == [r1.artifact_ref, r2.artifact_ref] and r2.artifact_ref in rv.orders[1].view_part
    n = node(p)
    assert (n["work"], n["attemptEpoch"], n["artifactRef"], n["effectiveAcceptance"]) == ("done", r2.attempt_epoch, r2.artifact_ref, "accepted")
    assert n["acceptance"]["attemptEpoch"] == r2.attempt_epoch and n["acceptance"]["evidenceRef"] == r2.evidence_ref
    # audit history keeps round 1's decision (PlanStore unchanged, plan 012)
    decided = [e for e in ok(setup.events(p.leaf))["events"] if e["kind"] == "decision_recorded"]
    assert [(e["attemptEpoch"], e["decision"]) for e in decided] == [(r1.attempt_epoch, "rejected"), (r2.attempt_epoch, "accepted")]


def test_an_older_accepted_decision_is_never_revived(pilot, setup, harness):
    """A round-1 ACCEPTED decision, then a new attempt: the node is a fresh review candidate (not decided),
    and the old approval does not satisfy a dependent Accepted gate until round 2 is accepted."""
    p = pilot(Verdicts("accepted"))
    res = p.run()
    assert res.outcome == "accepted"
    r1 = res.rounds[0]
    succ = str(uuid.uuid4())
    ok(setup.add_child(p.root, succ, "successor", 1, key(), 2))       # root revision: 1 after create, 2 after the pilot leaf
    ok(setup.add_dependency(succ, p.leaf, key(), 0))                    # default gate: Accepted
    assert leaf(setup, p.root, succ)["ready"] is True                                     # round 1 accepted: gate holds
    # a new attempt (reopen, Done -> InProgress) and its finish
    ok(setup.transition(p.leaf, {"to": "in_progress", "attemptId": "round-2", "operationKey": key(),
                                 "expectedStateRevision": node(p)["stateRevision"], "actor": "e1-setup"}))
    ok(setup.transition(p.leaf, {"to": "done", "attemptId": "round-2", "attemptEpoch": r1.attempt_epoch + 1, "artifactRef": "sha-round-2",
                                 "operationKey": key(), "expectedStateRevision": node(p)["stateRevision"], "actor": "e1-setup"}))
    n = node(p)
    assert n["acceptance"]["decision"] == "accepted" and n["acceptance"]["attemptEpoch"] == r1.attempt_epoch    # record kept
    assert n["effectiveAcceptance"] == "stale"
    blockers = leaf(setup, p.root, succ)["blockers"]
    assert [b["reason"] for b in blockers] == ["predecessor_acceptance_stale"]          # old approval does not satisfy the gate
    row = node_row(harness, p.leaf)
    rk2 = {"rootId": p.root, "nodeId": p.leaf, "attemptId": "round-2", "attemptEpoch": r1.attempt_epoch + 1, "artifactRef": "sha-round-2"}
    from e1 import acts as A
    from e1.evidence import derive_review
    from e1.recovery import snapshot_review
    assert (A.review_state(row, rk2), snapshot_review(row)) == ("candidate", "candidate")
    assert derive_review(n, leaf(setup, p.root, p.leaf)).review_class == "candidate"


def leaf(setup, root, node_id) -> dict:
    return next(x for x in ok(setup.plan(root))["readiness"]["leaves"] if x["nodeId"] == node_id)


def node_row(harness, node_id) -> dict:
    """The plan_node_state row exactly as the journal's read_facts reads it."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(harness.dsn, row_factory=dict_row) as c:
        r = c.execute("SELECT row_to_json(s)::text AS j FROM public.plan_node_state s WHERE node_id = %s", (node_id,)).fetchone()
    return json.loads(r["j"])


def test_a_stale_prior_round_decision_is_rejected_and_nothing_is_reset(pilot, setup):
    p = pilot(Verdicts("rejected", "accepted"))
    res = p.run()
    r1 = res.rounds[0]
    before = node(p)
    stale = setup.decide(p.leaf, {"decision": "accepted", "reviewedContentRevision": before["contentRevision"],
                                  "reviewedArtifactRef": r1.artifact_ref, "reviewedAttemptEpoch": r1.attempt_epoch,
                                  "evidenceRef": r1.evidence_ref, "operationKey": key(),
                                  "expectedStateRevision": before["stateRevision"], "actor": "lead-1"})
    assert stale.status != 200 and stale.code in ("stale_artifact", "stale_attempt")
    after = node(p)
    assert after == before


def test_max_rounds_stops_for_an_operator_without_resetting(pilot):
    p = pilot(Verdicts("rejected"), max_rounds=1)
    res = p.run()
    assert (res.outcome, res.reason, len(res.rounds)) == ("needs_operator", "max_rounds", 1)
    n = node(p)
    assert (n["work"], n["acceptance"]["decision"], n["attemptEpoch"]) == ("done", "rejected", res.rounds[0].attempt_epoch)


@pytest.mark.parametrize("status, reason", [("unknown", "launch_uncertain"), ("failed", "worker_failed")])
def test_an_uncertain_or_failed_launch_fails_closed(pilot, status, reason):
    p = pilot(Verdicts(), worker=lambda order: P.WorkReport(status))
    res = p.run()
    assert (res.outcome, res.reason, len(res.rounds)) == ("needs_operator", reason, 1)
    n = node(p)
    assert n["work"] == "in_progress" and n.get("artifactRef") is None                        # no finish
    assert any(i["kind"] == "open_intent" and i["id"].startswith("dispatch_intent@") for i in res.pending)


def test_an_artifact_not_bound_to_base_and_attempt_fails_closed(pilot):
    p = pilot(Verdicts(), worker=lambda order: P.WorkReport("ok", "0" * 40))
    res = p.run()
    assert (res.outcome, res.reason) == ("needs_operator", "artifact_not_bound") and node(p)["work"] == "in_progress"


def test_the_reviewer_sees_only_the_verified_fresh_view(pilot):
    rv = Verdicts("accepted")
    p = pilot(rv)
    res = p.run()
    o, r = rv.orders[0], res.rounds[0]
    assert o.candidate_digest == r.candidate_digest and o.view_digest == r.view_digest and o.view_digest != o.candidate_digest
    assert set(vars(o)) == {"round", "artifact_ref", "view_part", "view_digest", "candidate_digest", "criteria"}


def test_the_run_log_is_the_only_file_written(pilot, tmp_path):
    p = pilot(Verdicts("rejected", "accepted"))
    res = p.run()
    files = [f.relative_to(tmp_path).as_posix() for f in tmp_path.rglob("*") if f.is_file()]
    assert files == [f"pilot-{p.cfg.run_id}/run.json"]
    doc = json.loads((tmp_path / files[0]).read_text(encoding="utf-8"))
    assert doc["dryRun"] is True and doc["outcome"] == "accepted" and len(doc["rounds"]) == 2 and "HK-ISSUE-005" in doc["workaround"]
    assert doc["reason"] is None and doc["detail"] is None and doc["rounds"][1]["decision"] == "accepted"
    assert doc["rounds"][1]["previous"]["claimKey"] == res.rounds[0].claim_key


def test_frozen_accepted_modules_are_unchanged():
    got = {rel: hashlib.sha256((PROJECT / rel).read_bytes()).hexdigest() for rel in FROZEN}
    assert got == FROZEN


def test_plan_038_revised_modules_match_their_recorded_revision():
    got = {rel: hashlib.sha256((PROJECT / rel).read_bytes()).hexdigest() for rel in REVISED_038}
    missing = sorted(rel for rel, want in REVISED_038.items() if want is None)
    assert not missing, f"plan 038 hashes not yet recorded (fill from sha256sum after V&V): { {r: got[r] for r in missing} }"
    assert got == REVISED_038
