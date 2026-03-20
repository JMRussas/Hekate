#  Orchestration Engine - Fix Queue Service
#
#  Unified queue for failures that need resolution. Any subsystem can file
#  a fix item: learner (pattern-detected), sentinel (monitoring), odin
#  (autonomy), task lifecycle (execution failures), or human (manual).
#
#  Items flow: open → claimed → in_progress → resolved/wont_fix
#  Resolution feeds back into the learner and diagnostic RAG.
#
#  Depends on: db/connection.py, services/diagnostic_ingest.py
#  Used by:    services/learning/execution_learner.py, services/task_lifecycle.py,
#              services/sentinel/, routes/fixes.py

from __future__ import annotations

import json
import logging
import time
import uuid

logger = logging.getLogger("orchestration.fix_queue")

# Valid values for constrained fields
SOURCES = ("learner", "sentinel", "odin", "lifecycle", "human", "rescan")
CATEGORIES = ("model_failure", "infra_bug", "code_bug", "config", "pattern", "performance", "data_quality")
SEVERITIES = ("low", "medium", "high", "critical")
STATUSES = ("open", "claimed", "in_progress", "resolved", "wont_fix")


class FixQueue:
    """Manages the fix_queue table — file, query, claim, resolve."""

    def __init__(self, db):
        self._db = db

    # ------------------------------------------------------------------
    # File a new fix item
    # ------------------------------------------------------------------

    async def file(
        self,
        *,
        source: str,
        category: str,
        title: str,
        description: str,
        severity: str = "medium",
        evidence: list[dict] | None = None,
        proposed_fix: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        affected_component: str | None = None,
        dedupe_key: str | None = None,
    ) -> str | None:
        """File a new fix item. Returns the item ID, or None if deduplicated.

        If dedupe_key is provided and an open/claimed/in_progress item with
        the same key exists, the filing is silently skipped.
        """
        if source not in SOURCES:
            logger.warning("Invalid fix source '%s', defaulting to 'human'", source)
            source = "human"
        if category not in CATEGORIES:
            logger.warning("Invalid fix category '%s', defaulting to 'pattern'", category)
            category = "pattern"
        if severity not in SEVERITIES:
            severity = "medium"

        # Dedupe check
        if dedupe_key:
            existing = await self._db.fetchone(
                "SELECT id, status FROM fix_queue WHERE dedupe_key = $1",
                (dedupe_key,),
            )
            if existing and existing["status"] in ("open", "claimed", "in_progress"):
                logger.debug("Fix item deduplicated: %s (existing %s)", dedupe_key, existing["id"])
                return None

        item_id = uuid.uuid4().hex[:12]
        now = time.time()

        await self._db.execute_write(
            """INSERT INTO fix_queue
               (id, source, category, severity, title, description, evidence_json,
                proposed_fix, status, project_id, task_id, affected_component,
                dedupe_key, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)""",
            (
                item_id, source, category, severity, title, description,
                json.dumps(evidence or []), proposed_fix, "open",
                project_id, task_id, affected_component,
                dedupe_key, now, now,
            ),
        )

        logger.info(
            "Fix filed: [%s/%s] %s (id=%s, component=%s)",
            severity, category, title, item_id, affected_component or "?",
        )
        return item_id

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def list_open(
        self,
        *,
        category: str | None = None,
        severity: str | None = None,
        component: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List open fix items, ordered by severity then age."""
        conditions = ["status IN ('open', 'claimed', 'in_progress')"]
        params: list = []
        idx = 1

        if category:
            conditions.append(f"category = ${idx}")
            params.append(category)
            idx += 1
        if severity:
            conditions.append(f"severity = ${idx}")
            params.append(severity)
            idx += 1
        if component:
            conditions.append(f"affected_component = ${idx}")
            params.append(component)
            idx += 1

        where = " AND ".join(conditions)
        params.append(limit)

        rows = await self._db.fetchall(
            f"""SELECT * FROM fix_queue
                WHERE {where}
                ORDER BY
                  CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                  END,
                  created_at ASC
                LIMIT ${idx}""",
            tuple(params),
        )
        return [dict(r) for r in rows]

    async def get(self, item_id: str) -> dict | None:
        """Get a single fix item by ID."""
        row = await self._db.fetchone(
            "SELECT * FROM fix_queue WHERE id = $1", (item_id,),
        )
        return dict(row) if row else None

    async def count_by_status(self) -> dict[str, int]:
        """Count items by status."""
        rows = await self._db.fetchall(
            "SELECT status, COUNT(*) as cnt FROM fix_queue GROUP BY status",
            params=(),
        )
        return {r["status"]: r["cnt"] for r in rows}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def claim(self, item_id: str, claimed_by: str) -> bool:
        """Claim an open item. Returns True if successfully claimed."""
        result = await self._db.execute_write(
            """UPDATE fix_queue SET status = 'claimed', claimed_by = $1, updated_at = $2
               WHERE id = $3 AND status = 'open'""",
            (claimed_by, time.time(), item_id),
        )
        from backend.db.connection import parse_rowcount
        return parse_rowcount(result) > 0

    async def start(self, item_id: str) -> bool:
        """Move a claimed item to in_progress."""
        result = await self._db.execute_write(
            """UPDATE fix_queue SET status = 'in_progress', updated_at = $1
               WHERE id = $2 AND status IN ('open', 'claimed')""",
            (time.time(), item_id),
        )
        from backend.db.connection import parse_rowcount
        return parse_rowcount(result) > 0

    async def resolve(
        self,
        item_id: str,
        *,
        resolution: str,
        resolved_by: str = "human",
    ) -> bool:
        """Resolve a fix item with what was done.

        Feeds the resolution back into the diagnostic RAG ingest queue
        so future similar failures can find this solution.
        """
        now = time.time()
        result = await self._db.execute_write(
            """UPDATE fix_queue SET status = 'resolved', resolution = $1,
               resolved_by = $2, resolved_at = $3, updated_at = $4
               WHERE id = $5 AND status IN ('open', 'claimed', 'in_progress')""",
            (resolution, resolved_by, now, now, item_id),
        )
        from backend.db.connection import parse_rowcount
        changed = parse_rowcount(result) > 0

        if changed:
            # Feed resolution into diagnostic RAG
            item = await self.get(item_id)
            if item:
                try:
                    from backend.services.diagnostic_ingest import DiagnosticIngester
                    ingester = DiagnosticIngester()
                    await ingester.ingest_resolution(
                        error_text=item["title"],
                        resolution_text=resolution,
                        error_context=item["description"],
                        tags=["fix-queue", item["category"], item["source"]],
                        gotcha=item.get("proposed_fix") or "",
                    )
                except Exception as e:
                    logger.debug("Failed to ingest fix resolution to diagnostic RAG: %s", e)

            logger.info("Fix resolved: %s by %s", item_id, resolved_by)

        return changed

    async def wont_fix(self, item_id: str, reason: str) -> bool:
        """Mark a fix item as won't fix with explanation."""
        now = time.time()
        result = await self._db.execute_write(
            """UPDATE fix_queue SET status = 'wont_fix', resolution = $1,
               resolved_at = $2, updated_at = $3
               WHERE id = $4 AND status IN ('open', 'claimed', 'in_progress')""",
            (reason, now, now, item_id),
        )
        from backend.db.connection import parse_rowcount
        return parse_rowcount(result) > 0

    # ------------------------------------------------------------------
    # Bulk operations for learner/rescan integration
    # ------------------------------------------------------------------

    async def file_from_learner(
        self,
        *,
        pattern_id: str,
        title: str,
        description: str,
        severity: str = "medium",
        evidence: list[dict] | None = None,
        proposed_fix: str | None = None,
        affected_component: str | None = None,
    ) -> str | None:
        """Convenience: file a fix from the execution learner with auto-dedupe."""
        return await self.file(
            source="learner",
            category="pattern",
            title=title,
            description=description,
            severity=severity,
            evidence=evidence,
            proposed_fix=proposed_fix,
            affected_component=affected_component,
            dedupe_key=f"learner:{pattern_id}",
        )

    async def file_from_lifecycle(
        self,
        *,
        task_id: str,
        project_id: str,
        title: str,
        description: str,
        error: str,
        severity: str = "medium",
        affected_component: str | None = None,
    ) -> str | None:
        """Convenience: file a fix from task lifecycle (execution failure)."""
        return await self.file(
            source="lifecycle",
            category="code_bug",
            title=title,
            description=description,
            severity=severity,
            evidence=[{"type": "error", "content": error}],
            task_id=task_id,
            project_id=project_id,
            affected_component=affected_component,
            dedupe_key=f"lifecycle:{task_id}",
        )
