"""index_code.py — bulk-index a codebase into the Hekate context store.

Walks a root directory for source files, POSTs each to /api/code/decompose
under a named project. The project is created on first run (idempotent
on name — re-running returns the existing id).

Usage:
    python index_code.py --name DungeonCrawl --root D:\\Git\\DnD\\game
    python index_code.py            # uses the defaults above
    python index_code.py --base http://localhost:5102 --concurrency 8
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx


DEFAULT_CTX_STORE = os.environ.get("CTX_STORE_URL", "http://192.168.1.164:5102")
DEFAULT_NAME = "DungeonCrawl"
DEFAULT_ROOT = r"D:\Git\DnD\game"

EXCLUDE_PARTS = {".git", ".claude", "bin", "obj", "__pycache__", ".worktrees", ".venv", "node_modules"}
EXTENSIONS = (".cs",)


def find_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if not p.name.endswith(EXTENSIONS):
            continue
        if any(part in EXCLUDE_PARTS for part in p.parts):
            continue
        out.append(p)
    return sorted(out)


async def find_or_create_project(
    client: httpx.AsyncClient, base: str, name: str, root_path: str
) -> str:
    resp = await client.post(
        f"{base}/api/projects",
        json={"name": name, "rootPath": root_path},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def decompose_file(
    client: httpx.AsyncClient, base: str, project_id: str, path: Path
) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    resp = await client.post(
        f"{base}/api/code/decompose",
        json={
            "projectId": project_id,
            "filePath": str(path),
            "sourceText": text,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


async def main(name: str, root: Path, base: str, concurrency: int) -> int:
    if not root.exists():
        print(f"error: root {root} does not exist", file=sys.stderr)
        return 1

    files = find_files(root)
    if not files:
        print(f"no .cs files under {root}", file=sys.stderr)
        return 1

    print(f"indexing {len(files)} files from {root} -> {base} as '{name}'")

    async with httpx.AsyncClient() as client:
        project_id = await find_or_create_project(client, base, name, str(root))
        print(f"project_id = {project_id}")

        sem = asyncio.Semaphore(concurrency)
        ok = 0
        fail = 0
        total_nodes = 0
        total_calls = 0
        total_refs = 0

        async def one(path: Path) -> None:
            nonlocal ok, fail, total_nodes, total_calls, total_refs
            async with sem:
                rel = path.relative_to(root)
                try:
                    result = await decompose_file(client, base, project_id, path)
                    n = result.get("nodeCount", 0)
                    c = result.get("calls", 0)
                    r = result.get("references", 0)
                    total_nodes += n
                    total_calls += c
                    total_refs += r
                    print(f"  OK    {rel}  ({n} nodes, {c} calls, {r} refs)")
                    ok += 1
                except httpx.HTTPStatusError as e:
                    body = e.response.text[:200]
                    print(f"  FAIL  {rel}  HTTP {e.response.status_code}: {body}", file=sys.stderr)
                    fail += 1
                except Exception as e:
                    print(f"  FAIL  {rel}  {type(e).__name__}: {e}", file=sys.stderr)
                    fail += 1

        await asyncio.gather(*(one(p) for p in files))

    print(f"\ndone: {ok} ok, {fail} failed")
    print(f"totals: {total_nodes} nodes, {total_calls} calls, {total_refs} references")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"context-store project name (default: {DEFAULT_NAME})")
    ap.add_argument("--root", default=DEFAULT_ROOT, type=Path, help=f"directory to walk (default: {DEFAULT_ROOT})")
    ap.add_argument("--base", default=DEFAULT_CTX_STORE, help=f"context store URL (default: {DEFAULT_CTX_STORE})")
    ap.add_argument("--concurrency", type=int, default=4, help="parallel requests (default: 4)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.name, args.root, args.base, args.concurrency)))
