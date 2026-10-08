"""HK-ISSUE-007: a REAL Claude Code CLI worker adapter for the pilot driver (e1/pilot.py `Worker`).
Plan 048 adds a second backend, `codex exec --json` (CliConfig.backend="codex"): the same owned worktree, prompt,
worker-authored acts, timeouts, tree kill, supervisor commit and attempt trace; only the argv, the event parsing and
the recorded terms differ (see codex_command, codex_act_lines, codex_terminal, codex_usage, codex_worker_terms).
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
BACKENDS = ("claude", "codex")
BACKEND_KINDS = {"claude": ("claude-cli", "fake-cli"), "codex": ("codex-cli", "fake-cli")}
ENV_ALLOW = ("PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
ACT_PREFIX = "HEKATE-ACT "
ZERO_OID = "0" * 40
REF_ROOT = "refs/hekate-pilot"
SETUP_ERR_KEEP = 4096    # stderr chars kept from a failed round-setup git call


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
    execution_kind: str = "claude-cli"   # labels the attempt trace only (P.EXECUTION_KINDS); never changes behaviour
    # The worker CLI (plan 048): "claude" (default, unchanged) or "codex" (`codex exec --json`). For codex, `model` may
    # be None = the CLI's own default (requested nothing, reported nothing); the $/turn bounds are NOT enforced by it.
    backend: str = "claude"


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
    if cfg.backend not in BACKENDS:
        raise CliRefused("config_backend")
    if not ((isinstance(cfg.model, str) and MODEL.fullmatch(cfg.model)) or (cfg.backend == "codex" and cfg.model is None)):
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
    if cfg.execution_kind not in P.EXECUTION_KINDS or cfg.execution_kind not in BACKEND_KINDS[cfg.backend]:
        raise CliRefused("config_execution_kind")


def codex_command(cfg: CliConfig, workdir: Path) -> list[str]:
    """`codex exec` argv (plan 048, exactly the arguments proven by smoke-004 plus a writable workspace for the edit). The
    prompt goes to stdin (`-`). The user's config.toml (its MCP servers and credentials) is NOT loaded, nor are user
    execpolicy rules; approvals never prompt; the unelevated Windows sandbox confines writes to the worktree. Unlike
    Claude's Bash(<test_command>) allowlist, the sandbox lets the worker run ANY shell command inside the workspace.
    Never --dangerously-bypass-approvals-and-sandbox, --approve-for-me or codex's own --worktree."""
    return [*map(str, cfg.command), "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "workspace-write", "--color", "never", "-C", str(workdir), "-c", 'approval_policy="never"',
            "-c", 'windows.sandbox="unelevated"', *(["-m", cfg.model] if cfg.model else []), "-"]


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


def command_for(cfg: CliConfig, workdir: Path) -> list[str]:
    return codex_command(cfg, workdir) if cfg.backend == "codex" else build_command(cfg)


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def windows_apps_dir() -> str | None:
    base = os.environ.get("LOCALAPPDATA")
    return _norm(os.path.join(base, "Microsoft", "WindowsApps")) if base else None


def packaged_apps_root() -> str | None:
    base = os.environ.get("ProgramFiles")
    return _norm(os.path.join(base, "WindowsApps")) if base else None


def codex_path_excluded(entry: str) -> bool:
    """A PATH entry the codex worker must not see: the exact %LOCALAPPDATA%\\Microsoft\\WindowsApps alias directory, or
    %ProgramFiles%\\WindowsApps itself or any directory under it (a packaged app such as Store PowerShell 7). Neither can
    start under the unelevated sandbox's restricted token (smoke-003: alias, Access is denied; codex-trace-001: packaged
    pwsh 7.6.6, 0xC0070005). Everything else is kept, in order."""
    if not entry:
        return False
    p, alias, pkg = _norm(entry), windows_apps_dir(), packaged_apps_root()
    return p == alias or (pkg is not None and (p == pkg or p.startswith(pkg + os.sep)))


def worker_env(backend: str = "claude") -> dict[str, str]:
    """The allowlisted environment plus CONTROLLED values: no bytecode files from any Python the worker
    (or its test command) runs, so a test run cannot leave __pycache__ in the diff (msg 1545). For codex ONLY, the
    WindowsApps alias and packaged-app PATH entries are dropped (codex_path_excluded), so Codex's shell discovery falls
    back to System32 Windows PowerShell, which the sandbox can start (smoke-004, diag-002). Claude's PATH is unchanged."""
    env = {**{k: os.environ[k] for k in ENV_ALLOW if k in os.environ}, "PYTHONDONTWRITEBYTECODE": "1"}
    if backend == "codex" and "PATH" in env:
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if not codex_path_excluded(p))
    return env


def git_env() -> dict[str, str]:
    """A CONTROLLED git environment for every supervisor/verifier git call: system and global config are
    disabled (no user hooks, aliases, autocrlf or filters from them), and git never prompts."""
    return {**{k: os.environ[k] for k in ENV_ALLOW if k in os.environ},
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120, env=git_env())


def setup_error(step: str, p: subprocess.CompletedProcess[str], **extra: Any) -> dict[str, Any]:
    """A failed round-setup git call, kept as evidence (check-002 r2, root msg 2080): the step, git's rc and
    the bounded TAIL of its stderr (git puts the cause last), with the full length so a cut is visible."""
    err = p.stderr or ""
    return {"step": step, "rc": p.returncode, "stderr": err[-SETUP_ERR_KEEP:], "stderr_chars": len(err), **extra}


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


def _read_stdout(stream, q: queue.Queue, line_max: int, trace: "AttemptTrace | None" = None) -> None:
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
                if trace is not None:
                    trace.note("stdout_line_over_cap")
                q.put(("overflow", len(line)))
                continue
            if trace is not None:                             # receipt order, before the supervisor consumes it
                trace.stdout_line(line)
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


def _read_stderr(stream, acc: _Stderr, trace: "AttemptTrace | None" = None) -> None:
    try:
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                break
            if trace is not None:
                trace.stderr_chunk(chunk)
            acc.digest.update(chunk)
            acc.total += len(chunk)
            room = acc.keep - len(acc.kept)
            if room > 0:
                acc.kept += chunk[:room]
    except (OSError, ValueError):
        pass


# --- the attempt trace (root GO 2441; contract trace-contract-001 rev 2) --------------------------------------

TRACE_VERSION = "hekate-attempt-trace.v0"
TRACE_LINE_CUT = 256 << 10           # a retained stderr line longer than this is cut (stdout lines are bounded by stdout_line_max)
REASONING_BLOCKS = ("thinking", "redacted_thinking")


def trace_names(round_: int) -> tuple[str, str]:
    """The round's trace files, RELATIVE to the pilot run dir: (prompt, trace)."""
    return f"attempt-r{round_}.prompt.txt", f"attempt-r{round_}.trace.jsonl"


def redact_reasoning(event: Any) -> tuple[Any, bool]:
    """Drop private reasoning before retention: Claude assistant `thinking` / `redacted_thinking` content blocks and
    Codex `reasoning` items (their text). Everything else is kept as the CLI emitted it."""
    if not isinstance(event, dict):
        return event, False
    msg = event.get("message")
    if event.get("type") == "assistant" and isinstance(msg, dict) and isinstance(msg.get("content"), list):
        kept = [b for b in msg["content"] if not (isinstance(b, dict) and b.get("type") in REASONING_BLOCKS)]
        if len(kept) != len(msg["content"]):
            return {**event, "message": {**msg, "content": kept}}, True
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "reasoning":
        return {**event, "item": {k: v for k, v in item.items() if k in ("id", "type")}}, True
    return event, False


class AttemptTrace:
    """One round's retained conversation: the exact prompt bytes, and ONE ordered JSONL file of what the worker
    printed on stdout and stderr, in receipt order, plus fixed supervisor notes. Records are
    {seq, tMs, stream: stdout|stderr|hekate, text, cut, redacted}. Retention uses the adapter's EXISTING caps
    (stdout_lines_max / stdout_bytes_max, stderr_keep); a cap hit is one `hekate` note, never silent. The retained
    hash describes exactly the retained file bytes, unlike the full-stream stderr digest. Thread-safe; every write is
    flushed; after close() further writes are ignored so the final hash always matches the file."""

    def __init__(self, cfg: CliConfig, round_: int):
        self.run_dir = cfg.run_dir
        self.prompt_name, self.trace_name = trace_names(round_)
        self.kind = cfg.execution_kind
        self.caps = {"stdout_lines": cfg.stdout_lines_max, "stdout_bytes": cfg.stdout_bytes_max, "stderr_bytes": cfg.stderr_keep}
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.seq = 0
        self.digest = hashlib.sha256()
        self.bytes = 0
        self.stdout_lines = self.stdout_bytes = self.stderr_raw = 0
        self.capped: set[str] = set()
        self.pending = bytearray()           # a stderr partial line
        self.skipping = False                # inside an over-long stderr line, after its cut record
        self.prompt_meta: dict[str, Any] | None = None
        self.final: dict[str, Any] | None = None
        self.f = None

    def ref(self) -> dict[str, Any]:
        """The launch_intent block, written BEFORE spawn so a live, killed or aborted attempt can be found."""
        return {"version": TRACE_VERSION, "executionKind": self.kind, "runDir": str(self.run_dir),
                "prompt": self.prompt_name, "trace": self.trace_name}

    def open(self, prompt: bytes) -> None:
        """Create both files exclusively (an existing file is never overwritten) and write the prompt bytes."""
        with open(self.run_dir / self.prompt_name, "xb") as p:
            p.write(prompt)
        self.prompt_meta = {"bytes": len(prompt), "sha256": hashlib.sha256(prompt).hexdigest()}
        self.f = open(self.run_dir / self.trace_name, "xb")

    def start(self) -> None:
        self.t0 = time.monotonic()           # tMs counts from the spawn

    def _write(self, stream: str, text: str, cut: bool = False, redacted: bool = False) -> None:
        """Append one record. JSON with ASCII escapes is lossless for ANY Python text (a lone surrogate decoded from a
        worker's JSON escape included), so encoding cannot fail. After any write failure nothing more is written:
        the digest then still describes a prefix the supervisor wrote, and the trace is reported incomplete."""
        if self.f is None or self.final is not None or "write_error" in self.capped:
            return
        rec = {"seq": self.seq, "tMs": int((time.monotonic() - self.t0) * 1000), "stream": stream, "text": text,
               "cut": cut, "redacted": redacted}
        try:
            b = (json.dumps(rec, ensure_ascii=True) + "\n").encode("ascii")
            self.f.write(b)
            self.f.flush()
        except Exception:  # noqa: BLE001 -- retention must never stop the worker's protocol processing
            self.capped.add("write_error")
            return
        self.seq += 1
        self.bytes += len(b)
        self.digest.update(b)

    def note(self, text: str) -> None:
        with self.lock:
            self._write("hekate", text)

    def _cap(self, name: str) -> None:
        if name not in self.capped:
            self.capped.add(name)
            self._write("hekate", f"{name}_cap_reached")

    def stdout_line(self, raw: bytes) -> None:
        """Called from the stdout reader BEFORE the line is queued for the supervisor: it never raises."""
        try:
            line = raw.rstrip(b"\r\n")
            text, redacted = line.decode("utf-8", errors="replace"), False
            try:
                event = json.loads(line.decode("utf-8", errors="strict"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                event = None
            if event is not None:
                event, redacted = redact_reasoning(event)
                if redacted:
                    text = json.dumps(event, ensure_ascii=True)
            with self.lock:
                if "stdout" in self.capped:
                    return
                if self.stdout_lines + 1 > self.caps["stdout_lines"] or self.stdout_bytes + len(raw) > self.caps["stdout_bytes"]:
                    self._cap("stdout")
                    return
                self.stdout_lines += 1
                self.stdout_bytes += len(raw)
                self._write("stdout", text, redacted=redacted)
        except Exception:  # noqa: BLE001
            self.capped.add("write_error")

    def _stderr_line(self, line: bytes, cut: bool) -> None:
        self._write("stderr", line.rstrip(b"\r").decode("utf-8", errors="replace"), cut=cut)

    def stderr_chunk(self, chunk: bytes) -> None:
        """Split arbitrary stderr chunks into lines (a partial line waits for the next chunk). Retention is bounded at
        INTAKE by stderr_keep RAW bytes, delimiters included, so even a flood of empty lines stops at the cap: the
        bounded prefix is kept (a partial line at the cap as one `cut` record) plus one cap note. A line longer than
        TRACE_LINE_CUT is retained once, cut, and the rest of it up to its newline is skipped. Never raises."""
        with self.lock:
            try:
                if "stderr" in self.capped:
                    return
                take = chunk[:max(0, self.caps["stderr_bytes"] - self.stderr_raw)]
                self.stderr_raw += len(take)
                data = take
                while data:
                    i = data.find(b"\n")
                    if i < 0:                                        # an unterminated partial line
                        if not self.skipping:
                            self.pending += data
                            if len(self.pending) > TRACE_LINE_CUT:
                                self._stderr_line(bytes(self.pending[:TRACE_LINE_CUT]), True)
                                self.pending.clear()
                                self.skipping = True
                        break
                    seg, data = data[:i], data[i + 1:]
                    if self.skipping:                                # the newline that ends an already cut line
                        self.skipping = False
                        continue
                    whole = bytes(self.pending) + seg
                    self.pending.clear()
                    self._stderr_line(whole[:TRACE_LINE_CUT], len(whole) > TRACE_LINE_CUT)
                if len(take) < len(chunk):
                    if self.pending and not self.skipping:
                        self._stderr_line(bytes(self.pending), True)
                    self.pending.clear()
                    self._cap("stderr")
            except Exception:  # noqa: BLE001
                self.capped.add("write_error")

    def close(self, *, complete: bool, notes: tuple[str, ...] = ()) -> dict[str, Any] | None:
        """Flush a partial stderr line, write the closing notes, close the file and freeze the final metadata. A
        write failure is reported (writeError) and makes the trace incomplete: it can never be verified as whole."""
        with self.lock:
            if self.final is not None or self.f is None:
                return self.final
            if self.pending and not self.skipping:
                self._stderr_line(bytes(self.pending), False)
            self.pending.clear()
            for n in notes:
                self._write("hekate", n)
            try:
                self.f.close()
            except Exception:  # noqa: BLE001
                self.capped.add("write_error")
            failed = "write_error" in self.capped
            self.final = {"prompt": dict(self.prompt_meta or {}),
                          "trace": {"bytes": self.bytes, "sha256": self.digest.hexdigest(), "records": self.seq,
                                    "capped": bool(self.capped - {"write_error"}), "writeError": failed},
                          "complete": complete and not failed}
            return self.final


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
    if isinstance(c, int) and not isinstance(c, bool):          # every Python int is finite; never math.isfinite on
        cost = str(c) if c >= 0 else None                       # one (a huge JSON integer overflows it; root msg 2045)
    elif isinstance(c, float):
        cost = repr(c) if math.isfinite(c) and c >= 0 else None
    else:
        cost = None
    return {"label": USAGE_LABEL, "total_cost_usd": cost, "num_turns": count(event.get("num_turns")),
            "duration_ms": count(event.get("duration_ms"))}


# --- codex exec --json events (plan 048; shapes from smoke-004) -------------------------------------------------

# Codex's shell on this host is Windows PowerShell 5.1, which reads files without a BOM as ANSI (smoke-004 showed UTF-8
# README text as mojibake). Codex-only task context (root msg 2487); the Claude prompt is unchanged. Writes go through
# Codex's own patch tool: PowerShell 5.1's `Set-Content -Encoding UTF8` would prepend a byte-order mark to the file.
CODEX_ENCODING_NOTE = ("Repository text files are UTF-8 without a byte-order mark. When you read a file through PowerShell, "
                       "always pass -Encoding UTF8 (for example Get-Content -Encoding UTF8 -LiteralPath <file>). Make every "
                       "edit with your file-editing (patch) tool, never by writing files through PowerShell, and keep every "
                       "non-ASCII character exactly as it is.\n")
CODEX_USAGE_LABEL ="cli-reported tokens, not metered; no cost, turns or duration reported"
CODEX_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")


def codex_act_lines(event: dict[str, Any]) -> list[str]:
    """`HEKATE-ACT` payloads from the worker's OWN messages only: item.completed with item.type agent_message. Command
    output (command_execution aggregated_output), file changes and anything else never count."""
    item = event.get("item")
    if event.get("type") != "item.completed" or not isinstance(item, dict) or item.get("type") != "agent_message":
        return []
    text = item.get("text")
    return [ln[len(ACT_PREFIX):] for ln in text.splitlines() if ln.startswith(ACT_PREFIX)] if isinstance(text, str) else []


def codex_terminal(event: dict[str, Any]) -> str | None:
    """The class of a terminal codex event, else None. `turn.completed` only means the TURN ENDED ("success" here only
    permits the supervisor's capture checks; acceptance stays the verifier's). `turn.failed` or a top-level `error`
    event is an error."""
    t = event.get("type")
    if t == "turn.completed":
        return "success"
    if t == "turn.failed":
        return "error:turn_failed"
    if t == "error":
        return "error:error"
    return None


def codex_usage(event: dict[str, Any]) -> dict[str, Any]:
    """Token counts from a turn.completed `usage` (each independently null unless an int >= 0, not bool). Codex reports no
    cost, turn count or duration, so those stay null."""
    u = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    tokens = {k: (u.get(k) if isinstance(u.get(k), int) and not isinstance(u.get(k), bool) and u.get(k) >= 0 else None)
              for k in CODEX_TOKEN_FIELDS}
    return {"label": CODEX_USAGE_LABEL, "total_cost_usd": None, "num_turns": None, "duration_ms": None, "tokens": tokens}


def codex_worker_terms(cfg: CliConfig) -> dict[str, Any]:
    """The truthful terms of a codex launch (root GO 2469): which model was requested (None = the CLI default; codex
    reports no model), that the $ budget and turn limit are NOT enforced by the CLI (only the supervisor's timeouts and
    output caps are), that usage is tokens only, and the shell scope. Nothing about billing is inferred."""
    return {"backend": "codex", "requestedModel": cfg.model, "requestedModelSource": "override" if cfg.model else "cli-default",
            "reportedModel": "unreported", "budgetEnforced": False, "turnsEnforced": False,
            "enforcedBounds": ["total_timeout_s", "first_output_timeout_s", "inactivity_timeout_s", "stdout caps", "stderr retention cap"],
            "usage": "tokens only", "shellScope": "any shell command inside the workspace-write sandbox (no per-command allowlist)",
            "sandbox": {"mode": "workspace-write", "windows": "unelevated"}, "userConfig": "ignored", "execPolicyRules": "ignored"}


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
    setup_error: dict[str, Any] | None = None            # a failed worktree add / HEAD check: step, rc, bounded stderr
    trace: dict[str, Any] | None = None                  # the attempt trace: launch ref plus final retained-file metadata


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
                "(N = previous seq + 1, K = 1, 2, ...)\n" + (CODEX_ENCODING_NOTE if self.cfg.backend == "codex" else ""))

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
            ev.setup_error = setup_error("worktree_add", add)
            return P.WorkReport("failed", reason="worktree_add_failed")
        ev.worktree = str(wt)
        head = _git("rev-parse", "HEAD", cwd=wt)
        if head.stdout.strip() != cfg.base_sha:
            ev.setup_error = setup_error("worktree_head", head, head=head.stdout.strip()[:64])
            return P.WorkReport("unknown", reason="worktree_head_mismatch")
        if cfg.prepare is not None:
            try:
                cfg.prepare(wt)
            except Exception:  # noqa: BLE001 -- the host hook failed: no launch_intent, no spawn
                return P.WorkReport("failed", reason="prepare_failed")

        cmd = command_for(cfg, wt)
        prompt = self.prompt(order).encode("utf-8")
        trace = AttemptTrace(cfg, order.round)
        try:
            trace.open(prompt)                    # the exact stdin bytes; files are never overwritten
        except OSError:
            return P.WorkReport("failed", reason="trace_setup_failed")
        ev.trace = {"ref": trace.ref(), "final": None}
        # The intent is durable BEFORE the process can exist; it names the trace files so a live, killed or
        # aborted attempt can be found even when no `exited` record is ever written.
        intent = {"runId": cfg.run_id, "round": order.round, "command": " ".join(cmd)[:1024],
                  "worktreeRef": str(wt)[:512], "baseRef": cfg.base_sha, "budget": cfg.max_budget_usd,
                  "maxTurns": cfg.max_turns, "timeoutS": cfg.total_timeout_s, "trace": trace.ref()}
        if cfg.backend == "codex":                # what this CLI does NOT enforce, said where the launch is recorded
            intent["worker"] = codex_worker_terms(cfg)
        try:
            order.journal("launch_intent", intent)
        except BaseException:
            ev.trace["final"] = trace.close(complete=False, notes=("trace_incomplete:launch_intent_failed",))
            raise
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        trace.start()
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(wt),
                                    env=worker_env(cfg.backend), creationflags=flags, start_new_session=(sys.platform != "win32"))
        except OSError:
            ev.trace["final"] = trace.close(complete=True, notes=("spawn_failed",))
            order.journal("exited", {"code": None, "reason": "spawn_failed", "trace": ev.trace["final"]})
            return P.WorkReport("failed", reason="spawn_failed")
        ev.pid = proc.pid
        ctx: dict[str, Any] = {"threads": [], "queue": None, "trace": trace, "prompt": prompt}
        try:
            order.journal("launched", {"pid": proc.pid, "round": order.round, "observedBy": "supervisor"})   # spawn != ACK
            status, reason = self._supervise(proc, order, ev, ctx)
        except BaseException:
            # A callback (journal / act / delivered) or the supervisor itself failed while the worker may still
            # be alive: never leave it running unsupervised (review finding 6, msg 1561). Kill the tree, drain
            # and join the helpers, then re-raise. No `exited` record: the journal is what failed. The trace is
            # still finalized (complete=false) into the round evidence where possible.
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
        trace = ctx.get("trace")
        if trace is not None and ev.trace is not None:
            try:
                ev.trace["final"] = trace.close(complete=False, notes=("killed:supervisor_error", "trace_incomplete:supervisor_error"))
            except BaseException:  # noqa: BLE001 -- best effort; the original exception is the one re-raised
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
        trace: AttemptTrace | None = ctx.get("trace")
        prompt = ctx.get("prompt") or self.prompt(order).encode("utf-8")

        def write_prompt() -> None:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
                wrote["done"] = True
            except (OSError, ValueError):
                wrote["error"] = True

        t_in = threading.Thread(target=write_prompt, daemon=True)
        t_out = threading.Thread(target=_read_stdout, args=(proc.stdout, q, cfg.stdout_line_max, trace), daemon=True)
        t_err = threading.Thread(target=_read_stderr, args=(proc.stderr, err, trace), daemon=True)
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
            codex = cfg.backend == "codex"
            for m in ([] if codex else reported_models(event)):          # codex reports no model
                if m not in ev.reported_models and len(ev.reported_models) < MODELS_MAX:
                    ev.reported_models.append(m)
            terminal = codex_terminal(event) if codex else (result_class(event) if event.get("type") == "result" else None)
            if terminal is not None:
                ev.results.append(terminal)
                if ev.reported_usage is None:                # the first terminal result only; outcomes unchanged
                    ev.reported_usage = codex_usage(event) if codex else reported_usage(event)
                ev.result_sha256 = hashlib.sha256(item.rstrip(b"\r\n")).hexdigest()
            for raw in (codex_act_lines(event) if codex else assistant_act_lines(event)):
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
        exited = {"code": proc.returncode, "reason": kill_reason or "exit", "killVerified": verified,
                  "drained": threads_done, "promptDelivered": delivered, "stdoutLines": ev.stdout_lines,
                  "stderrBytes": err.total, "stderrSha256": ev.stderr_sha256, "results": list(ev.results),
                  "requestedModel": cfg.model, "reportedModels": list(ev.reported_models)}
        if trace is not None and ev.trace is not None:
            # Frozen BEFORE the record, so the journal's hash is the retained file's; a reader still alive after an
            # undrained join cannot change the file afterwards (writes after close are ignored).
            notes = ((f"killed:{kill_reason}",) if kill_reason else ()) + (f"exit:{proc.returncode}",)
            ev.trace["final"] = trace.close(complete=True, notes=notes)
            exited["trace"] = ev.trace["final"]
        order.journal("exited", exited)
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
