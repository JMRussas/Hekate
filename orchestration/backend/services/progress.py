#  Orchestration Engine - Progress Manager
#
#  SSE broadcast and event persistence for real-time task progress.
#
#  Depends on: backend/db/connection.py
#  Used by:    container.py, routes/events.py, services/executor.py

import asyncio
import json
import time


class ProgressManager:
    """Manages SSE subscriptions and event broadcasting.

    Events are persisted to SQLite (task_events table) and broadcast
    to active SSE subscribers via asyncio queues.
    """

    def __init__(self, db):
        self._db = db
        # project_id -> list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        # Global subscribers (receive events from ALL projects)
        self._global_subscribers: list[asyncio.Queue] = []

    async def push_event(
        self,
        project_id: str,
        event_type: str,
        message: str = "",
        task_id: str | None = None,
        **data,
    ):
        """Persist an event and broadcast to SSE subscribers."""
        now = time.time()

        # Persist to SQLite
        await self._db.execute_write(
            "INSERT INTO task_events (project_id, task_id, event_type, message, data_json, timestamp) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            (project_id, task_id, event_type, message, json.dumps(data), now),
        )

        # Broadcast to subscribers
        event = {
            "type": event_type,
            "message": message,
            "project_id": project_id,
            "task_id": task_id,
            "timestamp": now,
            **data,
        }

        for queue in self._subscribers.get(project_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop if subscriber is slow

        # Broadcast to global subscribers (Iris unified stream)
        for queue in self._global_subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop if subscriber is slow

    async def get_events(
        self,
        project_id: str,
        task_id: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Load events from SQLite."""
        if task_id:
            rows = await self._db.fetchall(
                "SELECT event_type, message, task_id, data_json, timestamp "
                "FROM task_events WHERE project_id = $1 AND task_id = $2 "
                "ORDER BY id DESC LIMIT $3",
                (project_id, task_id, limit),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT event_type, message, task_id, data_json, timestamp "
                "FROM task_events WHERE project_id = $1 "
                "ORDER BY id DESC LIMIT $2",
                (project_id, limit),
            )

        return [
            {
                "event_type": r["event_type"],
                "message": r["message"],
                "task_id": r["task_id"],
                "data": json.loads(r["data_json"]) if r["data_json"] else {},
                "timestamp": r["timestamp"],
            }
            for r in reversed(rows)
        ]

    async def subscribe(self, project_id: str):
        """Yield SSE-formatted strings for a project. Used by events endpoint."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.setdefault(project_id, []).append(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    if event.get("type") in ("project_complete", "project_failed"):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            subs = self._subscribers.get(project_id, [])
            if queue in subs:
                subs.remove(queue)
            if not subs and project_id in self._subscribers:
                del self._subscribers[project_id]

    async def subscribe_all(self):
        """Yield SSE-formatted strings for ALL projects.

        Used by the Iris desktop app unified stream endpoint.
        Maps internal event types to the Iris-expected types:
          task_start -> status, task_complete -> done, task_failed -> error,
          tool_call -> tool_call, task_output -> output, token -> token,
          phase -> phase, tool_result -> tool_result.
        Other events are passed through as 'status' type.
        """
        # Map internal event types to Iris SSE event types
        _EVENT_MAP = {
            "task_start": "status",
            "task_complete": "done",
            "task_failed": "error",
            "task_retry": "status",
            "task_cancelled": "status",
            "task_needs_review": "status",
            "task_zombie_recovered": "status",
            "project_complete": "done",
            "project_failed": "error",
            "project_blocked": "status",
            "wave_checkpoint": "status",
            "budget_warning": "status",
            "tool_call": "tool_call",
            "tool_result": "tool_result",
            "task_output": "output",
            "token": "token",
            "phase": "phase",
            "checkpoint": "status",
            "pr_created": "status",
            "branch_merged": "status",
        }

        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._global_subscribers.append(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    iris_type = _EVENT_MAP.get(event["type"], "status")
                    iris_event = {
                        "task_id": event.get("task_id"),
                        "project_id": event.get("project_id"),
                        "status": event.get("type"),
                        "message": event.get("message", ""),
                        "timestamp": event.get("timestamp"),
                    }
                    # Include extra data fields (cost_usd, tool, etc.)
                    for k, v in event.items():
                        if k not in ("type", "task_id", "project_id", "message", "timestamp"):
                            iris_event[k] = v
                    yield f"event: {iris_type}\ndata: {json.dumps(iris_event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in self._global_subscribers:
                self._global_subscribers.remove(queue)
