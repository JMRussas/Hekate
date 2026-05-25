"""AgentSource — Claude Code (via Agent SDK) as a graph-mutation stream.

Wraps the planning agent we proved out in agent.py. The agent's only output
channel is the six canvas mutation tools; everything Claude emits lands as a
mutation on the shared graph that the renderer is watching.

This is the *interpretive* source: pair it with a "plan this task" prompt and
you get planning; pair it with a "draw the implementation architecture of
this repo" prompt and you get an architecture diagram. Same source class.
"""
from __future__ import annotations

import asyncio
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from graph import Graph
from source import CanvasSource


DEFAULT_MODEL = os.environ.get("CANVAS_MODEL", "claude-sonnet-4-6")


DEFAULT_SYSTEM_PROMPT = """\
You are an agent that plans by emitting graph mutations. Your output channel
is exclusively the mutation tools you have been given. A human is watching the
graph fill in as you call these tools.

Node kinds:
- tool_call: a concrete action the eventual executor will perform. config
  must include a `tool` field naming the tool and the args it needs.
- sub_agent: delegated reasoning by another LLM. config must include a `role`
  field describing what the sub-agent should do.
- transform: a deterministic data shaping step (parse, extract, format).
  config should describe the shape, e.g. {"shape": "parse traceback"}.
- check: a verification gate. config must include an `asks` field stating
  the question being checked. Checks emit two ports: `pass` and `fail`.

Connecting nodes — ports:
- tool_call emits `result` (success) and `error` (failure).
- check emits `pass` and `fail`.
- transform and sub_agent emit `out` (default port; omit `from_port`).

Containment:
- A node can live *inside* another node by passing `parent_id` to add_node.
  Use containment for hierarchy (a phase contains tasks; a service contains
  components). Flow edges (connect) cross containment freely.

Constraints:
- Choose short unique ids (n1, n2, ...).
- Each node's `intent` is a one-line prose explanation for the human reader.
- Include at least one `check` node before any irreversible action.
- Do NOT call built-in file/shell tools — only the canvas mutation tools.
- Stop by calling mark_plan_complete when the graph fully describes the work.
"""


ALLOWED_TOOLS = [
    "mcp__canvas__add_node",
    "mcp__canvas__connect",
    "mcp__canvas__remove_node",
    "mcp__canvas__replace_node",
    "mcp__canvas__annotate",
    "mcp__canvas__mark_plan_complete",
]


def _ok() -> dict:
    return {"content": [{"type": "text", "text": "ok"}]}


def _err(e) -> dict:
    return {"content": [{"type": "text", "text": f"error: {e}"}], "is_error": True}


def _make_tools(graph: Graph) -> list:
    """Build the six mutation tools bound to `graph`."""

    @tool(
        "add_node",
        "Place a new node on the canvas. Optionally nest it inside another node via parent_id.",
        {
            "type": "object",
            "properties": {
                "id":   {"type": "string", "description": "short unique id (n1, n2, ...)"},
                "kind": {"type": "string", "enum": ["tool_call", "sub_agent", "transform", "check"]},
                "config": {"type": "object"},
                "intent": {"type": "string"},
                "parent_id": {"type": "string", "description": "optional containment parent id"},
            },
            "required": ["id", "kind", "config", "intent"],
        },
    )
    async def add_node(args):
        try:
            graph.add_node(
                args["id"], args["kind"], args.get("config", {}),
                intent=args.get("intent", ""),
                parent_id=args.get("parent_id"),
            )
            return _ok()
        except Exception as e:
            return _err(e)

    @tool(
        "connect",
        "Draw a flow edge between two nodes (crosses containment freely).",
        {
            "type": "object",
            "properties": {
                "from_id":   {"type": "string"},
                "to_id":     {"type": "string"},
                "from_port": {"type": "string", "enum": ["out", "result", "error", "pass", "fail"]},
            },
            "required": ["from_id", "to_id"],
        },
    )
    async def connect(args):
        try:
            graph.connect(args["from_id"], args["to_id"],
                          from_port=args.get("from_port", "out"))
            return _ok()
        except Exception as e:
            return _err(e)

    @tool(
        "remove_node",
        "Delete a node, its incident flow edges, and its contained sub-graph.",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    )
    async def remove_node(args):
        try:
            graph.remove_node(args["id"])
            return _ok()
        except Exception as e:
            return _err(e)

    @tool(
        "replace_node",
        "Swap a node's kind and/or config in place; preserves id, intent, and containment.",
        {
            "type": "object",
            "properties": {
                "id":   {"type": "string"},
                "kind": {"type": "string", "enum": ["tool_call", "sub_agent", "transform", "check"]},
                "config": {"type": "object"},
            },
            "required": ["id", "kind", "config"],
        },
    )
    async def replace_node(args):
        try:
            graph.replace_node(args["id"], args["kind"], args.get("config", {}))
            return _ok()
        except Exception as e:
            return _err(e)

    @tool(
        "annotate",
        "Update the intent text of an existing node.",
        {
            "type": "object",
            "properties": {
                "id":     {"type": "string"},
                "intent": {"type": "string"},
            },
            "required": ["id", "intent"],
        },
    )
    async def annotate(args):
        try:
            graph.annotate(args["id"], args["intent"])
            return _ok()
        except Exception as e:
            return _err(e)

    @tool(
        "mark_plan_complete",
        "Call when the graph fully describes the plan. Stops the planning phase.",
        {"type": "object", "properties": {}},
    )
    async def mark_plan_complete(args):
        try:
            graph.mark_plan_complete()
            return _ok()
        except Exception as e:
            return _err(e)

    return [add_node, connect, remove_node, replace_node, annotate, mark_plan_complete]


class AgentSource(CanvasSource):
    """Streams Claude's mutation tool calls into the graph.

    load() is a no-op — the canvas starts empty and fills in as the agent
    plans. subscribe() runs the agent loop, which terminates when the agent
    calls mark_plan_complete (or stops calling tools).
    """

    def __init__(
        self,
        task: str,
        *,
        model: str = DEFAULT_MODEL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ):
        self.task = task
        self.model = model
        self.system_prompt = system_prompt
        self.title = f"canvas — agent ({model})"

    async def load(self, graph: Graph) -> None:
        return  # agent produces everything during subscribe()

    async def subscribe(self, graph: Graph) -> None:
        server = create_sdk_mcp_server(
            name="canvas", version="0.1.0", tools=_make_tools(graph),
        )
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=self.system_prompt,
            tools=[],
            mcp_servers={"canvas": server},
            allowed_tools=ALLOWED_TOOLS,
            permission_mode="bypassPermissions",
            setting_sources=None,
        )

        graph.set_status_line("thinking...")
        try:
            async for message in query(prompt=self.task, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            name = block.name.removeprefix("mcp__canvas__")
                            graph.set_status_line(f"called {name}")
                            await asyncio.sleep(0.25)
                elif isinstance(message, ResultMessage):
                    if graph.plan_complete:
                        graph.set_status_line(
                            f"plan complete — {len(graph.nodes)} nodes, {len(graph.edges)} edges"
                        )
                    else:
                        graph.set_status_line(
                            f"stopped without mark_plan_complete "
                            f"(subtype={getattr(message, 'subtype', '?')})"
                        )
                    return
        except Exception as e:
            graph.set_status_line(f"error: {type(e).__name__}: {e}")
