"""Task state machine — formal states and validated transitions.

Defines the valid task states and which transitions between them are legal.
Used by handlers to enforce lifecycle invariants and create an audit trail
for future durable execution replay.

IMPORTANT: This module logs invalid transitions as warnings but does NOT
crash. The system ran without validation for months, so there may be edge
cases. Log and allow for now; enforce later.
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Any

logger = logging.getLogger("gods.task_states")


class TaskState(str, enum.Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"


# Valid transitions: {from_state: {to_state, ...}}
VALID_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING: {TaskState.DISPATCHED, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.RUNNING},
    TaskState.BLOCKED: {TaskState.PENDING, TaskState.CANCELLED},
    TaskState.QUEUED: {TaskState.DISPATCHED, TaskState.CANCELLED, TaskState.RUNNING},
    TaskState.DISPATCHED: {TaskState.RUNNING, TaskState.PENDING, TaskState.CANCELLED},
    TaskState.RUNNING: {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.COMPLETED: {TaskState.PENDING, TaskState.NEEDS_REVIEW},  # retry or human review after verification
    TaskState.FAILED: {TaskState.PENDING, TaskState.CANCELLED, TaskState.NEEDS_REVIEW},
    TaskState.NEEDS_REVIEW: {TaskState.COMPLETED, TaskState.PENDING, TaskState.CANCELLED},
}

TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})


def validate_transition(from_state: str, to_state: str) -> bool:
    """Check if a state transition is valid.

    Returns True for valid transitions, False otherwise.
    Also returns False if either state is not a recognized TaskState.
    """
    try:
        f = TaskState(from_state)
        t = TaskState(to_state)
        return t in VALID_TRANSITIONS.get(f, set())
    except ValueError:
        return False


async def transition_task(
    db: Any,
    task_id: str,
    new_state: str,
    *,
    extra_fields: dict[str, Any] | None = None,
    extra_sql: str = "",
    extra_params: tuple = (),
    source: str = "",
) -> None:
    """Transition a task to a new state with validation.

    Logs the transition for audit trail. If the transition is invalid,
    logs a warning but STILL performs it (soft enforcement).

    Args:
        db: Database adapter with fetchone() and execute_write()
        task_id: Task ID
        new_state: Target state string
        extra_fields: Additional column=value pairs to SET (e.g. {"error": "...", "retry_count": 3})
        extra_sql: Raw SQL fragment appended to SET clause (for expressions like retry_count + 1)
        extra_params: Parameters for extra_sql placeholders
        source: Handler name for logging context
    """
    row = await db.fetchone("SELECT status FROM tasks WHERE id = $1", (task_id,))
    if not row:
        logger.error("TaskState: task %s not found (source=%s)", task_id[:8], source)
        raise ValueError(f"Task {task_id} not found")

    current = row["status"]

    if validate_transition(current, new_state):
        logger.info("TaskState: %s %s -> %s%s",
                     task_id[:8], current, new_state,
                     f" ({source})" if source else "")
    else:
        logger.warning("TaskState: INVALID %s %s -> %s%s (allowing — soft enforcement)",
                        task_id[:8], current, new_state,
                        f" ({source})" if source else "")

    # Build the UPDATE statement
    set_parts = ["status = $1", "updated_at = $2"]
    params: list[Any] = [new_state, time.time()]

    if extra_fields:
        for col, val in extra_fields.items():
            idx = len(params) + 1
            set_parts.append(f"{col} = ${idx}")
            params.append(val)

    if extra_sql:
        set_parts.append(extra_sql)
        params.extend(extra_params)

    task_id_idx = len(params) + 1
    params.append(task_id)

    sql = f"UPDATE tasks SET {', '.join(set_parts)} WHERE id = ${task_id_idx}"
    await db.execute_write(sql, tuple(params))
