"""Prepared-plan CLI (root msg 1847): ONE entrypoint that feeds a prepared plan-import.v0 file to the EXISTING
plan-run v0 primitives (plan_import, plan_run, task_runner). TEST-SCOPED: the disposable harness only.

  validate  parse the plan and load/hash-check every pinned spec. NO harness, clone, install, test or spawn.
  run       validate everything first, then ONE disposable harness: import the plan and drive it with run_plan.

It adds no engine: no clone, npm, verifier or claim logic lives here, and it never touches a live database,
pushes, or edits the source checkout. Before any effect `run` requires an explicit plan file, a NEW run root
(an existing one is an earlier attempt: the in-flight fence), the executable and its sha256, --launch-real-model
and --root-go. `--exe-arg` (offline tests only) runs the FAKE CLI and labels the whole result fake.

The disposable harness drops its PlanStore database at exit, so a needs_operator stop is NOT resumable by this
CLI. A recipe node (D3 v1) derives its base within the run and needs no operator; a `spec_pending` node
(plan-run v0, operator-prepared bases) needs its predecessor's accepted artifact
integrated and its spec frozen, then a run of the completed plan in a NEW run root. Exit codes: 0 all_done,
1 needs_operator (or an error after the harness started), 2 refused before any effect.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from e1 import plan_import as PI
from e1 import plan_run as PR
from e1 import pilot_real as R

PROG = "plan_cli"
NOT_RESUMABLE = ("the disposable harness dropped its PlanStore database: this run cannot be resumed; "
                 "prepare what the stop names, then run the plan again in a NEW run root")


class Refused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(code)
        self.code, self.detail = code, detail


def load_plan(path: Path) -> tuple[bytes, PI.ImportDoc, dict[str, Any]]:
    """The plan file, parsed and with every pinned spec loaded and hash-checked (no effect)."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise Refused("plan_unreadable", str(path)) from None
    try:
        doc = PI.parse(raw)
        specs = PI.validate(doc)
    except PI.ImportRefused as e:
        raise Refused(e.code, e.detail) from None
    return raw, doc, specs


def summary(raw: bytes, doc: PI.ImportDoc, specs: dict[str, Any]) -> dict[str, Any]:
    """Per node: its predecessors, its spec reference as declared, and a pinned spec's scope and bounds."""
    declared = {n["key"]: n["spec"] for n in json.loads(raw)["nodes"]}       # already validated by PI.parse
    nodes = []
    for n in doc.nodes:
        ref = declared[n.key]
        item: dict[str, Any] = {"key": n.key, "name": n.name, "after": list(n.after)}
        if ref is None:
            item["spec"] = "pending"
        elif "recipe" in ref:
            item["spec"] = {"recipe": ref["recipe"]}
        else:
            d = specs[n.key].doc
            item["spec"] = {"path": ref["path"], "sha256": ref["sha256"], "issue": d["metadata"]["issue"],
                            "taskBaseCommit": d["source"]["taskBaseCommit"], "allow": [x["path"] for x in d["allow"]],
                            "verifySteps": [s["name"] for s in d["verify"]["steps"]],
                            "bounds": {k: d["worker"][k] for k in ("model", "budgetUsd", "maxRounds", "maxTurns")}}
        nodes.append(item)
    pending = [x["key"] for x in nodes if x["spec"] == "pending"]
    return {"plan": doc.title, "importSha256": doc.sha256, "nodes": nodes, "pendingSpec": pending,
            "note": ("a pending node stops the run with spec_pending (operator-prepared base); " + NOT_RESUMABLE)
            if pending else None}


def check_run_inputs(a: argparse.Namespace) -> tuple[Path, tuple[Path, ...]]:
    """Everything `run` needs, checked before the harness starts; returns (run root, executable argv)."""
    if a.run_root is None:
        raise Refused("run_root_required")
    if not (a.launch_real_model and a.root_go and a.root_go.strip() and a.exe and a.exe_sha256):
        raise Refused("run_needs", "--exe --exe-sha256 --launch-real-model --root-go")
    run_root = a.run_root.resolve()
    if run_root.exists() and getattr(a, "store", "harness") != "local":       # local: a BOUND run root may continue
        raise Refused("run_root_exists", str(run_root))
    command = (a.exe.resolve(),) + ((a.exe_arg.resolve(),) if a.exe_arg else ())
    for p in command:
        if not p.is_file():
            raise Refused("executable_missing", str(p))
    if R.sha256_file(command[-1]) != a.exe_sha256:
        raise Refused("executable_hash_mismatch", str(command[-1]))
    return run_root, command


def result_json(res: PR.PlanRunResult, *, fake: bool, root_go: str, run_root: Path) -> dict[str, Any]:
    logs = sorted(run_root.glob("plan-run-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    out: dict[str, Any] = {
        "outcome": res.outcome, "reason": res.reason, "detail": res.detail, "root": res.root, "importSha256": res.import_sha256,
        "executionKind": "fake-cli" if fake else "claude-cli", "rootGo": root_go, "runRoot": str(run_root),
        "planRunLog": str(logs[-1]) if logs else None,
        "steps": [{"key": s.key, "action": s.action, "outcome": s.outcome, "reason": s.reason, "runRoot": s.run_root,
                   "evidence": s.evidence} for s in res.steps],
        "nodes": {k: {"work": s["work"], "acceptance": s["acceptance"], "ready": s["ready"], "blockers": s["blockers"],
                      "artifactRef": s["artifactRef"]} for k, s in res.nodes.items()},
    }
    if fake:
        out["fake"] = "FAKE CLI worker (offline test hook): no model ran; nothing here is real-model evidence"
    if res.outcome != "all_done":
        out["resumable"], out["note"] = False, NOT_RESUMABLE
    return out


def run(a: argparse.Namespace, raw: bytes, run_root: Path, command: tuple[Path, ...],
        harness_factory: Callable[[], Any]) -> tuple[int, dict[str, Any]]:
    from e1.acts import E2C_BOUNDS
    from e1.acts_durable import ActsJournal, install_acts
    from e1.durable import install
    from e1.handoff_durable import install_handoff
    from e1.wire import SetupClient, SupervisorClient
    fake = a.exe_arg is not None
    try:
        h = harness_factory()
        h.start()                       # a failed start cleans up what it created itself (Harness.start)
    except Exception as e:  # noqa: BLE001 -- e.g. the port is in use: a typed result, never a traceback
        first = (str(e).splitlines() or [""])[0]
        return 1, {"outcome": "needs_operator", "reason": "harness_unavailable", "detail": f"{type(e).__name__}: {first}"[:200]}
    out: dict[str, Any] = {"outcome": "needs_operator", "reason": "unexpected_error"}
    ok = False
    try:
        install(h.dsn, E2C_BOUNDS)
        install_acts(h.dsn)
        install_handoff(h.dsn)
        setup = SetupClient(h.base_url)
        try:
            plan = PI.import_plan(setup, h.project_id, raw)
        except PI.ImportRefused as e:
            out = {"outcome": "needs_operator", "reason": "import_refused", "detail": {"code": e.code, "detail": e.detail}}
            return 1, out
        aj = ActsJournal(h.dsn, f"plan-cli#{uuid.uuid4().hex[:8]}", now=1000.0).open()
        try:
            res = PR.run_plan(plan, run_root, setup=setup, client=SupervisorClient(h.base_url), aj=aj, executable=command,
                              executable_sha256=a.exe_sha256, execution_kind="fake-cli" if fake else "claude-cli",
                              root_go=a.root_go)
        finally:
            aj.close()
        out = result_json(res, fake=fake, root_go=a.root_go, run_root=run_root)
        ok = res.outcome == "all_done"
    except Exception as e:  # noqa: BLE001 -- any failure after the harness started is a typed stop
        out = {"outcome": "needs_operator", "reason": "unexpected_error", "detail": type(e).__name__}
    finally:
        problems = h.stop(keep_work=not ok)
        if problems:
            out["harness"] = problems
    return (0 if ok else 1), out


RESUMABLE_LOCAL = ("the local coordinator database keeps the PlanStore state: fix what the stop names (e.g. pin a pending "
                   "spec), then run the SAME plan file again with --store local in the SAME run root (bound; no re-import; "
                   "accepted nodes are skipped; in-flight or uncertain work stops)")


BINDING = "plan.binding.json"
BOUND_PLAN = "plan.import.json"


def check_binding(a: argparse.Namespace, raw: bytes, run_root: Path) -> tuple[str, dict[str, str]]:
    """--store local run-root binding (root msg 1901), checked BEFORE any effect (no database access):
    a NEW run root is a first run; an existing one must carry a binding equal to {marker, projectId, planRoot,
    importSha256} of THIS store and THIS plan file (byte-identical: an edited file is a new plan, plan_changed)."""
    import hashlib
    from e1 import local_store as LS
    try:
        loc = LS.Locator.read(a.state_dir)
    except LS.LocalStoreRefused as e:
        raise Refused(e.code, e.detail) from None
    want = {"marker": loc.marker, "projectId": loc.project_id,
            "planRoot": PI.identities(PI.parse(raw), loc.project_id).root, "importSha256": hashlib.sha256(raw).hexdigest()}
    if not run_root.exists():
        return "first", want
    try:
        got = json.loads((run_root / BINDING).read_text(encoding="utf-8"))
        bound = (run_root / BOUND_PLAN).read_bytes()
    except (OSError, ValueError):
        raise Refused("run_root_unbound", str(run_root)) from None
    if got.get("importSha256") != want["importSha256"]:
        raise Refused("plan_changed", {"bound": got.get("importSha256"), "given": want["importSha256"]})
    if got != want or hashlib.sha256(bound).hexdigest() != want["importSha256"]:
        raise Refused("binding_mismatch", str(run_root / BINDING))
    return "continue", want


def run_local(a: argparse.Namespace, raw: bytes, run_root: Path, command: tuple[Path, ...],
              opener: Callable[[Path], Any] | None = None, mode: str = "first",
              binding: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """--store local (plan 043 rev 3): OPEN the coordinator's own marked database (never create or adopt one),
    verify it before any dispatch, run with the named operator surface and a real clock, stop the Api, keep the DB."""
    from e1 import local_store as LS
    try:
        store = (opener or LS.LocalStore.open)(a.state_dir)
    except LS.LocalStoreRefused as e:
        return 2, {"refused": e.code, "detail": e.detail}
    fake = a.exe_arg is not None
    out: dict[str, Any] = {"outcome": "needs_operator", "reason": "unexpected_error"}
    code = 1
    try:
        try:
            setup, client, aj = store.session(actor=a.actor, plan_bytes=raw)
        except LS.LocalStoreRefused as e:                # e.g. uncertain operator acts: classify first, nothing ran
            return 1, {"outcome": "needs_operator", "reason": e.code, "detail": e.detail}
        try:
            try:
                if mode == "continue":                  # the SAME plan file in its bound run root: verify, NO write
                    plan = PI.attach_plan(setup, store.loc.project_id, raw)
                else:
                    plan = PI.import_plan(setup, store.loc.project_id, raw)
                    run_root.mkdir(parents=True)        # bind only AFTER a successful import, exclusively
                    with open(run_root / BOUND_PLAN, "xb") as f:
                        f.write(raw)
                    with open(run_root / BINDING, "x", encoding="utf-8", newline="\n") as f:
                        f.write(json.dumps(binding, indent=1, sort_keys=True))
            except PI.ImportRefused as e:
                return 1, {"outcome": "needs_operator", "reason": "import_refused", "detail": {"code": e.code, "detail": e.detail}}
            res = PR.run_plan(plan, run_root, setup=setup, client=client, aj=aj, executable=command,
                              executable_sha256=a.exe_sha256, execution_kind="fake-cli" if fake else "claude-cli", root_go=a.root_go)
        finally:
            aj.close()
        out = result_json(res, fake=fake, root_go=a.root_go, run_root=run_root)
        out["store"] = {"kind": "local", "db": store.loc.db, "projectId": store.loc.project_id}
        if res.outcome != "all_done":
            out["resumable"], out["note"] = True, RESUMABLE_LOCAL
        code = 0 if res.outcome == "all_done" else 1
    except Exception as e:  # noqa: BLE001 -- any failure after the store opened is a typed stop
        out = {"outcome": "needs_operator", "reason": "unexpected_error", "detail": type(e).__name__}
    finally:
        store.stop()
    return code, out


def main(argv: list[str], *, harness_factory: Callable[[], Any] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=PROG, description="Validate or run a prepared plan-import.v0 plan (disposable harness).")
    ap.add_argument("command", choices=("validate", "run"))
    ap.add_argument("--plan", required=True, type=Path, help="the plan-import.v0 JSON file")
    ap.add_argument("--run-root", type=Path, help="run: a NEW directory for this run's clones, evidence and log")
    ap.add_argument("--exe", type=Path, help="run: the worker executable (the claude CLI)")
    ap.add_argument("--exe-arg", type=Path, help="offline tests only: the FAKE CLI script run by --exe")
    ap.add_argument("--exe-sha256", help="run: sha256 of the executable (of --exe-arg when given)")
    ap.add_argument("--launch-real-model", action="store_true", help="run: required acknowledgement that a model is launched")
    ap.add_argument("--root-go", help="run: the root GO reference recorded with the evidence")
    ap.add_argument("--store", choices=("harness", "local"), default="harness",
                    help="run: harness = a disposable database (default); local = the coordinator's own persistent database")
    ap.add_argument("--state-dir", type=Path, help="run --store local: the coordinator state dir holding its locator")
    ap.add_argument("--actor", default="operator:local", help="run --store local: the operator LABEL recorded with each act")
    a = ap.parse_args(argv)
    try:
        raw, doc, specs = load_plan(a.plan)
        info = summary(raw, doc, specs)
        if a.command == "validate":
            print(json.dumps(dict(info, validate="ok", effects="none: no harness, clone, install, test or spawn"), indent=1))
            return 0
        run_root, command = check_run_inputs(a)
        if a.store == "local" and a.state_dir is None:
            raise Refused("state_dir_required", "--store local needs --state-dir")
        mode, binding = check_binding(a, raw, run_root) if a.store == "local" else ("first", None)
    except Refused as e:
        print(json.dumps({"refused": e.code, "detail": e.detail}, default=str))
        return 2
    print(json.dumps(dict(info, run="starting", runRoot=str(run_root), store=a.store), indent=1), flush=True)
    if a.store == "local":
        code, out = run_local(a, raw, run_root, command, mode=mode, binding=binding)
        print(json.dumps(out, indent=1, default=str))
        return code
    if harness_factory is None:
        from e1.harness import Harness
        harness_factory = Harness
    code, out = run(a, raw, run_root, command, harness_factory)
    print(json.dumps(out, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
