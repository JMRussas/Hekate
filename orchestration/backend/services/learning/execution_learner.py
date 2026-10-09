#  Orchestration Engine - Execution Learner
#
#  Queries historical execution data to produce actionable insights:
#  - Model reliability scores (per task type)
#  - Failure pattern matching for routing avoidance
#  - Recovery strategy recommendations
#  - Prompt enrichment with historical context
#
#  Data sources: tasks table, task_events, sentinel_observations, usage_log
#  Refreshes on a configurable interval (default 15 min), caches in memory.
#
#  Depends on: db/connection.py, models/enums.py
#  Used by:    model_router.py (routing overrides), cli_common.py (prompt hints),
#              task_lifecycle.py (retry strategy)

from __future__ import annotations

import glob as globmod
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("orchestration.learning")

# Conversation log directories (Claude Code JSONL)
_CONVERSATION_DIRS = [
    Path(directory) for directory in os.environ.get("HEKATE_CLAUDE_LOG_DIRS", "").split(os.pathsep)
    if directory
]

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class StalenessReport:
    """How stale the mined data is relative to live sources."""
    taxonomy_age_hours: float  # Hours since failure_taxonomy.json was generated
    patterns_age_hours: float  # Hours since conversation_patterns.json was generated
    new_task_events: int       # Task events since last taxonomy scan
    new_conversations: int     # Conversation files newer than last pattern scan
    new_completed_tasks: int   # Completed tasks since last taxonomy scan
    needs_rescan: bool         # True if data is stale enough to warrant re-mining

    @property
    def summary(self) -> str:
        parts = []
        if self.taxonomy_age_hours > 0:
            parts.append(f"taxonomy: {self.taxonomy_age_hours:.1f}h old")
        if self.patterns_age_hours > 0:
            parts.append(f"patterns: {self.patterns_age_hours:.1f}h old")
        if self.new_task_events:
            parts.append(f"+{self.new_task_events} task events")
        if self.new_conversations:
            parts.append(f"+{self.new_conversations} conversations")
        if self.new_completed_tasks:
            parts.append(f"+{self.new_completed_tasks} completed tasks")
        if self.needs_rescan:
            parts.append("RESCAN RECOMMENDED")
        return " | ".join(parts) if parts else "up to date"


@dataclass
class ModelScore:
    """Reliability score for a model on a specific task type."""
    model: str
    task_type: str
    total: int = 0
    completed: int = 0
    failed: int = 0
    verification_retries: int = 0
    avg_output_len: int = 0
    empty_output_count: int = 0

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.completed / self.total

    @property
    def reliability(self) -> float:
        """0.0 to 1.0 reliability score factoring in retries and empty output."""
        if self.total == 0:
            return 0.0
        base = self.success_rate
        # Penalize for needing verification retries (even if they succeeded)
        retry_penalty = min(0.2, self.verification_retries / max(self.total, 1) * 0.5)
        # Penalize for empty outputs
        empty_penalty = min(0.3, self.empty_output_count / max(self.total, 1) * 0.6)
        return max(0.0, base - retry_penalty - empty_penalty)


@dataclass
class FailurePattern:
    """A recognized failure pattern with its frequency and model affinity."""
    pattern_id: str
    description: str
    error_signature: str  # regex or substring to match
    affected_models: list[str] = field(default_factory=list)
    frequency: int = 0
    recovery_action: str = ""  # "reassign_model", "retry_with_feedback", "skip", "escalate"


@dataclass
class RecoveryStrategy:
    """Recommended recovery approach based on historical data."""
    strategy: str  # "diagnose_first", "fallback_tool", "retry_modified", "escalate"
    success_rate: float
    sample_count: int
    description: str


@dataclass
class PromptHint:
    """A hint to inject into task prompts based on historical patterns."""
    hint_type: str  # "avoidance", "preference", "technique"
    content: str
    relevance_score: float  # 0.0 to 1.0


# ---------------------------------------------------------------------------
# Static data (loaded from mined JSON)
# ---------------------------------------------------------------------------

_FAILURE_PATTERNS: list[FailurePattern] = [
    FailurePattern(
        pattern_id="codex_cli_crash",
        description="Codex CLI v0.114.0 model not supported — 100% failure rate",
        error_signature="codex CLI failed",
        affected_models=["gpt-4o", "o4-mini"],
        frequency=16,
        recovery_action="reassign_model",
    ),
    FailurePattern(
        pattern_id="empty_output",
        description="CLI executor returned empty stdout after retries",
        error_signature="empty output",
        affected_models=["gemini-2.5-pro"],
        frequency=11,
        recovery_action="retry_with_feedback",
    ),
    FailurePattern(
        pattern_id="conversational_filler",
        description="Model produced text claiming work was done but wrote no files",
        error_signature="conversational filler",
        affected_models=["gemini-2.5-pro"],
        frequency=1,
        recovery_action="retry_with_feedback",
    ),
    FailurePattern(
        pattern_id="agent_self_destruct",
        description="Agent deleted its own output during cleanup",
        error_signature="cleaned up the created directory",
        affected_models=["gemini-2.5-pro"],
        frequency=1,
        recovery_action="reassign_model",
    ),
]

_RECOVERY_STRATEGIES: list[RecoveryStrategy] = [
    RecoveryStrategy(
        strategy="diagnose_first",
        success_rate=0.909,
        sample_count=77,
        description="Read files and search before retrying — highest success rate (91%)",
    ),
    RecoveryStrategy(
        strategy="fallback_tool",
        success_rate=0.865,
        sample_count=52,
        description="Switch to a different tool or approach — 87% success",
    ),
    RecoveryStrategy(
        strategy="explain_and_continue",
        success_rate=0.778,
        sample_count=201,
        description="Explain the error and move on to next step — 78% success",
    ),
    RecoveryStrategy(
        strategy="retry_modified",
        success_rate=0.442,
        sample_count=249,
        description="Retry same tool with modifications — only 44% success, avoid as default",
    ),
]


# ---------------------------------------------------------------------------
# Execution Learner
# ---------------------------------------------------------------------------

class ExecutionLearner:
    """Learns from historical execution data to improve routing and prompts.

    Loads static mined data at init, refreshes live scores from the DB
    on a configurable interval.
    """

    def __init__(self, db=None, refresh_interval_s: int = 900):
        self._db = db
        self._refresh_interval = refresh_interval_s
        self._last_refresh: float = 0
        self._model_scores: dict[tuple[str, str], ModelScore] = {}  # (model, task_type) → score
        self._static_loaded = False

    def _load_static_data(self):
        """Load the mined JSON files if available."""
        if self._static_loaded:
            return
        base = os.path.dirname(__file__)
        taxonomy_path = os.path.join(base, "failure_taxonomy.json")
        patterns_path = os.path.join(base, "conversation_patterns.json")

        if os.path.exists(taxonomy_path):
            try:
                with open(taxonomy_path) as f:
                    self._taxonomy = json.load(f)
                logger.info("Loaded failure taxonomy: %d failure categories",
                            len(self._taxonomy.get("failure_categories", {}).get("categories", {})))
            except Exception as e:
                logger.warning("Failed to load failure taxonomy: %s", e)
                self._taxonomy = {}

        if os.path.exists(patterns_path):
            try:
                with open(patterns_path) as f:
                    self._patterns = json.load(f)
                logger.info("Loaded conversation patterns: %d error recoveries, %d corrections",
                            len(self._patterns.get("error_recovery_patterns", [])),
                            len(self._patterns.get("user_corrections", [])))
            except Exception as e:
                logger.warning("Failed to load conversation patterns: %s", e)
                self._patterns = {}

        self._static_loaded = True

    async def refresh_scores(self):
        """Refresh model reliability scores from live DB data."""
        if self._db is None:
            return
        now = time.time()
        if now - self._last_refresh < self._refresh_interval:
            return

        try:
            rows = await self._db.fetchall("""
                SELECT model_used, task_type, status, verification_status, output_text,
                       LENGTH(output_text) as output_len
                FROM tasks
                WHERE model_used IS NOT NULL AND status IN ('completed', 'failed', 'needs_review')
            """)

            scores: dict[tuple[str, str], ModelScore] = {}
            for row in rows:
                model = row["model_used"]
                task_type = row.get("task_type") or "code"
                key = (model, task_type)
                if key not in scores:
                    scores[key] = ModelScore(model=model, task_type=task_type)
                s = scores[key]
                s.total += 1
                status = row["status"]
                if status == "completed":
                    s.completed += 1
                elif status == "failed":
                    s.failed += 1
                output_len = row.get("output_len") or 0
                if output_len == 0 and status == "completed":
                    s.empty_output_count += 1
                s.avg_output_len = (s.avg_output_len * (s.total - 1) + output_len) // s.total

            # Count verification retries per model/task_type
            retry_rows = await self._db.fetchall("""
                SELECT t.model_used, t.task_type
                FROM task_events te
                JOIN tasks t ON t.id = te.task_id
                WHERE te.event_type = 'task_verification_retry'
            """)
            for row in retry_rows:
                model = row["model_used"]
                task_type = row.get("task_type") or "code"
                key = (model, task_type)
                if key in scores:
                    scores[key].verification_retries += 1

            self._model_scores = scores
            self._last_refresh = now
            logger.info("Refreshed execution learner: %d model/task_type scores", len(scores))

        except Exception as e:
            logger.warning("Failed to refresh execution learner scores: %s", e)

    def get_model_reliability(self, model: str, task_type: str) -> float:
        """Get reliability score for a model on a task type. Returns 0.5 if unknown."""
        self._load_static_data()
        key = (model, task_type)
        if key in self._model_scores:
            return self._model_scores[key].reliability
        # Fall back to aggregate across task types
        model_scores = [s for (m, _), s in self._model_scores.items() if m == model]
        if model_scores:
            return sum(s.reliability for s in model_scores) / len(model_scores)
        return 0.5  # Unknown — neutral

    def get_model_score(self, model: str, task_type: str) -> ModelScore | None:
        """Get the full score object for a model/task_type pair."""
        return self._model_scores.get((model, task_type))

    def should_avoid_model(self, model: str, task_type: str) -> tuple[bool, str]:
        """Check if a model should be avoided for a task type.

        Returns (should_avoid, reason).
        """
        self._load_static_data()
        score = self.get_model_score(model, task_type)
        if score and score.total >= 3 and score.success_rate < 0.5:
            return True, f"Historical success rate {score.success_rate:.0%} ({score.completed}/{score.total})"
        if score and score.empty_output_count > 2:
            return True, f"Produced {score.empty_output_count} empty outputs"
        # Check static failure patterns — only block on high-frequency patterns
        for fp in _FAILURE_PATTERNS:
            if model in fp.affected_models and fp.recovery_action == "reassign_model" and fp.frequency >= 5:
                return True, fp.description
        return False, ""

    def match_failure_pattern(self, error_message: str) -> FailurePattern | None:
        """Match an error message against known failure patterns."""
        self._load_static_data()
        if not error_message:
            return None
        lower_msg = error_message.lower()
        for fp in _FAILURE_PATTERNS:
            if fp.error_signature.lower() in lower_msg:
                return fp
        return None

    def get_recovery_recommendation(self, error_message: str) -> str:
        """Recommend a recovery strategy based on the error type.

        Returns one of: "diagnose_first", "fallback_tool", "retry_modified",
        "reassign_model", "escalate"
        """
        self._load_static_data()
        pattern = self.match_failure_pattern(error_message)
        if pattern:
            return pattern.recovery_action

        # Default: diagnose_first has the best success rate (91%)
        return "diagnose_first"

    def get_prompt_hints(self, task_type: str, model: str) -> list[PromptHint]:
        """Generate prompt hints based on historical data for this task/model combo."""
        self._load_static_data()
        hints: list[PromptHint] = []

        # Hint 1: Model-specific avoidance patterns
        score = self.get_model_score(model, task_type)
        if score and score.empty_output_count > 0:
            hints.append(PromptHint(
                hint_type="avoidance",
                content=(
                    "IMPORTANT: Previous executions with this model sometimes produced empty output. "
                    "You MUST produce substantive output. If you encounter an error, diagnose it "
                    "and explain what happened rather than returning nothing."
                ),
                relevance_score=0.9,
            ))

        if score and score.verification_retries > 0:
            hints.append(PromptHint(
                hint_type="technique",
                content=(
                    "Previous tasks of this type required verification retries due to incomplete output. "
                    "Double-check that your output addresses ALL requirements before finishing. "
                    "Read back the task description and verify each requirement is met."
                ),
                relevance_score=0.8,
            ))

        # Hint 2: Recovery strategy guidance (from conversation mining)
        hints.append(PromptHint(
            hint_type="technique",
            content=(
                "ERROR RECOVERY PROTOCOL (derived from historical success rates):\n"
                "- When you encounter an error, FIRST read the relevant files to understand context (91% success rate).\n"
                "- If the current tool fails, try a different tool or approach (87% success rate).\n"
                "- Do NOT blindly retry the same command with minor modifications (only 44% success rate).\n"
                "- If stuck after 2 attempts, explain the blocker clearly instead of looping."
            ),
            relevance_score=0.7,
        ))

        # Hint 3: Task type guidance from successful patterns
        if task_type in ("code", "integration", "game_code"):
            hints.append(PromptHint(
                hint_type="preference",
                content=(
                    "PROVEN WORKFLOW for code tasks:\n"
                    "1. Read existing files first (understand before modifying)\n"
                    "2. Search for related patterns (grep for similar implementations)\n"
                    "3. Make targeted edits (prefer Edit over Write for existing files)\n"
                    "4. Verify your changes work (run relevant tests if available)"
                ),
                relevance_score=0.6,
            ))

        # Hint 4: Common user corrections to avoid
        if task_type in ("code", "integration"):
            hints.append(PromptHint(
                hint_type="avoidance",
                content=(
                    "COMMON MISTAKES TO AVOID (from historical corrections):\n"
                    "- Do NOT act before reading the relevant code (11 corrections for premature action)\n"
                    "- Do NOT assume system state — verify by reading files/running commands first\n"
                    "- Do NOT add unnecessary changes beyond the task scope"
                ),
                relevance_score=0.65,
            ))

        return hints

    def get_routing_override(self, task_type: str, current_model: str) -> str | None:
        """Suggest a better model if the current one has poor history.

        Returns None if the current model is fine, or a model name to use instead.
        """
        self._load_static_data()
        avoid, reason = self.should_avoid_model(current_model, task_type)
        if not avoid:
            return None

        # Find best alternative for this task type
        candidates = []
        for (model, tt), score in self._model_scores.items():
            if tt == task_type and model != current_model and score.total >= 3:
                candidates.append((model, score.reliability))

        if candidates:
            best = max(candidates, key=lambda x: x[1])
            if best[1] > 0.5:
                logger.info(
                    "Routing override: %s → %s for task_type=%s (reason: %s)",
                    current_model, best[0], task_type, reason,
                )
                return best[0]

        return None

    def _get_mined_file_mtime(self, filename: str) -> float:
        """Get mtime of a mined JSON file, or 0 if missing."""
        path = os.path.join(os.path.dirname(__file__), filename)
        return os.path.getmtime(path) if os.path.exists(path) else 0

    def _count_newer_conversations(self, since_ts: float) -> int:
        """Count JSONL conversation files newer than a timestamp."""
        count = 0
        for d in _CONVERSATION_DIRS:
            if not d.exists():
                continue
            for f in d.glob("*.jsonl"):
                try:
                    if f.stat().st_mtime > since_ts:
                        count += 1
                except OSError:
                    pass
        return count

    async def check_staleness(self) -> StalenessReport:
        """Compare mined data timestamps against live data sources.

        Returns a StalenessReport indicating how much new data exists
        since the last scan and whether a rescan is recommended.
        """
        now = time.time()
        taxonomy_mtime = self._get_mined_file_mtime("failure_taxonomy.json")
        patterns_mtime = self._get_mined_file_mtime("conversation_patterns.json")

        taxonomy_age_h = (now - taxonomy_mtime) / 3600 if taxonomy_mtime else float("inf")
        patterns_age_h = (now - patterns_mtime) / 3600 if patterns_mtime else float("inf")

        # Count new task events and completed tasks since taxonomy scan
        new_events = 0
        new_completed = 0
        if self._db and taxonomy_mtime:
            try:
                row = await self._db.fetchone(
                    "SELECT COUNT(*) as cnt FROM task_events WHERE timestamp > $1",
                    (taxonomy_mtime,),
                )
                new_events = row["cnt"] if row else 0

                row = await self._db.fetchone(
                    "SELECT COUNT(*) as cnt FROM tasks "
                    "WHERE status = 'completed' AND completed_at > $1",
                    (taxonomy_mtime,),
                )
                new_completed = row["cnt"] if row else 0
            except Exception as e:
                logger.debug("Staleness DB query failed: %s", e)

        # Count new conversation files since patterns scan
        new_convos = self._count_newer_conversations(patterns_mtime) if patterns_mtime else 0

        # Rescan heuristic: >24h old OR significant new data
        needs_rescan = (
            taxonomy_age_h > 24
            or patterns_age_h > 48
            or new_completed >= 10
            or new_convos >= 20
            or taxonomy_mtime == 0
            or patterns_mtime == 0
        )

        return StalenessReport(
            taxonomy_age_hours=taxonomy_age_h,
            patterns_age_hours=patterns_age_h,
            new_task_events=new_events,
            new_conversations=new_convos,
            new_completed_tasks=new_completed,
            needs_rescan=needs_rescan,
        )

    async def file_detected_issues(self):
        """Scan current data and file fix items for detected problems.

        Called after refresh_scores() or rescan. Files deduplicated items
        so repeat calls are safe.
        """
        if self._db is None:
            return

        try:
            from backend.services.fix_queue import FixQueue
            fq = FixQueue(self._db)
        except Exception:
            return

        self._load_static_data()

        # 1. Models with poor reliability on specific task types
        for (model, task_type), score in self._model_scores.items():
            if score.total >= 3 and score.success_rate < 0.5:
                await fq.file_from_learner(
                    pattern_id=f"low_reliability:{model}:{task_type}",
                    title=f"{model} has {score.success_rate:.0%} success on {task_type} tasks",
                    description=(
                        f"Model {model} completed {score.completed}/{score.total} "
                        f"{task_type} tasks. {score.failed} failed, "
                        f"{score.empty_output_count} empty outputs, "
                        f"{score.verification_retries} verification retries."
                    ),
                    severity="high" if score.success_rate == 0 else "medium",
                    evidence=[{
                        "type": "model_score",
                        "content": f"total={score.total} completed={score.completed} "
                                   f"failed={score.failed} empty={score.empty_output_count}",
                    }],
                    proposed_fix=f"Route {task_type} tasks away from {model}",
                    affected_component="model_router",
                )

            if score.empty_output_count >= 2:
                await fq.file_from_learner(
                    pattern_id=f"empty_output:{model}:{task_type}",
                    title=f"{model} produces empty output on {task_type} tasks",
                    description=(
                        f"Model {model} returned empty output {score.empty_output_count} "
                        f"times out of {score.total} {task_type} tasks."
                    ),
                    severity="medium",
                    proposed_fix="Add retry-with-explicit-instruction on empty output detection",
                    affected_component="task_lifecycle",
                )

        # 2. Sentinel intervention failure (from static data)
        taxonomy = getattr(self, "_taxonomy", {})
        sentinel = taxonomy.get("sentinel_pattern_analysis", {})
        interventions = sentinel.get("intervention_outcomes", {})
        if interventions.get("total", 0) > 0 and interventions.get("succeeded", 0) == 0:
            await fq.file_from_learner(
                pattern_id="sentinel_intervention_zero_success",
                title=f"Sentinel interventions have 0% success rate ({interventions['total']} attempts)",
                description=(
                    f"All {interventions['total']} auto-interventions (reassign_tier) failed. "
                    "Root cause: missing dominant_tier and failed_task_ids in observation details_json."
                ),
                severity="high",
                evidence=[{
                    "type": "sentinel_data",
                    "content": f"total={interventions['total']} succeeded=0 "
                               f"actions={interventions.get('actions', {})}",
                }],
                proposed_fix=(
                    "Fix intervention_executor.py: populate dominant_tier and "
                    "failed_task_ids in observation before reassign_tier action"
                ),
                affected_component="sentinel",
            )

        # 3. Observation noise
        obs = sentinel.get("category_distribution", {})
        resource_unavail = obs.get("resource_unavailable", {})
        if resource_unavail.get("count", 0) > 100:
            await fq.file_from_learner(
                pattern_id="sentinel_observation_noise",
                title=f"Sentinel generating {resource_unavail['count']} duplicate observations",
                description=(
                    f"{resource_unavail['count']} identical 'resource_unavailable' warnings "
                    "for codex_cli. Sentinel emits per-tick instead of per-state-change."
                ),
                severity="medium",
                proposed_fix="Deduplicate sentinel observations: emit once per state change, not per tick",
                affected_component="sentinel",
            )

        # 4. Mimir handler errors (from god relay analysis)
        god_relay = taxonomy.get("god_relay_analysis", {})
        by_source = god_relay.get("by_source", {})
        mimir_errors = by_source.get("mimir_handle_review_rejection", {})
        handler_errors = mimir_errors.get("events", {}).get("handler_error", 0)
        if handler_errors >= 5:
            await fq.file_from_learner(
                pattern_id="mimir_handler_errors",
                title=f"Mimir has {handler_errors} handler_error events",
                description=(
                    f"The mimir review rejection handler is throwing {handler_errors} errors. "
                    "This suggests bugs in the review pipeline."
                ),
                severity="medium",
                proposed_fix="Debug mimir review rejection handler — check error payloads in god_relay_events",
                affected_component="mimir",
            )

        logger.info("Learner filed detected issues to fix queue")

    def format_historical_context(self, task_type: str, model: str) -> str | None:
        """Format historical learnings as a context block for prompt injection.

        Returns None if there's nothing useful to inject.
        """
        hints = self.get_prompt_hints(task_type, model)
        if not hints:
            return None

        # Only include hints above relevance threshold
        relevant = [h for h in hints if h.relevance_score >= 0.6]
        if not relevant:
            return None

        lines = ["HISTORICAL EXECUTION INTELLIGENCE — Learned from prior task outcomes:"]
        for hint in sorted(relevant, key=lambda h: -h.relevance_score):
            lines.append("")
            lines.append(hint.content)

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: ExecutionLearner | None = None


def get_learner() -> ExecutionLearner:
    """Get or create the singleton ExecutionLearner."""
    global _instance
    if _instance is None:
        _instance = ExecutionLearner()
    return _instance


def init_learner(db) -> ExecutionLearner:
    """Initialize the learner with a database connection."""
    global _instance
    _instance = ExecutionLearner(db=db)
    return _instance
