#  Odin Dispatch Logic — Model Selection, Retry Strategy, Wave Parallelism
#
#  Absorbs the sentinel's 5-Whys reasoner and the executor's tier-selection
#  logic into Odin's reasoning loop.  Publishes dispatch_command events to
#  the sentinel bus so the executor (now a dumb worker) can pick them up.
#
#  Key responsibilities:
#    - select_provider_for_task: picks provider+model based on task type,
#      complexity, provider availability, failure history, and budget.
#    - diagnose_failure: 5-Whys-inspired analysis of a failed task to decide
#      retry_as_is / reassign_tier / modify_prompt / skip.
#    - compute_wave_parallelism: decides how many tasks to dispatch in
#      parallel based on resource availability and budget.
#    - build_dispatch_commands: main entry point — given world state, returns
#      a list of dispatch_command dicts ready to publish to the bus.
#
#  Depends on: gods/base.py (DatabaseLike)
#  Used by:    gods/odin/server.py (tick loop)

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("odin.dispatch")

# ---------------------------------------------------------------------------
# Configuration defaults (overridable via god.json config)
# ---------------------------------------------------------------------------

DEFAULT_MAX_CONCURRENT = 4
DEFAULT_MAX_RETRIES = 3
STALENESS_THRESHOLD = 300  # seconds before a running task is considered stale

# Provider tiers in preference order for fallback
CLOUD_FALLBACK_ORDER = ["claude_code", "gemini_cli", "ollama"]

# Task type + complexity → default provider mapping (mirrors model_router.py)
_DEFAULT_TIER_MAP: dict[tuple[str, str], str] = {
    ("code", "simple"): "claude_code",
    ("code", "medium"): "claude_code",
    ("code", "complex"): "claude_code",
    ("research", "simple"): "gemini_cli",
    ("research", "medium"): "gemini_cli",
    ("research", "complex"): "claude_code",
    ("analysis", "simple"): "gemini_cli",
    ("analysis", "medium"): "claude_code",
    ("analysis", "complex"): "claude_code",
    ("integration", "simple"): "gemini_cli",
    ("integration", "medium"): "claude_code",
    ("integration", "complex"): "claude_code",
    ("documentation", "simple"): "gemini_cli",
    ("documentation", "medium"): "claude_code",
    ("documentation", "complex"): "claude_code",
    ("asset", "simple"): "ollama",
    ("asset", "medium"): "ollama",
    ("asset", "complex"): "ollama",
    ("game_code", "simple"): "claude_code",
    ("game_code", "medium"): "claude_code",
    ("game_code", "complex"): "claude_code",
    ("game_design", "simple"): "gemini_cli",
    ("game_design", "medium"): "claude_code",
    ("game_design", "complex"): "claude_code",
    ("game_content", "simple"): "gemini_cli",
    ("game_content", "medium"): "claude_code",
    ("game_content", "complex"): "claude_code",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DispatchCommand:
    """A dispatch instruction for the executor."""
    task_id: str
    project_id: str
    provider: str          # claude_code, gemini_cli, ollama
    model: str | None      # specific model override, or None for provider default
    priority: int = 0      # lower = higher priority
    timeout: int = 600     # seconds
    reason: str = ""       # why Odin chose this dispatch

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "provider": self.provider,
            "model": self.model,
            "priority": self.priority,
            "timeout": self.timeout,
            "reason": self.reason,
        }


@dataclass
class FailureDiagnosis:
    """Result of diagnosing a task failure (absorbed 5-Whys logic)."""
    task_id: str
    fix_type: str = "retry_as_is"  # retry_as_is | reassign_tier | modify_prompt | skip
    confidence: float = 0.5
    root_cause: str = "unknown"
    reasoning: str = ""
    new_tier: str | None = None          # for reassign_tier
    prompt_guidance: str | None = None   # for modify_prompt
    why_chain: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "fix_type": self.fix_type,
            "confidence": self.confidence,
            "root_cause": self.root_cause,
            "reasoning": self.reasoning,
            "why_chain": self.why_chain,
        }
        if self.new_tier:
            d["new_tier"] = self.new_tier
        if self.prompt_guidance:
            d["prompt_guidance"] = self.prompt_guidance
        return d


# ---------------------------------------------------------------------------
# Model / Provider Selection
# ---------------------------------------------------------------------------

def select_provider(
    task_type: str,
    complexity: str,
    available_providers: dict[str, bool],
    failure_history: list[dict] | None = None,
    budget_remaining: float | None = None,
) -> tuple[str, str]:
    """Pick the best provider for a task.

    Returns (provider, reason).

    Selection logic:
      1. Look up default tier from task type + complexity
      2. Check if that provider is available (from LLM Gateway /providers)
      3. If not, walk the fallback chain
      4. If the task has failed before on a provider, avoid that provider
      5. If budget is tight, prefer cheaper providers

    Args:
        task_type: e.g. "code", "research", "analysis"
        complexity: "simple", "medium", "complex"
        available_providers: provider_name → is_available
        failure_history: list of dicts with "model_tier" and "error" from past attempts
        budget_remaining: remaining budget in USD, or None if unlimited
    """
    # Determine which providers have failed for this task before
    failed_providers: set[str] = set()
    if failure_history:
        for attempt in failure_history:
            tier = attempt.get("model_tier", "")
            if tier:
                failed_providers.add(tier)

    # Budget-sensitive: if budget is very low, prefer free providers
    budget_tight = budget_remaining is not None and budget_remaining < 1.0

    # Look up the recommended tier
    recommended = _DEFAULT_TIER_MAP.get(
        (task_type, complexity), "claude_code"
    )

    # Build candidate list: recommended first, then fallback order
    candidates = [recommended] + [
        p for p in CLOUD_FALLBACK_ORDER if p != recommended
    ]

    # If budget is tight, push ollama to the front
    if budget_tight and "ollama" in candidates:
        candidates.remove("ollama")
        candidates.insert(0, "ollama")

    for candidate in candidates:
        is_available = available_providers.get(candidate, True)
        if not is_available:
            logger.debug(
                "Provider %s unavailable, skipping for %s/%s",
                candidate, task_type, complexity,
            )
            continue

        if candidate in failed_providers:
            # Allow retry on same provider if it's the only option,
            # but prefer alternatives first
            continue

        reason = f"Selected {candidate} for {task_type}/{complexity}"
        if candidate != recommended:
            reason += f" (fallback from {recommended})"
        if budget_tight:
            reason += " (budget-sensitive)"
        return candidate, reason

    # All preferred providers failed or unavailable — retry on original
    # even if it failed before (with modified approach)
    for candidate in candidates:
        is_available = available_providers.get(candidate, True)
        if is_available:
            return candidate, (
                f"Re-selecting {candidate} for {task_type}/{complexity} "
                f"(all alternatives exhausted)"
            )

    # Last resort: ollama is always available locally
    return "ollama", f"Forced ollama fallback — all providers unavailable for {task_type}/{complexity}"


# ---------------------------------------------------------------------------
# Failure Diagnosis (absorbs Sentinel 5-Whys)
# ---------------------------------------------------------------------------

# Error pattern → (fix_type, root_cause_template)
_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    # Provider issues
    ("rate limit", "reassign_tier", "Provider rate-limited"),
    ("quota exceeded", "reassign_tier", "Provider quota exhausted"),
    ("429", "reassign_tier", "HTTP 429 rate limit"),
    ("503", "retry_as_is", "Provider temporarily unavailable (503)"),
    ("timeout", "retry_as_is", "Execution timed out"),
    ("timed out", "retry_as_is", "Execution timed out"),

    # Auth issues
    ("unauthorized", "reassign_tier", "Authentication failure"),
    ("401", "reassign_tier", "HTTP 401 unauthorized"),
    ("credential", "reassign_tier", "Credential issue"),

    # Code generation issues
    ("syntax error", "modify_prompt", "Generated code has syntax errors"),
    ("SyntaxError", "modify_prompt", "Python syntax error in output"),
    ("IndentationError", "modify_prompt", "Indentation error in generated code"),
    ("import error", "modify_prompt", "Missing import in generated code"),
    ("ImportError", "modify_prompt", "ImportError in generated code"),
    ("ModuleNotFoundError", "modify_prompt", "Module not found in generated code"),

    # Git/workspace issues
    ("merge conflict", "modify_prompt", "Git merge conflict"),
    ("worktree", "retry_as_is", "Worktree setup issue"),
    ("branch", "retry_as_is", "Git branch issue"),

    # Resource issues
    ("CUDA out of memory", "reassign_tier", "GPU memory exhausted"),
    ("OOM", "reassign_tier", "Out of memory"),
    ("disk space", "skip", "Disk space exhausted"),

    # CLI tool issues
    ("not found on PATH", "reassign_tier", "CLI tool not installed/accessible"),
    ("FileNotFoundError", "reassign_tier", "Required file or binary missing"),
    ("zombie", "retry_as_is", "Task became a zombie process"),
]


def diagnose_failure(
    task_id: str,
    error: str,
    retry_count: int,
    max_retries: int,
    model_tier: str,
    available_providers: dict[str, bool],
    recent_decisions: list[dict] | None = None,
) -> FailureDiagnosis:
    """Diagnose a task failure and recommend a fix.

    This absorbs the sentinel's 5-Whys reasoning into a deterministic
    pattern-matching layer (fast, no LLM call needed for common failures)
    with confidence scoring.

    The LLM-based deep reasoning (from sentinel/reasoner.py) is reserved
    for Odin's multi-round reasoning loop when pattern matching produces
    low confidence.

    Args:
        task_id: failed task ID
        error: error message from the task
        retry_count: how many times this task has been retried
        max_retries: maximum allowed retries
        model_tier: current model tier assigned to the task
        available_providers: provider availability map
        recent_decisions: recent Odin decisions (for dedup)
    """
    error_lower = (error or "").lower()
    why_chain: list[str] = []

    # Step 1: Check if retries exhausted
    if retry_count >= max_retries:
        why_chain.append(f"Task has been retried {retry_count}/{max_retries} times")
        why_chain.append("Retry budget exhausted — escalating to skip")
        return FailureDiagnosis(
            task_id=task_id,
            fix_type="skip",
            confidence=0.9,
            root_cause=f"Retries exhausted ({retry_count}/{max_retries})",
            reasoning=f"Task failed {retry_count} times, exceeding max retries. Skipping to unblock wave.",
            why_chain=why_chain,
        )

    # Step 2: Pattern match against known error signatures
    for pattern, fix_type, root_cause in _ERROR_PATTERNS:
        if pattern.lower() in error_lower:
            why_chain.append(f"Error contains '{pattern}' → {root_cause}")

            diag = FailureDiagnosis(
                task_id=task_id,
                fix_type=fix_type,
                confidence=0.8,
                root_cause=root_cause,
                reasoning=f"Pattern match: '{pattern}' in error → {fix_type}",
                why_chain=why_chain,
            )

            # Enrich based on fix_type
            if fix_type == "reassign_tier":
                new_tier = _pick_alternative_tier(
                    model_tier, available_providers
                )
                if new_tier:
                    diag.new_tier = new_tier
                    why_chain.append(f"Reassigning from {model_tier} to {new_tier}")
                else:
                    # No alternative available — retry as-is with lower confidence
                    diag.fix_type = "retry_as_is"
                    diag.confidence = 0.4
                    why_chain.append("No alternative tier available — retrying as-is")

            elif fix_type == "modify_prompt":
                diag.prompt_guidance = _generate_prompt_guidance(
                    root_cause, error
                )
                why_chain.append(f"Adding prompt guidance: {diag.prompt_guidance[:80]}...")

            return diag

    # Step 3: Check for repeated same-error (stuck in a loop)
    if recent_decisions:
        same_task_decisions = [
            d for d in recent_decisions
            if d.get("task_id") == task_id
        ]
        if len(same_task_decisions) >= 2:
            why_chain.append(f"Task has {len(same_task_decisions)} recent decisions — appears stuck")
            last_fix = same_task_decisions[0].get("action_taken", "")
            if "retry" in last_fix.lower():
                why_chain.append("Previous fix was retry — escalating to tier reassignment")
                new_tier = _pick_alternative_tier(model_tier, available_providers)
                return FailureDiagnosis(
                    task_id=task_id,
                    fix_type="reassign_tier" if new_tier else "modify_prompt",
                    confidence=0.7,
                    root_cause="Repeated failure after retry — likely tier-specific issue",
                    reasoning="Task stuck in retry loop, escalating fix strategy",
                    new_tier=new_tier,
                    prompt_guidance=_generate_prompt_guidance("repeated failure", error) if not new_tier else None,
                    why_chain=why_chain,
                )

    # Step 4: Unknown error — default to retry with low confidence
    # Odin's LLM reasoning loop can override this with deeper analysis
    why_chain.append(f"No pattern match for error: {error[:100]}")
    why_chain.append("Defaulting to retry_as_is with low confidence")
    return FailureDiagnosis(
        task_id=task_id,
        fix_type="retry_as_is",
        confidence=0.3,
        root_cause=f"Unknown error: {error[:200]}",
        reasoning="No known pattern matched — retrying. Odin LLM loop may override.",
        why_chain=why_chain,
    )


def _pick_alternative_tier(
    current_tier: str,
    available_providers: dict[str, bool],
) -> str | None:
    """Pick a different provider tier that's available."""
    for tier in CLOUD_FALLBACK_ORDER:
        if tier == current_tier:
            continue
        if available_providers.get(tier, True):
            return tier
    return None


def _generate_prompt_guidance(root_cause: str, error: str) -> str:
    """Generate prompt guidance text to append to the task's system prompt."""
    guidance_parts = [
        f"# Previous Attempt Failed",
        f"Root cause: {root_cause}",
        f"Error: {error[:300]}",
        "",
        "Please address this issue in your next attempt:",
    ]

    error_lower = error.lower()
    if "syntax" in error_lower or "indentation" in error_lower:
        guidance_parts.append(
            "- Ensure all generated Python code is syntactically valid. "
            "Use proper indentation (4 spaces). Verify string literals use "
            "\\n instead of raw newlines."
        )
    elif "import" in error_lower or "module" in error_lower:
        guidance_parts.append(
            "- Check that all imports exist and are spelled correctly. "
            "Use only standard library or packages listed in requirements.txt."
        )
    elif "merge conflict" in error_lower:
        guidance_parts.append(
            "- Resolve any git merge conflicts before making changes. "
            "Pull latest changes and rebase if necessary."
        )
    else:
        guidance_parts.append(
            f"- Review the error above and adjust your approach to avoid it."
        )

    return "\n".join(guidance_parts)


# ---------------------------------------------------------------------------
# Wave Parallelism
# ---------------------------------------------------------------------------

def compute_wave_parallelism(
    ready_task_count: int,
    running_task_count: int,
    available_providers: dict[str, bool],
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    budget_remaining: float | None = None,
) -> int:
    """Decide how many tasks to dispatch in this tick.

    Considers:
      - How many tasks are ready (pending in current wave)
      - How many are already running
      - Provider availability (fewer providers → less parallelism)
      - Budget constraints
      - Configured max concurrency

    Returns the number of new tasks to dispatch (0 if none should be started).
    """
    # How many slots are open?
    open_slots = max(0, max_concurrent - running_task_count)
    if open_slots == 0:
        logger.debug("Wave parallelism: 0 open slots (running=%d, max=%d)",
                      running_task_count, max_concurrent)
        return 0

    # Count available providers
    available_count = sum(1 for v in available_providers.values() if v)
    if available_count == 0:
        logger.warning("Wave parallelism: no providers available")
        return 0

    # Scale parallelism by provider availability (fewer providers → less parallel)
    # With 3 providers: full parallelism, with 1: cap at ceil(max/2)
    provider_scale = min(1.0, available_count / 2.0)
    scaled_slots = max(1, int(open_slots * provider_scale))

    # Budget constraint: if budget is very low, reduce parallelism
    if budget_remaining is not None and budget_remaining < 5.0:
        # Very tight budget — dispatch one at a time
        scaled_slots = min(scaled_slots, 1)
        logger.info("Wave parallelism: budget-constrained to 1 (remaining=$%.2f)",
                     budget_remaining)

    # Don't dispatch more than are ready
    to_dispatch = min(scaled_slots, ready_task_count)

    logger.info(
        "Wave parallelism: dispatching %d (ready=%d, running=%d, slots=%d, providers=%d)",
        to_dispatch, ready_task_count, running_task_count, scaled_slots, available_count,
    )
    return to_dispatch


# ---------------------------------------------------------------------------
# Main entry point: build dispatch commands from world state
# ---------------------------------------------------------------------------

async def build_dispatch_commands(
    world: dict,
    db: Any,
    llm_gateway_url: str = "http://localhost:5210",
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> tuple[list[DispatchCommand], list[FailureDiagnosis]]:
    """Analyze world state and produce dispatch commands + failure diagnoses.

    This is called from Odin's tick loop. It:
      1. Queries provider availability from LLM Gateway
      2. For each executing project, finds tasks in the current wave
      3. Diagnoses any failed tasks (5-Whys pattern matching)
      4. Selects providers for ready tasks
      5. Respects wave parallelism limits
      6. Returns (commands_to_dispatch, failure_diagnoses)

    The caller (Odin's tick) publishes the commands to the bus and
    persists the diagnoses as decisions.
    """
    commands: list[DispatchCommand] = []
    diagnoses: list[FailureDiagnosis] = []

    # 1. Get provider availability
    available_providers = await _fetch_provider_availability(llm_gateway_url)
    logger.info("Provider availability: %s", available_providers)

    # 2. Extract budget info from world state
    # Budget info would come from the world state's project config
    budget_remaining: float | None = None  # TODO: wire from world state when budget is tracked

    # 3. Count currently running tasks across all projects
    total_running = 0
    for proj in world.get("projects", []):
        total_running += proj.get("task_counts", {}).get("running", 0)

    # 4. Recent decisions for dedup
    recent_decisions = world.get("recent_decisions", [])

    # 5. Process each project
    for proj in world.get("projects", []):
        if proj.get("status") != "executing":
            continue

        pid = proj["id"]
        task_counts = proj.get("task_counts", {})
        current_wave = proj.get("current_wave", -1)

        if current_wave < 0:
            continue  # No active wave

        # 5a. Diagnose anomalies (failed/stale tasks)
        for anomaly in proj.get("anomalies") or []:
            if anomaly["status"] == "failed":
                diag = diagnose_failure(
                    task_id=anomaly["task_id"],
                    error=anomaly.get("error", ""),
                    retry_count=anomaly.get("retry_count", 0),
                    max_retries=DEFAULT_MAX_RETRIES,
                    model_tier=anomaly.get("model_tier", "claude_code"),
                    available_providers=available_providers,
                    recent_decisions=recent_decisions,
                )
                diagnoses.append(diag)
                logger.info(
                    "Diagnosis for task %s: %s (confidence=%.2f, cause=%s)",
                    anomaly["task_id"][:8], diag.fix_type,
                    diag.confidence, diag.root_cause,
                )

        # 5b. Find pending tasks in current wave that need dispatch
        pending_tasks = await _get_ready_tasks(db, pid, current_wave)
        running_count = task_counts.get("running", 0) + task_counts.get("queued", 0)

        # 5c. Compute how many to dispatch
        to_dispatch = compute_wave_parallelism(
            ready_task_count=len(pending_tasks),
            running_task_count=running_count,
            available_providers=available_providers,
            max_concurrent=max_concurrent,
            budget_remaining=budget_remaining,
        )

        # 5d. Build dispatch commands for the top N ready tasks
        for task_row in pending_tasks[:to_dispatch]:
            task_type = task_row.get("task_type", "code")
            complexity = task_row.get("complexity", "medium")

            # Get failure history for this specific task
            failure_history = []
            for anomaly in proj.get("anomalies") or []:
                if anomaly["task_id"] == task_row["id"]:
                    failure_history.append(anomaly)

            provider, reason = select_provider(
                task_type=task_type,
                complexity=complexity,
                available_providers=available_providers,
                failure_history=failure_history,
                budget_remaining=budget_remaining,
            )

            cmd = DispatchCommand(
                task_id=task_row["id"],
                project_id=pid,
                provider=provider,
                model=None,  # Use provider default
                priority=task_row.get("priority", 0),
                timeout=600,
                reason=reason,
            )
            commands.append(cmd)
            logger.info(
                "Dispatch command: task %s → %s (%s)",
                task_row["id"][:8], provider, reason,
            )

    return commands, diagnoses


async def _fetch_provider_availability(
    gateway_url: str,
) -> dict[str, bool]:
    """Query the LLM Gateway /providers endpoint for availability."""
    defaults = {"claude_code": True, "gemini_cli": True, "ollama": True}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{gateway_url}/providers")
            if resp.status_code == 200:
                data = resp.json()
                result: dict[str, bool] = {}
                for name, info in data.items():
                    if isinstance(info, dict):
                        result[name] = info.get("available", False)
                    else:
                        result[name] = bool(info)
                return result
    except Exception as exc:
        logger.debug("LLM Gateway unreachable: %s — assuming all providers available", exc)
    return defaults


async def _get_ready_tasks(
    db: Any,
    project_id: str,
    current_wave: int,
) -> list[dict]:
    """Fetch pending tasks in the current wave with all deps resolved."""
    try:
        rows = await db.fetchall(
            "SELECT t.id, t.title, t.status, t.wave, t.model_tier, "
            "t.priority, t.task_type, t.complexity, t.retry_count, t.max_retries "
            "FROM tasks t "
            "LEFT JOIN task_deps d ON d.task_id = t.id "
            "LEFT JOIN tasks dep ON dep.id = d.depends_on "
            "  AND dep.status NOT IN ('completed', 'cancelled', 'skipped') "
            "WHERE t.project_id = $1 AND t.status = 'pending' AND t.wave = $2 "
            "GROUP BY t.id HAVING COUNT(dep.id) = 0 "
            "ORDER BY t.priority ASC",
            (project_id, current_wave),
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("Failed to query ready tasks for project %s: %s", project_id[:8], exc)
        return []
