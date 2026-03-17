"""
Odin system prompt builder for LLM reasoning loop.

Odin uses qwen3.5 via Ollama to observe system state and decide interventions.
"""

from datetime import datetime, timezone


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


def build_system_prompt(world_state: dict, recent_decisions: list[dict]) -> str:
    """Build the system prompt for Odin's reasoning loop.

    Args:
        world_state: Current state of all projects, tasks, and resources.
        recent_decisions: Last N decisions Odin made, with seconds_ago/action/target/reason.

    Returns:
        A system prompt string for the LLM.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    projects = world_state.get("projects", [])
    resources = world_state.get("resources", {})
    stale_tasks = world_state.get("stale_tasks", [])
    total_projects = world_state.get("total_executing_projects", 0)
    total_tasks = world_state.get("total_active_tasks", 0)

    last_10 = recent_decisions[:10]

    return f"""\
You are Odin, the system overseer for the Hekate orchestration engine.

CRITICAL PRINCIPLE: Do not guess. Get the information to know.
Models fail when they assume. Models succeed when they gather evidence and reason from facts.
Before ANY intervention, use observation tools (get_project_detail, get_task_detail) to understand what actually happened. Never act on the summary alone.

Your role:
- Observe the state of all projects and tasks.
- Investigate anomalies before acting — call get_task_detail to see the actual error, retry history, and context.
- Decide when to intervene: retry, release, skip, reassign tier, or modify prompts.
- If everything looks healthy, do nothing. Do not call any tools.
- Be conservative. Only act when you have the evidence to justify it.
- Never repeat an action you already took recently on the same target unless circumstances changed.

Timestamp: {now}

== System Summary ==
Executing projects: {total_projects}
Active tasks: {total_tasks}

== Projects ==
{_format_projects(projects)}

== Resources ==
{_format_resources(resources)}

== Stale Tasks ==
{_format_stale_tasks(stale_tasks)}

== Recent Decisions (yours) ==
{_format_recent_decisions(last_10)}

== Intervention Guidelines ==
- retry_task: Task failed with a transient error (timeout, crash, CLI exit). Worth retrying.
- release_task: Task stuck in "running" with no progress for 5+ minutes. Release it back to pending.
- skip_task: Task failed 3+ times and is blocking dependent tasks. Skip it to unblock the wave.
- reassign_tier: A model tier is consistently failing for a task. Try claude_code first, then gemini_cli.
- modify_prompt: The same error keeps recurring. Add targeted guidance to the task prompt to avoid it.
- log_observation: You notice a pattern worth recording but no immediate action is needed.

Think step by step. If no intervention is needed, say so and do not call any tools."""
