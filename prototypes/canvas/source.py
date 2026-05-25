"""CanvasSource — the shared abstraction every view implements.

A Source is *the only thing that varies* between canvases. Everything else
(graph data model, renderer, Textual shell) is substrate. A Source promises
two operations:

- `load(graph)`     — populate the graph once, synchronously enough that the
                      first render shows something meaningful.
- `subscribe(graph)` — optionally stream mutations into the graph
                      indefinitely. Default no-op. Implementations that have
                      a live stream (orchestration SSE, agent tool calls,
                      file-watcher) override this.

The launcher wires a Source to a CanvasApp: it calls load() up front, then
runs subscribe() as a Textual worker so the canvas stays live as the source
emits mutations.

Why a class instead of a function:
- subscribe() is naturally long-running; load() is naturally one-shot. Keeping
  them on one object lets a source share state between them (e.g. the agent's
  Claude client, the viewer's HTTP client).
- New views often want a `__init__(*args)` that captures the subject — a
  plan_id, a repo_path, a task prompt — without leaking that into the launcher.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from graph import Graph


class CanvasSource(ABC):
    """A producer of graph mutations.

    Sources should be cheap to instantiate. Heavy work (HTTP, subprocess spawn,
    file walks) belongs in load() / subscribe(), not __init__.
    """

    #: Title shown in the Textual header. Override per source.
    title: str = "canvas"

    @abstractmethod
    async def load(self, graph: Graph) -> None:
        """Populate `graph` with the source's initial content.

        May be a no-op for sources that produce everything through subscribe()
        (e.g. a live execution stream). Must not raise on the empty-source
        case — return cleanly and let subscribe() do the work.
        """
        ...

    async def subscribe(self, graph: Graph) -> None:
        """Stream further mutations into `graph` for as long as this source
        has updates to emit. Default no-op for one-shot sources."""
        return
