"""supervised-task-spec.v0: the CLOSED operator task specification (bridge msgs 1626/1632/1636/1644). Pure.

Loaded strictly (valid UTF-8, no duplicate keys, no NaN/Infinity, NO floats anywhere), bounded (64 KiB),
and validated key by key: exactly the frozen keys at every level, typed, bounded and cross-checked. Any
deviation is a typed SpecRefused before anything else happens (no clone, no install, no spawn).

Effects (clone, install, baseline, run) live in e1/task_runner.py; this module never touches the disk
beyond reading the spec file.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from e1 import cli_worker as W
from e1 import consumer as C

SPEC_VERSION = "supervised-task-spec.v0"
SPEC_VERSION_V1 = "supervised-task-spec.v1"      # v0 plus ONE trusted supervisor formatter and one more verify step slot
SPEC_MAX = 64 << 10
PRETTIER_ENTRY = re.compile(r"^node_modules/prettier/[A-Za-z0-9._/\-]{1,200}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
STEP_NAME = re.compile(r"^[a-z0-9-]{1,32}$")
NODE_VERSION = re.compile(r"^v\d{1,3}\.\d{1,3}\.\d{1,3}$")
NPM_VERSION = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
ARGV_ELEMENT = re.compile(r"^[A-Za-z0-9._/:=\-]{1,300}$")       # literal; no spaces, quotes, globs or shell metacharacters
FORBIDDEN_ALLOW = re.compile(r"(^|/)(node_modules|\.git)(/|$)|^package(-lock)?\.json$|^npm-shrinkwrap\.json$|(^|/)\.[^/]+$")


class SpecRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class TaskSpec:
    doc: dict[str, Any]
    sha256: str                   # of the exact spec file bytes
    raw: bytes

    def step(self, name: str) -> dict[str, Any]:
        return next(s for s in self.doc["verify"]["steps"] if s["name"] == name)

    @property
    def formatter(self) -> dict[str, Any] | None:
        """The v1 trusted formatter descriptor; None for every v0 spec."""
        return self.doc.get("formatter")


def _fail(code: str, detail: str = ""):
    raise SpecRefused(code, detail)


def _exact(obj: Any, keys: tuple[str, ...], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict) or set(obj) != set(keys):
        got = sorted(obj) if isinstance(obj, dict) else type(obj).__name__
        _fail("spec_shape", f"{where}: keys must be exactly {sorted(keys)}, got {got}")
    return obj


def _str(v: Any, where: str, lo: int = 1, hi: int = 1000, pattern: re.Pattern | None = None) -> str:
    if not isinstance(v, str) or not lo <= len(v) <= hi or (pattern is not None and not pattern.fullmatch(v)):
        _fail("spec_value", where)
    return v


def _int(v: Any, where: str, lo: int, hi: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        _fail("spec_value", f"{where} must be an int in {lo}..{hi}")
    return v


def _abs_path(v: Any, where: str) -> str:
    s = _str(v, where, 3, 400)
    if not PurePosixPath(s.replace("\\", "/")).is_absolute() and not re.match(r"^[A-Za-z]:[\\/]", s):
        _fail("spec_value", f"{where} must be absolute")
    return s


def _rel_path(v: Any, where: str) -> str:
    s = _str(v, where, 1, 300)
    p = PurePosixPath(s)
    if "\\" in s or p.is_absolute() or ".." in p.parts or s.startswith("./") or s != str(p) or re.match(r"^[A-Za-z]:", s):
        _fail("spec_value", f"{where} must be a normalized repo-relative path")
    return s


def _no_float(v: Any, where: str = "spec") -> None:
    if isinstance(v, float):
        _fail("spec_float", where)
    if isinstance(v, dict):
        for k, x in v.items():
            _no_float(x, f"{where}.{k}")
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _no_float(x, f"{where}[{i}]")


def _argv(v: Any, where: str, node_path: str, lo: int = 2, hi: int = 16) -> list[str]:
    if not isinstance(v, list) or not lo <= len(v) <= hi:
        _fail("spec_value", f"{where} must be a list of {lo}..{hi} literal elements")
    if v[0] != node_path:
        _fail("spec_argv", f"{where}[0] must be hashes.pinnedNodeExe.path")
    for i, a in enumerate(v[1:], 1):
        _str(a, f"{where}[{i}]", 1, 300, ARGV_ELEMENT)
        # A path stays inside the repo as an operand AND as a flag value (msg 1659 F2): an operand, or the value
        # after a flag's first "=", that looks like a path must be a normalized repo-relative path; and no element
        # holds a ".." segment or an absolute or drive path anywhere.
        value = a if not a.startswith("-") else (a.split("=", 1)[1] if "=" in a else "")
        if value and ("/" in value or "." in value or ":" in value):
            _rel_path(value, f"{where}[{i}]")
        if ".." in re.split(r"[/=]", a) or re.search(r"(^|=)/|(^|[=/])[A-Za-z]:", a):
            _fail("spec_value", f"{where}[{i}] must not escape the repo")
    return v


def parse(raw: bytes) -> TaskSpec:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > SPEC_MAX:
        _fail("spec_size", f"at most {SPEC_MAX} bytes")
    try:
        doc = C.strict_loads(bytes(raw), "spec")
    except C.Refused as e:
        raise SpecRefused("spec_json", e.code) from None
    except RecursionError:
        raise SpecRefused("spec_json", "too deep") from None
    _no_float(doc)
    validate(doc)
    return TaskSpec(doc, hashlib.sha256(raw).hexdigest(), bytes(raw))


def load(path: Path) -> TaskSpec:
    p = Path(path)
    try:
        with open(p, "rb") as f:
            raw = f.read(SPEC_MAX + 1)
    except OSError:
        raise SpecRefused("spec_unreadable") from None
    return parse(raw)


def validate(d: Any) -> None:
    """v0 exactly as before; v1 is v0's rules on the same document minus `formatter` (and one more verify step),
    plus the formatter descriptor. A v0 document carrying a `formatter` key is a spec_shape refusal."""
    if isinstance(d, dict) and d.get("specVersion") == SPEC_VERSION_V1:
        _exact(d, ("specVersion", "source", "task", "allow", "oracle", "verify", "worker", "hashes", "deps", "metadata", "formatter"), "spec")
        _validate_v0_rules({**{k: v for k, v in d.items() if k != "formatter"}, "specVersion": SPEC_VERSION}, max_steps=5)
        _validate_formatter(d["formatter"], d)
        return
    _validate_v0_rules(d, max_steps=4)


def _validate_formatter(f: Any, d: dict[str, Any]) -> None:
    _exact(f, ("prettierEntry", "config", "paths", "timeoutS", "outputKeepBytes"), "formatter")
    entry = _exact(f["prettierEntry"], ("path", "version", "sha256"), "formatter.prettierEntry")
    _str(entry["path"], "formatter.prettierEntry.path", 1, 300, PRETTIER_ENTRY)
    _rel_path(entry["path"], "formatter.prettierEntry.path")
    _str(entry["path"], "formatter.prettierEntry.path", 1, 300, ARGV_ELEMENT)
    _str(entry["version"], "formatter.prettierEntry.version", 1, 16, NPM_VERSION)
    _str(entry["sha256"], "formatter.prettierEntry.sha256", 64, 64, HEX64)
    cfg = _exact(f["config"], ("path", "sha256"), "formatter.config")       # the pinned config: prettier reads no other
    _rel_path(cfg["path"], "formatter.config.path")
    _str(cfg["path"], "formatter.config.path", 1, 300, ARGV_ELEMENT)
    _str(cfg["sha256"], "formatter.config.sha256", 64, 64, HEX64)
    allow = {a["path"] for a in d["allow"]}
    oracle = {o["path"] for o in d["oracle"]["files"]}
    if cfg["path"].startswith("-") or cfg["path"] in allow | oracle or cfg["path"].startswith("node_modules/"):
        _fail("spec_formatter", "formatter.config.path must be a tracked config outside allow, oracle and node_modules")
    paths = f["paths"]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 8:
        _fail("spec_value", "formatter.paths must hold 1..8 entries")
    for i, p in enumerate(paths):
        _rel_path(p, f"formatter.paths[{i}]")
        _str(p, f"formatter.paths[{i}]", 1, 300, ARGV_ELEMENT)
        if p.startswith("-") or p not in allow:
            _fail("spec_formatter", f"formatter.paths[{i}] must be one of the allow paths and not look like a flag")
    if len(set(paths)) != len(paths):
        _fail("spec_formatter", "duplicate formatter path")
    _int(f["timeoutS"], "formatter.timeoutS", 1, 120)
    _int(f["outputKeepBytes"], "formatter.outputKeepBytes", 1, 64 << 10)


def _validate_v0_rules(d: Any, max_steps: int) -> None:
    _exact(d, ("specVersion", "source", "task", "allow", "oracle", "verify", "worker", "hashes", "deps", "metadata"), "spec")
    if d["specVersion"] != SPEC_VERSION:
        _fail("spec_version")
    src = _exact(d["source"], ("repo", "anchorCommit", "taskBaseCommit"), "source")
    _abs_path(src["repo"], "source.repo")
    _str(src["anchorCommit"], "source.anchorCommit", 40, 40, HEX40)
    _str(src["taskBaseCommit"], "source.taskBaseCommit", 40, 40, HEX40)
    if src["anchorCommit"] == src["taskBaseCommit"]:
        _fail("spec_value", "anchorCommit must differ from taskBaseCommit (the oracle is added after the anchor)")
    task = _exact(d["task"], ("text", "criteria"), "task")
    _str(task["text"], "task.text", 1, 8000)
    _str(task["criteria"], "task.criteria", 1, 1000)

    h = _exact(d["hashes"], ("pinnedNodeExe", "npmCli", "packageLock", "vitestEntry", "tscEntry"), "hashes")
    node = _exact(h["pinnedNodeExe"], ("path", "version", "sha256"), "hashes.pinnedNodeExe")
    node_path = _abs_path(node["path"], "hashes.pinnedNodeExe.path")
    _str(node["version"], "hashes.pinnedNodeExe.version", 1, 16, NODE_VERSION)
    _str(node["sha256"], "hashes.pinnedNodeExe.sha256", 64, 64, HEX64)
    npm = _exact(h["npmCli"], ("path", "version", "sha256"), "hashes.npmCli")
    _abs_path(npm["path"], "hashes.npmCli.path")
    _str(npm["version"], "hashes.npmCli.version", 1, 16, NPM_VERSION)
    _str(npm["sha256"], "hashes.npmCli.sha256", 64, 64, HEX64)
    for k in ("packageLock", "vitestEntry", "tscEntry"):
        _str(h[k], f"hashes.{k}", 64, 64, HEX64)

    oracle = _exact(d["oracle"], ("files", "baseline"), "oracle")
    files = oracle["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 8:
        _fail("spec_value", "oracle.files must hold 1..8 entries")
    oracle_paths: set[str] = set()
    for i, f in enumerate(files):
        _exact(f, ("path", "sha256"), f"oracle.files[{i}]")
        p = _rel_path(f["path"], f"oracle.files[{i}].path")
        _str(f["sha256"], f"oracle.files[{i}].sha256", 64, 64, HEX64)
        if p in oracle_paths:
            _fail("spec_value", "duplicate oracle path")
        oracle_paths.add(p)

    allow = d["allow"]
    if not isinstance(allow, list) or not 1 <= len(allow) <= 8:
        _fail("spec_value", "allow must hold 1..8 entries")
    seen: set[str] = set()
    for i, a in enumerate(allow):
        _exact(a, ("path", "status", "mode"), f"allow[{i}]")
        p = _rel_path(a["path"], f"allow[{i}].path")
        if a["status"] != "M" or a["mode"] != "100644":
            _fail("spec_value", f"allow[{i}]: v0 allows only status M with mode 100644")
        if p in seen or p in oracle_paths or FORBIDDEN_ALLOW.search(p):
            _fail("spec_allow", f"allow[{i}].path {p!r} is duplicated, an oracle file, a package/lock/dot-config or under node_modules/.git")
        seen.add(p)

    steps = _exact(d["verify"], ("steps",), "verify")["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= max_steps:
        _fail("spec_value", f"verify.steps must hold 1..{max_steps} steps")
    names: list[str] = []
    for i, s in enumerate(steps):
        _exact(s, ("name", "argv", "timeoutS", "outputKeepBytes"), f"verify.steps[{i}]")
        names.append(_str(s["name"], f"verify.steps[{i}].name", 1, 32, STEP_NAME))
        _argv(s["argv"], f"verify.steps[{i}].argv", node_path)
        _int(s["timeoutS"], f"verify.steps[{i}].timeoutS", 1, 900)
        _int(s["outputKeepBytes"], f"verify.steps[{i}].outputKeepBytes", 1, 1 << 20)
    if len(set(names)) != len(names) or names.count("oracle") != 1:
        _fail("spec_value", "step names must be unique and exactly one must be 'oracle'")

    base = _exact(oracle["baseline"], ("argv", "timeoutS", "reportMaxBytes", "expectedExit", "cases"), "oracle.baseline")
    _argv(base["argv"], "oracle.baseline.argv", node_path)
    _int(base["timeoutS"], "oracle.baseline.timeoutS", 1, 900)
    _int(base["reportMaxBytes"], "oracle.baseline.reportMaxBytes", 1, 4 << 20)
    _int(base["expectedExit"], "oracle.baseline.expectedExit", 0, 255)
    cases = base["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 200:
        _fail("spec_value", "oracle.baseline.cases must hold 1..200 entries")
    keys: set[tuple[str, str]] = set()
    for i, c in enumerate(cases):
        _exact(c, ("file", "fullName", "status", "failureFirstLine"), f"oracle.baseline.cases[{i}]")
        if c["file"] not in oracle_paths:
            _fail("spec_value", f"oracle.baseline.cases[{i}].file must be an oracle file")
        _str(c["fullName"], f"oracle.baseline.cases[{i}].fullName", 1, 1000)
        if c["status"] == "failed":
            _str(c["failureFirstLine"], f"oracle.baseline.cases[{i}].failureFirstLine", 1, 2000)
        elif c["status"] == "passed":
            if c["failureFirstLine"] is not None:
                _fail("spec_value", f"oracle.baseline.cases[{i}]: a passed case has failureFirstLine null")
        else:
            _fail("spec_value", f"oracle.baseline.cases[{i}].status must be passed|failed")
        k = (c["file"], c["fullName"])
        if k in keys:
            _fail("spec_value", "duplicate baseline case")
        keys.add(k)

    wk = _exact(d["worker"], ("testCommand", "model", "budgetUsd", "maxRounds", "maxTurns"), "worker")
    oracle_step = next(s for s in steps if s["name"] == "oracle")
    if wk["testCommand"] != " ".join(oracle_step["argv"]):
        _fail("spec_worker", "worker.testCommand must equal the oracle step argv joined by single spaces")
    _str(wk["testCommand"], "worker.testCommand", 1, 200, W.TEST_COMMAND)
    _str(wk["model"], "worker.model", 1, 64, W.MODEL)
    _str(wk["budgetUsd"], "worker.budgetUsd", 4, 6, W.BUDGET)
    if wk["budgetUsd"] == "0.00":
        _fail("spec_value", "worker.budgetUsd must be > 0")
    _int(wk["maxRounds"], "worker.maxRounds", 1, 2)
    _int(wk["maxTurns"], "worker.maxTurns", 1, 60)

    deps = _exact(d["deps"], ("kind", "network", "timeoutS", "outputKeepBytes"), "deps")
    if deps["kind"] != "npm-ci" or deps["network"] not in ("prefer-offline", "offline"):
        _fail("spec_value", "deps.kind must be npm-ci and deps.network prefer-offline|offline")
    _int(deps["timeoutS"], "deps.timeoutS", 1, 1800)
    _int(deps["outputKeepBytes"], "deps.outputKeepBytes", 1, 1 << 20)

    meta = _exact(d["metadata"], ("issue", "title", "notes"), "metadata")     # declared; never interpreted
    _str(meta["issue"], "metadata.issue", 1, 64)
    _str(meta["title"], "metadata.title", 1, 200)
    if not isinstance(meta["notes"], list) or len(meta["notes"]) > 16:
        _fail("spec_value", "metadata.notes must hold at most 16 strings")
    for i, n in enumerate(meta["notes"]):
        _str(n, f"metadata.notes[{i}]", 1, 1000)
