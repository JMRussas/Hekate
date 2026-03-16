#  Orchestration Engine - Hekate MCP Tool
#
#  Pre-flight code analysis via hecate-mcp. Called before task dispatch
#  to enrich task prompts with verdicts, constraints, analogs, and file paths.
#
#  Depends on: backend/config.py, tools/base.py
#  Used by:    services/executor.py (via tool registry)

import json
import logging

import httpx

from backend.config import cfg
from backend.tools.base import Tool

logger = logging.getLogger("orchestration.tools.hekate_mcp")

# Config with defaults
HEKATE_MCP_URL = cfg("hekate_mcp.url", "http://192.168.1.164:5110")
HEKATE_MCP_TIMEOUT = cfg("hekate_mcp.timeout", 60.0)
HEKATE_MCP_DEFAULT_PROJECT = cfg("hekate_mcp.default_project", "")


class HekateMcpError(Exception):
    """Raised when hecate-mcp returns an error."""


async def _call_tool(
    http_client: httpx.AsyncClient | None,
    tool_name: str,
    arguments: dict,
    timeout: float = HEKATE_MCP_TIMEOUT,
) -> dict:
    """Call an hecate-mcp tool via MCP JSON-RPC over HTTP.

    The MCP HTTP transport uses POST to the /mcp endpoint with
    JSON-RPC 2.0 format for tool calls.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    try:
        if http_client:
            resp = await http_client.post(
                f"{HEKATE_MCP_URL}/mcp",
                json=payload,
                timeout=timeout,
            )
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{HEKATE_MCP_URL}/mcp", json=payload)

        resp.raise_for_status()
        result = resp.json()

        if "error" in result:
            raise HekateMcpError(result["error"].get("message", str(result["error"])))

        # MCP tool results come back as content array
        content = result.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            return json.loads(content[0]["text"])
        return result.get("result", {})

    except httpx.ConnectError:
        raise HekateMcpError(f"hecate-mcp not reachable at {HEKATE_MCP_URL}")
    except json.JSONDecodeError:
        raise HekateMcpError("hecate-mcp returned non-JSON response")


class HekateAnalyzeTool(Tool):
    """Pre-flight analysis: calls decide + where to build a structured brief."""

    name = "hekate_analyze"
    description = (
        "Analyze a codebase before task execution. Returns constraints, "
        "patterns, nearest analogs, and recommended starting points. "
        "Use this to enrich task prompts with specific code context."
    )
    parameters = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "What the task is trying to do (e.g., 'add health bar UI')",
            },
            "project": {
                "type": "string",
                "default": "",
                "description": "Path to .csproj or .sln. Uses default project if empty.",
            },
            "file_path": {
                "type": "string",
                "default": "",
                "description": "Target file path for decide (optional).",
            },
        },
        "required": ["intent"],
    }

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._http = http_client

    async def execute(self, params: dict) -> str:
        intent = params["intent"]
        project = params.get("project") or HEKATE_MCP_DEFAULT_PROJECT
        file_path = params.get("file_path", "")

        if not project:
            return "Error: No project specified and no default_project configured in hekate_mcp config."

        results = {}
        errors = []

        # Call 'where' to find starting points
        try:
            where_result = await _call_tool(self._http, "where", {
                "intent": intent,
                "project": project,
            })
            results["where"] = where_result
        except HekateMcpError as e:
            errors.append(f"where: {e}")

        # Call 'decide' if we have a file path (from where results or param)
        target_file = file_path
        if not target_file and "where" in results:
            candidates = results["where"].get("Candidates", [])
            if candidates:
                target_file = candidates[0].get("FilePath", "")

        if target_file:
            try:
                decide_result = await _call_tool(self._http, "decide", {
                    "intent": intent,
                    "project": project,
                    "file_path": target_file,
                })
                results["decide"] = decide_result
            except HekateMcpError as e:
                errors.append(f"decide: {e}")

        # Build structured brief
        brief = self._build_brief(intent, results, errors)
        return brief

    def _build_brief(self, intent: str, results: dict, errors: list) -> str:
        """Build a structured analysis brief from hecate-mcp results."""
        sections = [f"## Hekate Analysis: {intent}\n"]

        # Signals
        where = results.get("where", {})
        signals = where.get("Signals", {})
        if signals.get("HasSignals"):
            parts = []
            if signals.get("IsCreateNew"):
                parts.append("CREATE NEW")
            if signals.get("AnalogTarget"):
                parts.append(f"analog: {signals['AnalogTarget']}")
            if signals.get("RoleHint"):
                parts.append(f"role: {signals['RoleHint']}")
            if signals.get("PatternMatches"):
                parts.append(f"patterns: {', '.join(signals['PatternMatches'])}")
            if parts:
                sections.append(f"**Signals**: {' | '.join(parts)}")

        # Starting points
        candidates = where.get("Candidates", [])
        if candidates:
            sections.append("\n**Starting Points**:")
            for c in candidates[:5]:
                fp = c.get("FilePath", "")
                tn = c.get("TypeName", "")
                mn = c.get("MemberName", "")
                role = c.get("Role", "")
                template = " [TEMPLATE]" if c.get("IsTemplate") else ""
                target = f"{tn}.{mn}" if mn else tn
                sections.append(f"- `{fp}` → {target} (role: {role}){template}")

        # Verdict (from decide)
        decide = results.get("decide", {})
        verdict = decide.get("Verdict") if isinstance(decide, dict) else decide
        if isinstance(verdict, dict):
            if verdict.get("Constraints"):
                sections.append("\n**Constraints**:")
                for c in verdict["Constraints"]:
                    rule = c.get("Rule", c) if isinstance(c, dict) else c
                    sections.append(f"- {rule}")

            if verdict.get("Guidelines"):
                sections.append("\n**Guidelines**:")
                for g in verdict["Guidelines"]:
                    avoid = g.get("Avoid", g) if isinstance(g, dict) else g
                    prefer = g.get("Prefer", "") if isinstance(g, dict) else ""
                    sections.append(f"- Avoid: {avoid}")
                    if prefer:
                        sections.append(f"  Prefer: {prefer}")

            if verdict.get("NearestAnalogs"):
                sections.append("\n**Nearest Analogs** (match these patterns):")
                for a in verdict["NearestAnalogs"][:5]:
                    fp = a.get("FilePath", "")
                    sim = a.get("Similarity", 0)
                    traits = ", ".join(a.get("SharedTraits", []))
                    sections.append(f"- `{fp}` ({sim:.0%} similar: {traits})")

            if verdict.get("Role"):
                sections.append(f"\n**Role**: {verdict['Role']}")
            if verdict.get("TrustLevel"):
                sections.append(f"**Trust Level**: {verdict['TrustLevel']}")

        if errors:
            sections.append(f"\n**Errors**: {'; '.join(errors)}")

        return "\n".join(sections)


class HekateReviewTool(Tool):
    """Post-flight review: validates files against role constraints."""

    name = "hekate_review"
    description = (
        "Review files against codebase constraints after changes. "
        "Returns violations with severity and suggested fixes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_paths": {
                "type": "string",
                "description": "Comma-separated file paths to review.",
            },
            "project": {
                "type": "string",
                "default": "",
                "description": "Path to .csproj or .sln. Uses default project if empty.",
            },
        },
        "required": ["file_paths"],
    }

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._http = http_client

    async def execute(self, params: dict) -> str:
        file_paths = params["file_paths"]
        project = params.get("project") or HEKATE_MCP_DEFAULT_PROJECT

        if not project:
            return "Error: No project specified and no default_project configured."

        try:
            result = await _call_tool(self._http, "review", {
                "project": project,
                "file_paths": file_paths,
            })
            return json.dumps(result, indent=2)
        except HekateMcpError as e:
            return f"Error: {e}"
