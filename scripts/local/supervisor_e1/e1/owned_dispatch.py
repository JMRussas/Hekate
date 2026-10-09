"""Owned dispatch (plan 050): a small, bounded dispatcher that keeps driving ONE prepared plan on the persistent LOCAL
store after the chat turn that started it is gone. LOCAL development only; no service, no scheduler, no second database.

It is a thin loop over the EXISTING primitives: the sole `LocalStore` owner (PostgreSQL advisory lock), `plan_cli`'s input
and binding checks, and `plan_run.run_plan` (which re-reads PlanStore, claims, runs the supervised pilot, and stops on
anything uncertain). PlanStore stays the only authority for claims, task state and acceptance; the files under
<state-dir>/dispatch/ are OBSERVATIONS about the dispatcher process, never state to resume from.

  run     the dispatcher process (foreground): validate every pin, open the store, then observe -> dispatch -> wait
  launch  start `run` as a hidden, detached child and wait until its status file shows that exact child
  status  read-only: the status file plus liveness derived from the owner pid and heartbeat age
  stop    request a graceful stop (a file the dispatcher polls): it finishes the node it is running, claims no new one

Never done here: infer a live worker from `in_progress`, reset/reclaim an in-flight node, accept or review anything,
retry a failed or uncertain dispatch, prepare or pin a spec, or run any command that a pinned spec does not declare.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from e1 import plan_cli as PC
from e1 import plan_import as PI
from e1 import plan_run as PR

PROG = "owned_dispatch"
SCHEMA = "owned-dispatch-status.v0"
DISPATCH_DIR, STATUS, EVENTS, STOP, LOG = "dispatch", "status.json", "events.jsonl", "stop.request", "dispatcher.log"
# Stops an operator can clear while the dispatcher keeps polling (pin a spec / review a finished node).
WAITABLE = frozenset({"spec_pending", "review_pending"})
# A dispatch THIS process started that did not end accepted: failed (never retried here).
FAILED = frozenset({"node_not_accepted", "unexpected_error", "run_refused", "preflight_refused"})
EXIT_OK, EXIT_STOPPED, EXIT_REFUSED = 0, 1, 2
CREATE_NO_WINDOW, CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB = 0x08000000, 0x00000200, 0x01000000
SECRET_KEY = re.compile(r"(token|secret|passw|credential|authorization|api[-_]?key|prompt)", re.I)
STEPS_KEPT, DETAIL_MAX = 20, 2000
# Fenced stop (plan 053): a request may name the launch it was meant for; only that dispatcher honours it.
LAUNCH_ID = re.compile(r"[0-9a-f]{32}")
STOP_READ_MAX, STOP_UNKNOWN_GRACE_S, STOP_RETAINED_MAX = 4096, 5.0, 16
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class Refused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class Limits:
    """Bounded by default and by hard caps: a dispatcher cannot be started unbounded."""
    max_duration_s: int = 4 * 3600
    poll_s: int = 60
    max_poll_s: int = 600
    heartbeat_s: int = 15
    max_nodes: int = 3

    CAPS = {"max_duration_s": (60, 24 * 3600), "poll_s": (5, 3600), "max_poll_s": (5, 3600), "heartbeat_s": (5, 300),
            "max_nodes": (1, 20)}

    def validate(self) -> "Limits":
        for name, (lo, hi) in self.CAPS.items():
            v = getattr(self, name)
            if type(v) is not int or not lo <= v <= hi:
                raise Refused("limit_out_of_range", {name: v, "min": lo, "max": hi})
        if self.max_poll_s < self.poll_s:
            raise Refused("limit_out_of_range", {"max_poll_s": self.max_poll_s, "poll_s": self.poll_s})
        return self


def public(v: Any, depth: int = 0) -> Any:
    """Bounded, secret-key-free copy for the status file. Prompts and credentials never reach it by construction (only
    PlanStore keys, reason codes and file paths do); this also drops any key that looks like one and bounds size."""
    if depth > 6:
        return "..."
    if isinstance(v, dict):
        return {str(k)[:80]: "[redacted]" if SECRET_KEY.search(str(k)) else public(x, depth + 1) for k, x in list(v.items())[:40]}
    if isinstance(v, (list, tuple)):
        return [public(x, depth + 1) for x in list(v)[:20]]
    if isinstance(v, str):
        return v[:300]
    return v if isinstance(v, (int, float, bool)) or v is None else str(v)[:300]


def safe_detail(value: Any) -> Any:
    """Public reasons contain schema-selected metadata, never arbitrary error text."""
    allowed = {"node", "next", "error", "code", "max", "limit", "reason", "predecessor", "missingNode"}
    if isinstance(value, dict):
        return {key: safe_detail(item) for key, item in list(value.items())[:20] if key in allowed}
    if isinstance(value, list):
        return [safe_detail(item) for item in value[:20]]
    if value is None or type(value) in (int, bool):
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
        return value
    return "[omitted]"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


# --- the status file -----------------------------------------------------------------------------------------------

class StatusFile:
    """The dispatcher's public observation file, replaced atomically; thread-safe (the heartbeat thread beats while the
    main thread is inside a node). A failed write is counted and never stops the dispatcher."""

    def __init__(self, directory: Path, doc: dict[str, Any], clock: Callable[[], float] = time.time):
        self.dir, self.doc, self.clock = Path(directory), doc, clock
        self.lock = threading.Lock()
        self.seq = 0
        self.write_errors = 0

    def _flush(self) -> None:
        tmp = self.dir / (STATUS + ".tmp")
        try:
            tmp.write_text(json.dumps(self.doc, indent=1, sort_keys=True, default=str), encoding="utf-8", newline="\n")
            os.replace(tmp, self.dir / STATUS)
        except OSError:
            self.write_errors += 1

    def update(self, **fields: Any) -> None:
        with self.lock:
            if "detail" in fields:
                fields["detail"] = safe_detail(fields["detail"])
            self.doc.update(public(fields))
            self.doc["writeErrors"] = self.write_errors
            self._flush()

    def beat(self) -> None:
        with self.lock:
            self.seq += 1
            now = self.clock()
            self.doc["heartbeat"] = {"seq": self.seq, "epoch": now, "at": iso(now), "intervalS": self.doc["limits"]["heartbeat_s"]}
            self._flush()

    def event(self, kind: str, **data: Any) -> None:
        line = json.dumps({"at": iso(self.clock()), "kind": kind, "data": public(data)}, sort_keys=True, default=str)
        try:
            with open(self.dir / EVENTS, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        except OSError:
            self.write_errors += 1


class Heartbeat(threading.Thread):
    def __init__(self, status: StatusFile, interval_s: float):
        super().__init__(name="owned-dispatch-heartbeat", daemon=True)
        self.status, self.interval_s, self.halt = status, interval_s, threading.Event()

    def run(self) -> None:
        while not self.halt.wait(self.interval_s):
            self.status.beat()


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k.OpenProcess.restype = ctypes.c_void_p
        k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k.OpenProcess(0x1000, False, pid)                     # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5                   # access denied: it exists
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259                     # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_birth(pid: int) -> str | None:
    """Native process creation identity; a pid by itself can be reused."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return None
        try:
            fields = [wintypes.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(h, *(ctypes.byref(field) for field in fields)):
                return None
            return str((fields[0].dwHighDateTime << 32) | fields[0].dwLowDateTime)
        finally:
            k.CloseHandle(h)
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def read_status(state_dir: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads((Path(state_dir) / DISPATCH_DIR / STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA else None


def liveness(doc: dict[str, Any], now: float, alive: Callable[[int], bool] = pid_alive) -> str:
    """exited | running | unresponsive | owner_gone. `running` needs BOTH a live owner pid AND a fresh heartbeat, so a
    native creation identity must match before this owner can be called alive; nothing is ever concluded from PlanStore's `in_progress`."""
    if doc.get("phase") == "exited":
        return "exited"
    pid = (doc.get("owner") or {}).get("pid")
    if type(pid) is not int or pid <= 0 or not alive(pid):
        return "owner_gone"
    recorded = (doc.get("owner") or {}).get("processBirth")
    current = process_birth(pid)
    if not recorded or current is None:
        return "owner_unverified"
    if recorded != current:
        return "owner_gone"
    hb = doc.get("heartbeat") or {}
    interval, epoch = hb.get("intervalS"), hb.get("epoch")
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 5 <= interval <= 300:
        return "unresponsive"
    if type(epoch) not in (int, float) or not math.isfinite(epoch):
        return "unresponsive"
    age = now - epoch
    return "running" if 0 <= age <= 3 * interval + 5 else "unresponsive"


def report(state_dir: Path, now: float | None = None, alive: Callable[[int], bool] = pid_alive) -> dict[str, Any]:
    """What an operator or chat turn should read: the recorded observation plus derived liveness. A dispatcher that is
    gone without a clean exit is reported `owner_gone` with its LAST phase: the node it was dispatching may still be
    in_progress in PlanStore and needs an operator, not a reset."""
    doc = read_status(state_dir)
    if doc is None:
        return {"state": "no_status", "liveness": None}
    live = liveness(doc, time.time() if now is None else now, alive)
    out = dict(doc, liveness=live)
    if live == "owner_gone":
        out.update(state="owner_gone", lastState=doc.get("state"), lastPhase=doc.get("phase"),
                   note="the dispatcher died without a clean exit; its last node may be in flight: inspect PlanStore, do not reset")
    elif live in ("unresponsive", "owner_unverified"):
        out.update(state=live, lastState=doc.get("state"), note="owner pid exists but the heartbeat is stale; state unverified")
    return out


# --- observation (pure) ------------------------------------------------------------------------------------------

@dataclass
class Observation:
    kind: str                          # done | ready | blocked
    reason: str | None
    detail: Any
    nodes: dict[str, dict[str, Any]]
    next: str | None = None


def observe(plan: Any, view: dict[str, Any], load_spec: Callable[[str, str], Any] = PI.load_spec) -> Observation:
    """ONE PlanStore view -> done | ready (the next node pins a hash-valid spec) | blocked(reason). Uses plan_run's own
    snapshot/classify so the dispatcher can never disagree with the runner about what blocks."""
    nodes: dict[str, dict[str, Any]] = {}
    try:
        state = PR.snapshot(plan, view)
        by_id = {node["id"]: node for node in view["nodes"]}
        nodes = {key: {"nodeId": plan.node_ids[key], "work": item["work"], "acceptance": item["acceptance"], "ready": item["ready"],
                       "attemptId": by_id[plan.node_ids[key]].get("attemptId"), "attemptEpoch": item["attemptEpoch"]}
                 for key, item in state.items()}
        verdict, what = PR.classify(state, PR.root_container(plan, view))
        if verdict == "done":
            return Observation("done", None, None, nodes)
        if verdict == "stop":
            return Observation("blocked", what[0], what[1], nodes)
        try:
            ref = PI.parse_node_ref(state[what]["value"])
            if ref is None:
                return Observation("blocked", "spec_pending", {"node": what}, nodes, what)
            if ref[0] == "spec":
                load_spec(ref[2], ref[1])
        except PI.ImportRefused as e:
            return Observation("blocked", "spec_mismatch", {"node": what, "code": e.code}, nodes, what)
        return Observation("ready", None, None, nodes, what)
    except PR._Stop as e:
        return Observation("blocked", e.reason, e.detail, nodes)


# --- the dispatcher ----------------------------------------------------------------------------------------------

class Dispatcher:
    """observe -> (dispatch | wait | stop), bounded. `read_view` / `dispatch` / `stop_requested` are seams: production
    binds them to the store, `run_plan` and the stop file; tests bind fakes."""

    def __init__(self, limits: Limits, plan: Any, status: StatusFile, *, read_view: Callable[[str], dict[str, Any]],
                 dispatch: Callable[[int], PR.PlanRunResult], stop_requested: Callable[[], bool], observe_only: bool = False,
                 run_root: Path | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, load_spec: Callable[[str, str], Any] = PI.load_spec):
        self.load_spec = load_spec
        self.duration_clock = time.monotonic if clock is time.time else clock
        self.run_root = run_root
        self.lim, self.plan, self.status, self.read_view, self.dispatch = limits, plan, status, read_view, dispatch
        self.stop_requested, self.observe_only, self.clock, self.sleep = stop_requested, observe_only, clock, sleep
        self.dispatched = 0
        self.cycles = 0

    def run(self) -> int:
        t0, poll, last = self.duration_clock(), self.lim.poll_s, None
        while True:
            if self.stop_requested():
                return self.finish("stopped", "stop_requested")
            if self.duration_clock() - t0 >= self.lim.max_duration_s:
                return self.finish("stopped", "duration_elapsed")
            self.cycles += 1
            self.status.update(phase="observing", counters={"cycles": self.cycles, "dispatched": self.dispatched})
            try:
                obs = observe(self.plan, self.read_view(self.plan.root), self.load_spec)
            except Exception as e:  # noqa: BLE001 -- an unreadable PlanStore is a stop, never a guess
                return self.finish("failed", "observe_failed", {"error": type(e).__name__})
            self.status.update(nodes=obs.nodes, observedAt=iso(self.clock()), observation={"kind": obs.kind, "reason": obs.reason, "next": obs.next})
            if obs.kind == "done":
                return self.finish("done", None)
            if obs.kind == "ready":
                if self.observe_only:
                    return self.finish("ready_idle", "observe_only", {"next": obs.next})
                final = self.dispatch_once(obs)
                if final is not None:
                    return self.finish(*final)
                continue
            # blocked
            if obs.reason not in WAITABLE or self.observe_only:
                return self.finish("blocked", obs.reason, obs.detail)
            sig = (obs.reason, json.dumps(obs.detail, sort_keys=True, default=str))
            poll = min(poll * 2, self.lim.max_poll_s) if sig == last else self.lim.poll_s
            last = sig
            self.status.update(phase="waiting", state="blocked", stopReason=obs.reason, detail=obs.detail, nextPollInS=poll,
                               blockedNodes=[dict(nodeId=node["nodeId"], work=node["work"], reason=obs.reason) for node in obs.nodes.values()
                                             if not node["ready"] and node["acceptance"] != "accepted"])
            self.wait(poll, t0)

    def wait(self, seconds: float, t0: float) -> bool:
        """Sleep in <=1s slices; False when a stop or the duration bound ended the wait early."""
        end = self.duration_clock() + seconds
        while self.duration_clock() < end:
            if self.stop_requested() or self.duration_clock() - t0 >= self.lim.max_duration_s:
                return False
            self.sleep(min(1.0, max(0.0, end - self.duration_clock())))
        return True

    def dispatch_once(self, obs: Observation) -> tuple[str, str | None, Any] | None:
        """ONE run_plan call (bounded by the remaining node budget). Returns a final (state, reason, detail), or None to
        observe again (the runner stopped on something an operator can clear). Never retries a failed dispatch."""
        remaining = self.lim.max_nodes - self.dispatched
        if remaining <= 0:
            return "ready_idle", "node_limit", {"next": obs.next}
        node_root = str(self.run_root / obs.next) if self.run_root is not None else None
        self.status.update(phase="dispatching", state="running", stopReason=None, detail=None,
                           current={"node": obs.next, "nodeId": self.plan.node_ids[obs.next], "workerLiveness": "unknown", "usefulProgress": "unknown", "since": iso(self.clock()), "nodeRunRoot": node_root})
        self.status.event("dispatch_start", node=obs.next, maxNodes=remaining)
        try:
            res = self.dispatch(remaining)
        except Exception as e:  # noqa: BLE001 -- the node may be in flight: stop, never retry
            self.status.update(current=None)
            return "failed", "dispatch_exception", {"error": type(e).__name__, "node": obs.next}
        ran = [s for s in res.steps if s.action == "ran"]
        self.dispatched += len(ran)
        steps = (self.status.doc.get("steps") or []) + [{"node": s.key, "outcome": s.outcome, "reason": s.reason, "evidence": s.evidence}
                                                      for s in ran]
        self.status.update(current=None, steps=steps[-STEPS_KEPT:], nodes=public({k: {"work": v["work"], "acceptance": v["acceptance"],
                           "ready": v["ready"]} for k, v in res.nodes.items()}), counters={"cycles": self.cycles, "dispatched": self.dispatched},
                           lastRun={"outcome": res.outcome, "reason": res.reason})
        self.status.event("dispatch_end", outcome=res.outcome, reason=res.reason, ran=len(ran))
        if res.outcome == "all_done":
            return "done", None, None
        if res.reason == "node_limit":
            return "ready_idle", "node_limit", res.detail
        if res.reason == "stop_requested":
            return "stopped", "stop_requested", res.detail
        if res.reason in WAITABLE and ran:                 # progress was made; observe again and wait for the operator
            return None
        if res.reason in FAILED or any(s.outcome != "accepted" for s in ran):
            return "failed", res.reason, res.detail
        return "blocked", res.reason, res.detail

    def finish(self, state: str, reason: str | None, detail: Any = None) -> int:
        self.status.update(phase="exited", state=state, stopReason=reason, detail=detail, current=None, exitedCleanly=True,
                           exitedAt=iso(self.clock()))
        self.status.event("exit", state=state, reason=reason)
        return EXIT_OK if state in ("done", "stopped", "ready_idle") else EXIT_STOPPED


# --- production wiring -------------------------------------------------------------------------------------------

def dispatch_dir(state_dir: Path) -> Path:
    return Path(state_dir) / DISPATCH_DIR


def read_stop_request(path: Path) -> tuple[str, Any]:
    """One bounded look at the stop file: (none | legacy | fenced | unknown, detail). Only a regular, non-link file of at
    most STOP_READ_MAX bytes is read; anything else is `unknown` and its content is never trusted. `legacy` is a JSON
    object that names no target (the unfenced request); `fenced` carries a well-formed targetLaunchId."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "none", None
    except OSError:
        return "unknown", "unreadable"
    if not stat.S_ISREG(st.st_mode) or getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        return "unknown", "not_regular_file"
    if st.st_size > STOP_READ_MAX:
        return "unknown", "oversized"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return "unknown", "unreadable"
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return "unknown", "not_regular_file"
        raw = os.read(fd, STOP_READ_MAX + 1)
    except OSError:
        return "unknown", "unreadable"
    finally:
        os.close(fd)
    if len(raw) > STOP_READ_MAX:
        return "unknown", "oversized"
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError:
        return "unknown", "malformed"
    if not isinstance(doc, dict):
        return "unknown", "malformed"
    if "targetLaunchId" not in doc:
        return "legacy", None
    target = doc["targetLaunchId"]
    if type(target) is str and LAUNCH_ID.fullmatch(target):
        return "fenced", target
    return "unknown", "malformed"


class StopGate:
    """The dispatcher's `stop_requested` seam. Honours the halt event, an unfenced (legacy) request, and a fenced request
    naming THIS dispatcher's launch id; the first honoured request latches. A well-formed request for another launch is
    ignored and a request whose content cannot be established (malformed, oversized, link, unreadable: `unknown`) is
    ignored once it persists past a short grace (so a half-written file is not judged). Both are RENAMED (never deleted)
    to a sibling evidence name in the dispatch directory, which this dispatcher owns while it holds the store."""

    def __init__(self, directory: Path, halt: threading.Event, launch_id: str | None = None, *,
                 clock: Callable[[], float] = time.time, note: Callable[..., None] | None = None):
        self.path, self.halt, self.launch_id, self.clock, self.note = Path(directory) / STOP, halt, launch_id, clock, note
        self.honored, self.ignored, self.unknown_since = False, 0, None

    def __call__(self) -> bool:
        if self.honored or self.halt.is_set():
            return True
        kind, info = read_stop_request(self.path)
        if kind == "none":
            self.unknown_since = None
            return False
        if kind == "legacy" or (kind == "fenced" and self.launch_id is not None and info == self.launch_id):
            self.honored = True
            return True
        if kind == "fenced":
            self.retain("foreign", {"target": info})
        else:
            now = self.clock()
            self.unknown_since = now if self.unknown_since is None else self.unknown_since
            if now - self.unknown_since >= STOP_UNKNOWN_GRACE_S:
                self.retain("unverified", {"reason": info})
        return False

    def retain(self, label: str, data: dict[str, Any]) -> None:
        self.unknown_since = None
        if self.ignored >= STOP_RETAINED_MAX:
            return                                                       # bounded evidence: still ignored, left in place
        dest = self.path.with_name(f"{STOP}.{label}-{int(self.clock() * 1000)}-{uuid.uuid4().hex[:6]}")
        try:
            os.replace(self.path, dest)
        except OSError:
            dest = None
        self.ignored += 1
        if self.note:
            self.note(f"{label}_stop_request_ignored", retained=dest.name if dest else None, **data)


def stop_flag(directory: Path, halt: threading.Event, launch_id: str | None = None, **kw: Any) -> StopGate:
    return StopGate(directory, halt, launch_id, **kw)


def previous_owner(state_dir: Path, now: float, alive: Callable[[int], bool] = pid_alive) -> dict[str, Any] | None:
    """Evidence about the dispatcher that wrote the status file before this start. A live or unresponsive one refuses
    the start; a gone one is only RECORDED (the store lock is the real exclusion, and PlanStore classifies the node)."""
    doc = read_status(state_dir)
    if doc is None:
        return None
    live = liveness(doc, now, alive)
    if live in ("running", "unresponsive", "owner_unverified"):
        raise Refused(f"owner_{live}", {"pid": (doc.get("owner") or {}).get("pid")})
    cur = doc.get("current") or {}
    return {"pid": (doc.get("owner") or {}).get("pid"), "liveness": live, "phase": doc.get("phase"), "state": doc.get("state"),
            "stopReason": doc.get("stopReason"), "lastHeartbeat": (doc.get("heartbeat") or {}).get("at"),
            "wasDispatching": cur.get("node")}


def run_namespace(a: argparse.Namespace) -> argparse.Namespace:
    """The argument shape plan_cli's own checks read, so the SAME pins and refusals apply (store=local: a bound root continues)."""
    return argparse.Namespace(run_root=a.run_root, launch_real_model=a.launch_real_model, root_go=a.root_go, exe=a.exe,
                              exe_arg=a.exe_arg, exe_sha256=a.exe_sha256, store="local", worker=a.worker,
                              worker_model=a.worker_model, state_dir=a.state_dir, actor=a.actor)


def bind_run_root(run_root: Path, raw: bytes, binding: dict[str, str]) -> None:
    run_root.mkdir(parents=True)
    with open(run_root / PC.BOUND_PLAN, "xb") as f:
        f.write(raw)
    with open(run_root / PC.BINDING, "x", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(binding, indent=1, sort_keys=True))


def serve(a: argparse.Namespace, *, opener: Callable[[Path], Any] | None = None) -> int:
    from e1 import local_store as LS
    from e1.wire import SetupClient
    try:
        limits = Limits(a.max_duration_s, a.poll_s, a.max_poll_s, a.heartbeat_s, a.max_nodes).validate()
        if a.state_dir is None:
            raise Refused("state_dir_required")
        raw, doc, _specs = PC.load_plan(a.plan)
        import hashlib
        if not a.plan_sha256 or hashlib.sha256(raw).hexdigest() != a.plan_sha256:
            raise Refused("plan_pin_mismatch", {"given": a.plan_sha256, "actual": hashlib.sha256(raw).hexdigest()})
        ns = run_namespace(a)
        run_root, command = PC.check_run_inputs(ns)
        mode, binding = PC.check_binding(ns, raw, run_root)
        ddir = dispatch_dir(a.state_dir)
        prev = previous_owner(a.state_dir, time.time())
    except (Refused, PC.Refused) as e:
        print(json.dumps({"refused": e.code, "detail": e.detail}, default=str))
        return EXIT_REFUSED
    try:
        store = (opener or LS.LocalStore.open)(a.state_dir)           # the sole owner: a second opener gets store_in_use
    except LS.LocalStoreRefused as e:
        print(json.dumps({"refused": e.code, "detail": e.detail}, default=str))
        return EXIT_REFUSED
    try:
        ddir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        status = StatusFile(ddir, {
            "schema": SCHEMA, "authority": "PlanStore is the only authority; this file is an observation of the dispatcher process",
            "importSha256": doc.sha256, "planFileSha256": a.plan_sha256, "runRoot": str(run_root), "limits": asdict(limits),
            "owner": {"pid": os.getpid(), "processBirth": process_birth(os.getpid()), "startedAt": iso(now), "python": sys.version.split()[0], "launch": a.launch_mode, "launchId": a.launch_id,
                      "storeDb": store.loc.db, "exeSha256": a.exe_sha256},
            "phase": "starting", "state": "starting", "stopReason": None, "detail": None, "previousOwner": prev, "steps": [],
            "nodes": {}, "current": None, "counters": {"cycles": 0, "dispatched": 0}, "exitedCleanly": False})
    except BaseException:
        store.stop()
        raise
    halt = threading.Event()
    hb = Heartbeat(status, limits.heartbeat_s)
    code = EXIT_STOPPED
    try:
        stale = ddir / STOP
        if stale.exists():                                              # we hold the store: that request was for a dead owner
            os.replace(stale, ddir / f"{STOP}.archived-{int(now)}")
            status.event("stale_stop_request_archived")
        status.beat()
        status.update()
        status.event("start", previousOwner=prev, pid=os.getpid())
        hb.start()
        for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_: halt.set())
                except ValueError:                                      # not the main thread: the stop file still works
                    break
        try:
            setup, _client, aj = store.session(actor=a.actor, plan_bytes=raw)
            try:
                if mode == "continue":
                    plan = PI.attach_plan(setup, store.loc.project_id, raw)       # verify, NO write
                else:
                    plan = PI.import_plan(setup, store.loc.project_id, raw)
                    bind_run_root(run_root, raw, binding)
            finally:
                aj.close()
        except (LS.LocalStoreRefused, PI.ImportRefused) as e:
            status.update(phase="exited", state="blocked", stopReason=e.code, detail=e.detail, exitedCleanly=True)
            return EXIT_STOPPED
        reader = SetupClient(store.base_url)

        def read_view(root: str) -> dict[str, Any]:
            r = reader.plan(root)
            if r.status != 200:
                raise RuntimeError(f"plan_read_{r.status}")
            return r.body

        def note_ignored(kind: str, **data: Any) -> None:
            status.event(kind, **data)
            status.update(ignoredStopRequests=should_stop.ignored)

        should_stop = stop_flag(ddir, halt, a.launch_id, note=note_ignored)

        def dispatch(max_nodes: int) -> PR.PlanRunResult:
            try:
                setup, client, aj = store.session(actor=a.actor, plan_bytes=raw)
            except LS.LocalStoreRefused as e:                          # e.g. uncertain operator acts: nothing ran
                return PR.PlanRunResult("needs_operator", e.code, plan.root, doc.sha256, detail=e.detail)
            try:
                return PR.run_plan(plan, run_root, setup=setup, client=client, aj=aj, executable=command,
                                   executable_sha256=a.exe_sha256, execution_kind=PC.execution_kind(ns), root_go=a.root_go,
                                   max_nodes=max_nodes, stop_requested=should_stop, **PC.worker_args(ns))
            finally:
                aj.close()

        d = Dispatcher(limits, plan, status, read_view=read_view, dispatch=dispatch, stop_requested=should_stop,
                       observe_only=a.observe_only, run_root=run_root)
        code = d.run()
    except BaseException as e:  # noqa: BLE001 -- recorded, then re-raised: a crash must not look like a clean stop
        status.update(phase="exited", state="failed", stopReason="dispatcher_exception", detail={"error": type(e).__name__},
                      exitedCleanly=False)
        raise
    finally:
        hb.halt.set()
        store.stop()
    print(json.dumps({"state": status.doc["state"], "stopReason": status.doc["stopReason"], "status": str(ddir / STATUS)}))
    return code


# --- hidden detached launch --------------------------------------------------------------------------------------

def hidden_popen_kwargs(platform: str = sys.platform, *, breakaway: bool = True) -> dict[str, Any]:
    """Child launch policy: Windows -> no console window (CREATE_NO_WINDOW + SW_HIDE), own process group, optionally
    outside the caller's job object so it outlives the chat tool; elsewhere a new session. stdio is set by the caller."""
    if platform != "win32":
        return {"start_new_session": True}
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | (CREATE_BREAKAWAY_FROM_JOB if breakaway else 0)
    kw: dict[str, Any] = {"creationflags": flags}
    if hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kw["startupinfo"] = si
    return kw


def run_argv(a: argparse.Namespace) -> list[str]:
    """The child's argv, rebuilt from parsed pins only (no shell, no pass-through of unknown text)."""
    out = ["run", "--plan", str(a.plan), "--plan-sha256", a.plan_sha256, "--state-dir", str(a.state_dir), "--run-root", str(a.run_root),
           "--exe", str(a.exe), "--exe-sha256", a.exe_sha256, "--root-go", a.root_go, "--worker", a.worker, "--actor", a.actor,
           "--max-duration-s", str(a.max_duration_s), "--poll-s", str(a.poll_s), "--max-poll-s", str(a.max_poll_s),
           "--heartbeat-s", str(a.heartbeat_s), "--max-nodes", str(a.max_nodes), "--launch-mode", "hidden"]
    if a.launch_id:
        out += ["--launch-id", a.launch_id]
    if a.launch_real_model:
        out.append("--launch-real-model")
    if a.exe_arg:
        out += ["--exe-arg", str(a.exe_arg)]
    if a.worker_model:
        out += ["--worker-model", a.worker_model]
    if a.observe_only:
        out.append("--observe-only")
    return out


def launch(a: argparse.Namespace, *, popen: Callable[..., Any] = subprocess.Popen, now: Callable[[], float] = time.time,
           sleep: Callable[[float], None] = time.sleep, platform: str = sys.platform) -> int:
    try:
        Limits(a.max_duration_s, a.poll_s, a.max_poll_s, a.heartbeat_s, a.max_nodes).validate()
        if a.state_dir is None:
            raise Refused("state_dir_required")
        previous_owner(a.state_dir, now())                               # refuses a live/unresponsive owner before any spawn
        raw, _, _ = PC.load_plan(a.plan)
        import hashlib
        if not a.plan_sha256 or hashlib.sha256(raw).hexdigest() != a.plan_sha256:
            raise Refused("plan_pin_mismatch")
        if type(a.wait_s) is not int or not 1 <= a.wait_s <= 60:
            raise Refused("launch_wait_out_of_range")
        ns = run_namespace(a)
        run_root, _ = PC.check_run_inputs(ns)
        PC.check_binding(ns, raw, run_root)
    except (Refused, PC.Refused) as e:
        print(json.dumps({"refused": e.code, "detail": e.detail}, default=str))
        return EXIT_REFUSED
    ddir = dispatch_dir(a.state_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    a.launch_id = uuid.uuid4().hex
    argv = [sys.executable, "-m", "e1.owned_dispatch", *run_argv(a)]
    cwd = Path(__file__).resolve().parents[1]
    breakaway = platform == "win32"
    with open(ddir / LOG, "ab") as log:
        while True:
            try:
                proc = popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             **hidden_popen_kwargs(platform, breakaway=breakaway))
                break
            except PermissionError:
                if not breakaway:
                    raise
                breakaway = False                                        # the caller's job forbids breakaway: recorded below
    deadline = now() + a.wait_s
    while now() < deadline:
        doc = read_status(a.state_dir)
        if doc and (doc.get("owner") or {}).get("launchId") == a.launch_id and liveness(doc, now()) == "running":
            print(json.dumps({"launched": True, "pid": doc["owner"]["pid"], "launcherPid": proc.pid, "launchId": a.launch_id, "breakaway": breakaway, "state": doc.get("state"),
                              "status": str(ddir / STATUS), "log": str(ddir / LOG)}))
            return EXIT_OK
        if proc.poll() is not None:
            print(json.dumps({"launched": False, "exitCode": proc.returncode, "log": str(ddir / LOG)}))
            return EXIT_STOPPED
        sleep(1.0)
    print(json.dumps({"launched": "unconfirmed", "pid": proc.pid, "launchId": a.launch_id, "note": "no matching status yet; check `status` before launching again",
                      "log": str(ddir / LOG)}))
    return EXIT_STOPPED


def request_stop(state_dir: Path, by: str, now: float | None = None, alive: Callable[[int], bool] = pid_alive,
                 expected_launch_id: str | None = None) -> tuple[int, dict[str, Any]]:
    """Leave a graceful-stop request for the live owner. With `expected_launch_id` the request is FENCED: it is refused
    unless the live owner now has that launch id, and it records the target so a different owner that appears before or
    after the write ignores it. The write is a request, not a delivery: nothing here claims the owner saw or obeyed it."""
    fenced = expected_launch_id is not None
    if fenced and not (type(expected_launch_id) is str and LAUNCH_ID.fullmatch(expected_launch_id)):
        return EXIT_REFUSED, {"refused": "invalid_expected_launch_id"}
    rep = report(state_dir, now, alive)
    if rep.get("liveness") != "running":
        return EXIT_REFUSED, {"refused": "no_live_owner", "state": rep.get("state")}
    if fenced and (rep.get("owner") or {}).get("launchId") != expected_launch_id:
        return EXIT_REFUSED, {"refused": "owner_changed", "expectedLaunchId": expected_launch_id}
    body = {"requestedBy": by[:80], "requestedAt": iso(time.time() if now is None else now), "id": uuid.uuid4().hex[:8]}
    if fenced:
        body["targetLaunchId"] = expected_launch_id
    path = dispatch_dir(state_dir) / STOP
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(body))
    except FileExistsError:
        return EXIT_REFUSED, {"refused": "stop_already_requested"}
    out: dict[str, Any] = {"stopRequested": True, "note": "graceful: the running node finishes (bounded by its spec), then no new claim"}
    if fenced:
        out.update(targetLaunchId=expected_launch_id, note="request recorded for that launch only; delivery and stop completion are not confirmed: read `status`")
    return EXIT_OK, out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("run", "launch", "status", "stop"))
    ap.add_argument("--state-dir", type=Path)
    ap.add_argument("--plan", type=Path, help="the prepared plan-import.v0 file")
    ap.add_argument("--plan-sha256", help="pin: sha256 of the plan file bytes")
    ap.add_argument("--run-root", type=Path, help="the plan's run root (bound: continued; new: imported and bound)")
    ap.add_argument("--exe", type=Path)
    ap.add_argument("--exe-arg", type=Path, help="offline tests only: the FAKE CLI")
    ap.add_argument("--exe-sha256")
    ap.add_argument("--launch-real-model", action="store_true")
    ap.add_argument("--root-go")
    ap.add_argument("--worker", choices=PC.W.BACKENDS, default="claude")
    ap.add_argument("--worker-model")
    ap.add_argument("--actor", default="operator:local-dispatch")
    ap.add_argument("--observe-only", action="store_true", help="observe once and report; never dispatch")
    ap.add_argument("--launch-mode", choices=("foreground", "hidden"), default="foreground")
    ap.add_argument("--launch-id", help="host correlation across Windows virtualenv launcher/interpreter processes")
    ap.add_argument("--expected-launch-id", help="stop only: fence the request to the owner with this 32-char lowercase hex launch id")
    ap.add_argument("--wait-s", type=int, default=60, help="launch: how long to wait for the child's status")
    d = Limits()
    ap.add_argument("--max-duration-s", type=int, default=d.max_duration_s)
    ap.add_argument("--poll-s", type=int, default=d.poll_s)
    ap.add_argument("--max-poll-s", type=int, default=d.max_poll_s)
    ap.add_argument("--heartbeat-s", type=int, default=d.heartbeat_s)
    ap.add_argument("--max-nodes", type=int, default=d.max_nodes)
    return ap


def main(argv: list[str]) -> int:
    a = build_parser().parse_args(argv)
    if a.expected_launch_id is not None and a.command != "stop":
        print(json.dumps({"refused": "unsupported_flag", "detail": {"flag": "--expected-launch-id", "command": a.command}}))
        return EXIT_REFUSED
    if a.command in ("status", "stop"):
        if a.state_dir is None:
            print(json.dumps({"refused": "state_dir_required"}))
            return EXIT_REFUSED
        if a.command == "status":
            rep = report(a.state_dir)
            print(json.dumps(rep, indent=1, default=str))
            return EXIT_REFUSED if rep["state"] == "no_status" else EXIT_OK
        code, out = request_stop(a.state_dir, a.actor, expected_launch_id=a.expected_launch_id)
        print(json.dumps(out))
        return code
    if a.command == "launch":
        return launch(a)
    return serve(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
