"""Operator task runner fixtures (OFFLINE): a two-commit anchor/base repo, a FAKE pinned "node" (the base
Python through an underscore-free junction, so the worker's test command passes W.TEST_COMMAND) and the
fake npm/vitest/tsc in tests/fake_node/. No real npm, Node or model."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from e1 import cli_worker as W
from e1 import task_spec as T

FAKE_NODE = Path(__file__).resolve().parent / "fake_node"
NPM_CLI = FAKE_NODE / "npm_cli.py"
VITEST = "node_modules/vitest/vitest.mjs"
TSC = "node_modules/typescript/bin/tsc"
ORACLE_A = "tests/unit/value.test.ts"
ORACLE_B = "tests/integration/value2.test.ts"
FIRST = "AssertionError: expected '0' to be '42'"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_file(p: Path) -> str:
    return sha(Path(p).read_bytes())


def pinned_node(basetemp: Path) -> Path:
    """<basetemp>/pynode -> the base interpreter's directory (stdlib only is needed by the fakes)."""
    link = Path(basetemp) / "pynode"
    if not link.exists():
        target = Path(sys.base_prefix)
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    exe = link / ("python.exe" if sys.platform == "win32" else "bin/python3")
    assert W.TEST_COMMAND.fullmatch(exe.as_posix()), f"pinned node path must pass TEST_COMMAND: {exe}"
    return exe


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=W.git_env()).stdout.strip()


def commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false", "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


ORACLE_FILES = {
    ORACLE_A: '{"cases": [{"name": "value is 42", "expect": "42"}, {"name": "value file loads", "expect": "*"}]}\n',
    ORACLE_B: '{"cases": [{"name": "value is still 42", "expect": "42"}]}\n',
}
LOCK = '{"lockfileVersion": 3}\n'


def make_repo(root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """c0 (initial) -> anchor (the project, plus `extra` files) -> base (adds ONLY the two oracle files);
    also a side commit."""
    repo = Path(root) / "src-repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    c0 = commit(repo, {"README.md": "fixture\n"}, "c0")
    anchor = commit(repo, {".gitignore": "node_modules/\n", "package.json": '{"name": "fx", "private": true}\n',
                           "package-lock.json": LOCK, "src/value.txt": "0\n", "src/other.txt": "x\n", **(extra or {})},
                    "anchor")
    base = commit(repo, ORACLE_FILES, "oracle")
    git(repo, "checkout", "-q", "-b", "side", anchor)
    side = commit(repo, {"src/other.txt": "side\n"}, "side")
    git(repo, "checkout", "-q", "main")
    return {"repo": str(repo), "c0": c0, "anchor": anchor, "base": base, "side": side}


def make_spec(fx: dict[str, str], node: Path) -> dict:
    vitest_argv = [node.as_posix(), VITEST, "run", "--reporter=json"]
    return {
        "specVersion": T.SPEC_VERSION,
        "source": {"repo": Path(fx["repo"]).as_posix(), "anchorCommit": fx["anchor"], "taskBaseCommit": fx["base"]},
        "task": {"text": "Set src/value.txt to 42.", "criteria": "Every oracle case passes."},
        "allow": [{"path": "src/value.txt", "status": "M", "mode": "100644"}],
        "oracle": {
            "files": [{"path": p, "sha256": sha(t.encode("utf-8"))} for p, t in ORACLE_FILES.items()],
            "baseline": {"argv": vitest_argv, "timeoutS": 60, "reportMaxBytes": 65536, "expectedExit": 1, "cases": [
                {"file": ORACLE_B, "fullName": "value is still 42", "status": "failed", "failureFirstLine": FIRST},
                {"file": ORACLE_A, "fullName": "value is 42", "status": "failed", "failureFirstLine": FIRST},
                {"file": ORACLE_A, "fullName": "value file loads", "status": "passed", "failureFirstLine": None},
            ]},
        },
        "verify": {"steps": [
            {"name": "tsc", "argv": [node.as_posix(), TSC, "--noEmit"], "timeoutS": 60, "outputKeepBytes": 4096},
            {"name": "oracle", "argv": vitest_argv, "timeoutS": 60, "outputKeepBytes": 65536},
        ]},
        "worker": {"testCommand": " ".join(vitest_argv), "model": "fake-model", "budgetUsd": "0.50", "maxRounds": 2, "maxTurns": 5},
        "hashes": {
            "pinnedNodeExe": {"path": node.as_posix(), "version": "v24.0.0", "sha256": sha_file(node)},
            "npmCli": {"path": NPM_CLI.as_posix(), "version": "11.0.0", "sha256": sha_file(NPM_CLI)},
            "packageLock": sha(LOCK.encode("utf-8")),
            "vitestEntry": sha_file(FAKE_NODE / "vitest.py"),
            "tscEntry": sha_file(FAKE_NODE / "tsc.py"),
        },
        "deps": {"kind": "npm-ci", "network": "offline", "timeoutS": 60, "outputKeepBytes": 4096},
        "metadata": {"issue": "FX-1", "title": "fixture task", "notes": []},
    }


def write_spec(doc: dict, path: Path) -> T.TaskSpec:
    Path(path).write_bytes(json.dumps(doc, indent=1).encode("utf-8"))
    return T.load(path)


def edited(doc: dict, fn) -> dict:
    d = copy.deepcopy(doc)
    fn(d)
    return d


def source_state(repo: str) -> tuple:
    """Everything a run could disturb in the user's source checkout."""
    return (git(Path(repo), "status", "--porcelain", "--untracked-files=all", "--ignored"), git(Path(repo), "rev-parse", "HEAD"),
            git(Path(repo), "for-each-ref"), git(Path(repo), "worktree", "list", "--porcelain"), os.listdir(repo))
