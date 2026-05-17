#!/usr/bin/env python3
"""
seed_patterns.py — Seed the context store pattern library from C# source trees.

Walks one or more source directories, posts each .cs file to /api/code/decompose,
and reports coverage stats. Idempotent: re-seeding a file replaces its nodes.

Usage:
    python tools/seed_patterns.py --dry-run
    python tools/seed_patterns.py
    python tools/seed_patterns.py --sources noz iris hekate
    python tools/seed_patterns.py --url http://localhost:5102 --sources noz

Sources (resolved from SOURCES dict below, or pass absolute paths directly):
    noz     ->C:/Users/jruss/Git/noz-cs
    iris    ->C:/Users/jruss/Git/iris
    hekate  ->C:/Users/jruss/Git/Hekate/context-store
              C:/Users/jruss/Git/Hekate/orchestration  (skipped — Python, not C#)
"""

import argparse
import sys
import time
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:5102"

SOURCES = {
    "noz":    "C:/Users/jruss/Git/noz-cs",
    "iris":   "C:/Users/jruss/Git/iris",
    "hekate": "C:/Users/jruss/Git/Hekate/context-store",
}

# Directories to skip entirely
SKIP_DIRS = {"bin", "obj", ".git", "node_modules", "packages", ".vs"}

# File name patterns to skip (generated/designer files add noise without patterns)
SKIP_SUFFIXES = {".g.cs", ".g.i.cs", ".Designer.cs"}
SKIP_NAMES = {"AssemblyInfo.cs", "GlobalUsings.g.cs"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def collect_cs_files(root: Path) -> list[Path]:
    """Walk root recursively, return all .cs files not in skip dirs/names."""
    files = []
    for path in root.rglob("*.cs"):
        # Skip if any ancestor directory is in SKIP_DIRS
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_NAMES:
            continue
        if any(path.name.endswith(suf) for suf in SKIP_SUFFIXES):
            continue
        files.append(path)
    return sorted(files)


def ensure_project(client: httpx.Client, base_url: str, name: str, root_path: str) -> str:
    """GET or create a project. Returns project ID."""
    resp = client.post(f"{base_url}/api/projects", json={"name": name, "rootPath": root_path})
    resp.raise_for_status()
    return resp.json()["id"]


def decompose_file(client: httpx.Client, base_url: str, project_id: str, file_path: Path) -> dict:
    """POST /api/code/decompose for a single file. Returns result dict."""
    resp = client.post(
        f"{base_url}/api/code/decompose",
        json={"projectId": project_id, "filePath": str(file_path).replace("\\", "/")},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Seed context store pattern library")
    parser.add_argument(
        "--sources", nargs="+", default=list(SOURCES.keys()),
        help="Source keys (noz, iris, hekate) or absolute paths",
    )
    parser.add_argument("--url", default=BASE_URL, help="Context store base URL")
    parser.add_argument("--dry-run", action="store_true", help="List files without ingesting")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")

    # Resolve source paths
    roots: list[tuple[str, Path]] = []
    for src in args.sources:
        if src in SOURCES:
            roots.append((src, Path(SOURCES[src])))
        else:
            p = Path(src)
            roots.append((p.name, p))

    # Collect files
    all_files: list[tuple[str, Path, Path]] = []  # (project_name, root, file)
    for name, root in roots:
        if not root.exists():
            print(f"[WARN] {name}: path not found — {root}")
            continue
        files = collect_cs_files(root)
        print(f"{name}: {len(files)} .cs files in {root}")
        for f in files:
            all_files.append((name, root, f))

    print(f"\nTotal: {len(all_files)} files")

    if args.dry_run:
        print("\n-- DRY RUN: first 20 files --")
        for name, root, f in all_files[:20]:
            print(f"  [{name}] {f.relative_to(root)}")
        if len(all_files) > 20:
            print(f"  ... and {len(all_files) - 20} more")
        return

    # Ingest
    client = httpx.Client(timeout=60.0)
    project_ids: dict[str, str] = {}

    # Ensure projects exist
    for name, root in roots:
        if not root.exists():
            continue
        try:
            pid = ensure_project(client, base_url, name, str(root))
            project_ids[name] = pid
            print(f"[project] {name} ->{pid}")
        except Exception as exc:
            print(f"[ERROR] Could not ensure project '{name}': {exc}")
            sys.exit(1)

    # Decompose files
    total = len(all_files)
    ok = 0
    errors = 0
    t0 = time.monotonic()

    for i, (name, root, file_path) in enumerate(all_files, 1):
        pid = project_ids.get(name)
        if not pid:
            continue
        try:
            result = decompose_file(client, base_url, pid, file_path)
            node_count = result.get("nodeCount", "?")
            rel = file_path.relative_to(root)
            print(f"[{i:4}/{total}] {name}/{rel} ->{node_count} nodes")
            ok += 1
        except httpx.HTTPStatusError as exc:
            print(f"[{i:4}/{total}] ERROR {file_path.name}: HTTP {exc.response.status_code}")
            errors += 1
        except Exception as exc:
            print(f"[{i:4}/{total}] ERROR {file_path.name}: {exc}")
            errors += 1

    elapsed = time.monotonic() - t0
    print(f"\nDone in {elapsed:.1f}s — {ok} seeded, {errors} errors")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
