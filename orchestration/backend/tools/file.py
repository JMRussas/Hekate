#  Orchestration Engine - File Tool
#
#  Sandboxed file read/write for task output.
#  All blocking I/O runs in asyncio.to_thread to avoid blocking the event loop.
#
#  Depends on: backend/config.py, tools/base.py
#  Used by:    services/executor.py (via tool registry)

import asyncio
from pathlib import Path

from backend.config import DATA_DIR
from backend.tools.base import Tool

OUTPUT_BASE = DATA_DIR / "projects"


def _safe_path(project_id: str, rel_path: str, ensure_base: bool = False) -> Path:
    """Resolve a relative path within the project sandbox. Prevents path traversal.

    Args:
        ensure_base: If True, create the project base directory (for writes).
    """
    base = OUTPUT_BASE / project_id
    if ensure_base:
        base.mkdir(parents=True, exist_ok=True)

    resolved = (base / rel_path).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"Path traversal detected: {rel_path}")
    return resolved


def _read_sync(project_id: str, rel_path: str) -> str:
    """Sync file read — runs in thread pool."""
    fp = _safe_path(project_id, rel_path)
    if not fp.exists():
        return f"Error: File not found: {rel_path}"
    content = fp.read_text(encoding="utf-8")
    if len(content) > 50_000:
        return content[:50_000] + f"\n\n... (truncated, {len(content)} chars total)"
    return content


def _write_sync(project_id: str, rel_path: str, content: str) -> str:
    """Sync file write — runs in thread pool."""
    fp = _safe_path(project_id, rel_path, ensure_base=True)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return f"File written: {rel_path} ({len(content)} chars)"


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a file from the project workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path within the project workspace"},
            "project_id": {"type": "string", "description": "Project ID (auto-injected by executor)"},
        },
        "required": ["path", "project_id"],
    }

    async def execute(self, params: dict) -> str:
        try:
            return await asyncio.to_thread(
                _read_sync, params["project_id"], params["path"]
            )
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {e}"


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write a file to the project workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path within the project workspace"},
            "content": {"type": "string", "description": "File content to write"},
            "project_id": {"type": "string", "description": "Project ID (auto-injected by executor)"},
        },
        "required": ["path", "content", "project_id"],
    }

    async def execute(self, params: dict) -> str:
        try:
            return await asyncio.to_thread(
                _write_sync, params["project_id"], params["path"], params["content"]
            )
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {e}"
