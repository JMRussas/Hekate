"""Textual renderer for the planning graph.

Polls Graph.version on a fast interval and re-renders when it changes. Polling
keeps us off the hook for thread-safe notification — the agent and executor can
mutate the graph from any context and the UI will catch up within a frame.

Layout: nodes are stacked top-to-bottom in insertion order. Each node panel
shows its incoming edges as text ("← n2[result]"), so multi-parent and
multi-port connections render unambiguously without a full spatial layout.
This is intentionally crude — once the experiment proves out we can invest in
real graph layout.
"""
from __future__ import annotations

from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

from graph import Graph, NodeKind, NodeStatus


STATUS_COLOR = {
    NodeStatus.PLANNED: "grey50",
    NodeStatus.READY:   "yellow",
    NodeStatus.RUNNING: "cyan",
    NodeStatus.DONE:    "green",
    NodeStatus.FAILED:  "red",
}

KIND_GLYPH = {
    # behavioral
    NodeKind.TOOL_CALL:  "⚙",
    NodeKind.SUB_AGENT:  "◆",
    NodeKind.TRANSFORM:  "▶",
    NodeKind.CHECK:      "?",
    # structural (context store)
    NodeKind.PLAN:       "★",
    NodeKind.PLAN_PHASE: "▣",
    NodeKind.TASK:       "□",
    NodeKind.QUESTION:   "?",
    NodeKind.RISK:       "△",
    # filesystem (synthetic)
    NodeKind.DIRECTORY:  "▸",
    NodeKind.FILE:       "▤",
    # code (context store decomposer)
    NodeKind.COMPILATION_UNIT: "≡",
    NodeKind.NAMESPACE:        "§",
    NodeKind.CLASS:            "▦",
    NodeKind.STRUCT:           "▥",
    NodeKind.METHOD:           "→",
    NodeKind.FIELD:            "·",
    NodeKind.CONSTRUCTOR:      "+",
    NodeKind.PARAMETER:        "·",
}


class GraphView(VerticalScroll):
    """Renders a Graph as a stack of Static widgets, one per node."""

    DEFAULT_CSS = """
    GraphView { padding: 1 2; }
    GraphView > Static { margin-bottom: 1; }
    """

    def __init__(self, graph: Graph):
        super().__init__()
        self.graph = graph
        self._last_version = -1
        self._header = Static(self._render_header())
        self._node_widgets: dict[str, Static] = {}

    def compose(self) -> ComposeResult:
        yield self._header

    def on_mount(self) -> None:
        self.set_interval(0.05, self._tick)
        self._tick()

    def _tick(self) -> None:
        if self.graph.version == self._last_version:
            return
        self._last_version = self.graph.version
        self._refresh()

    def _refresh(self) -> None:
        self._header.update(self._render_header())

        # Remove widgets for nodes that were removed from the graph.
        for nid in list(self._node_widgets):
            if nid not in self.graph.nodes:
                self._node_widgets[nid].remove()
                del self._node_widgets[nid]

        # Add/update widgets in graph insertion order. Appending new nodes at
        # the end (rather than re-sorting topologically on every change)
        # actually conveys "the agent added this later" naturally.
        for nid in self.graph.nodes:
            panel = self._render_node(nid)
            if nid in self._node_widgets:
                self._node_widgets[nid].update(panel)
            else:
                w = Static(panel)
                self._node_widgets[nid] = w
                self.mount(w)

    def _render_header(self) -> Text:
        g = self.graph
        out = Text()
        out.append("plan", style="bold")
        out.append(f"  v{g.version}  ", style="dim")
        if g.plan_complete:
            out.append("complete", style="green bold")
        elif g.nodes:
            out.append("drafting", style="yellow")
        else:
            out.append("waiting for first node", style="grey50 italic")
        out.append(f"  ·  {len(g.nodes)} nodes, {len(g.edges)} edges", style="dim")
        if g.status_line:
            out.append(f"\n{g.status_line}", style="cyan italic")
        return out

    def _render_node(self, nid: str):
        n = self.graph.nodes[nid]
        color = STATUS_COLOR[n.status]
        glyph = KIND_GLYPH[n.kind]
        depth = self.graph.depth(nid)

        body = Text()
        body.append(f"{glyph} ", style=f"bold {color}")
        body.append(nid, style=f"bold {color}")
        body.append(f"   {n.kind.value}", style="dim")
        body.append("   ")
        body.append(n.status.value, style=f"{color} bold")

        if n.intent:
            body.append(f"\n  {n.intent}", style="italic")

        parents = self.graph.parents(nid)
        if parents:
            body.append("\n  ← ", style="dim")
            for i, (pid, port) in enumerate(parents):
                if i > 0:
                    body.append(", ", style="dim")
                body.append(pid, style="dim")
                if port != "out":
                    body.append(f"[{port}]", style="dim italic")

        if n.config:
            body.append("\n  ", style="dim")
            cfg_str = ", ".join(f"{k}={v!r}" for k, v in n.config.items())
            if len(cfg_str) > 80:
                cfg_str = cfg_str[:77] + "..."
            body.append(cfg_str, style="dim")

        if n.result:
            r = n.result if len(n.result) <= 100 else n.result[:97] + "..."
            body.append(f"\n  → {r}", style="green")
        if n.error:
            e = n.error if len(n.error) <= 100 else n.error[:97] + "..."
            body.append(f"\n  ✗ {e}", style="red")

        panel = Panel(body, border_style=color, expand=True, padding=(0, 1))
        # Indent contained nodes so the visual hierarchy mirrors parent_id.
        # Flow edges still render inline as "← parent[port]"; this margin is
        # *only* containment, never flow.
        if depth > 0:
            return Padding(panel, (0, 0, 0, depth * 3))
        return panel


class CanvasApp(App):
    """Hosts a single GraphView."""

    CSS = """
    Screen { background: #0a0a0a; }
    """

    BINDINGS = [
        ("q", "quit", "quit"),
    ]

    def __init__(self, graph: Graph, title: str = "canvas"):
        super().__init__()
        self.graph = graph
        self.title = title

    def compose(self) -> ComposeResult:
        yield Header()
        yield GraphView(self.graph)
        yield Footer()
