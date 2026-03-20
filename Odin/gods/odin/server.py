#!/usr/bin/env python3
"""
Odin — The All-Father. System overseer for the Hekate orchestration engine.

Runs as an NSSM service (HekateOdin) on port 5220. Provides:
  - /health endpoint for service monitoring
  - Heartbeat to god_events table via God base class
  - FastMCP tool surface for MCP clients

Uses the God base class for Tier 1 self-awareness (heartbeat, tool tracking,
dependency health checks).
"""

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Ensure gods package is importable when run from gods/odin/
_GODS_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_GODS_ROOT) not in sys.path:
    sys.path.insert(0, str(_GODS_ROOT))

from gods.base import God, DatabaseLike
from gods.odin.dispatch import (
    DispatchCommand,
    FailureDiagnosis,
    build_dispatch_commands,
)
from gods.odin.mcp_client import MCPClientManager, MCPServerRegistry

# ---------------------------------------------------------------------------
# Configuration — loaded from god.json, overridable via env vars
# ---------------------------------------------------------------------------

_GOD_JSON = Path(__file__).parent / "god.json"

def _load_config() -> dict:
    with open(_GOD_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = _load_config()
PORT = int(os.environ.get("ODIN_PORT", CONFIG["port"]))
LLM_GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "http://localhost:5210")

_cfg = CONFIG.get("config", {})
TICK_INTERVAL = int(os.environ.get("ODIN_TICK_INTERVAL", _cfg.get("tick_interval", 30)))
HEARTBEAT_INTERVAL = float(_cfg.get("heartbeat_interval", 30))
MCP_IDLE_TIMEOUT = float(_cfg.get("mcp_idle_timeout", 300))
MCP_REAP_INTERVAL = float(_cfg.get("mcp_reap_interval", 30))
MAX_CONCURRENT = int(_cfg.get("max_concurrent", 4))
DISPATCH_ENABLED = _cfg.get("features", {}).get("intelligent_dispatch", True)

# Orchestration DB path — Odin reads the orchestration SQLite database
# to build world state and query tasks. This is read-write because Odin
# publishes dispatch commands by updating task status.
ORCHESTRATION_DB_PATH = os.environ.get(
    "ORCHESTRATION_DB",
    str(Path(os.environ.get("HEKATE_SOURCE", "C:/Users/jruss/Documents/GitHub/Hekate"))
        / "orchestration" / "data" / "orchestration.db"),
)
ORCHESTRATION_API_URL = os.environ.get("ORCHESTRATION_API_URL", "http://localhost:5200")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-5s %(message)s",
)
logger = logging.getLogger("odin")


# ---------------------------------------------------------------------------
# Lightweight SQLite database adapter (matches DatabaseLike protocol)
# ---------------------------------------------------------------------------

class _SQLiteDB:
    """Minimal async SQLite wrapper matching the God base class protocol.

    Translates Postgres-style $N placeholders to ? for SQLite, consistent
    with the orchestration Database class convention.
    """

    def __init__(self, path: str | Path):
        self._path = str(path)
        self._conn = None

    async def init(self):
        import aiosqlite
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = _dict_factory
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._ensure_tables()

    async def _ensure_tables(self):
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS god_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                god_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT,
                severity TEXT DEFAULT 'info',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_god_events_name_created
            ON god_events (god_name, created_at)
        """)
        await self._conn.commit()

    @staticmethod
    def _pg_to_sqlite(sql: str) -> str:
        """Convert $1, $2, ... placeholders to ?."""
        import re
        return re.sub(r"\$\d+", "?", sql)

    async def execute_write(self, sql: str, params: tuple | list = ()) -> str:
        translated = self._pg_to_sqlite(sql)
        cursor = await self._conn.execute(translated, params)
        await self._conn.commit()
        return str(cursor.lastrowid)

    async def fetchone(self, sql: str, params: tuple | list = ()):
        translated = self._pg_to_sqlite(sql)
        cursor = await self._conn.execute(translated, params)
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list:
        translated = self._pg_to_sqlite(sql)
        cursor = await self._conn.execute(translated, params)
        return await cursor.fetchall()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None


def _dict_factory(cursor, row):
    """aiosqlite row factory → dict."""
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


# ---------------------------------------------------------------------------
# Odin god instance
# ---------------------------------------------------------------------------

class OdinGod(God):
    """Odin — the All-Father. Inherits Tier 1 self-awareness from God."""

    def __init__(
        self,
        db: DatabaseLike,
        mcp_manager: MCPClientManager,
        orch_db: DatabaseLike | None = None,
    ):
        super().__init__("odin", db, heartbeat_interval=HEARTBEAT_INTERVAL)
        self._tick_count = 0
        self._last_tick_at: float = 0
        self._decisions_count = 0
        self._recent_decisions: list[dict] = []
        self.mcp = mcp_manager
        self._orch_db: DatabaseLike = orch_db or db
        self._dispatch_task: asyncio.Task | None = None
        # Sentinel observation polling — tracks last poll timestamp
        self._last_obs_poll_at: float = time.time()
        self._recent_anomalies: list[dict] = []

    async def on_start(self):
        """Register dependency health checks, start MCP manager and dispatch loop."""
        self.register_dependency("llm_gateway", self._check_llm_gateway)
        await self.mcp.start()
        if DISPATCH_ENABLED:
            self._dispatch_task = asyncio.create_task(
                self._dispatch_loop(), name="odin-dispatch"
            )
            logger.info("Dispatch loop started (tick=%ds, max_concurrent=%d)",
                        TICK_INTERVAL, MAX_CONCURRENT)
        logger.info("Odin dependencies registered, MCP client manager started")

    async def on_stop(self):
        """Tear down dispatch loop and MCP servers."""
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            self._dispatch_task = None
        await self.mcp.stop()

    # ------------------------------------------------------------------
    # Dispatch reasoning loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self):
        """Main dispatch tick loop — runs every TICK_INTERVAL seconds."""
        while self._running:
            try:
                await self._dispatch_tick()
            except Exception:
                logger.exception("Dispatch tick failed")
            try:
                await asyncio.sleep(TICK_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _dispatch_tick(self):
        """One dispatch cycle: poll sentinel events, build world state, reason, publish commands."""
        import uuid as _uuid
        t0 = time.monotonic()
        self._tick_count += 1
        self._last_tick_at = time.time()

        # Poll sentinel observations from orchestration DB
        await self._poll_sentinel_observations()

        world = await self._build_world_state()
        if not world.get("projects"):
            return

        async with self.track_tool("build_dispatch_commands"):
            commands, diagnoses = await build_dispatch_commands(
                world=world,
                db=self._orch_db,
                llm_gateway_url=LLM_GATEWAY_URL,
                max_concurrent=MAX_CONCURRENT,
            )

        for diag in diagnoses:
            await self._apply_diagnosis(diag)

        for cmd in commands:
            await self._publish_dispatch_command(cmd)

        total = len(commands) + len(diagnoses)
        self._decisions_count += total

        elapsed_ms = (time.monotonic() - t0) * 1000
        if commands or diagnoses:
            logger.info(
                "Dispatch tick #%d: %d commands, %d diagnoses in %.0fms",
                self._tick_count, len(commands), len(diagnoses), elapsed_ms,
            )

    # ------------------------------------------------------------------
    # Sentinel observation polling (R6: Absorb Sentinel Intelligence)
    # ------------------------------------------------------------------

    async def _poll_sentinel_observations(self):
        """Poll the orchestration DB for sentinel observations since last tick.

        PlanSentinel and SystemSentinel write observations to the
        sentinel_observations table. Odin reads them here and logs receipt.
        """
        try:
            rows = await self._orch_db.fetchall(
                "SELECT id, project_id, task_id, category, severity, "
                "message, details_json, created_at "
                "FROM sentinel_observations "
                "WHERE created_at > $1 "
                "ORDER BY created_at ASC",
                (self._last_obs_poll_at,),
            )
        except Exception:
            # Table may not exist yet or DB may be unreachable
            return

        if not rows:
            return

        self._last_obs_poll_at = time.time()

        for row in rows:
            category = row.get("category", "unknown")
            severity = row.get("severity", "info")
            project_id = row.get("project_id", "")
            task_id = row.get("task_id", "")
            message = row.get("message", "")

            anomaly = {
                "id": row.get("id"),
                "category": category,
                "severity": severity,
                "project_id": project_id,
                "task_id": task_id,
                "message": message,
                "received_at": time.time(),
            }
            self._recent_anomalies.append(anomaly)

            logger.info(
                "Odin received sentinel observation [%s/%s] project=%s task=%s: %s",
                category, severity,
                project_id[:8] if project_id else "-",
                task_id[:8] if task_id else "-",
                message[:120],
            )

        # Cap recent anomalies list
        if len(self._recent_anomalies) > 100:
            self._recent_anomalies = self._recent_anomalies[-100:]

        logger.info(
            "Polled %d new sentinel observation(s) from orchestration DB",
            len(rows),
        )

    async def _build_world_state(self) -> dict:
        """Query orchestration DB for current system state."""
        now = time.time()
        staleness = 300

        rows = await self._orch_db.fetchall(
            "SELECT id, name, status, config_json FROM projects "
            "WHERE status NOT IN ($1, $2) "
            "ORDER BY CASE status "
            "  WHEN 'failed' THEN 0 WHEN 'executing' THEN 1 "
            "  WHEN 'planning' THEN 2 WHEN 'draft' THEN 3 ELSE 4 END, "
            "created_at DESC",
            ("completed", "cancelled"),
        )

        projects = []
        for row in rows:
            pid = row["id"]
            task_rows = await self._orch_db.fetchall(
                "SELECT status, COUNT(*) as cnt FROM tasks "
                "WHERE project_id = $1 GROUP BY status", (pid,),
            )
            counts = {r["status"]: r["cnt"] for r in task_rows}

            wave_row = await self._orch_db.fetchone(
                "SELECT MIN(wave) as w FROM tasks "
                "WHERE project_id = $1 AND status NOT IN ($2, $3, $4)",
                (pid, "completed", "cancelled", "skipped"),
            )
            current_wave = wave_row["w"] if wave_row and wave_row.get("w") is not None else -1

            anomalies = []
            problem_rows = await self._orch_db.fetchall(
                "SELECT id, title, status, error, retry_count, model_tier, "
                "started_at, max_retries "
                "FROM tasks WHERE project_id = $1 "
                "AND (status = $2 OR (status = $3 AND started_at < $4))",
                (pid, "failed", "running", now - staleness),
            )
            for t in problem_rows:
                running_sec = int(now - t["started_at"]) if t.get("started_at") else 0
                anomalies.append({
                    "task_id": t["id"], "title": t["title"],
                    "status": t["status"],
                    "error": (t.get("error") or "")[:200],
                    "retry_count": t.get("retry_count") or 0,
                    "max_retries": t.get("max_retries") or 3,
                    "model_tier": t.get("model_tier", ""),
                    "running_seconds": running_sec,
                })

            projects.append({
                "id": pid, "name": row["name"], "status": row["status"],
                "task_counts": counts, "current_wave": current_wave,
                "anomalies": anomalies if anomalies else None,
            })

        summary_rows = await self._orch_db.fetchall(
            "SELECT status, COUNT(*) as cnt FROM projects GROUP BY status", ()
        )
        project_summary = {r["status"]: r["cnt"] for r in summary_rows}

        recent_decisions_db: list[dict] = []
        try:
            dec_rows = await self._orch_db.fetchall(
                "SELECT decision_id, project_id, task_id, decision_type, "
                "confidence, action_taken, created_at "
                "FROM odin_decisions ORDER BY created_at DESC LIMIT 20", (),
            )
            for dr in dec_rows:
                recent_decisions_db.append({
                    k: dr.get(k) for k in
                    ("decision_id", "project_id", "task_id",
                     "decision_type", "confidence", "action_taken", "created_at")
                })
        except Exception:
            pass

        return {
            "projects": projects,
            "project_summary": project_summary,
            "total_executing_projects": project_summary.get("executing", 0),
            "recent_decisions": recent_decisions_db,
        }

    async def _apply_diagnosis(self, diag: FailureDiagnosis):
        """Apply a failure diagnosis — update the task in orchestration DB."""
        import uuid as _uuid
        now = time.time()
        decision_id = _uuid.uuid4().hex[:12]

        try:
            if diag.fix_type == "retry_as_is":
                await self._orch_db.execute_write(
                    "UPDATE tasks SET status = 'pending', error = NULL, "
                    "retry_count = retry_count + 1, updated_at = $1 WHERE id = $2",
                    (now, diag.task_id),
                )
                action = "Retried task as-is"
            elif diag.fix_type == "reassign_tier" and diag.new_tier:
                await self._orch_db.execute_write(
                    "UPDATE tasks SET model_tier = $1, status = 'pending', "
                    "error = NULL, updated_at = $2 WHERE id = $3",
                    (diag.new_tier, now, diag.task_id),
                )
                action = f"Reassigned to {diag.new_tier}"
            elif diag.fix_type == "modify_prompt" and diag.prompt_guidance:
                task = await self._orch_db.fetchone(
                    "SELECT system_prompt FROM tasks WHERE id = $1", (diag.task_id,),
                )
                current = (task.get("system_prompt") or "") if task else ""
                updated = f"{current}\n\n{diag.prompt_guidance}".strip()
                await self._orch_db.execute_write(
                    "UPDATE tasks SET system_prompt = $1, status = 'pending', "
                    "error = NULL, updated_at = $2 WHERE id = $3",
                    (updated, now, diag.task_id),
                )
                action = f"Modified prompt (+{len(diag.prompt_guidance)} chars)"
            elif diag.fix_type == "skip":
                await self._orch_db.execute_write(
                    "UPDATE tasks SET status = 'cancelled', "
                    "error = $1, updated_at = $2 WHERE id = $3",
                    (f"Skipped by Odin: {diag.root_cause}", now, diag.task_id),
                )
                action = f"Skipped: {diag.root_cause}"
            else:
                return

            await self.log_decision(
                decision_type=f"diagnosis:{diag.fix_type}",
                reasoning=diag.reasoning,
                confidence=diag.confidence,
                action=action,
            )
            try:
                await self._orch_db.execute_write(
                    "INSERT INTO odin_decisions (decision_id, project_id, task_id, "
                    "decision_type, params_json, confidence, reasoning, "
                    "action_taken, details_json, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                    (decision_id, "", diag.task_id, f"diagnosis:{diag.fix_type}",
                     json.dumps(diag.to_dict()), diag.confidence, diag.reasoning,
                     action, json.dumps({"why_chain": diag.why_chain}), now),
                )
            except Exception:
                pass

            logger.info("Applied diagnosis for task %s: %s (confidence=%.2f)",
                        diag.task_id[:8], action, diag.confidence)
        except Exception:
            logger.exception("Failed to apply diagnosis for task %s", diag.task_id[:8])

    async def _publish_dispatch_command(self, cmd: DispatchCommand):
        """Publish a dispatch command to the orchestration API."""
        import uuid as _uuid
        now = time.time()
        decision_id = _uuid.uuid4().hex[:12]

        # Try HTTP to orchestration API (the bus lives there)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{ORCHESTRATION_API_URL}/api/dispatch",
                    json=cmd.to_dict(),
                )
                if resp.status_code in (200, 201, 202):
                    logger.info("Dispatched task %s -> %s via API",
                                cmd.task_id[:8], cmd.provider)
                    await self._persist_dispatch_decision(decision_id, cmd, now, "api")
                    return
        except Exception as exc:
            logger.warning("Dispatch API unreachable for task %s: %s — DB fallback",
                          cmd.task_id[:8], exc)

        # Fallback: set model_tier directly so executor picks it up
        try:
            await self._orch_db.execute_write(
                "UPDATE tasks SET model_tier = $1, status = 'pending', "
                "updated_at = $2 WHERE id = $3 AND status = 'pending'",
                (cmd.provider, now, cmd.task_id),
            )
            logger.info("Dispatched task %s -> %s via DB fallback",
                        cmd.task_id[:8], cmd.provider)
            await self._persist_dispatch_decision(decision_id, cmd, now, "db_fallback")
        except Exception:
            logger.exception("Failed to dispatch task %s", cmd.task_id[:8])

    async def _persist_dispatch_decision(
        self, decision_id: str, cmd: DispatchCommand, now: float, method: str,
    ):
        """Persist dispatch decision to god_events and odin_decisions."""
        await self.log_decision(
            decision_type="dispatch",
            reasoning=cmd.reason,
            confidence=0.9,
            action=f"dispatch:{cmd.provider} via {method}",
        )
        try:
            await self._orch_db.execute_write(
                "INSERT INTO odin_decisions (decision_id, project_id, task_id, "
                "decision_type, params_json, confidence, reasoning, "
                "action_taken, details_json, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                (decision_id, cmd.project_id, cmd.task_id, "dispatch",
                 json.dumps(cmd.to_dict()), 0.9, cmd.reason,
                 f"dispatch:{cmd.provider} via {method}",
                 json.dumps({"method": method}), now),
            )
        except Exception:
            pass
        self._recent_decisions = (
            [{"task_id": cmd.task_id, "action_taken": f"dispatch:{cmd.provider}"}]
            + self._recent_decisions
        )[:20]

    async def _check_llm_gateway(self):
        """Check LLM Gateway availability."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{LLM_GATEWAY_URL}/providers")
            resp.raise_for_status()

    async def dispatch_task_mcp(self, task: dict) -> dict:
        """Dispatch a task using MCP servers for tool execution.

        Selects the appropriate MCP servers for the task, spawns them,
        calls the required tools, and returns results. Servers stay alive
        for reuse; the idle reaper handles cleanup.

        Args:
            task: Task dict with at minimum 'id' and optionally
                  'mcp_servers', 'tags', 'required_tools', 'tool_calls'.

        Returns:
            Dict with 'task_id', 'results' (per-tool-call), and 'servers_used'.
        """
        task_id = task.get("id", "unknown")

        # Determine which servers to spawn
        server_names = self.mcp.select_servers_for_task(task)
        if not server_names:
            return {
                "task_id": task_id,
                "results": [],
                "servers_used": [],
                "error": "No MCP servers matched for this task",
            }

        # Spawn all needed servers
        spawned = []
        for name in server_names:
            try:
                async with self.track_tool(f"mcp_spawn_{name}"):
                    await self.mcp.spawn(name)
                spawned.append(name)
            except Exception as e:
                logger.error("Failed to spawn MCP server %s for task %s: %s",
                             name, task_id, e)

        # Execute tool calls
        tool_calls = task.get("tool_calls", [])
        results = []
        for call in tool_calls:
            server = call.get("server")
            tool = call.get("tool")
            args = call.get("arguments")
            if not server or not tool:
                results.append({"error": "Missing server or tool in call spec"})
                continue
            try:
                async with self.track_tool(f"mcp_call_{server}.{tool}"):
                    result = await self.mcp.call_tool(server, tool, args)
                from gods.odin.mcp_client import extract_text
                results.append({
                    "server": server,
                    "tool": tool,
                    "text": extract_text(result),
                    "is_error": result.isError,
                })
            except Exception as e:
                results.append({
                    "server": server,
                    "tool": tool,
                    "error": str(e),
                    "is_error": True,
                })

        return {
            "task_id": task_id,
            "results": results,
            "servers_used": spawned,
        }

    def health_snapshot(self) -> dict:
        """Build health response payload."""
        return {
            "status": "ok",
            "service": "odin",
            "port": PORT,
            "version": CONFIG.get("version", "0.1.0"),
            "uptime_s": round(self.uptime_seconds, 1),
            "tick_count": self._tick_count,
            "last_tick_at": self._last_tick_at or None,
            "decisions_made": self._decisions_count,
            "tool_stats": self.tool_stats,
            "mcp": self.mcp.get_status(),
            "recent_anomaly_count": len(self._recent_anomalies),
        }


# ---------------------------------------------------------------------------
# Module-level god instance (created during lifespan)
# ---------------------------------------------------------------------------

_odin: OdinGod | None = None
_db: _SQLiteDB | None = None
_orch_db: _SQLiteDB | None = None


# ---------------------------------------------------------------------------
# FastAPI app with lifespan
# ---------------------------------------------------------------------------

def _build_mcp_registry() -> MCPServerRegistry:
    """Build the MCP server registry from config files."""
    registry = MCPServerRegistry()

    # Load from .mcp.json files specified in god.json config
    mcp_sources = _cfg.get("mcp_sources", [])
    odin_dir = Path(__file__).parent
    for source in mcp_sources:
        path = Path(source)
        if not path.is_absolute():
            path = odin_dir / path
        registry.load_from_mcp_json(path)

    # Load from gods directory (gods with mcp_servers in their god.json)
    gods_dir = odin_dir.parent
    registry.load_from_gods_dir(gods_dir)

    # Load from workspace .mcp.json if it exists (Odin project root)
    workspace_mcp = _GODS_ROOT / ".mcp.json"
    if workspace_mcp.exists():
        registry.load_from_mcp_json(workspace_mcp)

    # Load from Hekate root .mcp.json if it exists
    hekate_root = os.environ.get("HEKATE_SOURCE")
    if hekate_root:
        root_mcp = Path(hekate_root) / ".mcp.json"
        if root_mcp.exists():
            registry.load_from_mcp_json(root_mcp)

    return registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _odin, _db, _orch_db

    # Database — use orchestration DB if ORCHESTRATION_DSN is set,
    # otherwise fall back to a local SQLite file.
    dsn = os.environ.get("ORCHESTRATION_DSN")
    if dsn and ("postgresql" in dsn or "postgres" in dsn):
        # For Postgres, import the orchestration Database class if available.
        # For now, fall back to local SQLite.
        logger.warning("Postgres DSN provided but standalone Postgres adapter not yet wired — using local SQLite")

    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = data_dir / "odin.db"

    _db = _SQLiteDB(db_path)
    await _db.init()
    logger.info("Database initialized at %s", db_path)

    # Orchestration DB — connects to the orchestration engine's SQLite database
    # for reading projects/tasks and writing dispatch state.
    _orch_db = None
    orch_db_path = Path(ORCHESTRATION_DB_PATH)
    if orch_db_path.exists():
        _orch_db = _SQLiteDB(orch_db_path)
        await _orch_db.init()
        logger.info("Orchestration DB connected at %s", orch_db_path)
    else:
        logger.warning(
            "Orchestration DB not found at %s — dispatch will use local DB",
            orch_db_path,
        )

    # MCP client manager
    registry = _build_mcp_registry()
    mcp_manager = MCPClientManager(
        registry,
        idle_timeout=MCP_IDLE_TIMEOUT,
        reap_interval=MCP_REAP_INTERVAL,
    )

    _odin = OdinGod(_db, mcp_manager, orch_db=_orch_db)
    await _odin.start()

    yield

    await _odin.stop()
    if _orch_db:
        await _orch_db.close()
    await _db.close()
    logger.info("Odin shut down cleanly")


app = FastAPI(title="Odin", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    if _odin is None:
        return {"status": "starting"}
    return _odin.health_snapshot()


@app.get("/config")
async def get_config():
    """Return current god.json configuration (non-sensitive)."""
    return CONFIG


@app.get("/api/odin/anomalies")
async def odin_anomalies(limit: int = 50):
    """Recent sentinel anomalies received by Odin."""
    if _odin is None:
        return {"anomalies": [], "error": "Odin not ready"}
    return {"anomalies": _odin._recent_anomalies[-limit:]}


@app.get("/api/odin/decisions")
async def odin_decisions(limit: int = 20):
    """Recent Odin decisions with reasoning (R9 audit trail)."""
    if _orch_db is None:
        return {"decisions": [], "error": "Orchestration DB not connected"}
    try:
        rows = await _orch_db.fetchall(
            "SELECT decision_id, project_id, task_id, decision_type, "
            "confidence, reasoning, action_taken, created_at "
            "FROM odin_decisions ORDER BY created_at DESC LIMIT $1",
            (limit,),
        )
        return {"decisions": [dict(r) for r in rows]}
    except Exception as e:
        return {"decisions": [], "error": str(e)}


# ---------------------------------------------------------------------------
# MCP management routes
# ---------------------------------------------------------------------------

@app.get("/mcp/status")
async def mcp_status():
    """Return MCP client manager status — active servers, registered servers."""
    if _odin is None:
        return {"status": "starting"}
    return _odin.mcp.get_status()


@app.get("/mcp/servers")
async def mcp_servers():
    """List all registered MCP servers and their configs."""
    if _odin is None:
        return {"status": "starting"}
    return {
        name: {
            "command": cfg.command,
            "args": cfg.args,
            "tags": cfg.tags,
            "running": name in _odin.mcp.active_servers,
        }
        for name, cfg in _odin.mcp.registry.all().items()
    }


@app.get("/mcp/tools/{server_name}")
async def mcp_tools(server_name: str):
    """List tools from a specific MCP server (spawns it if not running)."""
    if _odin is None:
        return {"error": "Odin not ready"}
    try:
        tools = await _odin.mcp.list_tools(server_name)
        return {
            "server": server_name,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.inputSchema,
                }
                for t in tools
            ],
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to connect to {server_name}: {e}"}


@app.post("/mcp/call")
async def mcp_call(body: dict):
    """Call a tool on an MCP server.

    Body: {"server": "name", "tool": "tool_name", "arguments": {...}}
    """
    if _odin is None:
        return {"error": "Odin not ready"}
    server = body.get("server")
    tool = body.get("tool")
    arguments = body.get("arguments")
    if not server or not tool:
        return {"error": "Missing 'server' or 'tool' in request body"}
    try:
        text = await _odin.mcp.call_tool_text(server, tool, arguments)
        return {"server": server, "tool": tool, "result": text, "is_error": False}
    except Exception as e:
        return {"server": server, "tool": tool, "error": str(e), "is_error": True}


@app.post("/mcp/dispatch")
async def mcp_dispatch(task: dict):
    """Dispatch a task using MCP servers for tool execution.

    Body: task dict with 'id', 'mcp_servers'|'tags'|'required_tools', 'tool_calls'.
    """
    if _odin is None:
        return {"error": "Odin not ready"}
    return await _odin.dispatch_task_mcp(task)


@app.get("/ping")
async def ping():
    return {"message": "pong"}


@app.post("/mcp/teardown/{server_name}")
async def mcp_teardown(server_name: str):
    """Tear down a specific MCP server process."""
    if _odin is None:
        return {"error": "Odin not ready"}
    await _odin.mcp.teardown(server_name)
    return {"status": "ok", "server": server_name, "action": "torn_down"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
