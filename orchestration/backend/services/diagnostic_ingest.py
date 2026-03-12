#  Orchestration Engine - Diagnostic Ingest Service
#
#  Appends error-resolution pairs to diagnostic-rag's ingest queue.
#  The ingest queue is a JSONL file that diagnostic-rag's pipeline.py
#  reads and embeds via `pipeline.py ingest`.
#
#  Thread safety: uses threading.Lock for file writes since multiple
#  async tasks may complete simultaneously.
#
#  Depends on: config.py
#  Used by:    services/task_lifecycle.py, routes/checkpoints.py

import asyncio
import json
import logging
import threading

from backend.config import RAG_DIAGNOSTIC_INGEST_PATH

logger = logging.getLogger("orchestration.diagnostic_ingest")


class DiagnosticIngester:
    """Appends error-resolution pairs to diagnostic-rag's ingest queue."""

    def __init__(self):
        self._lock = threading.Lock()

    async def ingest_resolution(
        self,
        *,
        error_text: str,
        resolution_text: str,
        error_context: str = "",
        tags: list[str] | None = None,
        gotcha: str = "",
    ):
        """Write one entry to ingest.jsonl. No-op if path not configured."""
        if not RAG_DIAGNOSTIC_INGEST_PATH:
            return

        entry = {
            "error_pattern": error_text,
            "error_context": error_context,
            "resolution": resolution_text,
            "tags": tags or [],
            "gotcha": gotcha,
        }
        await asyncio.to_thread(self._append_sync, entry)

    def _append_sync(self, entry: dict):
        with self._lock:
            try:
                with open(RAG_DIAGNOSTIC_INGEST_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.warning("Failed to write to diagnostic ingest file: %s", e)
