#  Decision Logger
#
#  Audit trail for every sentinel decision — writes to sentinel_decisions table.
#
#  Used by: plan_sentinel.py, reasoner_context.py, routes/sentinel.py

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from backend.services.sentinel.models import DecisionRecord

logger = logging.getLogger(__name__)


class DecisionLogger:
    """Async utility for recording and querying sentinel decisions."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def log_decision(
        self,
        project_id: str,
        command: str,
        reasoning: str,
        confidence: float,
        outcome: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Persist a decision to the sentinel_decisions table.

        Returns the generated decision_id.
        """
        decision_id = uuid.uuid4().hex
        details_json = json.dumps(details) if details else None
        try:
            await self._db.execute_write(
                """INSERT INTO sentinel_decisions
                   (id, project_id, timestamp, command, reasoning, confidence, outcome, details_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    project_id,
                    time.time(),
                    command,
                    reasoning,
                    confidence,
                    outcome,
                    details_json,
                ),
            )
        except Exception:
            logger.warning("Failed to log decision for project %s", project_id, exc_info=True)
        return decision_id

    async def query_decisions(
        self,
        project_id: str,
        limit: int = 50,
        command: str | None = None,
    ) -> list[DecisionRecord]:
        """Fetch recent decisions for a project, newest first.

        Optionally filter by command type.
        """
        try:
            if command:
                rows = await self._db.fetchall(
                    """SELECT id, project_id, timestamp, command, reasoning,
                              confidence, outcome, details_json
                       FROM sentinel_decisions
                       WHERE project_id = ? AND command = ?
                       ORDER BY timestamp DESC LIMIT ?""",
                    (project_id, command, limit),
                )
            else:
                rows = await self._db.fetchall(
                    """SELECT id, project_id, timestamp, command, reasoning,
                              confidence, outcome, details_json
                       FROM sentinel_decisions
                       WHERE project_id = ?
                       ORDER BY timestamp DESC LIMIT ?""",
                    (project_id, limit),
                )
            return [self._row_to_record(r) for r in rows]
        except Exception:
            logger.debug("Failed to query decisions for %s", project_id, exc_info=True)
            return []

    async def query_similar_decisions(
        self,
        command: str,
        limit: int = 20,
    ) -> list[DecisionRecord]:
        """Fetch recent decisions across all projects for a given command type.

        Useful for learning from past interventions of the same kind.
        """
        try:
            rows = await self._db.fetchall(
                """SELECT id, project_id, timestamp, command, reasoning,
                          confidence, outcome, details_json
                   FROM sentinel_decisions
                   WHERE command = ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (command, limit),
            )
            return [self._row_to_record(r) for r in rows]
        except Exception:
            logger.debug("Failed to query similar decisions for %s", command, exc_info=True)
            return []

    @staticmethod
    def _row_to_record(row: Any) -> DecisionRecord:
        """Convert a sqlite3.Row into a DecisionRecord."""
        from datetime import datetime, timezone

        ts = row["timestamp"]
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)

        details_raw = row["details_json"]
        details = json.loads(details_raw) if details_raw else {}

        from backend.services.sentinel.models import SentinelCommand

        try:
            cmd = SentinelCommand(row["command"])
        except ValueError:
            cmd = SentinelCommand.DISPATCH_TASK  # fallback for unknown commands

        record = DecisionRecord(
            id=row["id"],
            project_id=row["project_id"],
            timestamp=dt,
            command=cmd,
            reasoning=row["reasoning"],
            confidence=row["confidence"],
            outcome=row["outcome"] or "",
        )
        # Attach parsed details for callers that need it
        record.details = details  # type: ignore[attr-defined]
        return record
