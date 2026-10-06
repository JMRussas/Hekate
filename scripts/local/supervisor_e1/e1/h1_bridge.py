"""Invoke ChatAgent's ACTUAL buildPlanTaskContext (H1) for E1b interop tests (test-only).

Pinned and fail-closed: the checkout must be at H1_COMMIT with the H1 module, its transitive
sources and the manifests tracked and clean, and tsx already installed (never installs).
A missing, dirty or wrong checkout raises H1Unavailable; the interop suites FAIL on it,
they never skip. No HTTP, no container, no provider.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .exact import WireError, loads_exact

H1_COMMIT = "5255daacfc670a4919f61439eb12adcb6a401920"
DEFAULT_DIR = r"D:\Git\ChatAgent"
BRIDGE = Path(__file__).resolve().with_name("h1_bridge.mjs")
TRACKED = ("src/integrations/hekate/planTask.ts", "src/app/contextBuilder.ts", "src/domain/context.ts",
           "src/domain/contextRendering.ts", "package.json", "package-lock.json", ".node-version")
CLEAN = ("src/integrations/hekate", "src/app", "src/domain", "package.json", "package-lock.json", "tsconfig.json",
         ".node-version")
# H1's accepted runtime (recipe 812): the repo pins Node in .node-version; the PATH Node (24.15)
# had native crashes in ChatAgent, so the pinned executable is used, never PATH.
KNOWN_NODE = Path("node_modules") / ".cache" / "worker-diagnosis" / "new24" / "node.exe"


class H1Unavailable(RuntimeError):
    """The pinned H1 source/runtime is not usable; selected interop must fail, not skip."""


class H1BridgeError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def chatagent_dir() -> Path:
    return Path(os.environ.get("HEKATE_E1_CHATAGENT_DIR", DEFAULT_DIR))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if not git:
        raise H1Unavailable("git not found on PATH")
    return subprocess.run([git, "-C", str(repo), *args], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


def node_runtime(repo: Path) -> tuple[Path, str]:
    """The Node executable for H1: HEKATE_E1_NODE or the known pinned path, whose --version must
    equal the repo's .node-version exactly."""
    pinned = (repo / ".node-version").read_text(encoding="utf-8").strip()
    exe = Path(os.environ.get("HEKATE_E1_NODE") or (repo / KNOWN_NODE))
    if not exe.is_file():
        raise H1Unavailable(f"pinned Node executable not found: {exe}")
    version = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    if version != f"v{pinned}":
        raise H1Unavailable(f"Node at {exe} is {version!r}; .node-version pins v{pinned}")
    return exe, version


def verify_checkout(repo: Path | None = None) -> Path:
    repo = repo or chatagent_dir()
    if not (repo / ".git").exists():
        raise H1Unavailable(f"{repo} is not a git checkout")
    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != H1_COMMIT:
        raise H1Unavailable(f"ChatAgent HEAD is {head.stdout.strip() or '?'}; pinned H1 is {H1_COMMIT}")
    for f in TRACKED:
        if _git(repo, "ls-files", "--error-unmatch", "--", f).returncode != 0:
            raise H1Unavailable(f"{f} is not tracked at the pinned commit")
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=all", "--", *CLEAN)
    if dirty.returncode != 0 or dirty.stdout.strip():
        raise H1Unavailable("H1 sources or manifests are dirty or have untracked files: "
                            + ", ".join(line[3:] for line in dirty.stdout.splitlines()[:5]))
    tsx = repo / "node_modules" / ".bin" / ("tsx.cmd" if os.name == "nt" else "tsx")
    if not tsx.exists() or not (repo / "node_modules" / "tsx" / "package.json").exists():
        raise H1Unavailable("tsx is not installed in the ChatAgent checkout (this suite never installs)")
    node_runtime(repo)
    return repo


def build(options: dict[str, Any], *, probe: dict[str, Any] | None = None, repo: Path | None = None) -> dict[str, Any]:
    """One buildPlanTaskContext call. Returns the parsed bridge line ({ok: true, ...} or a typed
    {ok: false, kind, code}). Raises H1Unavailable / H1BridgeError on infrastructure failure."""
    repo = verify_checkout(repo)
    node, version = node_runtime(repo)
    payload = json.dumps({"options": options, "probe": probe or {}})
    try:
        p = subprocess.run([str(node), "--import", "tsx", str(BRIDGE)], cwd=str(repo), input=payload,
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    except subprocess.TimeoutExpired as e:   # subprocess.run kills the owned child on timeout
        raise H1BridgeError("bridge_timeout") from e
    lines = [line for line in p.stdout.splitlines() if line.strip()]
    if p.returncode != 0 or len(lines) != 1:
        raise H1BridgeError("bridge_failed", f"exit {p.returncode}; stderr tail: {p.stderr.strip()[-300:]}")
    try:
        result = loads_exact(lines[0])
    except WireError as e:
        raise H1BridgeError("bridge_output_" + e.code) from e
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise H1BridgeError("bridge_output_shape")
    result["_runtime"] = {"node": str(node), "version": version, "h1Commit": H1_COMMIT}
    return result
