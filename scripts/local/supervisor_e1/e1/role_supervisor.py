"""Run ONE read-only role under the EXISTING `e1.supervisor.Supervisor` (task c2fd26d1, managed-role-adapter).

A thin composition, not a scheduler: the Supervisor still owns claim, receipt/pin checks and finish. This module only
supplies its three seams: a package builder (pinned task content from the claim receipt), a worker (the injected
`BaseChatModel` role via `role_worker.run_role`) and a journal adapter (links the launch/exit records to a native
`AttemptTrace`). Nothing here accepts or reviews: success ends at `done`, awaiting independent review. See ROLE-SUPERVISOR.md.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel

from . import role_evidence as RE
from . import role_worker as RW
from .cli_worker import AttemptTrace, TraceCaptureConfig
from .seam import Envelope, opaque_package
from .supervisor import (FakeWorker, Journal, Outcome, PackageRefused, RunEnvelope, SupervisedRun, Supervisor, WorkResult,
                         current_node_state, echo)

EXECUTION_KIND = "langchain-role"
TOKEN_PREFIX = "e1-role:"
RESERVED_EVIDENCE_ID = "hekate-claim-receipt"
RESULT_FILE, MANIFEST_FILE = "role-result.json", "role-manifest.json"
EXIT_OK, EXIT_ROLE_FAILED, EXIT_HOST_FAILED = 0, 2, 3
SHA40 = re.compile(r"[0-9a-f]{40}")
INTENT_KINDS = ("claim_intent", "launch_intent", "finish_intent")
RUN_DIR_MAX_BYTES = 200                      # keeps launch_intent inside the journal's 2048-byte record bound
MAX_FILE_BYTES, MAX_TOTAL_BYTES = 2 << 20, 8 << 20
DATA_SENSITIVITY = "restricted_raw"          # evidence snapshot, prompt, output and trace are retained verbatim; NO secret scrubber


class RoleHostRefused(Exception):
    """A typed host refusal (code only, never caller content)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RolePackage:
    """The opaque package's identities verbatim (plus a role token) and what the role will see."""
    token: str
    root_id: str
    claim_key: str
    node_id: str
    attempt_id: str
    attempt_epoch: int
    executor_ref: str | None
    event_seq: int
    content_revision: int
    content_digest: str
    prereq_digest: str
    state_revision: int
    correlation: dict[str, Any]
    snapshot_sha256: str
    prompt: bytes                            # exact system prompt + canonical snapshot, as the model receives them


def _is_revision(v: Any) -> bool:
    return type(v) is int and 1 <= v <= RW.MAX_SAFE_INT


def _reparse(path: str) -> bool:
    st = os.lstat(path)
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _no_links(path: Path) -> None:
    """Every EXISTING component of `path` must be a plain directory entry: no symlink, junction or reparse point."""
    cur = Path(path.anchor)
    for part in path.parts[1:]:
        cur = cur / part
        try:
            if _reparse(str(cur)):
                raise RoleHostRefused("run_dir_link")
        except FileNotFoundError:
            return


def _write_new(path: Path, data: bytes) -> None:
    with open(path, "xb") as f:               # exclusive: an existing file is never overwritten
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _read_back(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read(MAX_FILE_BYTES + 1)


class RoleSupervisor:
    """One instance = one role invocation. `run(...)` may be called once; build a new instance (and run_dir) for another."""

    def __init__(self, *, client: Any, journal: Journal, role: RW.RoleConfig, model: BaseChatModel, snapshot: Any,
                 task_id: str, source_revision: str, observed_at: str, run_dir: Path, model_identity: str,
                 binding_id: str | None = None, provider: str = "unknown"):
        if not callable(journal):
            raise RoleHostRefused("journal_missing")
        if not all(callable(getattr(client, n, None)) for n in ("claim", "get_claim", "get_plan", "transition")):
            raise RoleHostRefused("client_invalid")
        try:
            if not isinstance(role, RW.RoleConfig):
                raise RW.RoleError(RW.CONFIG_INVALID)
            role.validate()
            data, _ = RW.validate_snapshot(copy.deepcopy(snapshot), role.max_input_bytes)
        except RW.RoleError:
            raise RoleHostRefused("role_or_snapshot_invalid") from None
        binding = role.binding_id if binding_id is None else binding_id
        if not isinstance(model, BaseChatModel):
            raise RoleHostRefused("model_invalid")
        if type(binding) is not str or binding not in role.allowed_bindings:
            raise RoleHostRefused("binding_not_allowed")
        if type(source_revision) is not str or not SHA40.fullmatch(source_revision):
            raise RoleHostRefused("source_revision_invalid")
        if type(model_identity) is not str or not RW.MODEL_IDENTITY.fullmatch(model_identity):
            raise RoleHostRefused("model_identity_invalid")
        self._client, self._hook, self._role, self._model = client, journal, role, model
        self._snapshot_bytes = data                                   # immutable copy of what the host supplied
        self._task_id, self._source, self._observed_at = task_id, source_revision, observed_at
        self._binding, self._provider, self._model_identity = binding, provider, model_identity
        self._run_dir = Path(run_dir) if isinstance(run_dir, (str, Path)) else None
        self.pkg: RolePackage | None = None
        self._snapshot_final = b""                                    # host snapshot + the reserved receipt item
        self.worker = FakeWorker(self._work)
        self._used = False
        self._trace: AttemptTrace | None = None
        self._final: dict[str, Any] | None = None
        self._reason = "not_run"
        self._result_sha: str | None = None
        self._role_status: str | None = None
        self._launched: Any = None
        self._launch_failed = False
        # Everything that does not need the claim is validated now, before any effect (placeholders for the run ids).
        self._check_run_dir(create=False)
        self._ident("00000000-0000-4000-8000-000000000000", "claim-key", "attempt", "run-0", 1, 1, 1, "0" * 64)

    # --- preflight -------------------------------------------------------------------------------------------------
    def _check_run_dir(self, *, create: bool) -> None:
        d = self._run_dir
        if d is None or not d.is_absolute() or len(str(d).encode("utf-8")) > RUN_DIR_MAX_BYTES:
            raise RoleHostRefused("run_dir_invalid")
        _no_links(d.parent)
        if not d.parent.is_dir():
            raise RoleHostRefused("run_dir_parent")
        if os.path.lexists(d):
            raise RoleHostRefused("run_dir_not_fresh")
        if create:
            try:
                os.mkdir(d)                                           # exclusive: fails if it appeared meanwhile
            except OSError:
                raise RoleHostRefused("run_dir_not_fresh") from None
            if _reparse(str(d)) or os.listdir(d):
                raise RoleHostRefused("run_dir_not_fresh")

    def _ident(self, root: str, claim_key: str, attempt_id: str, run_id: str, epoch: int, state_rev: int, content_rev: int,
               snapshot_sha: str) -> dict[str, Any]:
        """The manifest identity; a dry build proves the host config and run arguments are acceptable BEFORE the claim."""
        try:
            corr = RW.Correlation.from_mapping({"rootId": root, "taskId": self._task_id, "attemptId": attempt_id,
                                                "epoch": epoch, "contentRevision": content_rev, "observedAt": self._observed_at})
        except RW.RoleError:
            raise RoleHostRefused("correlation_invalid") from None
        r = self._role
        ident = {"planRoot": root, "taskId": corr.task_id, "runId": run_id, "attemptId": attempt_id, "epoch": epoch,
                 "stateRevision": state_rev, "contentRevision": content_rev, "claimKey": claim_key,
                 "operationKey": f"supervisor:{claim_key}:finish",
                 "role": {"id": r.id, "version": str(r.version), "definitionSha256": r.definition_hash},
                 "binding": {"binding": self._binding, "provider": self._provider, "model": self._model_identity},
                 # deadlineMs is the deadline rounded UP to whole ms (an upper bound); the role definition hash binds the exact value
                 "limits": {"deadlineMs": math.ceil(r.deadline_seconds * 1000), "maxInputBytes": r.max_input_bytes,
                            "maxOutputBytes": r.max_output_bytes, "maxModelCalls": 1},
                 "source": {"revision": self._source, "snapshotSha256": snapshot_sha}, "reviewerRefs": [],
                 "linkage": "host_asserted"}
        try:
            RE.build_manifest(ident, {}, max_file_bytes=MAX_FILE_BYTES, max_total_bytes=MAX_TOTAL_BYTES)
        except RE.EvidenceRefused:
            raise RoleHostRefused("identity_invalid") from None
        return ident

    # --- the public run --------------------------------------------------------------------------------------------
    def run(self, root: str, claim_key: str, attempt_id: str, executor_ref: str | None) -> SupervisedRun:
        """Preflight, then delegate claim/dispatch/finish to the existing Supervisor. A refusal here returns
        needs_operator with NO claim and NO model call."""
        refused = SupervisedRun(Outcome.NEEDS_OPERATOR, "")
        try:
            if self._used:
                raise RoleHostRefused("already_used")
            self._used = True
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RoleHostRefused("running_event_loop")
            self._ident(root, claim_key, attempt_id, "run-0", 1, 1, 1, "0" * 64)
            self._check_run_dir(create=True)
        except RoleHostRefused as e:
            refused.reason = f"host_refused:{e.code}"
            return refused
        sup = Supervisor(self._client, self.worker, package_builder=self._build, journal=self._journal)
        try:
            return sup.run(root, claim_key, attempt_id, executor_ref)
        finally:
            self._close_trace(complete=False, notes=("trace_incomplete:host_exit",))     # no-op when already closed

    # --- seam 1: package builder -----------------------------------------------------------------------------------
    def _build(self, env: Envelope) -> RolePackage:
        base = opaque_package(env)
        r = env.receipt
        if base.node_id != self._task_id:
            raise PackageRefused("wrong_target", overflow=False)
        try:
            node = current_node_state(self._client.get_plan(base.root_id), base.root_id, base.node_id)
        except Exception as e:  # noqa: BLE001  unreadable plan: the Supervisor reports uncertain:package_build
            raise RuntimeError(type(e).__name__) from None
        if (node.get("work") != "in_progress" or node.get("attemptId") != base.attempt_id
                or node.get("attemptEpoch") != base.attempt_epoch
                or not _is_revision(node.get("contentRevision")) or node["contentRevision"] != base.content_revision):
            raise PackageRefused("plan_drift", overflow=False)
        snap = json.loads(self._snapshot_bytes)
        if any(e["id"] == RESERVED_EVIDENCE_ID for e in snap["evidence"]):
            raise PackageRefused("evidence_id_collision", overflow=False)
        snap["evidence"].append({
            "id": RESERVED_EVIDENCE_ID, "kind": "claim_receipt", "rootId": r.root_id, "taskId": base.node_id,
            "claimKey": r.claim_key, "attemptId": base.attempt_id, "attemptEpoch": base.attempt_epoch,
            "contentRevision": base.content_revision, "contentDigest": base.content_digest,
            "prereqDigest": base.prereq_digest, "pinnedContent": r.content_snapshot_norm})
        try:
            data, _ = RW.validate_snapshot(snap, self._role.max_input_bytes)
        except RW.RoleError:
            raise PackageRefused("snapshot_invalid", overflow=True) from None
        sha = hashlib.sha256(data).hexdigest()
        corr = {"rootId": base.root_id, "taskId": base.node_id, "attemptId": base.attempt_id, "epoch": base.attempt_epoch,
                "contentRevision": base.content_revision, "observedAt": self._observed_at}
        try:
            RW.Correlation.from_mapping(corr)
            self._ident(base.root_id, base.claim_key, base.attempt_id, "run-0", base.attempt_epoch, node["stateRevision"],
                        base.content_revision, sha)
        except (RW.RoleError, RoleHostRefused) as e:
            raise PackageRefused(getattr(e, "code", "correlation_invalid"), overflow=False) from None
        prompt = (b"[system]\n" + self._role.system_prompt().encode("utf-8") + b"\n\n[user]\n" + data)
        ids = {f.name: getattr(base, f.name) for f in fields(base)}
        ids["token"] = TOKEN_PREFIX + base.claim_key
        self.pkg = RolePackage(**ids, state_revision=node["stateRevision"], correlation=corr, snapshot_sha256=sha, prompt=prompt)
        self._snapshot_final = data
        return self.pkg

    # --- seam 2: journal adapter -----------------------------------------------------------------------------------
    def _raw(self, kind: str, data: dict[str, Any]) -> Any:
        ret = self._hook(kind, data)
        if getattr(ret, "degraded", False) and kind in INTENT_KINDS:
            raise RoleHostRefused("journal_degraded")
        return ret

    def _journal(self, kind: str, data: dict[str, Any]) -> Any:
        if kind == "launch_intent":
            return self._launch_intent(data)
        if kind == "launched":
            if self._launch_failed or self._launched is None:
                raise RoleHostRefused("launched_unrecorded")
            return self._launched                                     # the host's own record; no duplicate model-only one
        if kind == "exited":
            code = data.get("exitCode")
            # AttemptTrace.ParseExited reads `code`, and any reason other than 'exit' is a kill reason: a role that ran
            # to a typed failure still EXITED, so that failure travels separately as `roleReason`.
            data = {"runId": data.get("runId"), "executionKind": EXECUTION_KIND, "inProcess": True, "code": code,
                    "timedOut": data.get("timedOut"), "killed": data.get("killed"), "reason": "exit",
                    "roleReason": self._reason, "trace": self._final}
        elif kind == "result_captured":
            ok = data.get("artifactRef") is not None and self._role_status == "completed"
            data = {"runId": data.get("runId"), "artifactRef": data.get("artifactRef"), "candidate": ok,
                    "resultSha256": self._result_sha, "roleStatus": self._role_status}
        return self._raw(kind, data)

    def _launch_intent(self, data: dict[str, Any]) -> Any:
        pkg = self.pkg
        if pkg is None or data.get("packageToken") != pkg.token:
            raise RoleHostRefused("package_mismatch")
        cfg = TraceCaptureConfig(self._run_dir, EXECUTION_KIND, 100, 2 * self._role.max_output_bytes + 65536, 1)
        trace = AttemptTrace(cfg, 1)
        try:
            trace.open(pkg.prompt)                                    # exclusive files in the fresh run_dir
        except OSError:
            raise RoleHostRefused("trace_setup_failed") from None
        self._trace = trace
        trace.note("stage:launch_intent")
        intent = {k: v for k, v in data.items() if k != "modelOnly"}
        intent.update(executionKind=EXECUTION_KIND, inProcess=True, trace=trace.ref())
        try:
            return self._raw("launch_intent", intent)
        except BaseException:
            self._close_trace(complete=False, notes=("trace_incomplete:launch_intent_failed",))
            raise

    def _close_trace(self, *, complete: bool, notes: tuple[str, ...] = ()) -> dict[str, Any] | None:
        if self._trace is None or self._trace.final is not None:
            return self._final
        self._final = self._trace.close(complete=complete, notes=notes)
        return self._final

    # --- seam 3: worker --------------------------------------------------------------------------------------------
    def _fail(self, pkg: RolePackage, run: RunEnvelope, code: str, exit_code: int, *, complete: bool) -> WorkResult:
        self._reason = code
        self._close_trace(complete=complete, notes=(f"role_failed:{code}",) if complete else (f"trace_incomplete:{code}",))
        return echo(pkg, run, exit_code=exit_code, structured_result={"status": "failed", "code": code}, artifact_ref=None)

    def _work(self, pkg: RolePackage, run: RunEnvelope) -> WorkResult:
        trace = self._trace
        if not isinstance(pkg, RolePackage) or pkg is not self.pkg or trace is None:
            raise RoleHostRefused("worker_state")                     # the Supervisor reports worker_raised; nothing ran
        trace.start()
        try:
            self._launched = self._raw("launched", {"runId": run.run_id, "executionKind": EXECUTION_KIND, "inProcess": True,
                                                    "invocation": "start"})
            if getattr(self._launched, "degraded", False):
                raise RoleHostRefused("journal_degraded")
        except Exception:  # noqa: BLE001
            self._launch_failed = True
            return self._fail(pkg, run, "launched_unrecorded", EXIT_HOST_FAILED, complete=False)
        trace.note("stage:invocation_start")
        try:
            res = asyncio.run(RW.run_role(self._role, self._model, json.loads(self._snapshot_final), pkg.correlation,
                                          binding_id=self._binding, model_identity=self._model_identity))
        except BaseException:
            self._reason = "interrupted"
            self._close_trace(complete=False, notes=("trace_incomplete:interrupted",))
            raise                                                     # effects of the model are unknown: stays in progress
        self._role_status = res.status
        if res.status != "completed" or res.metadata.snapshot_hash != pkg.snapshot_sha256:
            return self._fail(pkg, run, res.failure_code or "snapshot_mismatch", EXIT_ROLE_FAILED, complete=True)
        try:
            return self._finish_ok(pkg, run, res, trace)
        except (OSError, RE.EvidenceRefused):
            return self._fail(pkg, run, "file_or_manifest_failed", EXIT_HOST_FAILED, complete=False)

    def _finish_ok(self, pkg: RolePackage, run: RunEnvelope, res: RW.RoleResult, trace: AttemptTrace) -> WorkResult:
        output = res.output
        out_bytes = RW.canonical_bytes(output)
        trace.stdout_line(out_bytes + b"\n")                          # the validated output only; no raw provider response
        result_doc = RW.canonical_bytes({
            "schema": RW.RESULT_SCHEMA, "status": "completed", "reviewStatus": res.review_status, "accepted": res.accepted,
            "role": {"id": self._role.id, "version": self._role.version, "hash": self._role.definition_hash},
            "correlation": pkg.correlation, "snapshotSha256": pkg.snapshot_sha256, "outputSha256": res.metadata.result_hash,
            "output": output})
        _write_new(self._run_dir / RESULT_FILE, result_doc)
        self._result_sha = hashlib.sha256(result_doc).hexdigest()
        final = self._close_trace(complete=True, notes=("stage:role_completed",))
        if not final or not final["complete"] or final["trace"]["capped"] or final["trace"]["writeError"]:
            return self._fail(pkg, run, "trace_incomplete", EXIT_HOST_FAILED, complete=False)
        prompt_name, trace_name = trace.prompt_name, trace.trace_name
        files = {prompt_name: (pkg.prompt, "text/plain", "prompt"), trace_name: (_read_back(self._run_dir / trace_name),
                 "application/x-ndjson", "trace"), RESULT_FILE: (result_doc, "application/json", "role-result")}
        if hashlib.sha256(files[trace_name][0]).hexdigest() != final["trace"]["sha256"] \
                or _read_back(self._run_dir / prompt_name) != pkg.prompt:
            return self._fail(pkg, run, "trace_file_mismatch", EXIT_HOST_FAILED, complete=False)
        ident = self._ident(pkg.root_id, pkg.claim_key, pkg.attempt_id, run.run_id, pkg.attempt_epoch, pkg.state_revision,
                            pkg.content_revision, pkg.snapshot_sha256)
        built = RE.build_manifest(ident, {n: RE.Payload(d, ct, p, DATA_SENSITIVITY) for n, (d, ct, p) in files.items()},
                                  max_file_bytes=MAX_FILE_BYTES, max_total_bytes=MAX_TOTAL_BYTES)
        _write_new(self._run_dir / MANIFEST_FILE, built.body)
        on_disk = {n: _read_back(self._run_dir / n) for n in files}
        ver = RE.verify_manifest(_read_back(self._run_dir / MANIFEST_FILE), on_disk, expected=ident,
                                 max_file_bytes=MAX_FILE_BYTES, max_total_bytes=MAX_TOTAL_BYTES, expected_digest=built.digest)
        if not ver.ok:
            return self._fail(pkg, run, f"manifest_unverified:{ver.code}", EXIT_HOST_FAILED, complete=False)
        try:                                                          # the manifest names this stateRevision: it must still hold
            node = current_node_state(self._client.get_plan(pkg.root_id), pkg.root_id, pkg.node_id)
        except Exception:  # noqa: BLE001
            return self._fail(pkg, run, "state_unreadable", EXIT_HOST_FAILED, complete=False)
        if node["stateRevision"] != pkg.state_revision:
            return self._fail(pkg, run, "state_changed", EXIT_HOST_FAILED, complete=False)
        self._reason = "ok"
        return echo(pkg, run, exit_code=EXIT_OK, artifact_ref=f"role-manifest:sha256:{built.digest}",
                    structured_result={"status": "ok", "roleStatus": "completed", "reviewStatus": "pending",
                                       "manifestSha256": built.digest})


def run_managed_role(root: str, claim_key: str, attempt_id: str, executor_ref: str | None, **host: Any) -> SupervisedRun:
    """Convenience: `RoleSupervisor(**host).run(root, claim_key, attempt_id, executor_ref)`."""
    return RoleSupervisor(**host).run(root, claim_key, attempt_id, executor_ref)
