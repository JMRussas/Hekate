"""plan-run v0 (plan 042; root msg 1793): drive the task nodes of ONE imported plan, one supervised node at a time.
TEST-SCOPED: P1 runs only against the disposable harness database, with the fake CLI and temporary repos.

The driver keeps NO status. Each iteration starts from ONE authoritative PlanStore read (`GET /plans/{root}`, a
single coherent snapshot) and decides from it alone:

  plan drift / a node in flight / rejected / cancelled  -> STOP needs_operator (classified, nothing claimed)
  every imported task node accepted                     -> all_done (the ONLY success; `no_ready_work` is not it)
  no ready node                                         -> STOP needs_operator, naming each node's blockers
  otherwise the first ready node in sibling order (= PlanStore's own claim order) is EXPECTED, and:
    its content value must pin a spec (pending -> STOP spec_pending), the spec file must still hash to the pin,
    every accepted predecessor's artifact must be an ANCESTOR of the spec's base in the source repo (D3 v0:
    operator-prepared bases; else STOP base_not_chained, before any clone or spend),
    a NEW per-node run root (an existing one = an earlier attempt -> STOP), the existing preflight, then ONE
    existing supervised pilot ATTACHED to that node. Its claim must return exactly that node (else claim_mismatch:
    nothing dispatched) with exactly the pinned content and predecessor artifacts (claim_check).
  a node that does not end `accepted` -> STOP needs_operator; successors stay blocked by PlanStore's own rule.

There is NO automatic integration and NO resume: a re-run re-derives everything from PlanStore and the run
roots, and anything in flight or uncertain stops for an operator instead of being re-claimed.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from e1 import cli_worker as W
from e1 import plan_import as PI
from e1 import task_runner as TR

PLAN_RUN_LOG = "plan-run-{n}.json"


@dataclass
class NodeStep:
    key: str
    node_id: str
    action: str                       # ran | stopped
    outcome: str | None = None
    reason: str | None = None
    run_root: str | None = None
    evidence: str | None = None
    detail: Any = None


@dataclass
class PlanRunResult:
    outcome: str                      # all_done | needs_operator
    reason: str | None
    root: str
    import_sha256: str
    steps: list[NodeStep] = field(default_factory=list)
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)      # the final authoritative view, per key
    detail: Any = None


class _Stop(Exception):
    def __init__(self, reason: str, detail: Any = None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def _ok(resp, what: str) -> dict[str, Any]:
    if resp.status != 200:
        raise _Stop(f"{what}_refused", {"status": resp.status, "code": resp.code})
    return resp.body


def snapshot(plan: PI.ImportedPlan, view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per imported key, from ONE plan view: work, acceptance, readiness, blockers, content and artifact."""
    leaves = {x["nodeId"]: x for x in view["readiness"]["leaves"]}
    by_id = {n["id"]: n for n in view["nodes"]}
    out = {}
    for key, nid in plan.node_ids.items():
        n, r = by_id.get(nid), leaves.get(nid)
        if n is None or r is None:
            raise _Stop("plan_drift", {"missingNode": key})
        out[key] = {"nodeId": nid, "work": n["work"], "acceptance": n["effectiveAcceptance"], "ready": r["ready"],
                    "blockers": [{"reason": b["reason"], "predecessor": plan.key_of(b.get("predecessorId") or "")} for b in r["blockers"]],
                    "value": n["value"], "contentRevision": n["contentRevision"], "siblingOrder": n["siblingOrder"],
                    "artifactRef": n["artifactRef"], "attemptEpoch": n["attemptEpoch"]}
    extra = set(by_id) - set(plan.node_ids.values()) - {plan.root}
    if extra:
        raise _Stop("plan_drift", {"unexpectedNodes": sorted(extra)})
    return out


def root_container(plan: PI.ImportedPlan, view: dict[str, Any]) -> tuple[str, str]:
    """PlanStore's own verdict on the plan root, from the SAME view (readiness.containers)."""
    c = next((x for x in view["readiness"]["containers"] if x["nodeId"] == plan.root), None)
    if c is None:
        raise _Stop("plan_drift", {"rootContainer": "missing"})
    return c["completion"], c["acceptance"]


def classify(state: dict[str, dict[str, Any]], container: tuple[str, str] | None = None) -> tuple[str, Any]:
    """('stop', reason/detail) | ('done', None) | ('next', key). Distinct stop reasons (review 1827): `inflight` (an
    attempt is open), `review_pending` (done, no decision), `acceptance_stale` (accepted, then its content changed).
    `container` is PlanStore's own root verdict (completion, acceptance); done requires it to agree (else plan_drift)."""
    for reason, test in (("inflight", lambda s: s["work"] == "in_progress"),
                         ("review_pending", lambda s: s["work"] == "done" and s["acceptance"] == "none"),
                         ("acceptance_stale", lambda s: s["work"] == "done" and s["acceptance"] == "stale")):
        hit = [k for k, s in state.items() if test(s)]
        if hit:
            return "stop", (reason, {k: {"work": state[k]["work"], "acceptance": state[k]["acceptance"]} for k in hit})
    rejected = [k for k, s in state.items() if s["acceptance"] == "rejected"]
    if rejected:
        return "stop", ("node_rejected", rejected)
    cancelled = [k for k, s in state.items() if s["work"] == "cancelled"]
    if cancelled:
        return "stop", ("node_cancelled", cancelled)
    leaves_done = all(s["work"] == "done" and s["acceptance"] == "accepted" for s in state.values())
    root_done = container == ("complete", "accepted")
    if container is not None and leaves_done != root_done:
        return "stop", ("plan_drift", {"leavesAllAccepted": leaves_done, "rootContainer": list(container)})
    if leaves_done:
        return "done", None
    ready = sorted((s["siblingOrder"], k) for k, s in state.items() if s["ready"])
    if not ready:
        return "stop", ("no_ready_work", {k: s["blockers"] for k, s in state.items() if not (s["work"] == "done" and s["acceptance"] == "accepted")})
    return "next", ready[0][1]


def is_ancestor(repo: str, ancestor: str, rev: str) -> bool:
    """Read-only, in the spec's SOURCE repo: is `ancestor` a commit and an ancestor of `rev`?"""
    run = lambda *a: subprocess.run(["git", "-C", repo, "--no-optional-locks", *a], capture_output=True, text=True,  # noqa: E731
                                    timeout=120, env=W.git_env())
    return (run("cat-file", "-e", f"{ancestor}^{{commit}}").returncode == 0
            and run("merge-base", "--is-ancestor", ancestor, rev).returncode == 0)


def make_claim_check(nid: str, value: str, content_revision: int, preds: dict[str, str]) -> Callable[[dict[str, Any]], Any]:
    """The claimed node must carry EXACTLY the content and predecessor artifacts this iteration verified."""
    def check(receipt: dict[str, Any]) -> Any:
        if receipt.get("nodeId") != nid:
            return {"why": "node", "got": receipt.get("nodeId")}
        snap = receipt.get("contentSnapshot") or {}
        if snap.get("value") != value or receipt.get("contentRevision") != content_revision:
            return {"why": "content_pin", "value": snap.get("value"), "contentRevision": receipt.get("contentRevision")}
        nodes = {n.get("id"): n for n in (receipt.get("prereqSnapshot") or {}).get("nodes") or []}
        for pid, art in preds.items():
            p = nodes.get(pid)
            acc = (p or {}).get("acceptance") or {}
            if p is None or p.get("artifactRef") != art or acc.get("decision") != "accepted":
                return {"why": "prerequisite_pin", "predecessor": pid, "artifactRef": (p or {}).get("artifactRef")}
        return None
    return check


def integration_dir(run_root: Path, key: str) -> Path:
    return Path(run_root) / f"{key}.integration"


def spec_ran(plan: PI.ImportedPlan, run_root: Path, key: str, value: str) -> TR.T.TaskSpec:
    """The frozen spec a node RAN with: its pinned spec, or (a recipe node) the resolved spec of its integration,
    whose provenance must name the same recipe pin."""
    kind, sha, path = PI.parse_node_ref(value)
    if kind == "spec":
        return PI.load_spec(path, sha)
    out = integration_dir(run_root, key)
    prov = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    spec = TR.T.load(out / "spec.resolved.json")
    if prov.get("recipeSha256") != sha or prov.get("resolvedSpecSha256") != spec.sha256:
        raise _Stop("predecessor_provenance", {"node": key})
    return spec


def same_repo(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def original_repo(value: str) -> str:
    """The ORIGINAL source repository a node's work is based on (root msg 1860): a pinned spec's source.repo, or a
    recipe's template.source.repo (never a recipe node's owned integration clone)."""
    from e1 import successor as S
    kind, sha, path = PI.parse_node_ref(value)
    if kind == "spec":
        return PI.load_spec(path, sha).doc["source"]["repo"]
    return S.load_recipe(path, sha)["template"]["source"]["repo"]


def materialize_successor(plan: PI.ImportedPlan, run_root: Path, key: str, sha: str, path: str,
                          state: dict[str, dict[str, Any]]) -> TR.T.TaskSpec:
    """Recipe -> pinned/tamper-checked inputs -> the predecessor bound to its own run -> the owned integration base
    -> the resolved frozen spec. Every failure is a typed stop BEFORE any clone of the node run root or any claim."""
    from e1 import successor as S
    (pk,) = plan.after[key]
    p = state[pk]
    try:
        recipe = S.load_recipe(path, sha)
        if p["acceptance"] != "accepted" or not isinstance(p["artifactRef"], str):
            raise S.SuccessorStop("predecessor_not_accepted", {"predecessor": pk})
        # v1 is SAME-repository lineage only: the recipe must target the predecessor's original repository (msg 1860)
        mine, theirs = recipe["template"]["source"]["repo"], original_repo(p["value"])
        if not same_repo(mine, theirs):
            raise S.SuccessorStop("repo_lineage_mismatch", {"recipeRepo": mine, "predecessorRepo": theirs})
        pred = S.bind_predecessor(Path(run_root) / pk, p["artifactRef"], spec_ran(plan, run_root, pk, p["value"]))
        return S.materialize(recipe, sha, pred, p["artifactRef"], integration_dir(run_root, key), f"plan/{plan.root[:8]}/{key}")
    except S.SuccessorStop as e:
        raise _Stop(e.code, {"node": key, "detail": e.detail}) from None
    except PI.ImportRefused as e:
        raise _Stop("predecessor_spec", {"node": key, "code": e.code}) from None
    except (OSError, ValueError, TR.T.SpecRefused) as e:     # a corrupt/unreadable predecessor record (review 1866 R4)
        raise _Stop("predecessor_evidence", {"node": key, "error": type(e).__name__}) from None


def _write_log(run_root: Path, result: PlanRunResult) -> Path:
    n = 1
    while (run_root / PLAN_RUN_LOG.format(n=n)).exists():
        n += 1
    p = run_root / PLAN_RUN_LOG.format(n=n)
    with open(p, "x", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(dict(asdict(result), writtenAt=datetime.now(timezone.utc).isoformat()), indent=1, sort_keys=True, default=str))
    return p


def run_plan(plan: PI.ImportedPlan, run_root: Path, *, setup, client, aj, executable: tuple[Path, ...], executable_sha256: str,
             execution_kind: str, root_go: str | None, timeouts: tuple[int, int, int] = (1200, 600, 300),
             task_suffix: Callable[[str], str] | None = None) -> PlanRunResult:
    run_root = Path(run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    result = PlanRunResult("needs_operator", None, plan.root, plan.doc.sha256)
    try:
        for _ in range(len(plan.node_ids) + 1):
            v = _ok(setup.plan(plan.root), "plan")
            state = snapshot(plan, v)
            result.nodes = state
            verdict, what = classify(state, root_container(plan, v))
            if verdict == "done":
                result.outcome, result.reason = "all_done", None
                break
            if verdict == "stop":
                raise _Stop(*what)
            key = what
            s = state[key]
            step = NodeStep(key, s["nodeId"], "stopped")
            result.steps.append(step)
            ref = PI.parse_node_ref(s["value"])
            if ref is None:
                raise _Stop("spec_pending", {"node": key})
            kind, sha, path = ref
            node_root = run_root / key
            if kind == "spec":
                try:
                    spec = PI.load_spec(path, sha)
                except PI.ImportRefused as e:
                    raise _Stop("spec_mismatch", {"node": key, "code": e.code, "detail": e.detail}) from None
            else:
                # D3 v1: derive the base from the ONE accepted predecessor, with no operator step (plan 044)
                if node_root.exists():
                    raise _Stop("node_run_root_exists", {"node": key, "path": str(node_root)})
                if integration_dir(run_root, key).exists():
                    # an earlier materialization stopped after creating it: a PRECISE operator stop, the directory is
                    # kept as evidence (automatic archive/retry is deferred, root msg 1901)
                    raise _Stop("integration_exists", {"node": key, "path": str(integration_dir(run_root, key))})
                spec = materialize_successor(plan, run_root, key, sha, path, state)
            base, source = spec.doc["source"]["taskBaseCommit"], spec.doc["source"]["repo"]
            preds = {}
            for pk in plan.after[key]:
                art = state[pk]["artifactRef"]
                if not (state[pk]["acceptance"] == "accepted" and isinstance(art, str) and is_ancestor(source, art, base)):
                    raise _Stop("base_not_chained", {"node": key, "predecessor": pk, "artifactRef": art, "taskBaseCommit": base})
                preds[state[pk]["nodeId"]] = art
            step.run_root = str(node_root)
            if node_root.exists():
                raise _Stop("node_run_root_exists", {"node": key, "path": str(node_root)})
            try:
                TR.preflight(spec, node_root)
            except TR.PreflightRefused as e:
                raise _Stop("preflight_refused", {"node": key, "code": e.code}) from None
            res, ev = TR.run(spec, node_root, executable=executable, executable_sha256=executable_sha256, setup=setup, client=client,
                             aj=aj, project_id=plan.project_id, execution_kind=execution_kind, root_go=root_go,
                             task_suffix=task_suffix(key) if task_suffix else "", timeouts=timeouts,
                             attach=(plan.root, s["nodeId"]),
                             claim_check=make_claim_check(s["nodeId"], s["value"], s["contentRevision"], preds))
            step.action, step.outcome, step.reason = "ran", res.outcome, res.reason
            step.evidence = str(Path(ev["runDir"]) / "evidence.json")
            if res.outcome != "accepted":
                raise _Stop("node_not_accepted", {"node": key, "reason": res.reason, "detail": res.detail})
        else:
            raise _Stop("iteration_bound")
    except _Stop as e:
        result.outcome, result.reason, result.detail = "needs_operator", e.reason, e.detail
    except TR.PreflightRefused as e:
        result.outcome, result.reason, result.detail = "needs_operator", "run_refused", e.code
    except Exception as e:  # noqa: BLE001 -- any unexpected failure is a typed stop; the log still gets written
        result.outcome, result.reason, result.detail = "needs_operator", "unexpected_error", type(e).__name__
    if result.outcome != "all_done":
        try:                                                    # the final authoritative view, for the operator
            result.nodes = snapshot(plan, _ok(setup.plan(plan.root), "plan"))
        except Exception:  # noqa: BLE001
            pass
    _write_log(run_root, result)
    return result
