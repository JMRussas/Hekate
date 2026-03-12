#!/usr/bin/env python3
#  context_health — Audit CLAUDE.md for staleness and drift
#
#  Checks:
#  1. File tree drift (documented vs actual files)
#  2. Stale file references (paths mentioned that don't exist)
#  3. Node types drift (NodeTypes.cs vs documented types)
#  4. Size/bloat warning
#  5. Required sections present
#
#  Depends on: nothing (stdlib only)
#  Used by:    utils-mcp run_task("context_health")

import json
import re
from pathlib import Path

DESCRIPTION = "Audit CLAUDE.md for staleness, file tree drift, and structural issues"

# Sections every CLAUDE.md should have per global conventions
REQUIRED_SECTIONS = [
    "Quick Start",
    "Project Structure",
    "Conventions",
    "Git Workflow",
]

SIZE_WARN_THRESHOLD = 300  # lines


async def run(project_root: str, **params) -> str:
    root = Path(project_root)
    claude_md = root / "CLAUDE.md"
    issues = []
    info = []

    if not claude_md.exists():
        return json.dumps({"error": "CLAUDE.md not found", "path": str(claude_md)})

    content = claude_md.read_text(encoding="utf-8")
    lines = content.splitlines()

    # --- 1. Size check ---
    line_count = len(lines)
    if line_count > SIZE_WARN_THRESHOLD:
        issues.append({
            "check": "size",
            "severity": "warn",
            "detail": f"CLAUDE.md is {line_count} lines (threshold: {SIZE_WARN_THRESHOLD})",
        })
    else:
        info.append(f"CLAUDE.md is {line_count} lines (under {SIZE_WARN_THRESHOLD} threshold)")

    # --- 2. Required sections ---
    headings = [line.lstrip("#").strip() for line in lines if line.startswith("#")]
    for section in REQUIRED_SECTIONS:
        if not any(section.lower() in h.lower() for h in headings):
            issues.append({
                "check": "missing_section",
                "severity": "warn",
                "detail": f"Missing recommended section: '{section}'",
            })

    # --- 3. File tree drift ---
    # Extract paths from the documented file tree block (lines starting with ├── or └── or │)
    tree_files = _extract_tree_paths(content, root)
    if tree_files:
        missing_from_disk = []
        for rel_path in tree_files:
            full = root / rel_path
            if not full.exists():
                missing_from_disk.append(rel_path)

        if missing_from_disk:
            issues.append({
                "check": "tree_drift",
                "severity": "error",
                "detail": f"{len(missing_from_disk)} documented paths not found on disk",
                "paths": missing_from_disk[:20],
            })
        else:
            info.append(f"All {len(tree_files)} documented tree paths exist on disk")

        # Check for significant undocumented top-level items
        actual_top = set()
        for p in root.iterdir():
            if p.name.startswith(".") or p.name in ("bin", "obj", "node_modules", "output"):
                continue
            actual_top.add(p.name)

        documented_top = set()
        for rel in tree_files:
            documented_top.add(rel.split("/")[0])

        undocumented = actual_top - documented_top
        if undocumented:
            issues.append({
                "check": "undocumented_top_level",
                "severity": "info",
                "detail": f"{len(undocumented)} top-level items not in documented tree",
                "items": sorted(undocumented),
            })
    else:
        info.append("No file tree block detected in CLAUDE.md")

    # --- 4. Node types drift ---
    node_types_file = root / "ContextRouter" / "NodeTypes.cs"
    if node_types_file.exists():
        cs_types = _extract_cs_node_types(node_types_file)
        doc_types = _extract_documented_node_types(content)

        if cs_types and doc_types:
            in_code_not_doc = cs_types - doc_types
            in_doc_not_code = doc_types - cs_types

            if in_code_not_doc:
                issues.append({
                    "check": "node_types_drift",
                    "severity": "warn",
                    "detail": f"{len(in_code_not_doc)} types in NodeTypes.cs but not documented",
                    "types": sorted(in_code_not_doc),
                })
            if in_doc_not_code:
                issues.append({
                    "check": "node_types_drift",
                    "severity": "warn",
                    "detail": f"{len(in_doc_not_code)} types documented but not in NodeTypes.cs",
                    "types": sorted(in_doc_not_code),
                })
            if not in_code_not_doc and not in_doc_not_code:
                info.append(f"Node types in sync ({len(cs_types)} types)")
        elif not cs_types:
            info.append("Could not parse node types from NodeTypes.cs")
        elif not doc_types:
            info.append("No node types section found in CLAUDE.md")

    # --- 5. Stale file references ---
    # Find paths like `path/to/file.ext` in the markdown (not inside code blocks)
    referenced_paths = _extract_file_references(content, root)
    stale_refs = [p for p in referenced_paths if not (root / p).exists()]
    if stale_refs:
        issues.append({
            "check": "stale_references",
            "severity": "warn",
            "detail": f"{len(stale_refs)} referenced file paths don't exist",
            "paths": stale_refs[:15],
        })
    elif referenced_paths:
        info.append(f"All {len(referenced_paths)} file references are valid")

    # --- Summary ---
    errors = sum(1 for i in issues if i["severity"] == "error")
    warns = sum(1 for i in issues if i["severity"] == "warn")
    infos = sum(1 for i in issues if i["severity"] == "info")

    return json.dumps({
        "summary": f"{errors} errors, {warns} warnings, {infos} info",
        "healthy": errors == 0 and warns == 0,
        "issues": issues,
        "info": info,
        "line_count": line_count,
    }, indent=2)


def _extract_tree_paths(content: str, root: Path) -> list[str]:
    """Extract relative file paths from a markdown file tree block."""
    paths = []
    in_tree = False
    tree_lines = []

    for line in content.splitlines():
        # Detect tree blocks: lines with ├──, └──, or │
        if "├──" in line or "└──" in line:
            in_tree = True
            tree_lines.append(line)
        elif in_tree and ("│" in line or line.strip() == ""):
            if line.strip():
                tree_lines.append(line)
            else:
                in_tree = False
        else:
            in_tree = False

    # Parse tree structure to get paths
    path_stack = []
    for line in tree_lines:
        # Count indent depth (each level is typically 4 chars: │   or ├── or └──)
        stripped = line.rstrip()
        # Find the filename after ├── or └──
        match = re.search(r"[├└]── (.+?)(?:\s{2,}.*)?$", stripped)
        if not match:
            continue

        name = match.group(1).strip()
        # Remove trailing comment (# ...)
        name = re.sub(r"\s+#.*$", "", name)

        # Calculate depth by position of ├ or └
        pos = stripped.index("├") if "├" in stripped else stripped.index("└")
        depth = pos // 4

        # Adjust path stack
        if depth < len(path_stack):
            path_stack = path_stack[:depth]
        path_stack.append(name)

        full_path = "/".join(path_stack)

        # Only add leaf files (not directories — those end with /)
        if not name.endswith("/"):
            paths.append(full_path)

    return paths


def _extract_cs_node_types(path: Path) -> set[str]:
    """Extract node type string constants from NodeTypes.cs."""
    types = set()
    content = path.read_text(encoding="utf-8")
    # Match: public const string Xxx = "yyy";
    for match in re.finditer(r'=\s*"([^"]+)"', content):
        types.add(match.group(1))
    return types


def _extract_documented_node_types(content: str) -> set[str]:
    """Extract node type names from CLAUDE.md's Node Types section."""
    types = set()
    in_section = False
    for line in content.splitlines():
        if re.match(r"^##\s+Node Types", line):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            # Match backtick-delimited type names: `type_name`
            for match in re.finditer(r"`(\w+)`", line):
                val = match.group(1)
                # Filter out things that aren't node types
                if not val[0].isupper() and "_" in val or val.islower():
                    types.add(val)
    return types


def _extract_file_references(content: str, root: Path) -> list[str]:
    """Extract file path references from markdown (outside code blocks)."""
    paths = set()
    in_code_block = False

    for line in content.splitlines():
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        # Match paths like `path/to/file.ext` or path/to/file.ext
        for match in re.finditer(r"[`]?([A-Za-z][\w\-]*/[\w\-./]+\.\w+)[`]?", line):
            p = match.group(1)
            # Skip URLs, version numbers, etc.
            if "http" in p or ".." in p:
                continue
            # Only include if it looks like a project path
            if any(p.startswith(d) for d in ("Api/", "DbLayer/", "GraphLayer/",
                                              "Generator/", "BuildRunner/", "AgentCoordination/",
                                              "ContextRouter/", "Renderer/", "SeedData/",
                                              "tools/", "ui/", "plans/", "research/")):
                paths.add(p)

    return sorted(paths)
