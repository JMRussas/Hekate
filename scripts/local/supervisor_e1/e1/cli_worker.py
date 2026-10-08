"""HK-ISSUE-007: a REAL Claude Code CLI worker adapter for the pilot driver (e1/pilot.py `Worker`).
TEST-SCOPED: exercised with a fake CLI (tests/fake_cli.py); a real model run is a separate, reviewed
step and is never started automatically.

It reuses the PATTERN of the gods pipeline's CLI provider, not its code or defaults:
  Odin/gods/providers/claude.py sha256 75d8f08d1266572fdd9a74bd6e6a2c0b811392da4f080c94bac75a43b738bf84
  Odin/gods/providers/base.py   sha256 d2d3dd281506ab2c2a6989e31d15bcae7100e4274aa2fdc731e489d96ffb5bd5
  Odin/gods/handlers/hermes_async.py sha256 504fd0585e082b9fb5faeb0d31465d0b2dd60478aa1b9dbd49fa390fa9f5f5d4
Those default to --dangerously-skip-permissions, attach Hekate MCP servers, set no budget, discard
stderr and kill only the parent process on timeout. Here instead (root GO, msg 1484):
- explicit config, validated before any git call or spawn; never shell=True; no skip-permissions;
  an empty strict MCP config; an explicit budget, turn limit and timeouts; a tool allowlist;
- an OWNED detached worktree at the base commit (not Claude's --worktree);
- launch_intent journaled BEFORE the process exists; `launched` is the supervisor-observed spawn,
  never an ACK;
- worker ACK/progress only from worker-authored `HEKATE-ACT {...}` lines in ASSISTANT text (claimed
  worker attestation, not an authenticated identity; HK-ISSUE-008, provisional for the pilot). Markers
  in tool input/output or user turns never count. The supervisor binds the exact ExecutionKey and
  requires the next sequence number;
- bounded stdout (line, line-count and byte caps; a finite queue drained after any kill) and bounded
  stderr (a kept prefix plus a digest and byte count of everything);
- a process-TREE kill with a verified exit of the PARENT. Proven only for a live parent; for a parent that
  already exited, tree_kill returns True and any surviving descendants are unobservable here (not
  detected, not contained; register HK-ISSUE-007);
- the supervisor (not the worker) commits the worktree diff; the commit's parent must be the base, the
  worker must not have moved HEAD, the diff must be non-empty and contain no submodule; a run-owned
  ref keeps the artifact reachable until an explicit operator cleanup.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from e1 import pilot as P

SHA40 = re.compile(r"^[0-9a-f]{40}$")
RUN_ID = re.compile(r"^[a-z0-9]{1,32}$")
MODEL = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,64}$")
BUDGET = re.compile(r"^(0|[1-9][0-9]{0,2})\.[0-9]{2}$")           # dollars as a fixed 2-decimal string
TEST_COMMAND = re.compile(r"^[A-Za-z0-9 ._/:=\-]{1,200}$")          # no shell metacharacters
SAFE_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write")
PERMISSION_MODES = ("acceptEdits", "default", "plan")
ENV_ALLOW = ("PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
ACT_PREFIX = "HEKATE-ACT "
ZERO_OID = "0" * 40
REF_ROOT = "refs/hekate-pilot"


class CliRefused(Exception):
    """A typed refusal before any git call or spawn."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class CliConfig:
    command: tuple[Path, ...]          # absolute existing files: (claude,) or, in tests, (python, fake_cli.py)
    repo: Path
    base_sha: str
    run_id: str
    run_dir: Path                      # the pilot's own run directory (already created by the driver)
    model: str
    max_budget_usd: str                # e.g. "1.50"
    max_turns: int
    total_timeout_s: int
    first_output_timeout_s: int
    inactivity_timeout_s: int
    permission_mode: str = "acceptEdits"
    allowed_tools: tuple[str, ...] = SAFE_TOOLS
    test_command: str | None = None    # the only Bash the worker may run: Bash(<test_command>)
    stdout_line_max: int = 1 << 20
    stdout_lines_max: int = 50_000
    stdout_bytes_max: int = 64 << 20
    stderr_keep: int = 64 << 10
    kill_wait_s: int = 20
    committer: str = "hekate-pilot"
    restricted: bool = True            # --restricted (see build_command)
    # Optional host hook (operator task runner, msg 1632): prepare(worktree) runs AFTER `worktree add` and the
    # HEAD check and BEFORE launch_intent, e.g. to install the worker's own dependencies. Any exception is a
    # typed `prepare_failed`: nothing is journaled and nothing is spawned. None (the default) = unchanged.
    prepare: Callable[[Path], None] | None = None


def _int(v: Any, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def validate(cfg: CliConfig) -> None:
    if not (1 <= len(cfg.command) <= 2) or not all(isinstance(p, Path) and p.is_absolute() and p.is_file() for p in cfg.command):
        raise CliRefused("config_command", "command must be 1-2 absolute existing files")
    if not (isinstance(cfg.repo, Path) and cfg.repo.is_absolute() and cfg.repo.is_dir()):
        raise CliRefused("config_repo")
    if not (isinstance(cfg.base_sha, str) and SHA40.fullmatch(cfg.base_sha)):
        raise CliRefused("config_base", "base_sha must be a full lowercase commit SHA")
    if not (isinstance(cfg.run_id, str) and RUN_ID.fullmatch(cfg.run_id)):
        raise CliRefused("config_run_id")
    if not (isinstance(cfg.run_dir, Path) and cfg.run_dir.is_absolute() and cfg.run_dir.is_dir()):
        raise CliRefused("config_run_dir")
    if not (isinstance(cfg.model, str) and MODEL.fullmatch(cfg.model)):
        raise CliRefused("config_model")
    if not (isinstance(cfg.max_budget_usd, str) and BUDGET.fullmatch(cfg.max_budget_usd) and cfg.max_budget_usd != "0.00"):
        raise CliRefused("config_budget", "an explicit budget like '1.50' (> 0) is required")
    if not _int(cfg.max_turns, 1, 500):
        raise CliRefused("config_turns")
    if not (_int(cfg.total_timeout_s, 1, 86_400) and _int(cfg.first_output_timeout_s, 1, cfg.total_timeout_s)
            and _int(cfg.inactivity_timeout_s, 1, cfg.total_timeout_s)):
        raise CliRefused("config_timeouts")
    if cfg.permission_mode not in PERMISSION_MODES:
        raise CliRefused("config_permission", "bypassPermissions and skip-permissions are never allowed")
    if not (isinstance(cfg.allowed_tools, tuple) and cfg.allowed_tools and all(t in SAFE_TOOLS for t in cfg.allowed_tools)):
        raise CliRefused("config_tools", f"allowed tools must be a subset of {SAFE_TOOLS}")
    if cfg.test_command is not None and not (isinstance(cfg.test_command, str) and TEST_COMMAND.fullmatch(cfg.test_command)):
        raise CliRefused("config_test_command")
    for name in ("stdout_line_max", "stdout_lines_max", "stdout_bytes_max", "stderr_keep", "kill_wait_s"):
        if not _int(getattr(cfg, name), 1, 1 << 30):
            raise CliRefused("config_bounds", name)
    if cfg.prepare is not None and not callable(cfg.prepare):
        raise CliRefused("config_prepare")
    if not isinstance(cfg.restricted, bool):
        raise CliRefused("config_restricted")
    if not RUN_ID.fullmatch(cfg.committer.replace("-", "")):
        raise CliRefused("config_committer")


def build_command(cfg: CliConfig) -> list[str]:
    """argv only (the prompt goes to stdin). Never --dangerously-skip-permissions; an EMPTY strict MCP config.
    Two separate controls (root review msgs 1501, 1526):
    - `--tools` sets which built-in tools EXIST for the worker (the exposed surface): the allowlist, plus
      `Bash` only when a test command is configured;
    - `--allowedTools` only AUTO-APPROVES calls: the allowlist and exactly `Bash(<test_command>)`, so any
      other Bash call needs a permission that headless mode cannot grant.
    `--restricted` (default on) also ignores user/project/local settings, confines file tools to the
    working directory, refuses bypassPermissions and protects settings/git/tool-config files."""
    exposed = list(cfg.allowed_tools) + (["Bash"] if cfg.test_command else [])
    approved = list(cfg.allowed_tools) + ([f"Bash({cfg.test_command})"] if cfg.test_command else [])
    return [*map(str, cfg.command), "-p", "--output-format", "stream-json", "--verbose", "--model", cfg.model,
            "--max-turns", str(cfg.max_turns), "--max-budget-usd", cfg.max_budget_usd, "--permission-mode", cfg.permission_mode,
            *(["--restricted"] if cfg.restricted else []), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-session-persistence", "--tools", ",".join(exposed), "--allowedTools", *approved]


def worker_env() -> dict[str, str]:
    """The allowlisted environment plus CONTROLLED values: no bytecode files from any Python the worker
    (or its test command) runs, so a test run cannot leave __pycache__ in the diff (msg 1545)."""
    return {**{k: os.environ[k] for k in ENV_ALLOW if k in os.environ}, "PYTHONDONTWRITEBYTECODE": "1"}


def git_env() -> dict[str, str]:
    """A CONTROLLED git environment for every supervisor/verifier git call: system and global config are
    disabled (no user hooks, aliases, autocrlf or filters from them), and git never prompts."""
    return {**{k: os.environ[k] for k in ENV_ALLOW if k in os.environ},
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120, env=git_env())


def tree_kill(proc: subprocess.Popen, wait_s: int) -> bool:
    """Kill the whole process tree; True only when the PARENT's exit is verified. An already-exited parent
    returns True immediately: its surviving descendants, if any, are not observable from here."""
    if proc.poll() is not None:
        return True
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=wait_s)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=wait_s)
        return True
    except subprocess.TimeoutExpired:
        return False


# --- bounded readers -------------------------------------------------------------------------------------

_EOF = object()


def _read_stdout(stream, q: queue.Queue, line_max: int) -> None:
    try:
        while True:
            line = stream.readline(line_max + 1)
            if not line:
                break
            if len(line) > line_max and not line.endswith(b"\n"):
                while True:                                   # drain the rest of the over-long line, discarding it
                    rest = stream.readline(line_max + 1)
                    if not rest or rest.endswith(b"\n"):
                        break
                q.put(("overflow", len(line)))
                continue
            q.put(("line", line))
    except (OSError, ValueError):
        pass
    finally:
        q.put((_EOF, None))


@dataclass
class _Stderr:
    keep: int
    kept: bytearray = field(default_factory=bytearray)
    digest: Any = field(default_factory=hashlib.sha256)
    total: int = 0


def _read_stderr(stream, acc: _Stderr) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            acc.digest.update(chunk)
            acc.total += len(chunk)
            room = acc.keep - len(acc.kept)
            if room > 0:
                acc.kept += chunk[:room]
    except (OSError, ValueError):
        pass


# --- worker-authored acts ----------------------------------------------------------------------------------

def assistant_act_lines(event: dict[str, Any]) -> list[str]:
    """`HEKATE-ACT` payloads from ASSISTANT-authored text blocks only. Tool use, tool results, user turns
    and partial deltas are ignored, so a marker quoted in a file or a tool output never counts."""
    if event.get("type") != "assistant":
        return []
    msg = event.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    out = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            out += [ln[len(ACT_PREFIX):] for ln in block["text"].splitlines() if ln.startswith(ACT_PREFIX)]
    return out


def build_act(raw: str, key: dict[str, Any], expected_seq: int) -> tuple[dict[str, Any] | None, str | None]:
    """Bind a worker line to the EXACT ExecutionKey; the worker must use the next sequence number."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, "act_json"
    if not isinstance(doc, dict) or doc.get("kind") not in ("worker_ack", "worker_progress"):
        return None, "act_kind"
    if doc.get("seq") != expected_seq or isinstance(doc.get("seq"), bool):
        return None, "act_seq"
    if doc["kind"] == "worker_ack":
        if set(doc) != {"kind", "seq"}:
            return None, "act_fields"
        return {"kind": "worker_ack", "key": key, "actSeq": expected_seq}, None
    if set(doc) != {"kind", "seq", "checkpointId", "evidence"} or not _int(doc.get("checkpointId"), 1, 2**31) \
            or not isinstance(doc.get("evidence"), str) or not doc["evidence"].strip() or len(doc["evidence"]) > 1024:
        return None, "act_fields"
    return {"kind": "worker_progress", "key": key, "actSeq": expected_seq, "checkpointId": doc["checkpointId"],
            "evidenceDigest": hashlib.sha256(doc["evidence"].encode("utf-8")).hexdigest()}, None


MODELS_MAX = 8
MODEL_REPORTED = re.compile(r"^[A-Za-z0-9._:\-\[\]@/]{1,128}$")


def reported_models(event: dict[str, Any]) -> list[str]:
    """CLI-REPORTED model identifiers (provenance as the CLI states it; not authenticated): `model` of a
    system/init event and `message.model` of an assistant event. Bounded and pattern-checked."""
    out = []
    if event.get("type") == "system" and event.get("subtype") == "init":
        out.append(event.get("model"))
    if event.get("type") == "assistant" and isinstance(event.get("message"), dict):
        out.append(event["message"].get("model"))
    return [m for m in out if isinstance(m, str) and MODEL_REPORTED.fullmatch(m)]


def result_class(event: dict[str, Any]) -> str:
    """success | error:<subtype> | malformed, for one stream-json `type=result` event. Only an event with
    subtype "success" and is_error exactly False is a success; an error subtype (e.g. a budget or turn
    limit) or is_error True is an error; anything else is malformed (msg 1492)."""
    subtype, is_error = event.get("subtype"), event.get("is_error")
    if not isinstance(subtype, str) or not isinstance(is_error, bool):
        return "malformed"
    if subtype == "success" and is_error is False:
        return "success"
    return f"error:{subtype[:64]}"


USAGE_LABEL = "cli-reported, not metered"


def reported_usage(event: dict[str, Any]) -> dict[str, Any]:
    """The CLI's OWN cost/turns/duration from one terminal `type=result` event (root msgs 2030/2034), recorded as
    reported and never metered or enforced (--max-budget-usd stays the guard). Each field is independently null
    unless well formed: the cost a finite number >= 0 (not bool), kept as a decimal STRING so the evidence holds no
    float; turns and duration integers >= 0 (not bool). Never changes the round's outcome."""
    def count(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    c = event.get("total_cost_usd")
    ok = isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) and c >= 0
    cost = (str(c) if isinstance(c, int) else repr(c)) if ok else None
    return {"label": USAGE_LABEL, "total_cost_usd": cost, "num_turns": count(event.get("num_turns")),
            "duration_ms": count(event.get("duration_ms"))}


# --- the adapter --------------------------------------------------------------------------------------------

@dataclass
class RunEvidence:
    worktree: str | None = None
    ref: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    kill_reason: str | None = None
    acts_accepted: int = 0
    acts_refused: dict[str, int] = field(default_factory=dict)
    stdout_lines: int = 0
    stdout_bytes: int = 0
    result_sha256: str | None = None
    stderr_sha256: str | None = None
    stderr_bytes: int = 0
    stderr_head: str = ""
    drained: bool | None = None          # abort path: whether every helper thread ended
    reported_models: list[str] = field(default_factory=list)   # CLI-REPORTED (system/init, assistant.message.model); not authenticated
    files_changed: int = 0
    results: list[str] = field(default_factory=list)     # one class per terminal `result` event
    reported_usage: dict[str, Any] | None = None         # CLI-REPORTED cost/turns/duration of the FIRST result event; not metered


class CliWorker:
    """A pilot `Worker`: __call__(WorkOrder) -> WorkReport. One instance per run; one worktree per round."""

    def __init__(self, cfg: CliConfig):
        validate(cfg)
        self.cfg = cfg
        self.evidence: dict[int, RunEvidence] = {}

    def prompt(self, order: P.WorkOrder) -> str:
        return (f"{order.task_text}\n\n"
                "Work only inside the current directory. Do NOT commit, branch or change git configuration; the supervisor commits.\n"
                "Report progress with lines of exactly this form in your own reply text (never inside files or tool calls):\n"
                f'{ACT_PREFIX}{{"kind":"worker_ack","seq":1}}  (once, first)\n'
                f'{ACT_PREFIX}{{"kind":"worker_progress","seq":N,"checkpointId":K,"evidence":"<one short line>"}}  '
                "(N = previous seq + 1, K = 1, 2, ...)\n")

    def __call__(self, order: P.WorkOrder) -> P.WorkReport:
        ev = self.evidence.setdefault(order.round, RunEvidence())
        cfg = self.cfg
        if order.journal is None or order.act is None or order.delivered is None:
            return P.WorkReport("failed", reason="adapter_needs_journal")
        wt = cfg.run_dir / f"wt-r{order.round}"
        if wt.exists():
            return P.WorkReport("failed", reason="worktree_exists")
        add = _git("worktree", "add", "--detach", str(wt), cfg.base_sha, cwd=cfg.repo)
        if add.returncode != 0:
            return P.WorkReport("failed", reason="worktree_add_failed")
        ev.worktree = str(wt)
        if _git("rev-parse", "HEAD", cwd=wt).stdout.strip() != cfg.base_sha:
            return P.WorkReport("unknown", reason="worktree_head_mismatch")
        if cfg.prepare is not None:
            try:
                cfg.prepare(wt)
            except Exception:  # noqa: BLE001 -- the host hook failed: no launch_intent, no spawn
                return P.WorkReport("failed", reason="prepare_failed")

        cmd = build_command(cfg)
        # The intent is durable BEFORE the process can exist.
        order.journal("launch_intent", {"runId": cfg.run_id, "round": order.round, "command": " ".join(cmd)[:1024],
                                        "worktreeRef": str(wt)[:512], "baseRef": cfg.base_sha, "budget": cfg.max_budget_usd,
                                        "maxTurns": cfg.max_turns, "timeoutS": cfg.total_timeout_s})
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(wt),
                                    env=worker_env(), creationflags=flags, start_new_session=(sys.platform != "win32"))
        except OSError:
            order.journal("exited", {"code": None, "reason": "spawn_failed"})
            return P.WorkReport("failed", reason="spawn_failed")
        ev.pid = proc.pid
        ctx: dict[str, Any] = {"threads": [], "queue": None}
        try:
            order.journal("launched", {"pid": proc.pid, "round": order.round, "observedBy": "supervisor"})   # spawn != ACK
            status, reason = self._supervise(proc, order, ev, ctx)
        except BaseException:
            # A callback (journal / act / delivered) or the supervisor itself failed while the worker may still
            # be alive: never leave it running unsupervised (review finding 6, msg 1561). Kill the tree, drain
            # and join the helpers, then re-raise. No `exited` record: the journal is what failed.
            self._abort(proc, ev, ctx)
            raise
        if status != "ok":
            return P.WorkReport(status, reason=reason, attested=True)
        return self._capture(order, ev, wt)

    def _abort(self, proc: subprocess.Popen, ev: RunEvidence, ctx: dict[str, Any]) -> None:
        """The exception path: tree-kill a live worker, drain the finite queue so the stdout reader can end,
        and join every helper thread within kill_wait_s. Best effort; it never raises."""
        ev.kill_reason = "supervisor_error"
        try:
            if proc.poll() is None:
                tree_kill(proc, self.cfg.kill_wait_s)
            deadline = time.monotonic() + self.cfg.kill_wait_s
            q = ctx.get("queue")
            while q is not None and any(t.is_alive() for t in ctx["threads"]) and time.monotonic() < deadline:
                try:
                    q.get(timeout=0.1)
                except queue.Empty:
                    pass
            for t in ctx["threads"]:
                t.join(timeout=max(0.0, deadline - time.monotonic()))
            ev.exit_code = proc.poll()
            ev.drained = not any(t.is_alive() for t in ctx["threads"])
        except BaseException:  # noqa: BLE001 -- the original exception is the one re-raised
            pass

    def _supervise(self, proc: subprocess.Popen, order: P.WorkOrder, ev: RunEvidence,
                   ctx: dict[str, Any] | None = None) -> tuple[str, str | None]:
        cfg = self.cfg
        ctx = ctx if ctx is not None else {"threads": [], "queue": None}
        # The deadline starts BEFORE the prompt is written: a child that never reads stdin must not be able
        # to block the supervisor (root review msg 1492). The prompt goes through a bounded writer thread.
        start = time.monotonic()
        q: queue.Queue = queue.Queue(maxsize=1024)
        err = _Stderr(cfg.stderr_keep)
        wrote = {"done": False, "error": False}

        def write_prompt() -> None:
            try:
                proc.stdin.write(self.prompt(order).encode("utf-8"))
                proc.stdin.close()
                wrote["done"] = True
            except (OSError, ValueError):
                wrote["error"] = True

        t_in = threading.Thread(target=write_prompt, daemon=True)
        t_out = threading.Thread(target=_read_stdout, args=(proc.stdout, q, cfg.stdout_line_max), daemon=True)
        t_err = threading.Thread(target=_read_stderr, args=(proc.stderr, err), daemon=True)
        ctx["queue"] = q
        for t in (t_in, t_out, t_err):
            ctx["threads"].append(t)
            t.start()
        delivered = False
        last = start
        got_first, kill_reason, seq, eof = False, None, 1, False
        while True:
            if not delivered and wrote["done"]:
                delivered = True
                order.delivered()                       # only after the whole prompt was written AND stdin closed
            now = time.monotonic()
            if now - start >= cfg.total_timeout_s:
                kill_reason = "total_timeout"
                break
            idle = now - last
            if not got_first and idle >= cfg.first_output_timeout_s:
                kill_reason = "first_output_timeout" if delivered else "prompt_not_delivered"
                break
            if got_first and idle >= cfg.inactivity_timeout_s:
                kill_reason = "inactivity"
                break
            if eof:
                break
            try:
                kind, item = q.get(timeout=0.1)
            except queue.Empty:
                continue
            if kind is _EOF:
                eof = True
                continue
            got_first, last = True, time.monotonic()
            if kind == "overflow":
                kill_reason = "stdout_line_cap"
                break
            ev.stdout_lines += 1
            ev.stdout_bytes += len(item)
            if ev.stdout_lines > cfg.stdout_lines_max or ev.stdout_bytes > cfg.stdout_bytes_max:
                kill_reason = "stdout_cap"
                break
            try:
                event = json.loads(item.decode("utf-8", errors="strict"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            for m in reported_models(event):
                if m not in ev.reported_models and len(ev.reported_models) < MODELS_MAX:
                    ev.reported_models.append(m)
            if event.get("type") == "result":
                ev.results.append(result_class(event))
                if ev.reported_usage is None:                # the first terminal result only; outcomes unchanged
                    ev.reported_usage = reported_usage(event)
                ev.result_sha256 = hashlib.sha256(item.rstrip(b"\r\n")).hexdigest()
            for raw in assistant_act_lines(event):
                act, why = build_act(raw, order.execution_key, seq)
                if act is not None:
                    d = order.act(json.dumps(act))
                    if d.outcome == "accepted":
                        ev.acts_accepted += 1
                        seq += 1
                        continue
                    why = f"intake_{d.outcome}"
                ev.acts_refused[why] = ev.acts_refused.get(why, 0) + 1
        verified = True
        if kill_reason is not None:
            ev.kill_reason = kill_reason
            verified = tree_kill(proc, cfg.kill_wait_s)
        else:
            try:
                proc.wait(timeout=cfg.kill_wait_s)
            except subprocess.TimeoutExpired:
                ev.kill_reason = kill_reason = "no_exit_after_eof"
                verified = tree_kill(proc, cfg.kill_wait_s)
        # Drain the finite queue so the stdout reader can finish, then join EVERY helper thread: the run is
        # only "drained" when the writer and both readers have ended.
        deadline = time.monotonic() + cfg.kill_wait_s
        while t_out.is_alive() and time.monotonic() < deadline:
            try:
                q.get(timeout=0.1)
            except queue.Empty:
                pass
        for t in (t_in, t_err, t_out):
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        threads_done = not (t_in.is_alive() or t_out.is_alive() or t_err.is_alive())
        if not delivered and wrote["done"]:                # the write completed just before the stream ended
            delivered = True
            order.delivered()
        ev.exit_code = proc.returncode
        ev.stderr_sha256, ev.stderr_bytes = err.digest.hexdigest(), err.total
        ev.stderr_head = bytes(err.kept[:512]).decode("utf-8", errors="replace")
        order.journal("exited", {"code": proc.returncode, "reason": kill_reason or "exit", "killVerified": verified,
                                 "drained": threads_done, "promptDelivered": delivered, "stdoutLines": ev.stdout_lines,
                                 "stderrBytes": err.total, "stderrSha256": ev.stderr_sha256, "results": list(ev.results),
                                 "requestedModel": cfg.model, "reportedModels": list(ev.reported_models)})
        if not verified or not threads_done:
            return "unknown", "kill_unconfirmed" if not verified else "not_drained"
        if kill_reason == "prompt_not_delivered" or not delivered:
            return "unknown", "prompt_not_delivered"
        if kill_reason is not None:
            return "failed", kill_reason
        if proc.returncode != 0:
            return "failed", "nonzero_exit"
        # A well-formed SUCCESS terminal result is required before anything is captured (msg 1492).
        if not ev.results:
            return "unknown", "no_result"
        if len(ev.results) > 1:
            return "unknown", "ambiguous_result"
        if ev.results[0] == "malformed":
            return "unknown", "result_malformed"
        if ev.results[0] != "success":
            return "failed", "result_error"
        return "ok", None

    def _capture(self, order: P.WorkOrder, ev: RunEvidence, wt: Path) -> P.WorkReport:
        cfg = self.cfg
        head = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        if head != cfg.base_sha:
            return P.WorkReport("failed", reason="worker_moved_head", attested=True)
        if _git("add", "-A", cwd=wt).returncode != 0:
            return P.WorkReport("unknown", reason="git_add_failed", attested=True)
        raw = _git("diff", "--cached", "--raw", "-z", "--no-renames", cwd=wt).stdout
        fields = [f for f in raw.split("\0") if f]
        entries = [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]
        if not entries:
            return P.WorkReport("failed", reason="empty_diff", attested=True)
        for meta, path in entries:
            modes = meta.lstrip(":").split()[:2]
            if "160000" in modes or path.startswith(("/", "../")) or "/../" in path or path.startswith(".git/"):
                return P.WorkReport("failed", reason="diff_refused", attested=True)
        ev.files_changed = len(entries)
        msg = f"hekate-pilot run {cfg.run_id} round {order.round}\n\nattempt {order.execution_key['attemptId']} epoch {order.execution_key['attemptEpoch']}"
        c = _git("-c", f"user.name={cfg.committer}", "-c", f"user.email={cfg.committer}@hekate.local", "-c", "commit.gpgsign=false",
                 "commit", "-q", "-m", msg, cwd=wt)
        if c.returncode != 0:
            return P.WorkReport("unknown", reason="commit_failed", attested=True)
        sha = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        parent = _git("rev-parse", "HEAD^", cwd=wt).stdout.strip()
        if not SHA40.fullmatch(sha) or parent != cfg.base_sha:
            return P.WorkReport("unknown", reason="commit_parent_mismatch", attested=True)
        ref = f"{REF_ROOT}/{cfg.run_id}/r{order.round}"
        if _git("update-ref", ref, sha, ZERO_OID, cwd=cfg.repo).returncode != 0:      # create-only
            return P.WorkReport("unknown", reason="ref_create_failed", attested=True)
        ev.ref = ref
        order.journal("result_captured", {"artifactRef": sha, "parentRef": cfg.base_sha, "runRef": ref,
                                          "filesChanged": ev.files_changed, "resultSha256": ev.result_sha256,
                                          "actsAccepted": ev.acts_accepted, "requestedModel": cfg.model,
                                          "reportedModels": list(ev.reported_models)})
        return P.WorkReport("ok", sha, checkpoints=0, attested=True)


# --- explicit operator cleanup ------------------------------------------------------------------------------

def cleanup(cfg: CliConfig, round_: int, *, remove_ref: bool = False) -> str:
    """Operator-invoked only. Removes the round's OWNED worktree when it is clean (never forced) and,
    only when asked, the run-owned ref. Anything unknown or dirty is refused, not deleted."""
    validate(cfg)
    wt = cfg.run_dir / f"wt-r{round_}"
    listed = _git("worktree", "list", "--porcelain", cwd=cfg.repo).stdout
    owned = any(Path(line[len("worktree "):]).resolve() == wt.resolve() for line in listed.splitlines() if line.startswith("worktree "))
    if not owned:
        return "refused_not_owned_worktree"
    if _git("status", "--porcelain", "--untracked-files=all", cwd=wt).stdout.strip():
        return "refused_dirty"
    if _git("worktree", "remove", str(wt), cwd=cfg.repo).returncode != 0 or wt.exists():
        return "refused_remove_failed"
    if remove_ref:
        ref = f"{REF_ROOT}/{cfg.run_id}/r{round_}"
        if _git("update-ref", "-d", ref, cwd=cfg.repo).returncode != 0:
            return "worktree_removed_ref_failed"
    return "removed"
