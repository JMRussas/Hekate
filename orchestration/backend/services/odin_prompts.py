"""
Odin system prompt builder for LLM reasoning loop.

Odin uses qwen3.5 via Ollama to observe system state and decide interventions.
Now uses PromptSpec for model-specific rendering.
"""

from datetime import datetime, timezone

from backend.services.prompt_renderer import ContextEntry, ContextType, PromptSpec, render_prompt


def _format_project_summary(summary: dict) -> str:
    """Format the system-wide project status counts."""
    if not summary:
        return "No projects"
    parts = []
    for status in ["executing", "planning", "draft", "failed", "completed", "cancelled"]:
        count = summary.get(status, 0)
        if count > 0:
            parts.append(f"{status}: {count}")
    return "Projects across system — " + ", ".join(parts) if parts else "No projects"


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}h {minutes}m"


def _format_time_ago(seconds: float) -> str:
    """Format seconds-ago into a relative label."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _format_projects(projects: list[dict]) -> str:
    if not projects:
        return "No active projects."

    lines = []
    for p in projects:
        counts = p.get("task_counts", {})
        parts = []
        for status in ("pending", "running", "completed", "failed", "skipped"):
            n = counts.get(status, 0)
            if n > 0:
                parts.append(f"{n} {status}")
        count_str = ", ".join(parts) if parts else "no tasks"
        wave = p.get("current_wave")
        wave_str = f" (wave {wave})" if wave is not None else ""

        lines.append(f"  {p['name']} [{p['status']}]{wave_str}: {count_str}")

        anomalies = p.get("anomalies") or []
        for a in anomalies:
            tier = a.get("model_tier", "unknown")
            error = a.get("error", "")
            error_preview = (error[:80] + "...") if len(error) > 80 else error
            running = a.get("running_seconds")
            running_str = f", running {_format_duration(running)}" if running else ""
            retries = a.get("retry_count", 0)
            lines.append(
                f"    ! {a['title']} [{a['status']}] tier={tier} retries={retries}{running_str}"
            )
            if error_preview:
                lines.append(f"      error: {error_preview}")

    return "\n".join(lines)


def _format_resources(resources: dict) -> str:
    if not resources:
        return "No resource data."

    lines = []
    for name, info in resources.items():
        status = info.get("status", "unknown")
        avg_ms = info.get("avg_ms")
        latency = f" ({avg_ms}ms avg)" if avg_ms is not None else ""
        marker = "OK" if status == "healthy" else status.upper()
        lines.append(f"  {name}: {marker}{latency}")
    return "\n".join(lines)


def _format_stale_tasks(stale: list[dict]) -> str:
    if not stale:
        return "None."

    lines = []
    for t in stale:
        dur = _format_duration(t.get("running_seconds", 0))
        lines.append(f"  {t['title']} (project: {t['project_name']}) — running {dur}")
    return "\n".join(lines)


def _format_recent_decisions(decisions: list[dict]) -> str:
    if not decisions:
        return "No recent decisions."

    lines = []
    for d in decisions:
        ago = _format_time_ago(d.get("seconds_ago", 0))
        action = d.get("action", "?")
        target = d.get("target", "")
        reason = d.get("reason", "")
        reason_str = f" ({reason})" if reason else ""
        lines.append(f"  {ago}: {action} on {target}{reason_str}")
    return "\n".join(lines)


_ODIN_IDENTITY = """\
You are Odin, the system overseer for the Hekate orchestration engine."""

_ODIN_CONSTRAINTS = [
    "CRITICAL: Do not guess. Get the information to know. Models fail when they assume, succeed when they gather evidence.",
    "Before ANY intervention, use observation tools (get_project_detail, get_task_detail) to understand what actually happened.",
    "Observe the state of all projects and tasks.",
    "Investigate anomalies before acting — see the actual error, retry history, and context.",
    "Fix problems: retry failed tasks, release stuck claims, skip blockers, reassign tiers, modify prompts.",
    "Drive progress: create new projects, plan them, start execution. Keep the system moving forward.",
    "If everything looks healthy and there's nothing to create or fix, do nothing.",
    "Be conservative with interventions. Only act when you have evidence to justify it.",
    "Be proactive with new work. Plan draft projects. Start planned projects.",
    "Iterate: review completed projects. Create follow-up projects to improve on what was built.",
    "Never repeat an action you already took recently on the same target unless circumstances changed.",
    "Think step by step. If no intervention is needed, say so and do not call any tools.",
]

_INTERVENTION_GUIDELINES = """\
- retry_task: Task failed with a transient error (timeout, crash, CLI exit). Worth retrying.
- release_task: Task stuck in "running" with no progress for 5+ minutes. Release it back to pending.
- skip_task: Task failed 3+ times and is blocking dependent tasks. Skip it to unblock the wave.
- reassign_tier: A model tier is consistently failing for a task. Try claude_code first, then gemini_cli.
- modify_prompt: The same error keeps recurring. Add targeted guidance to the task prompt to avoid it.
- log_observation: You notice a pattern worth recording but no immediate action is needed.
- create_project: You identify work that needs doing. Create a project with clear, specific requirements.
- plan_project: A draft project needs a task plan. This calls Claude to generate the plan.
- start_project: A planned project is ready for execution. Start it to begin wave dispatch.
- review_completed_project: A project finished. Review what worked, what failed, what was learned.
- get_project_knowledge: Read the accumulated learnings from a project's execution.
- get_recent_completions: Find recently finished projects that may need follow-up work.
- wave_readiness: Check which tasks in a project are ready for dispatch.
- dispatch_task: Queue a specific task for execution.

Dispatch rules:
- Use wave_readiness to see dispatchable tasks, dispatch_task to start them.
- Consider model tier suitability — use reassign_tier if a task keeps failing on one tier.
- Respect wave ordering — don't dispatch wave N+1 tasks until wave N is complete.
- Check resource health and provider quotas before dispatching.
- For retry strategies: analyze the error before retrying blindly.
- Prefer parallel dispatch when multiple tasks are ready and resources are healthy."""


def build_odin_spec(world_state: dict, recent_decisions: list[dict]) -> PromptSpec:
    """Build a PromptSpec for Odin's reasoning loop.

    Returns a PromptSpec that can be rendered per-provider.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    projects = world_state.get("projects", [])
    resources = world_state.get("resources", {})
    stale_tasks = world_state.get("stale_tasks", [])
    total_tasks = world_state.get("total_active_tasks", 0)
    last_10 = recent_decisions[:10]

    # Build context entries from world state sections
    context_entries = [
        ContextEntry(
            type=ContextType.GENERIC,
            tag="system_summary",
            content=(
                f"Timestamp: {now}\n"
                f"{_format_project_summary(world_state.get('project_summary', {}))}\n"
                f"Active tasks in focus: {total_tasks}"
            ),
            priority_override=0,  # Highest priority — always include
        ),
        ContextEntry(
            type=ContextType.GENERIC,
            tag="focus_project",
            content=_format_projects(projects),
            priority_override=1,
        ),
        ContextEntry(
            type=ContextType.GENERIC,
            tag="resources",
            content=_format_resources(resources),
            priority_override=2,
        ),
        ContextEntry(
            type=ContextType.GENERIC,
            tag="stale_tasks",
            content=_format_stale_tasks(stale_tasks),
            priority_override=3,
        ),
        ContextEntry(
            type=ContextType.GENERIC,
            tag="recent_decisions",
            content=_format_recent_decisions(last_10),
            priority_override=4,
        ),
        ContextEntry(
            type=ContextType.GENERIC,
            tag="intervention_guidelines",
            content=_INTERVENTION_GUIDELINES,
            priority_override=5,
        ),
    ]

    return PromptSpec(
        role="overseer",
        identity=_ODIN_IDENTITY,
        task_description="What needs attention? If everything looks healthy, say so and don't call any tools.",
        context=context_entries,
        constraints=_ODIN_CONSTRAINTS,
    )


def build_system_prompt(world_state: dict, recent_decisions: list[dict], provider: str = "ollama") -> str:
    """Build the system prompt for Odin's reasoning loop.

    Backward-compatible wrapper that renders with the specified provider.

    Args:
        world_state: Current state of all projects, tasks, and resources.
        recent_decisions: Last N decisions Odin made, with seconds_ago/action/target/reason.
        provider: Provider name for rendering ("ollama" or "claude").

    Returns:
        A system prompt string for the LLM.
    """
    spec = build_odin_spec(world_state, recent_decisions)
    rendered = render_prompt(spec, provider)
    return rendered.system_prompt
