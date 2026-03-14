#  Orchestration Engine - Enrichment Service
#
#  Transforms context store nodes into XML-tagged prompt enrichment blocks.
#  Classifies nodes by type, respects a token budget, and returns a ready-to-
#  append XML string alongside metadata for usage logging.
#
#  Depends on: config.py (cfg), services/context_store_client.py
#  Used by:    services/task_lifecycle.py

import logging
from dataclasses import dataclass, field

from backend.config import cfg
from backend.services.context_store_client import ContextStoreClient, ContextStoreResult

logger = logging.getLogger("orchestration.enrichment")

# Approximate characters per token (conservative — errs toward fewer tokens used).
_CHARS_PER_TOKEN = 4

# Node type strings that belong to each XML section (lower-cased for matching).
_CONVENTION_TYPES = {"architecture", "decision", "convention", "pattern", "constraint"}
_GOTCHA_TYPES = {"gotcha", "warning", "pitfall", "issue", "bug"}
_OUTCOME_TYPES = {"outcome", "finding", "result", "discovery", "reference"}
# File-context nodes are identified by a populated file path field, not by type.

# XML section ordering — controls which sections get truncated last when over budget.
_SECTION_PRIORITY = [
    "relevant_gotchas",      # highest value — avoid known pitfalls first
    "project_conventions",
    "affected_file_context",
    "prior_outcomes",        # lowest — only when include_prior_outcomes=True
]


@dataclass
class EnrichmentResult:
    """Output of a successful enrichment pass."""
    xml_block: str            # Ready-to-append XML string
    tokens_used: int          # Estimated token count of xml_block
    node_count: int           # Total nodes that contributed content
    latency_ms: float         # Wall-clock time for the context store call


class EnrichmentService:
    """Fetches and formats context store knowledge for prompt injection.

    Thread-safe: no mutable state beyond the config snapshots set in __init__.
    """

    def __init__(self, client: ContextStoreClient | None = None):
        self._enabled: bool = cfg("context_enrichment.enabled", False)
        self._max_tokens: int = cfg("context_enrichment.max_context_tokens", 2000)
        self._include_prior_outcomes: bool = cfg(
            "context_enrichment.include_prior_outcomes", True
        )
        self._client = client or ContextStoreClient()

    async def enrich(self, query: str) -> EnrichmentResult | None:
        """Query the context store and return formatted XML enrichment.

        Returns None when enrichment is disabled, the context store is
        unreachable, or no relevant nodes are found.  Never raises.
        """
        if not self._enabled:
            return None

        result: ContextStoreResult | None = await self._client.preview(query)
        if result is None or not result.nodes:
            return None

        sections = _classify_nodes(result.nodes, self._include_prior_outcomes)
        if not any(sections.values()):
            return None

        xml_block, tokens_used, node_count = _build_xml(
            sections, self._max_tokens, self._include_prior_outcomes
        )
        if not xml_block:
            return None

        return EnrichmentResult(
            xml_block=xml_block,
            tokens_used=tokens_used,
            node_count=node_count,
            latency_ms=result.latency_ms,
        )


# ---------------------------------------------------------------------------
# Node classification
# ---------------------------------------------------------------------------

def _classify_nodes(
    nodes: list[dict], include_prior_outcomes: bool
) -> dict[str, list[dict]]:
    """Sort nodes into the four XML sections.

    A node may appear in at most one section.  File-context is checked first
    (a node with a file path goes to file_context regardless of type), then
    type-based classification, then prior_outcomes as a catch-all when enabled.
    """
    sections: dict[str, list[dict]] = {
        "project_conventions": [],
        "relevant_gotchas": [],
        "prior_outcomes": [],
        "affected_file_context": [],
    }

    for node in nodes:
        file_path = _get_file_path(node)
        if file_path:
            sections["affected_file_context"].append(node)
            continue

        node_type = str(node.get("type") or node.get("nodeType") or "").lower()

        if node_type in _GOTCHA_TYPES:
            sections["relevant_gotchas"].append(node)
        elif node_type in _CONVENTION_TYPES:
            sections["project_conventions"].append(node)
        elif include_prior_outcomes and node_type in _OUTCOME_TYPES:
            sections["prior_outcomes"].append(node)
        elif include_prior_outcomes:
            # Unrecognised types still contribute as prior outcomes
            sections["prior_outcomes"].append(node)
        # else: node is silently dropped when prior_outcomes are disabled and
        # the type does not match conventions or gotchas.

    return sections


def _get_file_path(node: dict) -> str:
    """Return the file path from a node dict, or empty string if absent."""
    for key in ("filePath", "file_path", "path", "fileName"):
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


# ---------------------------------------------------------------------------
# XML formatting with token budget
# ---------------------------------------------------------------------------

def _build_xml(
    sections: dict[str, list[dict]],
    max_tokens: int,
    include_prior_outcomes: bool,
) -> tuple[str, int, int]:
    """Render sections as XML, truncating to stay within max_tokens.

    Returns (xml_block, estimated_tokens, total_node_count).
    Returns ("", 0, 0) if the budget is exhausted before any content fits.
    """
    budget_chars = max_tokens * _CHARS_PER_TOKEN
    parts: list[str] = []
    total_nodes = 0

    for section_key in _SECTION_PRIORITY:
        if section_key == "prior_outcomes" and not include_prior_outcomes:
            continue

        node_list = sections.get(section_key, [])
        if not node_list:
            continue

        tag = section_key  # XML tag matches section key
        section_lines: list[str] = []

        for node in node_list:
            line = _format_node(node, section_key)
            if not line:
                continue
            # Account for the line plus a newline
            if budget_chars <= len(line) + 1:
                break  # No room for even one more line in this section
            section_lines.append(line)
            budget_chars -= len(line) + 1

        if section_lines:
            block = f"<{tag}>\n" + "\n".join(section_lines) + f"\n</{tag}>"
            parts.append(block)
            total_nodes += len(section_lines)

    if not parts:
        return "", 0, 0

    xml_block = "\n".join(parts)
    tokens_used = _count_tokens(xml_block)
    return xml_block, tokens_used, total_nodes


def _format_node(node: dict, section_key: str) -> str:
    """Render a single node as a plain text line suitable for XML body content.

    Prefers title + content; falls back through common field names.
    For file-context nodes, prefixes with the file path.
    """
    content = (
        node.get("content")
        or node.get("text")
        or node.get("body")
        or node.get("summary")
        or ""
    )
    title = node.get("title") or node.get("name") or ""

    content = str(content).strip()
    title = str(title).strip()

    if not content and not title:
        return ""

    if section_key == "affected_file_context":
        file_path = _get_file_path(node)
        label = f"[{file_path}]" if file_path else ""
        if title and content:
            return f"{label} {title}: {content}".strip()
        return f"{label} {title or content}".strip()

    if title and content:
        return f"{title}: {content}"
    return title or content


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def _count_tokens(text: str) -> int:
    """Estimate token count using a character-based approximation.

    Uses 4 chars/token — a conservative estimate that keeps us safely under
    the budget without requiring a tokenizer dependency.
    """
    return max(1, len(text) // _CHARS_PER_TOKEN)
