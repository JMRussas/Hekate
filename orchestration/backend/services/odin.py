#  Orchestration Engine - Odin
#
#  LLM-driven system overseer. Replaces the rule-based sentinel system
#  with the noz-ai pattern: present world state to an LLM, let it decide
#  what tools to call, execute them, feed results back.
#
#  Architecture:
#    - Ticks every 30s (configurable)
#    - Builds world state from DB (projects, tasks, resources)
#    - Calls LLM via configured provider (claude CLI or Ollama)
#    - LLM can call 10 tools: 4 observation + 6 intervention
#    - Up to 4 rounds per tick
#    - Persists world model to DB for restart recovery
#
#  Providers:
#    - "claude" — Claude CLI (sonnet by default, subscription billing)
#    - "ollama" — Local Ollama via OpenAI-compatible endpoint
#
#  Depends on: odin_tools.py, odin_prompts.py, db/connection.py,
#              sentinel/bus.py, sentinel/decision_logger.py
#  Used by:    container.py, app.py

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid

import httpx

from backend.config import LLM_GATEWAY_URL, OLLAMA_URL, cfg
from backend.services.llm_router import _resolve_cmd
from backend.services.sentinel.bus import SentinelBus
from backend.services.sentinel.decision_logger import DecisionLogger

logger = logging.getLogger("orchestration.odin")

MAX_ROUNDS = 4
TICK_INTERVAL = int(cfg("odin.tick_interval", 30))
# OLLAMA_URL, LLM_GATEWAY_URL imported from config
ODIN_PROVIDER = cfg("odin.provider", "claude")  # "claude" or "ollama"
ODIN_MODEL = cfg("odin.model", "sonnet")  # claude: sonnet/haiku/opus, ollama: qwen3.5:latest etc
STALENESS_THRESHOLD = int(cfg("odin.staleness_seconds", 300))


class Odin:
    """LLM-driven system overseer.

    Observes all projects and tasks, reasons about anomalies via qwen3.5,
    and intervenes when confident (retry, skip, reassign, modify prompts).
    """

    def __init__(
        self,
        db,
        resource_monitor,
        bus: SentinelBus,
        progress_manager=None,
    ):
        self._db = db
        self._resource_monitor = resource_monitor
        self._bus = bus
        self._progress = progress_manager
        self._decision_logger = DecisionLogger(db)

        self._running = False
        self._task: asyncio.Task | None = None
        self._world_model: dict = {}
        self._last_tick_at: float = 0
        self._decisions_count: int = 0
        self._recent_decisions: list[dict] = []
        self._priority_queue: list[str] = []
        self._unsub_hooks: list = []

    @property
    def running(self) -> bool:
        return self._running

    @property
    def world_model(self) -> dict:
        return self._world_model

    async def start(self):
        """Start the background observation loop."""
        if self._running:
            return
        self._running = True
        await self._load_state()
        self._task = asyncio.create_task(self._run_loop())

        # Subscribe to lifecycle events for reactive scheduling
        self._unsub_hooks = [
            self._bus.on("project_created", self._on_project_event),
            self._bus.on("project_planned", self._on_project_event),
            self._bus.on("project_started", self._on_project_event),
            self._bus.on("odin_anomaly", self._on_anomaly),
        ]

        logger.info("Odin started (provider=%s, model=%s, tick=%ds)", ODIN_PROVIDER, ODIN_MODEL, TICK_INTERVAL)

    async def stop(self):
        """Stop the loop and persist state."""
        self._running = False
        # Unsubscribe from lifecycle events
        for unsub in self._unsub_hooks:
            unsub()
        self._unsub_hooks.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._persist_state()
        logger.info("Odin stopped (decisions=%d)", self._decisions_count)

    async def _run_loop(self):
        """Main loop — round-robin one project per tick, response time IS the interval."""
        self._project_cursor = 0  # round-robin index
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Odin tick failed: %s", e, exc_info=True)
            # Brief pause between ticks — just enough to not spin
            await asyncio.sleep(2)

    async def _on_project_event(self, msg):
        """Handle lifecycle events -- trigger immediate tick for the affected project."""
        payload = msg.payload if hasattr(msg, "payload") else msg
        project_id = payload.get("project_id") if isinstance(payload, dict) else None
        if project_id:
            logger.info("Odin: lifecycle event for project %s, scheduling immediate tick", project_id[:8])
            self._priority_queue.append(project_id)

    async def _on_anomaly(self, msg):
        """Handle sentinel anomaly observations -- schedule focused attention."""
        payload = msg.payload if hasattr(msg, "payload") else msg
        project_id = payload.get("project_id") if isinstance(payload, dict) else None
        if project_id:
            severity = payload.get("severity", "info")
            logger.info("Odin: anomaly for project %s [%s], scheduling attention", project_id[:8], severity)
            self._priority_queue.append(project_id)

    async def _tick(self):
        """One observation-reasoning-intervention cycle for a SINGLE project."""
        t0 = time.monotonic()
        self._last_tick_at = time.time()

        # 1. Get all active project IDs (lightweight query)
        rows = await self._db.fetchall(
            "SELECT id, name, status FROM projects "
            "WHERE status NOT IN ($1, $2) "
            "ORDER BY CASE status "
            "  WHEN 'failed' THEN 0 WHEN 'executing' THEN 1 "
            "  WHEN 'planning' THEN 2 WHEN 'draft' THEN 3 ELSE 4 END, "
            "created_at DESC",
            ("completed", "cancelled"),
        )
        if not rows:
            # Even with no active projects, check priority queue for newly created ones
            if not self._priority_queue:
                return

        # 2. Priority queue -- handle lifecycle events before round-robin
        target = None
        if self._priority_queue:
            target_id = self._priority_queue.pop(0)
            target_row = await self._db.fetchone(
                "SELECT id, name, status FROM projects WHERE id = $1", (target_id,)
            )
            if target_row:
                target = target_row
            # else: project not found, fall through to round-robin

        # 3. Round-robin fallback
        if target is None:
            if not rows:
                return
            idx = self._project_cursor % len(rows)
            self._project_cursor = idx + 1
            target = rows[idx]

        # 4. Build focused world state for just this project
        world = await self._build_world_state(target_project_id=target["id"])
        self._world_model = world

        # 5. Reason via LLM
        focus_name = world["projects"][0]["name"] if world["projects"] else "none"
        logger.info("Odin tick: focusing on '%s' (%s)", focus_name, target["status"])
        decisions = await self._reason(world)

        # 6. Persist
        self._decisions_count += len(decisions)
        await self._persist_state()

        elapsed = (time.monotonic() - t0) * 1000
        if decisions:
            logger.info(
                "Odin tick: %d decisions in %.0fms (%d active projects)",
                len(decisions), elapsed, world["total_executing_projects"],
            )

    async def _build_world_state(self, target_project_id: str | None = None) -> dict:
        """Query DB for current system state, focused on one project."""
        now = time.time()

        projects = []
        if target_project_id:
            rows = await self._db.fetchall(
                "SELECT id, name, status, config_json FROM projects WHERE id = $1",
                (target_project_id,),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT id, name, status, config_json FROM projects "
                "WHERE status NOT IN ($1, $2) LIMIT 1",
                ("completed", "cancelled"),
            )
        for row in rows:
            pid = row["id"]

            # Task counts by status
            task_rows = await self._db.fetchall(
                "SELECT status, COUNT(*) as cnt FROM tasks WHERE project_id = $1 GROUP BY status",
                (pid,),
            )
            counts = {r["status"]: r["cnt"] for r in task_rows}

            # Current wave
            wave_row = await self._db.fetchone(
                "SELECT MIN(wave) as w FROM tasks WHERE project_id = $1 AND status NOT IN ($2, $3, $4)",
                (pid, "completed", "cancelled", "skipped"),
            )
            current_wave = wave_row["w"] if wave_row and wave_row["w"] is not None else -1

            # Anomalies: failed tasks, stale running tasks
            anomalies = []
            problem_rows = await self._db.fetchall(
                "SELECT id, title, status, error, retry_count, model_tier, started_at "
                "FROM tasks WHERE project_id = $1 AND (status = $2 OR (status = $3 AND started_at < $4))",
                (pid, "failed", "running", now - STALENESS_THRESHOLD),
            )
            for t in problem_rows:
                running_sec = int(now - t["started_at"]) if t["started_at"] else 0
                anomalies.append({
                    "task_id": t["id"],
                    "title": t["title"],
                    "status": t["status"],
                    "error": (t["error"] or "")[:200],
                    "retry_count": t["retry_count"] or 0,
                    "model_tier": t["model_tier"],
                    "running_seconds": running_sec,
                })

            projects.append({
                "id": pid,
                "name": row["name"],
                "status": row["status"],
                "task_counts": counts,
                "current_wave": current_wave,
                "anomalies": anomalies if anomalies else None,
            })

        # Resource health
        resources = {}
        if self._resource_monitor:
            for state in self._resource_monitor.get_all():
                name = state.resource_id if hasattr(state, "resource_id") else str(state)
                resources[name] = {
                    "status": state.status.value if hasattr(state.status, "value") else str(state.status),
                    "details": state.details if hasattr(state, "details") else {},
                }

        # Stale tasks across all projects
        stale_rows = await self._db.fetchall(
            "SELECT t.id, t.title, p.name as project_name, t.started_at "
            "FROM tasks t JOIN projects p ON t.project_id = p.id "
            "WHERE t.status = $1 AND t.started_at < $2",
            ("running", now - STALENESS_THRESHOLD),
        )
        stale = [
            {
                "task_id": r["id"],
                "title": r["title"],
                "project_name": r["project_name"],
                "running_seconds": int(now - r["started_at"]),
            }
            for r in stale_rows
        ]

        # Quick summary of all projects for context
        summary_rows = await self._db.fetchall(
            "SELECT status, COUNT(*) as cnt FROM projects GROUP BY status", ()
        )
        project_summary = {r["status"]: r["cnt"] for r in summary_rows}

        # God heartbeats from god_events table
        gods: dict[str, dict] = {}
        try:
            heartbeat_cutoff = now - 300  # 5 minutes
            god_rows = await self._db.fetchall(
                "SELECT god_name, event_type, payload, created_at "
                "FROM god_events WHERE event_type = $1 "
                "AND created_at > $2 "
                "ORDER BY created_at DESC",
                ("heartbeat", heartbeat_cutoff),
            )
            for gr in god_rows:
                name = gr["god_name"]
                if name not in gods:  # Most recent heartbeat per god
                    payload = gr["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload) if payload else {}
                    elif payload is None:
                        payload = {}
                    gods[name] = {
                        "last_heartbeat": str(gr["created_at"]),
                        "uptime_s": payload.get("uptime_s", 0),
                        "dependencies": payload.get("dependencies", {}),
                    }
        except Exception as exc:
            logger.debug("Could not query god_events: %s", exc)

        # Provider availability via LLM Gateway /providers endpoint
        provider_status: dict = {}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{LLM_GATEWAY_URL}/providers")
                if resp.status_code == 200:
                    provider_status = resp.json()
        except Exception:
            pass  # Gateway down — not critical

        # Recent decisions from odin_decisions table (for dedup & context)
        recent_decisions_db: list[dict] = []
        try:
            dec_rows = await self._db.fetchall(
                "SELECT decision_id, project_id, task_id, decision_type, "
                "confidence, action_taken, created_at "
                "FROM odin_decisions ORDER BY created_at DESC LIMIT 20",
                (),
            )
            for dr in dec_rows:
                recent_decisions_db.append({
                    "decision_id": dr["decision_id"],
                    "project_id": dr["project_id"],
                    "task_id": dr.get("task_id"),
                    "decision_type": dr["decision_type"],
                    "confidence": dr["confidence"],
                    "action_taken": dr.get("action_taken"),
                    "created_at": dr["created_at"],
                })
        except Exception as exc:
            logger.debug("Could not query odin_decisions: %s", exc)

        return {
            "projects": projects,
            "resources": resources,
            "stale_tasks": stale,
            "project_summary": project_summary,
            "total_executing_projects": project_summary.get("executing", 0),
            "total_active_tasks": sum(
                sum(p["task_counts"].values()) - p["task_counts"].get("completed", 0) - p["task_counts"].get("cancelled", 0)
                for p in projects
            ),
            "god_health": gods,
            "provider_status": provider_status,
            "recent_decisions": recent_decisions_db,
        }

    async def _reason(self, world: dict) -> list[dict]:
        """Multi-round LLM loop — the noz-ai pattern."""
        from backend.services.odin_prompts import build_odin_spec
        from backend.services.odin_tools import TOOLS, INTERVENTION_TOOLS, execute_tool
        from backend.services.prompt_renderer import render_prompt

        spec = build_odin_spec(world, self._recent_decisions)
        rendered = render_prompt(spec, ODIN_PROVIDER)
        messages = [
            {"role": "system", "content": rendered.system_prompt},
            {"role": "user", "content": "/no_think\n" + rendered.user_message},
        ]

        decisions = []

        for round_num in range(MAX_ROUNDS):
            try:
                text, tool_calls = await self._call_llm(messages, TOOLS)
            except Exception as e:
                logger.warning("Odin LLM call failed (round %d): %s: %s", round_num, type(e).__name__, e)
                break

            if not tool_calls:
                if text and round_num == 0:
                    logger.debug("Odin assessment: %s", text[:200])
                break

            # Build assistant message
            assistant_msg = {"role": "assistant", "content": text or None}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            # Execute tools
            for tc in tool_calls:
                result = await execute_tool(tc["name"], tc["arguments"], self._db, self._bus)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

                # Log interventions
                if tc["name"] in INTERVENTION_TOOLS:
                    decision = {
                        "id": uuid.uuid4().hex[:12],
                        "tool": tc["name"],
                        "arguments": tc["arguments"],
                        "result": result[:500],
                        "timestamp": time.time(),
                        "round": round_num,
                        "reasoning": text or "",
                    }
                    decisions.append(decision)

                    # Persist to odin_decisions (renamed from sentinel_decisions in migration 024)
                    await self._db.execute_write(
                        "INSERT INTO odin_decisions (decision_id, project_id, task_id, "
                        "decision_type, params_json, confidence, reasoning, "
                        "action_taken, details_json, created_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                        (
                            decision["id"],
                            tc["arguments"].get("project_id", ""),
                            tc["arguments"].get("task_id"),
                            tc["name"],
                            json.dumps(tc["arguments"]),
                            0.8,  # Odin acts with high confidence
                            text or "",
                            result[:500],
                            json.dumps({"round": round_num}),
                            time.time(),
                        ),
                    )

                    # Publish to bus
                    await self._bus.publish_dict("odin_decision", {
                        "tool": tc["name"],
                        "arguments": tc["arguments"],
                        "result": result[:200],
                    })

        # Track recent decisions for dedup
        self._recent_decisions = (decisions + self._recent_decisions)[:20]
        return decisions

    async def _call_llm(self, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
        """Route to configured provider. Returns (text, tool_calls)."""
        if ODIN_PROVIDER == "claude":
            return await self._call_claude(messages, tools)
        return await self._call_ollama(messages, tools)

    async def _call_claude(self, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
        """Call Claude via CLI. Sends messages + tool defs as structured prompt."""
        # Build a prompt from messages
        parts = []
        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")
            if role == "system":
                parts.append(f"<system>\n{content}\n</system>")
            elif role == "tool":
                parts.append(f"<tool_result name=\"{msg.get('name', '')}\">\n{content}\n</tool_result>")
            else:
                parts.append(f"<{role}>\n{content}\n</{role}>")

        if tools:
            tool_desc = json.dumps([{
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "parameters": t["function"].get("parameters", {}),
            } for t in tools], indent=2)
            parts.append(f"\n<available_tools>\n{tool_desc}\n</available_tools>")
            parts.append(
                "\nIf you want to call a tool, respond with ONLY a JSON object: "
                '{"tool_calls": [{"name": "tool_name", "arguments": {...}}]}\n'
                "If no tool call needed, respond with plain text."
            )

        prompt = "\n\n".join(parts)

        # Run claude CLI — resolve full path to handle missing PATH in NSSM context
        claude_bin = _resolve_cmd("claude")
        if not claude_bin:
            raise FileNotFoundError("claude CLI not found on PATH or in npm global bin")
        proc = await asyncio.to_thread(
            subprocess.run,
            [claude_bin, "--print", "-", "--output-format", "text", "--model", ODIN_MODEL],
            input=prompt, capture_output=True, text=True, timeout=120,
        )

        if proc.returncode != 0:
            logger.warning("Claude CLI failed (exit %d): %s", proc.returncode, proc.stderr[:200])
            raise RuntimeError(f"Claude CLI exit {proc.returncode}")

        raw = proc.stdout.strip()
        logger.debug("Claude response: %s", raw[:300])

        # Parse tool calls from response
        tool_calls = []
        text = raw
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "tool_calls" in parsed:
                text = ""
                for tc in parsed["tool_calls"]:
                    tool_calls.append({
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": tc["name"],
                        "arguments": tc.get("arguments", {}),
                    })
        except (json.JSONDecodeError, KeyError):
            pass  # Plain text response, no tool calls

        return text, tool_calls

    async def _call_ollama(self, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
        """Call Ollama via OpenAI-compatible endpoint. Non-streaming."""
        payload = {
            "model": ODIN_MODEL,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "max_tokens": 2048,
            "temperature": 0.3,
        }

        # Prefer Gungnir (4090, full VRAM), fall back to Sisyphus (3090)
        ollama_hosts = ["http://192.168.1.164:11434", OLLAMA_URL]
        resp = None
        async with httpx.AsyncClient(timeout=180) as client:
            for host in ollama_hosts:
                try:
                    resp = await client.post(f"{host}/v1/chat/completions", json=payload)
                    resp.raise_for_status()
                    break
                except Exception:
                    continue
            if resp is None:
                raise RuntimeError("All Ollama hosts unreachable")
            data = resp.json()

        choice = data["choices"][0]
        text = choice["message"].get("content", "") or ""
        tool_calls = []
        for tc in choice["message"].get("tool_calls", []):
            args = tc["function"]["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append({
                "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                "name": tc["function"]["name"],
                "arguments": args,
            })

        return text, tool_calls

    async def _persist_state(self):
        """Save world model to DB for restart recovery."""
        try:
            await self._db.execute_write(
                "INSERT INTO odin_state (id, world_model_json, last_tick_at, decisions_count, updated_at) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (id) DO UPDATE SET "
                "world_model_json = $2, last_tick_at = $3, decisions_count = $4, updated_at = $5",
                (
                    "singleton",
                    json.dumps(self._world_model),
                    self._last_tick_at,
                    self._decisions_count,
                    time.time(),
                ),
            )
        except Exception as e:
            logger.warning("Failed to persist Odin state: %s", e)

    async def _load_state(self):
        """Restore world model from DB on startup."""
        try:
            row = await self._db.fetchone(
                "SELECT world_model_json, last_tick_at, decisions_count FROM odin_state WHERE id = $1",
                ("singleton",),
            )
            if row:
                self._world_model = json.loads(row["world_model_json"] or "{}")
                self._last_tick_at = row["last_tick_at"] or 0
                self._decisions_count = row["decisions_count"] or 0
                logger.info(
                    "Odin restored state: %d prior decisions, last tick %.0fs ago",
                    self._decisions_count,
                    time.time() - self._last_tick_at,
                )
        except Exception as e:
            logger.warning("Failed to load Odin state: %s", e)

    def get_status(self) -> dict:
        """Status for REST endpoint."""
        return {
            "running": self._running,
            "model": ODIN_MODEL,
            "tick_interval": TICK_INTERVAL,
            "last_tick_at": self._last_tick_at,
            "decisions_count": self._decisions_count,
            "active_projects": self._world_model.get("total_executing_projects", 0),
            "recent_decisions": self._recent_decisions[:5],
        }
