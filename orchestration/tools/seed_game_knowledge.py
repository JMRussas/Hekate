#  Orchestration Engine - Game Knowledge Seeder
#
#  Seeds project_knowledge table with game dev findings from existing
#  game projects (DnD, OrcKing). Run once per project to bootstrap
#  the orchestrator with lessons learned from prior development.
#
#  Usage:
#    python tools/seed_game_knowledge.py <project_id> --source dnd
#    python tools/seed_game_knowledge.py <project_id> --source orcking
#    python tools/seed_game_knowledge.py <project_id> --source <path_to_claude_dir>
#
#  Depends on: backend/db/connection.py
#  Used by:    manual seeding (one-time)

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

# Default source project paths
_SOURCES = {
    "dnd": Path.home() / "Documents" / "git" / "DnD",
    "orcking": Path.home() / "Documents" / "git" / "OrcKing",
}

# Files to extract knowledge from, with their category mappings
_KNOWLEDGE_FILES = [
    ("CLAUDE.md", "gotcha", "engine_gotchas"),
    (".claude/anti-patterns.md", "gotcha", "anti_patterns"),
    (".claude/decision-log.md", "decision", "decision_log"),
    (".claude/noz-learnings.md", "discovery", "noz_learnings"),
    (".claude/ui-patterns.md", "reference", "ui_patterns"),
]


def _extract_sections(text: str) -> list[dict]:
    """Split markdown into sections by ## headings, returning structured findings."""
    findings = []
    # Split by ## headings
    sections = re.split(r'^##\s+', text, flags=re.MULTILINE)

    for section in sections[1:]:  # Skip preamble before first ##
        lines = section.strip().split('\n', 1)
        title = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""

        if not body or len(body) < 20:
            continue

        findings.append({
            "title": title,
            "content": body[:2000],  # Cap at 2000 chars per finding
        })

    return findings


def _extract_gotchas(text: str) -> list[dict]:
    """Extract individual gotcha entries from bullet-point lists."""
    findings = []
    # Match bullet points that look like gotchas (- **Name**: description)
    pattern = r'^[-*]\s+\*\*(.+?)\*\*[:\s]+(.+?)(?=\n[-*]\s+\*\*|\n##|\Z)'
    matches = re.findall(pattern, text, re.MULTILINE | re.DOTALL)

    for name, description in matches:
        content = description.strip()
        if len(content) < 10:
            continue
        findings.append({
            "title": name.strip(),
            "content": content[:1000],
        })

    return findings


def seed_knowledge(db_path: str, project_id: str, source_path: Path):
    """Seed project_knowledge table from a game project's documentation."""
    if not source_path.is_dir():
        print(f"Error: source path does not exist: {source_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Verify project exists
    cursor.execute("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not cursor.fetchone():
        print(f"Error: project {project_id} not found in database")
        conn.close()
        return

    inserted = 0
    skipped = 0

    for rel_path, category, source_label in _KNOWLEDGE_FILES:
        file_path = source_path / rel_path
        if not file_path.is_file():
            print(f"  Skip (not found): {rel_path}")
            continue

        text = file_path.read_text(encoding="utf-8", errors="replace")

        # Use gotcha extractor for gotcha files, section extractor for others
        if category == "gotcha":
            findings = _extract_gotchas(text)
            if not findings:
                findings = _extract_sections(text)
        else:
            findings = _extract_sections(text)

        print(f"  {rel_path}: {len(findings)} findings")

        for finding in findings:
            content = f"[{source_label}] {finding['title']}: {finding['content']}"
            content_hash = hashlib.md5(content.encode()).hexdigest()[:16]

            # Deduplicate by content hash
            cursor.execute(
                "SELECT id FROM project_knowledge WHERE project_id = ? AND content_hash = ?",
                (project_id, content_hash),
            )
            if cursor.fetchone():
                skipped += 1
                continue

            finding_id = uuid.uuid4().hex[:12]
            source_title = f"seed:{source_path.name}:{source_label}"

            cursor.execute(
                "INSERT INTO project_knowledge "
                "(id, project_id, task_id, category, content, content_hash, source_task_title, created_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                (finding_id, project_id, category, content, content_hash, source_title, time.time()),
            )
            inserted += 1

    conn.commit()
    conn.close()
    print(f"\nDone: {inserted} inserted, {skipped} skipped (duplicates)")


def main():
    parser = argparse.ArgumentParser(description="Seed game dev knowledge into orchestration DB")
    parser.add_argument("project_id", help="Target project ID in the orchestration database")
    parser.add_argument(
        "--source", required=True,
        help="Source: 'dnd', 'orcking', or a path to a project with .claude/ docs",
    )
    parser.add_argument(
        "--db", default=str(Path(__file__).parent.parent / "data" / "orchestration.db"),
        help="Path to orchestration SQLite database",
    )
    args = parser.parse_args()

    # Resolve source path
    if args.source in _SOURCES:
        source_path = _SOURCES[args.source]
    else:
        source_path = Path(args.source)

    print(f"Seeding knowledge from: {source_path}")
    print(f"Into project: {args.project_id}")
    print(f"Database: {args.db}\n")

    seed_knowledge(args.db, args.project_id, source_path)


if __name__ == "__main__":
    main()
