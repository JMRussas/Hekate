"""Seed pipeline: parse Claude Code conversations, extract knowledge, correlate with git.

Discovers Claude Code .jsonl conversation files, parses them into sessions,
runs LLM extraction (Haiku) to pull out decisions/findings/questions, and
cross-references each session's time window against local git commit history.

Usage:
    python orchestration/tools/seed_claude_conversations.py [--claude-dir DIR] [--repo-path PATH] [--dry-run] [--output FILE]
    python orchestration/tools/seed_claude_conversations.py --extract [--max-sessions N]
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import httpx

# Reuse parsing logic from the existing tool
from parse_conversations import (
    find_conversation_files,
    parse_jsonl_stream,
)


@dataclass
class GitCommit:
    hash: str
    author: str
    timestamp: str
    subject: str


@dataclass
class ConversationRecord:
    """Intermediate data structure for a parsed conversation session."""
    session_id: str
    project: str
    first_message: Optional[str] = None
    last_message: Optional[str] = None
    message_count: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    cwd: Optional[str] = None
    source_files: list = field(default_factory=list)
    git_commits: list = field(default_factory=list)
    messages: list = field(default_factory=list)


def parse_all_sessions(conversation_files: list[str]) -> dict[str, ConversationRecord]:
    """Parse conversation files into ConversationRecord objects grouped by session."""
    sessions: dict[str, ConversationRecord] = {}

    for filepath in conversation_files:
        file_project = os.path.basename(os.path.dirname(filepath))

        for msg in parse_jsonl_stream(filepath):
            sid = msg["session_id"]
            if sid is None:
                continue

            if sid not in sessions:
                sessions[sid] = ConversationRecord(
                    session_id=sid,
                    project=msg.get("cwd") or file_project,
                    cwd=msg.get("cwd"),
                )

            rec = sessions[sid]

            # Track source files
            if filepath not in rec.source_files:
                rec.source_files.append(filepath)

            # Update project/cwd if not set
            if rec.cwd is None and msg.get("cwd"):
                rec.cwd = msg["cwd"]

            # Track timestamps
            ts = msg.get("timestamp")
            if ts:
                if rec.first_message is None or ts < rec.first_message:
                    rec.first_message = ts
                if rec.last_message is None or ts > rec.last_message:
                    rec.last_message = ts

            # Count by role
            rec.message_count += 1
            if msg["role"] == "user":
                rec.user_message_count += 1
            else:
                rec.assistant_message_count += 1

            rec.messages.append({
                "role": msg["role"],
                "content": msg["content"],
            })

    return sessions


def _parse_iso_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse an ISO timestamp string to datetime, handling various formats."""
    if not ts_str:
        return None
    try:
        # Handle ISO format with or without timezone
        ts_str = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def git_commits_in_range(
    repo_path: str,
    since: str,
    until: str,
    max_count: int = 200,
) -> list[GitCommit]:
    """Query git log for commits between two ISO timestamps.

    Returns a list of GitCommit objects for commits in [since, until].
    """
    since_dt = _parse_iso_timestamp(since)
    until_dt = _parse_iso_timestamp(until)
    if not since_dt or not until_dt:
        return []

    # Format for git --since/--until (ISO 8601)
    since_fmt = since_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    until_fmt = until_dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    cmd = [
        "git", "-C", repo_path, "log",
        "--all",
        f"--since={since_fmt}",
        f"--until={until_fmt}",
        f"--max-count={max_count}",
        "--format=%H|%an|%aI|%s",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    commits = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append(GitCommit(
                hash=parts[0],
                author=parts[1],
                timestamp=parts[2],
                subject=parts[3],
            ))

    return commits


def correlate_with_git(
    sessions: dict[str, ConversationRecord],
    repo_path: str,
) -> None:
    """Enrich each session with git commits from its time window."""
    for sid, rec in sessions.items():
        if not rec.first_message or not rec.last_message:
            continue

        commits = git_commits_in_range(
            repo_path,
            rec.first_message,
            rec.last_message,
        )
        rec.git_commits = [asdict(c) for c in commits]


# ---------------------------------------------------------------------------
# LLM Knowledge Extraction
# ---------------------------------------------------------------------------

# Map extraction categories → context store node types
_CATEGORY_TO_NODE_TYPE = {
    "decision": "decision",
    "architecture": "decision",
    "constraint": "finding",
    "discovery": "finding",
    "reference": "finding",
    "gotcha": "finding",
    "pivot": "decision",
    "pain_point": "finding",
    "question": "question",
}

# Cap conversation text sent to extraction model
_MAX_CONVERSATION_CHARS = 6000

_CONVERSATION_EXTRACTION_PROMPT = """\
You are a knowledge extraction assistant. Given a Claude Code conversation between \
a developer and an AI assistant, extract architectural knowledge that would be \
valuable for understanding the project's evolution.

Extract these categories of knowledge:

<categories>
1. Decisions: architectural or design choices with rationale ("chose X over Y because...")
2. Architecture: structural patterns, component relationships, data flow designs
3. Pivots: changes in direction, abandoned approaches, strategy shifts
4. Pain points: recurring friction, things that broke, developer frustrations
5. Discoveries: API behavior, library quirks, undocumented features, key findings
6. Constraints: limitations discovered ("X must be Y", "API limits to N")
7. Questions: unresolved questions, open design issues, things to revisit
</categories>

<rules>
- Only extract findings that are REUSABLE — skip ephemeral debugging chatter.
- Each item should be self-contained (understandable without reading the full conversation).
- If there are NO reusable findings, return an empty array.
- Keep each item concise (1-3 sentences).
- For "rationale": explain WHY — what motivated the decision or what was learned.
  If the conversation doesn't explain why, write "Inferred from conversation context".
- For "confidence": assess reliability based on evidence in the conversation.
  - "high": explicitly discussed, confirmed, or implemented.
  - "medium": reasonable inference from context.
  - "low": speculative or mentioned in passing.
</rules>

Respond with ONLY a JSON object (no markdown fences):
{
  "items": [
    {
      "category": "decision|architecture|pivot|pain_point|discovery|constraint|question",
      "title": "Short summary (under 80 chars)",
      "content": "The finding itself (1-3 sentences)",
      "rationale": "Why this matters",
      "confidence": "high|medium|low"
    }
  ]
}
"""

_VALID_CATEGORIES = set(_CATEGORY_TO_NODE_TYPE.keys())
_VALID_CONFIDENCE = {"high", "medium", "low"}


@dataclass
class ExtractedItem:
    """A single knowledge item extracted from a conversation."""
    category: str
    node_type: str  # decision | finding | question
    title: str
    content: str
    rationale: str
    confidence: str
    source_session_id: str
    source_project: str


def _build_conversation_text(rec: ConversationRecord) -> str:
    """Build a condensed text representation of a conversation for extraction."""
    parts = []
    char_budget = _MAX_CONVERSATION_CHARS

    for msg in rec.messages:
        role = msg["role"].upper()
        content = msg.get("content", "")
        if isinstance(content, list):
            # Handle structured content blocks (tool calls, etc.)
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            content = "\n".join(text_parts)

        if not content or not content.strip():
            continue

        line = f"[{role}]: {content.strip()}"
        if len("\n".join(parts)) + len(line) > char_budget:
            # Truncate to stay within budget
            remaining = char_budget - len("\n".join(parts)) - 10
            if remaining > 100:
                parts.append(line[:remaining] + "...")
            break
        parts.append(line)

    return "\n".join(parts)


def _extract_json_from_response(raw: str) -> dict | None:
    """Parse JSON from LLM response, handling markdown fences and quirks."""
    if not raw:
        return None

    # Try direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


async def extract_knowledge_from_conversation(
    rec: ConversationRecord,
    client,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
) -> list[ExtractedItem]:
    """Extract architectural knowledge from a single conversation using Haiku.

    Returns a list of ExtractedItem objects. Never raises — returns [] on failure.
    """
    conversation_text = _build_conversation_text(rec)

    # Skip very short conversations (unlikely to contain architectural knowledge)
    if len(conversation_text.strip()) < 200:
        return []

    user_msg = (
        f"## Project: {rec.project}\n\n"
        f"### Conversation ({rec.message_count} messages, "
        f"{rec.first_message or 'unknown'} to {rec.last_message or 'unknown'})\n\n"
        f"{conversation_text}"
    )

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_CONVERSATION_EXTRACTION_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            timeout=120,
        )

        raw = "".join(
            block.text for block in response.content if block.type == "text"
        )
    except Exception as e:
        print(f"  LLM call failed for session {rec.session_id}: {e}", file=sys.stderr)
        return []

    parsed = _extract_json_from_response(raw)
    if not parsed or not isinstance(parsed, dict):
        print(f"  Could not parse extraction response for {rec.session_id}", file=sys.stderr)
        return []

    items_raw = parsed.get("items", [])
    if not isinstance(items_raw, list):
        return []

    extracted = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue

        content = (item.get("content") or "").strip()
        if not content:
            continue

        category = (item.get("category") or "discovery").strip().lower()
        if category not in _VALID_CATEGORIES:
            category = "discovery"

        confidence = (item.get("confidence") or "medium").strip().lower()
        if confidence not in _VALID_CONFIDENCE:
            confidence = "medium"

        node_type = _CATEGORY_TO_NODE_TYPE[category]
        title = (item.get("title") or content[:80]).strip()
        rationale = (item.get("rationale") or "").strip()

        extracted.append(ExtractedItem(
            category=category,
            node_type=node_type,
            title=title,
            content=content,
            rationale=rationale,
            confidence=confidence,
            source_session_id=rec.session_id,
            source_project=rec.project,
        ))

    return extracted


async def extract_all_sessions(
    sessions: dict[str, ConversationRecord],
    max_sessions: int | None = None,
    model: str = "claude-haiku-4-5-20251001",
) -> dict[str, list[ExtractedItem]]:
    """Run LLM extraction across all (or a subset of) sessions.

    Returns a dict mapping session_id → list of extracted items.
    """
    try:
        import anthropic
    except ImportError:
        print("ERROR: anthropic package required. pip install anthropic", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.AsyncAnthropic(api_key=api_key)

    # Sort sessions by timestamp for deterministic processing
    sorted_sessions = sorted(
        sessions.items(),
        key=lambda x: x[1].first_message or "",
    )

    if max_sessions:
        sorted_sessions = sorted_sessions[:max_sessions]

    results: dict[str, list[ExtractedItem]] = {}
    total_items = 0

    print(f"Extracting knowledge from {len(sorted_sessions)} sessions...", file=sys.stderr)

    for i, (sid, rec) in enumerate(sorted_sessions, 1):
        print(f"  [{i}/{len(sorted_sessions)}] {sid[:12]}... ({rec.project})", file=sys.stderr)

        items = await extract_knowledge_from_conversation(rec, client, model=model)
        results[sid] = items
        total_items += len(items)

        if items:
            print(f"    → {len(items)} items extracted", file=sys.stderr)

    print(f"Extraction complete: {total_items} items from {len(sorted_sessions)} sessions.",
          file=sys.stderr)

    return results


def build_report(sessions: dict[str, ConversationRecord]) -> dict:
    """Build a summary report of the parsed conversations."""
    projects = defaultdict(int)
    total_messages = 0
    total_commits = 0
    sessions_with_commits = 0

    for rec in sessions.values():
        projects[rec.project] += 1
        total_messages += rec.message_count
        total_commits += len(rec.git_commits)
        if rec.git_commits:
            sessions_with_commits += 1

    return {
        "total_conversations": len(sessions),
        "total_messages": total_messages,
        "total_git_commits_correlated": total_commits,
        "sessions_with_commits": sessions_with_commits,
        "sessions_without_commits": len(sessions) - sessions_with_commits,
        "conversations_per_project": dict(sorted(projects.items(), key=lambda x: -x[1])),
    }


def build_extraction_report(
    extraction_results: dict[str, list[ExtractedItem]],
) -> dict:
    """Build a summary report of extraction results."""
    by_node_type = defaultdict(int)
    by_category = defaultdict(int)
    by_project = defaultdict(int)
    total = 0

    for sid, items in extraction_results.items():
        for item in items:
            total += 1
            by_node_type[item.node_type] += 1
            by_category[item.category] += 1
            by_project[item.source_project] += 1

    return {
        "total_items_extracted": total,
        "sessions_processed": len(extraction_results),
        "sessions_with_items": sum(1 for items in extraction_results.values() if items),
        "by_node_type": dict(sorted(by_node_type.items(), key=lambda x: -x[1])),
        "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1])),
        "by_project": dict(sorted(by_project.items(), key=lambda x: -x[1])),
    }


# ---------------------------------------------------------------------------
# Context Store Ingestion
# ---------------------------------------------------------------------------

_CONTEXT_STORE_URL = "http://localhost:5102"


@dataclass
class IngestionStats:
    """Tracks ingestion metrics."""
    nodes_created: int = 0
    nodes_failed: int = 0
    projects_ensured: int = 0
    projects_cached: int = 0
    nodes_per_project: dict = field(default_factory=lambda: defaultdict(int))
    errors: list = field(default_factory=list)


class ContextStoreIngestor:
    """Lightweight httpx client for writing extracted nodes to the context store."""

    def __init__(self, base_url: str = _CONTEXT_STORE_URL, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)
        self._project_cache: dict[str, str] = {}  # project name → project ID

    async def close(self):
        await self._client.aclose()

    async def health_check(self) -> bool:
        """Verify the context store is reachable."""
        try:
            resp = await self._client.get(f"{self._base_url}/api/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def ensure_project(self, name: str, root_path: str | None = None) -> str | None:
        """Get or create a project. Returns project ID. Caches results."""
        if name in self._project_cache:
            return self._project_cache[name]

        payload = {"name": name}
        if root_path:
            payload["rootPath"] = root_path

        try:
            resp = await self._client.post(
                f"{self._base_url}/api/projects", json=payload,
            )
            resp.raise_for_status()
            project_id = resp.json().get("id")
            if project_id:
                self._project_cache[name] = project_id
            return project_id
        except Exception as exc:
            print(f"  Failed to ensure project '{name}': {exc}", file=sys.stderr)
            return None

    async def create_node(
        self,
        project_id: str,
        node_type: str,
        name: str,
        value: str,
        attributes: dict,
    ) -> str | None:
        """Create a root-level node in a project. Returns node ID or None."""
        payload = {
            "nodeType": node_type,
            "name": name,
            "value": value,
            "attributes": attributes,
        }
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/project/{project_id}/nodes", json=payload,
            )
            resp.raise_for_status()
            return resp.json().get("id")
        except Exception as exc:
            print(f"  Failed to create node '{name}': {exc}", file=sys.stderr)
            return None


def _build_node_attributes(
    item: "ExtractedItem",
    session: ConversationRecord,
) -> dict:
    """Build the attributes dict for a context store node."""
    attrs = {
        "category": item.category,
        "confidence": item.confidence,
        "source": "claude_conversation_seed",
        "source_session_id": item.source_session_id,
    }

    if item.rationale:
        attrs["rationale"] = item.rationale

    if session.first_message:
        attrs["conversation_start"] = session.first_message
    if session.last_message:
        attrs["conversation_end"] = session.last_message

    # Inject correlated git commits
    if session.git_commits:
        commit_summaries = [
            f"{c['hash'][:8]} {c['subject']}" for c in session.git_commits[:10]
        ]
        attrs["git_commits"] = "; ".join(commit_summaries)
        attrs["git_commit_count"] = str(len(session.git_commits))
        attrs["git_commit_hashes"] = ",".join(
            c["hash"][:12] for c in session.git_commits[:10]
        )

    return attrs


async def ingest_to_context_store(
    extraction_results: dict[str, list["ExtractedItem"]],
    sessions: dict[str, ConversationRecord],
    base_url: str = _CONTEXT_STORE_URL,
) -> IngestionStats:
    """Write all extracted items to the context store as nodes.

    Each item becomes a root-level node under its source project, with
    git commits and extraction metadata injected as attributes.
    """
    stats = IngestionStats()
    ingestor = ContextStoreIngestor(base_url=base_url)

    try:
        # Pre-flight check
        if not await ingestor.health_check():
            print("ERROR: Context store is not reachable at "
                  f"{base_url}. Is it running?", file=sys.stderr)
            stats.errors.append("Context store unreachable")
            return stats

        print(f"Connected to context store at {base_url}", file=sys.stderr)

        for sid, items in extraction_results.items():
            if not items:
                continue

            session = sessions.get(sid)
            if not session:
                continue

            # Ensure project exists
            project_name = session.project
            was_cached = project_name in ingestor._project_cache
            project_id = await ingestor.ensure_project(
                project_name, root_path=session.cwd,
            )

            if not project_id:
                stats.errors.append(f"Could not ensure project: {project_name}")
                stats.nodes_failed += len(items)
                continue

            if was_cached:
                stats.projects_cached += 1
            else:
                stats.projects_ensured += 1

            # Ingest each extracted item as a node
            for item in items:
                attributes = _build_node_attributes(item, session)

                node_id = await ingestor.create_node(
                    project_id=project_id,
                    node_type=item.node_type,
                    name=item.title,
                    value=item.content,
                    attributes=attributes,
                )

                if node_id:
                    stats.nodes_created += 1
                    stats.nodes_per_project[project_name] += 1
                else:
                    stats.nodes_failed += 1
                    stats.errors.append(
                        f"Failed node: {item.title[:50]} (project={project_name})"
                    )

    finally:
        await ingestor.close()

    return stats


def build_ingestion_report(
    stats: IngestionStats,
    extraction_results: dict[str, list["ExtractedItem"]],
    sessions: dict[str, ConversationRecord],
) -> dict:
    """Build the final comprehensive ingestion report."""
    total_items = sum(len(items) for items in extraction_results.values())
    total_sessions = len(sessions)
    sessions_with_items = sum(
        1 for items in extraction_results.values() if items
    )

    return {
        "total_conversations_processed": total_sessions,
        "total_items_extracted": total_items,
        "total_nodes_created": stats.nodes_created,
        "total_nodes_failed": stats.nodes_failed,
        "projects_ensured": stats.projects_ensured,
        "nodes_per_project": dict(
            sorted(stats.nodes_per_project.items(), key=lambda x: -x[1])
        ),
        "coverage": {
            "sessions_with_extractions": sessions_with_items,
            "sessions_total": total_sessions,
            "coverage_pct": round(
                sessions_with_items / total_sessions * 100, 1
            ) if total_sessions > 0 else 0,
            "ingestion_success_pct": round(
                stats.nodes_created / total_items * 100, 1
            ) if total_items > 0 else 0,
        },
        "errors": stats.errors[:20],  # cap error list
    }


def serialize_sessions(sessions: dict[str, ConversationRecord], include_messages: bool = False) -> list[dict]:
    """Convert sessions to serializable dicts."""
    results = []
    for sid, rec in sessions.items():
        d = asdict(rec)
        if not include_messages:
            d.pop("messages", None)
        results.append(d)
    # Sort by first_message timestamp
    results.sort(key=lambda x: x.get("first_message") or "")
    return results


def serialize_extraction_results(
    extraction_results: dict[str, list[ExtractedItem]],
) -> list[dict]:
    """Convert extraction results to serializable dicts."""
    items = []
    for sid, extracted in extraction_results.items():
        for item in extracted:
            items.append(asdict(item))
    return items


def main():
    parser = argparse.ArgumentParser(
        description="Parse Claude Code conversations and correlate with git history."
    )
    parser.add_argument(
        "--claude-dir",
        type=str,
        default="~/.claude",
        help="Path to the .claude directory (default: ~/.claude)",
    )
    parser.add_argument(
        "--repo-path",
        type=str,
        default=".",
        help="Path to the git repository for commit correlation (default: cwd)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON file path. If not specified, prints to stdout.",
    )
    parser.add_argument(
        "--include-messages",
        action="store_true",
        help="Include full message text in output (large).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only discover and count files, don't parse.",
    )
    parser.add_argument(
        "--extract",
        action="store_true",
        help="Run LLM extraction (Haiku) on each conversation.",
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=None,
        help="Limit number of sessions to extract from (useful for testing).",
    )
    parser.add_argument(
        "--extraction-model",
        type=str,
        default="claude-haiku-4-5-20251001",
        help="Model to use for extraction (default: claude-haiku-4-5-20251001).",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Write extracted nodes to the context store (implies --extract).",
    )
    parser.add_argument(
        "--context-store-url",
        type=str,
        default=_CONTEXT_STORE_URL,
        help=f"Context store base URL (default: {_CONTEXT_STORE_URL}).",
    )

    args = parser.parse_args()
    # --ingest requires extraction
    if args.ingest:
        args.extract = True
    repo_path = os.path.abspath(args.repo_path)

    # Step 1: Discover conversation files
    conversation_files = find_conversation_files(args.claude_dir)

    if not conversation_files:
        print("No conversation files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Discovered {len(conversation_files)} conversation files.", file=sys.stderr)

    if args.dry_run:
        for f in sorted(conversation_files):
            print(f)
        return

    # Step 2: Parse into sessions
    print("Parsing conversations...", file=sys.stderr)
    sessions = parse_all_sessions(conversation_files)
    print(f"Parsed {len(sessions)} sessions.", file=sys.stderr)

    # Step 3: Correlate with git history
    print(f"Correlating with git history in {repo_path}...", file=sys.stderr)
    correlate_with_git(sessions, repo_path)

    # Step 4: LLM extraction (optional)
    extraction_results = None
    if args.extract:
        print("Running LLM knowledge extraction...", file=sys.stderr)
        extraction_results = asyncio.run(
            extract_all_sessions(
                sessions,
                max_sessions=args.max_sessions,
                model=args.extraction_model,
            )
        )

    # Step 5: Ingest to context store (optional)
    ingestion_report = None
    if args.ingest and extraction_results is not None:
        print("Ingesting extracted nodes to context store...", file=sys.stderr)
        ingestion_stats = asyncio.run(
            ingest_to_context_store(
                extraction_results,
                sessions,
                base_url=args.context_store_url,
            )
        )
        ingestion_report = build_ingestion_report(
            ingestion_stats, extraction_results, sessions,
        )

    # Step 6: Build report
    report = build_report(sessions)
    print(f"Git correlation: {report['sessions_with_commits']} sessions matched commits, "
          f"{report['total_git_commits_correlated']} total commits found.", file=sys.stderr)

    # Step 7: Output
    output_data = {
        "report": report,
        "sessions": serialize_sessions(sessions, include_messages=args.include_messages),
    }

    if extraction_results is not None:
        extraction_report = build_extraction_report(extraction_results)
        output_data["extraction_report"] = extraction_report
        output_data["extracted_items"] = serialize_extraction_results(extraction_results)

    if ingestion_report is not None:
        output_data["ingestion_report"] = ingestion_report

    output_json = json.dumps(output_data, indent=2, ensure_ascii=False)

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"Output written to {os.path.abspath(args.output)}", file=sys.stderr)
    else:
        print(output_json)

    # Print report summary to stderr
    print("\n--- Report ---", file=sys.stderr)
    print(f"  Total conversations: {report['total_conversations']}", file=sys.stderr)
    print(f"  Total messages: {report['total_messages']}", file=sys.stderr)
    print(f"  Git commits correlated: {report['total_git_commits_correlated']}", file=sys.stderr)
    print(f"  Sessions with commits: {report['sessions_with_commits']}", file=sys.stderr)
    print(f"  Projects:", file=sys.stderr)
    for proj, count in report["conversations_per_project"].items():
        print(f"    {proj}: {count}", file=sys.stderr)

    if extraction_results is not None:
        ext_report = output_data["extraction_report"]
        print(f"\n--- Extraction ---", file=sys.stderr)
        print(f"  Total items extracted: {ext_report['total_items_extracted']}", file=sys.stderr)
        print(f"  Sessions with items: {ext_report['sessions_with_items']}/{ext_report['sessions_processed']}", file=sys.stderr)
        print(f"  By node type:", file=sys.stderr)
        for nt, count in ext_report["by_node_type"].items():
            print(f"    {nt}: {count}", file=sys.stderr)
        print(f"  By category:", file=sys.stderr)
        for cat, count in ext_report["by_category"].items():
            print(f"    {cat}: {count}", file=sys.stderr)

    if ingestion_report is not None:
        ir = ingestion_report
        print(f"\n--- Ingestion ---", file=sys.stderr)
        print(f"  Nodes created: {ir['total_nodes_created']}", file=sys.stderr)
        print(f"  Nodes failed: {ir['total_nodes_failed']}", file=sys.stderr)
        print(f"  Projects ensured: {ir['projects_ensured']}", file=sys.stderr)
        print(f"  Nodes per project:", file=sys.stderr)
        for proj, count in ir["nodes_per_project"].items():
            print(f"    {proj}: {count}", file=sys.stderr)
        cov = ir["coverage"]
        print(f"  Coverage:", file=sys.stderr)
        print(f"    Sessions with extractions: {cov['sessions_with_extractions']}/{cov['sessions_total']} "
              f"({cov['coverage_pct']}%)", file=sys.stderr)
        print(f"    Ingestion success rate: {cov['ingestion_success_pct']}%", file=sys.stderr)
        if ir["errors"]:
            print(f"  Errors ({len(ir['errors'])}):", file=sys.stderr)
            for err in ir["errors"][:10]:
                print(f"    - {err}", file=sys.stderr)


if __name__ == "__main__":
    main()
