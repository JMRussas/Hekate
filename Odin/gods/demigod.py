"""Demigod runtime — jailed LLM navigation loop.

A demigod is a disposable, short-lived agent that can only see what we
show it and can only pick from the actions we present. It talks to gods
(Hades, Odin, etc.) exclusively through their MCP interfaces. It never
touches CLIs, filesystems, or raw APIs directly.

Architecture:
  - Role: states + transitions + constrained action sets (the "prison")
  - Runtime: present state → LLM picks → validate → execute → next state
  - Prison: the model only sees the current state context + action list
  - Correction: invalid picks get rejected with "that's not an option"
  - LLM: routed through the LLM Gateway (port 5210) — supports Claude,
    Gemini, Ollama, or any provider the gateway knows about

The LLM is just the transition function in a state machine. We control
the states, the actions, and the data. It just picks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

import httpx

logger = logging.getLogger("gods.demigod")

# Default gateway URL — the single LLM entry point
LLM_GATEWAY_URL = "http://localhost:5210"
HADES_URL = "http://localhost:5201"


# ---------------------------------------------------------------------------
# Role definition — the "prison" configuration
# ---------------------------------------------------------------------------

@dataclass
class Action:
    """A single action available in a state."""
    name: str
    description: str
    # MCP call: {server, tool, args_template}
    # args_template values can use {key} to reference context keys
    mcp_call: dict[str, Any] | None = None
    # HTTP call: {method, url, body_template}
    http_call: dict[str, Any] | None = None
    # Custom async handler (for testing / local logic)
    handler: Callable[..., Awaitable[dict]] | None = field(
        default=None, repr=False
    )
    # Requires a parameter from the model (e.g., "service_name")
    # If set, the model must respond with "action_name:param_value"
    param: str | None = None
    # Transitions to this state after execution
    next_state: str | None = None
    # Ends the demigod run
    terminal: bool = False


@dataclass
class State:
    """A node in the action tree. Defines what the demigod sees and can do."""
    name: str
    prompt: str
    actions: list[Action] = field(default_factory=list)
    # Async function to gather context. Receives the full RunContext so it
    # can read previous action results, params, etc.
    gather: Callable[[RunContext], Awaitable[dict]] | None = field(
        default=None, repr=False
    )
    terminal: bool = False


@dataclass
class Role:
    """Complete role definition — the demigod's entire world."""
    name: str
    description: str
    system_prompt: str
    states: dict[str, State] = field(default_factory=dict)
    initial_state: str = "start"
    max_steps: int = 20
    max_retries: int = 3


# ---------------------------------------------------------------------------
# Run context — threaded through the entire run, carries state
# ---------------------------------------------------------------------------

# Type for optional event persistence callback.
# Called with (event_type: str, payload: dict) after each step.
EventCallback = Callable[[str, dict], Awaitable[None]]


@dataclass
class RunContext:
    """Mutable context that flows through the demigod run.

    Roles and gather functions can read/write this to thread data between
    states (e.g., which service was selected, what the last result was).
    """
    role_name: str
    params: dict[str, str] = field(default_factory=dict)
    last_result: dict[str, Any] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    step: int = 0

    # God URLs — the endpoints this demigod is allowed to reach
    hades_url: str = HADES_URL
    gateway_url: str = LLM_GATEWAY_URL

    # Cancellation — set this event to abort the run
    _cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    # Optional persistence callback — called after each step
    on_event: EventCallback | None = field(default=None, repr=False)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()


# ---------------------------------------------------------------------------
# LLM — calls the gateway, which routes to any provider
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    """Which provider/model to use through the gateway."""
    provider: str = "gemini"        # claude | gemini | ollama
    model: str | None = None        # provider-specific model override
    gateway_url: str = LLM_GATEWAY_URL
    timeout: int = 60


async def call_llm(
    config: LLMConfig,
    system_prompt: str,
    user_message: str,
) -> str:
    """Call the LLM Gateway and return the text response.

    Raises RuntimeError with a clear message if the gateway is
    unreachable or the provider returns an error.
    """
    payload = {
        "provider": config.provider,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "timeout": config.timeout,
    }
    if config.model:
        payload["model"] = config.model

    try:
        async with httpx.AsyncClient(timeout=float(config.timeout + 10)) as client:
            resp = await client.post(f"{config.gateway_url}/v1/chat", json=payload)
    except httpx.ConnectError:
        raise RuntimeError(
            f"LLM Gateway unreachable at {config.gateway_url} — "
            f"is HekateLLMGateway running?"
        )
    except httpx.TimeoutException:
        raise RuntimeError(
            f"LLM Gateway timed out ({config.timeout}s) — "
            f"provider={config.provider}, model={config.model}"
        )

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(
            f"LLM Gateway error (HTTP {resp.status_code}): {detail}"
        )

    return resp.json()["text"]


# ---------------------------------------------------------------------------
# Action execution — the bridge to god HTTP APIs
# ---------------------------------------------------------------------------

# Map of god server names → base URLs (extensible by roles)
GOD_URLS: dict[str, str] = {
    "hades": HADES_URL,
}

# Hades REST route map — tool name → (method, path_template, body_builder)
_HADES_ROUTES: dict[str, tuple[str, str, Callable | None]] = {
    "list_services":    ("GET",  "/services",                  None),
    "service_status":   ("GET",  "/services/{name}",           None),
    "restart_service":  ("POST", "/services/{name}/restart",   None),
    "stop_service":     ("POST", "/services/{name}/stop",      None),
    "start_service":    ("POST", "/services/{name}/start",     None),
    "restart_core":     ("POST", "/restart-core",              None),
    "restart_all":      ("POST", "/restart-all",               None),
    "tail_logs":        ("GET",  "/logs/{service}",            None),
    "system_info":      ("GET",  "/info",                      None),
    "deploy":           ("POST", "/deploy",
                         lambda a: {"skip_frontend": a.get("skip_frontend", False)}),
    "sync_check":       ("GET",  "/services/sync-check",      None),
    "clear_pycache":    ("POST", "/clear-pycache",             None),
}


async def execute_action(
    action: Action,
    ctx: RunContext,
    state_context: dict,
) -> dict:
    """Execute an action and return structured result.

    Merges RunContext.params into the template context so actions can
    reference {service_name} etc. from earlier picks.
    """
    # Build full template context: state context + run params + last result
    tpl = {**state_context, **ctx.params}

    if action.handler:
        return await action.handler(ctx)

    if action.http_call:
        call = action.http_call
        method = call.get("method", "GET").upper()
        url = _template(call["url"], tpl)
        body = None
        if "body_template" in call:
            body = json.loads(_template(json.dumps(call["body_template"]), tpl))
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.request(method, url, json=body)
                if resp.status_code >= 400:
                    return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:300]}
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text[:500], "status_code": resp.status_code}
        except httpx.ConnectError:
            return {"error": f"Cannot connect to {url}"}
        except httpx.TimeoutException:
            return {"error": f"Request to {url} timed out"}

    if action.mcp_call:
        call = action.mcp_call
        server = call.get("server", "hades")
        tool = call["tool"]
        args = {}
        if "args_template" in call:
            args = json.loads(_template(json.dumps(call["args_template"]), tpl))

        if server == "hades":
            return await _call_hades(tool, args, ctx.hades_url)

        # Generic god: POST to /mcp/{tool}
        base = GOD_URLS.get(server, call.get("base_url", ""))
        if not base:
            return {"error": f"No URL for god server: {server}"}
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(f"{base}/mcp/{tool}", json=args)
                if resp.status_code >= 400:
                    return {"error": f"HTTP {resp.status_code} from {server}/{tool}",
                            "detail": resp.text[:300]}
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text[:500], "status_code": resp.status_code}
        except httpx.ConnectError:
            return {"error": f"Cannot reach god '{server}' at {base}"}
        except httpx.TimeoutException:
            return {"error": f"God '{server}' tool '{tool}' timed out"}

    return {"error": "Action has no execution method"}


async def _call_hades(tool: str, args: dict, base_url: str) -> dict:
    """Call a Hades REST endpoint by MCP tool name."""
    route = _HADES_ROUTES.get(tool)
    if not route:
        return {"error": f"Unknown Hades tool: {tool}"}

    method, path_tpl, body_fn = route
    path = path_tpl
    for key, val in args.items():
        path = path.replace(f"{{{key}}}", str(val))

    body = body_fn(args) if body_fn else None
    timeout = 330.0 if tool == "deploy" else 60.0

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                params = {k: v for k, v in args.items()
                          if f"{{{k}}}" not in path_tpl}
                resp = await client.get(
                    f"{base_url}{path}", params=params or None
                )
            else:
                resp = await client.post(f"{base_url}{path}", json=body)

            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    detail = body.get("detail", body.get("error", resp.text[:200]))
                except Exception:
                    detail = resp.text[:200]
                return {
                    "error": f"Hades {tool} returned HTTP {resp.status_code}",
                    "detail": detail,
                }

            try:
                return resp.json()
            except Exception:
                return {"raw": resp.text[:500], "status_code": resp.status_code}

    except httpx.ConnectError:
        return {"error": f"Cannot reach Hades at {base_url} — service may be down"}
    except httpx.TimeoutException:
        return {"error": f"Hades {tool} timed out after {timeout}s"}


def _template(s: str, ctx: dict) -> str:
    """Template substitution: {key} → value from ctx.

    Only replaces keys that exist in ctx. Non-string values are
    converted via str() for path params, json.dumps() for bodies.
    Skips replacement if the key doesn't look like a known param
    (avoids collisions with JSON braces by requiring exact match).
    """
    for key, val in ctx.items():
        placeholder = f"{{{key}}}"
        if placeholder not in s:
            continue
        if isinstance(val, str):
            s = s.replace(placeholder, val)
        elif isinstance(val, (int, float, bool)):
            s = s.replace(placeholder, str(val))
        else:
            s = s.replace(placeholder, json.dumps(val))
    return s


# ---------------------------------------------------------------------------
# Prompt building — what the model sees (its entire universe)
# ---------------------------------------------------------------------------

def build_prompt(
    role: Role,
    state: State,
    context: dict,
    correction: str | None = None,
) -> tuple[str, str]:
    """Build system + user messages for the LLM call.

    Returns (system_prompt, user_message) for the gateway.
    """
    system = f"""{role.system_prompt}

CURRENT STATE: {state.name}
{state.prompt}

RULES:
- Respond with ONLY the action name. Nothing else.
- If the action requires a parameter, respond with: action_name:parameter_value
- Pick exactly one action from the list.
- No explanations. No reasoning. Just the pick."""

    # Format context
    ctx_lines = []
    for key, val in context.items():
        if isinstance(val, (dict, list)):
            val = json.dumps(val, indent=2)
        ctx_lines.append(f"  {key}: {val}")

    # Format actions
    action_lines = []
    for i, action in enumerate(state.actions, 1):
        line = f"  {i}. {action.name}"
        if action.param:
            line += f":<{action.param}>"
        line += f" — {action.description}"
        action_lines.append(line)

    user = f"""CONTEXT:
{chr(10).join(ctx_lines)}

ACTIONS:
{chr(10).join(action_lines)}

Pick an action:"""

    if correction:
        user += (
            f"\n\nYour previous response '{correction}' was invalid. "
            f"Valid actions: {', '.join(a.name for a in state.actions)}. "
            f"Respond with ONLY the action name."
        )

    return system, user


@dataclass
class Pick:
    """Parsed LLM pick: action + optional parameter."""
    action: Action
    param_value: str | None = None


def parse_pick(response: str, actions: list[Action]) -> Pick | None:
    """Parse the LLM response into an action + optional param.

    Accepts formats:
      - "action_name"
      - "action_name:param_value"
      - "1" (number)
      - "action_name — description blah" (prefix match)
      - "action name" (spaces instead of underscores)
    """
    raw = response.strip()
    # Strip thinking tags some models emit
    if "</think>" in raw:
        raw = raw.split("</think>")[-1].strip()
    raw = raw.replace("<think>", "").strip()

    clean = raw.lower()
    # Normalize: models often use spaces instead of underscores
    normalized = clean.replace(" ", "_").replace("-", "_")

    # Try "action_name:param_value" format
    if ":" in normalized:
        name_part, param_part = normalized.split(":", 1)
        name_part = name_part.strip()
        param_part = raw.split(":", 1)[1].strip() if ":" in raw else ""
        for action in actions:
            if name_part == action.name.lower():
                return Pick(action=action, param_value=param_part if param_part else None)

    # Exact name match (with normalization)
    for action in actions:
        aname = action.name.lower()
        if normalized == aname or clean == aname:
            return Pick(action=action)

    # Number match
    try:
        idx = int(clean.split(":")[0].strip()) - 1
        if 0 <= idx < len(actions):
            a = actions[idx]
            param = None
            if ":" in raw:
                param = raw.split(":", 1)[1].strip() or None
            return Pick(action=a, param_value=param)
    except ValueError:
        pass

    # Prefix match (normalized)
    for action in actions:
        if normalized.startswith(action.name.lower()):
            return Pick(action=action)

    # Substring match (last resort) — longest names first to avoid
    # "all" matching "check_all" when "all_healthy" was intended
    by_length = sorted(actions, key=lambda a: len(a.name), reverse=True)
    for action in by_length:
        if action.name.lower() in normalized:
            return Pick(action=action)

    return None


# ---------------------------------------------------------------------------
# Runtime — the main loop
# ---------------------------------------------------------------------------

@dataclass
class DemigodResult:
    """Result of a demigod run."""
    role: str
    success: bool
    steps: int
    duration_ms: float
    final_state: str
    history: list[dict] = field(default_factory=list)
    error: str | None = None


async def run_demigod(
    role: Role,
    llm: LLMConfig,
    *,
    hades_url: str = HADES_URL,
    gateway_url: str = LLM_GATEWAY_URL,
    initial_params: dict[str, str] | None = None,
    on_event: EventCallback | None = None,
    cancel_event: asyncio.Event | None = None,
) -> DemigodResult:
    """Execute a demigod — the jailed navigation loop.

    1. Start at initial state
    2. Gather context (pass RunContext so gatherers see previous results)
    3. Present context + actions to LLM via gateway
    4. Validate pick (retry with correction if invalid)
    5. Execute action through god HTTP APIs
    6. Thread result + params into RunContext
    7. Transition to next state (or terminate)

    Args:
        on_event: Optional async callback fired after each step for persistence.
        cancel_event: Optional asyncio.Event — set it to abort the run.
    """
    t0 = time.monotonic()
    ctx = RunContext(
        role_name=role.name,
        hades_url=hades_url,
        gateway_url=gateway_url,
        on_event=on_event,
    )
    if cancel_event is not None:
        ctx._cancel = cancel_event
    if initial_params:
        ctx.params.update(initial_params)

    llm.gateway_url = gateway_url

    async def _emit(event_type: str, payload: dict):
        if ctx.on_event:
            try:
                await ctx.on_event(event_type, payload)
            except Exception as e:
                logger.warning("Event callback failed: %s", e)

    await _emit("demigod_start", {
        "role": role.name, "provider": llm.provider,
        "model": llm.model, "max_steps": role.max_steps,
    })

    logger.info(
        "Demigod '%s' starting (provider=%s, model=%s, max_steps=%d)",
        role.name, llm.provider, llm.model or "default", role.max_steps,
    )

    current_state_name = role.initial_state

    def _result(success: bool, error: str | None = None) -> DemigodResult:
        return DemigodResult(
            role=role.name, success=success, steps=ctx.step,
            duration_ms=(time.monotonic() - t0) * 1000,
            final_state=current_state_name, history=ctx.history,
            error=error,
        )

    while ctx.step < role.max_steps:
        # Check cancellation
        if ctx.cancelled:
            logger.info("Demigod '%s' cancelled at step %d", role.name, ctx.step)
            ctx.history.append({"state": current_state_name, "cancelled": True})
            r = _result(False, "Cancelled")
            await _emit("demigod_cancelled", {"step": ctx.step})
            return r

        ctx.step += 1
        state = role.states.get(current_state_name)
        if state is None:
            r = _result(False, f"Unknown state: {current_state_name}")
            await _emit("demigod_error", {"error": r.error})
            return r

        if state.terminal:
            logger.info("Demigod '%s' → terminal state: %s", role.name, state.name)
            ctx.history.append({"state": state.name, "terminal": True})
            current_state_name = state.name
            r = _result(True)
            await _emit("demigod_done", {"final_state": state.name, "steps": ctx.step})
            return r

        if not state.actions:
            r = _result(False, f"State '{state.name}' has no actions")
            await _emit("demigod_error", {"error": r.error})
            return r

        # Gather context — gatherers see the full RunContext
        state_context: dict = {}
        if state.gather:
            try:
                state_context = await state.gather(ctx)
            except Exception as e:
                logger.error("Gather failed in '%s': %s", state.name, e)
                state_context = {"error": f"Failed to gather context: {e}"}

        # LLM pick loop
        pick: Pick | None = None
        last_bad: str | None = None

        for attempt in range(role.max_retries + 1):
            if ctx.cancelled:
                break
            correction = last_bad if attempt > 0 else None
            sys_prompt, user_msg = build_prompt(role, state, state_context, correction)

            try:
                response = await call_llm(llm, sys_prompt, user_msg)
            except Exception as e:
                logger.error("LLM call failed (attempt %d): %s", attempt + 1, e)
                last_bad = f"[LLM error: {e}]"
                continue

            pick = parse_pick(response, state.actions)
            if pick:
                break

            logger.warning(
                "Invalid pick '%s' in state '%s' (attempt %d/%d)",
                response.strip(), state.name, attempt + 1, role.max_retries + 1,
            )
            last_bad = response.strip()

        if ctx.cancelled:
            ctx.history.append({"state": state.name, "cancelled": True})
            r = _result(False, "Cancelled")
            await _emit("demigod_cancelled", {"step": ctx.step, "state": state.name})
            return r

        if pick is None:
            ctx.history.append({
                "state": state.name,
                "error": f"No valid pick after {role.max_retries + 1} attempts",
                "last_response": last_bad,
            })
            r = _result(False, f"LLM failed to pick in state '{state.name}'")
            await _emit("demigod_error", {"error": r.error, "state": state.name})
            return r

        # Thread param into context
        if pick.param_value and pick.action.param:
            ctx.params[pick.action.param] = pick.param_value

        logger.info(
            "Demigod '%s' [%s] → %s%s",
            role.name, state.name, pick.action.name,
            f":{pick.param_value}" if pick.param_value else "",
        )

        # Execute action
        result: dict = {}
        if not pick.action.terminal and (
            pick.action.mcp_call or pick.action.http_call or pick.action.handler
        ):
            try:
                result = await execute_action(pick.action, ctx, state_context)
            except Exception as e:
                logger.error("Action '%s' failed: %s", pick.action.name, e)
                result = {"error": str(e)}

        ctx.last_result = result
        step_entry = {
            "state": state.name,
            "action": pick.action.name,
            "param": pick.param_value,
            "result_summary": _summarize(result),
        }
        ctx.history.append(step_entry)

        await _emit("demigod_step", {
            "step": ctx.step, **step_entry,
        })

        # Terminal?
        if pick.action.terminal:
            logger.info("Demigod '%s' done via: %s", role.name, pick.action.name)
            current_state_name = state.name
            r = _result(True)
            await _emit("demigod_done", {
                "final_state": state.name, "steps": ctx.step,
                "terminal_action": pick.action.name,
            })
            return r

        # Transition
        if pick.action.next_state:
            current_state_name = pick.action.next_state

    r = _result(False, f"Exhausted max steps ({role.max_steps})")
    await _emit("demigod_error", {"error": r.error, "steps": ctx.step})
    return r


def _summarize(result: dict, max_len: int = 200) -> str:
    """Short summary of a result for the history log."""
    if not result:
        return "(no result)"
    if "error" in result:
        return f"ERROR: {result['error'][:max_len]}"
    s = json.dumps(result, default=str)
    return s[:max_len] + "..." if len(s) > max_len else s
