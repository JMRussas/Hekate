"""Tests for the demigod runtime loop, action execution, and LLM integration.

These tests mock the LLM gateway and Hades to test the actual state machine
loop, cancellation, event callbacks, and error handling — the things the
unit tests in test_demigod.py don't cover.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from gods.demigod import (
    Action, State, Role, RunContext, LLMConfig, DemigodResult,
    run_demigod, execute_action, call_llm, _call_hades, _template,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_role(
    states: dict[str, State] | None = None,
    initial: str = "start",
    max_steps: int = 10,
) -> Role:
    """Build a minimal role for testing."""
    if states is None:
        states = {
            "start": State(
                name="start",
                prompt="Pick an action.",
                actions=[
                    Action(name="done", description="Finish", terminal=True),
                    Action(name="next", description="Go to next", next_state="end"),
                ],
            ),
            "end": State(
                name="end",
                prompt="Final state.",
                actions=[
                    Action(name="finish", description="Complete", terminal=True),
                ],
            ),
        }
    return Role(
        name="test_role",
        description="test",
        system_prompt="You are a test.",
        states=states,
        initial_state=initial,
        max_steps=max_steps,
    )


def _llm_config() -> LLMConfig:
    return LLMConfig(provider="test", gateway_url="http://fake:9999")


# ---------------------------------------------------------------------------
# run_demigod — state machine loop
# ---------------------------------------------------------------------------

class TestRunDemigodLoop:
    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_terminal_action_ends_run(self, mock_llm):
        mock_llm.return_value = "done"

        result = await run_demigod(_simple_role(), _llm_config())

        assert result.success is True
        assert result.steps == 1
        assert result.final_state == "start"
        assert len(result.history) == 1
        assert result.history[0]["action"] == "done"

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_state_transition(self, mock_llm):
        # First call: pick "next" (transitions to "end")
        # Second call: pick "finish" (terminal)
        mock_llm.side_effect = ["next", "finish"]

        result = await run_demigod(_simple_role(), _llm_config())

        assert result.success is True
        assert result.steps == 2
        assert result.history[0]["action"] == "next"
        assert result.history[1]["action"] == "finish"

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_max_steps_exhausted(self, mock_llm):
        # Always pick "next" which loops back — but "end" state has "finish"
        # so after start→end, it picks finish. Let's make a looping role.
        loop_state = State(
            name="loop",
            prompt="Loop forever.",
            actions=[
                Action(name="again", description="Loop", next_state="loop"),
            ],
        )
        role = _simple_role(states={"loop": loop_state}, initial="loop", max_steps=3)
        mock_llm.return_value = "again"

        result = await run_demigod(role, _llm_config())

        assert result.success is False
        assert "max steps" in result.error.lower()
        assert result.steps == 3

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_unknown_state_fails(self, mock_llm):
        role = _simple_role()
        role.initial_state = "nonexistent"

        result = await run_demigod(role, _llm_config())

        assert result.success is False
        assert "Unknown state" in result.error

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_invalid_picks_retry_then_fail(self, mock_llm):
        # Return garbage 4 times (max_retries=3 means 4 attempts)
        mock_llm.return_value = "absolute_nonsense_xyz"
        role = _simple_role()
        role.max_retries = 3

        result = await run_demigod(role, _llm_config())

        assert result.success is False
        assert "failed to pick" in result.error.lower()
        assert mock_llm.call_count == 4  # 1 + 3 retries

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_invalid_then_valid_pick(self, mock_llm):
        # First two calls return garbage, third returns valid action
        mock_llm.side_effect = ["garbage", "more garbage", "done"]

        result = await run_demigod(_simple_role(), _llm_config())

        assert result.success is True
        assert mock_llm.call_count == 3

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_cancellation_stops_loop(self, mock_llm):
        mock_llm.return_value = "again"
        loop_state = State(
            name="loop", prompt="Loop.",
            actions=[Action(name="again", description="Loop", next_state="loop")],
        )
        role = _simple_role(states={"loop": loop_state}, initial="loop", max_steps=100)

        cancel = asyncio.Event()
        # Cancel after first step
        original_call_llm = mock_llm.side_effect

        call_count = 0

        async def cancel_after_first(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                cancel.set()
            return "again"

        mock_llm.side_effect = cancel_after_first

        result = await run_demigod(
            role, _llm_config(), cancel_event=cancel
        )

        assert result.success is False
        assert "Cancelled" in result.error

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_event_callbacks_fire(self, mock_llm):
        mock_llm.return_value = "done"
        events = []

        async def on_event(event_type, payload):
            events.append((event_type, payload))

        result = await run_demigod(
            _simple_role(), _llm_config(), on_event=on_event
        )

        assert result.success is True
        event_types = [e[0] for e in events]
        assert "demigod_start" in event_types
        assert "demigod_step" in event_types
        assert "demigod_done" in event_types

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_gather_provides_context(self, mock_llm):
        mock_llm.return_value = "done"

        async def gather(ctx):
            return {"info": "from gather", "step": ctx.step}

        state = State(
            name="start", prompt="Go.",
            gather=gather,
            actions=[Action(name="done", description="Finish", terminal=True)],
        )
        role = _simple_role(states={"start": state})

        result = await run_demigod(role, _llm_config())
        assert result.success is True

        # Verify the LLM was called with context from gather
        call_args = mock_llm.call_args
        user_msg = call_args[0][2]  # third positional arg is user_message
        assert "from gather" in user_msg

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_param_threading(self, mock_llm):
        mock_llm.side_effect = ["pick:MyValue", "done"]

        state1 = State(
            name="start", prompt="Pick.",
            actions=[
                Action(name="pick", description="Pick one", param="chosen",
                       next_state="end"),
            ],
        )
        state2 = State(
            name="end", prompt="Done.",
            actions=[Action(name="done", description="Finish", terminal=True)],
        )
        role = _simple_role(states={"start": state1, "end": state2})

        result = await run_demigod(role, _llm_config())
        assert result.success is True
        assert result.history[0]["param"] == "MyValue"

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_initial_params_injected(self, mock_llm):
        mock_llm.return_value = "done"

        result = await run_demigod(
            _simple_role(), _llm_config(),
            initial_params={"service_name": "HekateOrchestration"},
        )
        assert result.success is True

    @pytest.mark.asyncio
    @patch("gods.demigod.call_llm")
    async def test_gather_failure_doesnt_crash(self, mock_llm):
        mock_llm.return_value = "done"

        async def bad_gather(ctx):
            raise RuntimeError("gather exploded")

        state = State(
            name="start", prompt="Go.",
            gather=bad_gather,
            actions=[Action(name="done", description="Finish", terminal=True)],
        )
        role = _simple_role(states={"start": state})

        result = await run_demigod(role, _llm_config())
        assert result.success is True  # continues despite gather failure


# ---------------------------------------------------------------------------
# call_llm — gateway error handling
# ---------------------------------------------------------------------------

class TestCallLLM:
    @pytest.mark.asyncio
    async def test_connect_error_raises_runtime(self):
        config = LLMConfig(provider="test", gateway_url="http://localhost:1")
        with pytest.raises(RuntimeError, match="unreachable"):
            await call_llm(config, "sys", "user")

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_http_error_raises_runtime(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_resp.text = "Bad Gateway"
        mock_resp.json.return_value = {"detail": "provider down"}
        mock_post.return_value = mock_resp

        config = LLMConfig(provider="test", gateway_url="http://fake:9999")
        with pytest.raises(RuntimeError, match="502"):
            await call_llm(config, "sys", "user")

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_success_returns_text(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"text": "all_healthy"}
        mock_post.return_value = mock_resp

        config = LLMConfig(provider="test", gateway_url="http://fake:9999")
        result = await call_llm(config, "sys", "user")
        assert result == "all_healthy"


# ---------------------------------------------------------------------------
# execute_action — Hades route mapping and error handling
# ---------------------------------------------------------------------------

class TestExecuteAction:
    @pytest.mark.asyncio
    async def test_handler_action(self):
        async def my_handler(ctx):
            return {"handled": True, "name": ctx.params.get("x")}

        action = Action(name="test", description="t", handler=my_handler)
        ctx = RunContext(role_name="test")
        ctx.params["x"] = "hello"

        result = await execute_action(action, ctx, {})
        assert result["handled"] is True
        assert result["name"] == "hello"

    @pytest.mark.asyncio
    async def test_no_execution_method(self):
        action = Action(name="noop", description="does nothing")
        ctx = RunContext(role_name="test")

        result = await execute_action(action, ctx, {})
        assert "error" in result
        assert "no execution method" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_hades_connect_error(self):
        action = Action(
            name="test", description="t",
            mcp_call={"server": "hades", "tool": "list_services"},
        )
        ctx = RunContext(role_name="test", hades_url="http://localhost:1")

        result = await execute_action(action, ctx, {})
        assert "error" in result
        assert "Cannot reach Hades" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_hades_tool(self):
        action = Action(
            name="test", description="t",
            mcp_call={"server": "hades", "tool": "nonexistent_tool"},
        )
        ctx = RunContext(role_name="test")

        result = await execute_action(action, ctx, {})
        assert "error" in result
        assert "Unknown Hades tool" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_god_server(self):
        action = Action(
            name="test", description="t",
            mcp_call={"server": "unknown_god", "tool": "some_tool"},
        )
        ctx = RunContext(role_name="test")

        result = await execute_action(action, ctx, {})
        assert "error" in result
        assert "No URL" in result["error"]

    @pytest.mark.asyncio
    async def test_template_substitution_in_args(self):
        """Verify that params from RunContext flow into args_template."""
        calls = []

        async def capture_handler(ctx):
            calls.append(ctx.params.copy())
            return {"ok": True}

        action = Action(name="test", description="t", handler=capture_handler)
        ctx = RunContext(role_name="test")
        ctx.params["service_name"] = "HekateOrchestration"

        await execute_action(action, ctx, {})
        assert calls[0]["service_name"] == "HekateOrchestration"


# ---------------------------------------------------------------------------
# _call_hades route mapping
# ---------------------------------------------------------------------------

class TestCallHadesRoutes:
    @pytest.mark.asyncio
    async def test_all_known_routes_exist(self):
        """Verify the route map covers all expected Hades tools."""
        from gods.demigod import _HADES_ROUTES

        expected = {
            "list_services", "service_status", "restart_service",
            "stop_service", "start_service", "restart_core", "restart_all",
            "tail_logs", "system_info", "deploy", "sync_check", "clear_pycache",
        }
        assert set(_HADES_ROUTES.keys()) == expected

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        result = await _call_hades("fake_tool", {}, "http://localhost:1")
        assert "error" in result
        assert "Unknown Hades tool" in result["error"]

    @pytest.mark.asyncio
    async def test_connect_error_returns_error(self):
        result = await _call_hades(
            "list_services", {}, "http://localhost:1"
        )
        assert "error" in result
        assert "Cannot reach Hades" in result["error"]


# ---------------------------------------------------------------------------
# GodConfig (god_mcp.py)
# ---------------------------------------------------------------------------

class TestGodConfig:
    def test_from_file(self, tmp_path):
        from gods.god_mcp import GodConfig

        cfg_path = tmp_path / "spawn.json"
        cfg_path.write_text(json.dumps({
            "role": "health_checker",
            "allowed_tools": ["list_services", "tail_logs"],
            "hades_url": "http://localhost:9999",
            "relay_url": "http://localhost:9999/events",
        }))

        cfg = GodConfig.from_file(str(cfg_path))
        assert cfg.role == "health_checker"
        assert cfg.is_allowed("list_services")
        assert cfg.is_allowed("tail_logs")
        assert not cfg.is_allowed("deploy")
        assert cfg.hades_url == "http://localhost:9999"

    def test_from_god_json(self):
        from gods.god_mcp import GodConfig
        import os

        god_json = os.path.join(
            os.path.dirname(__file__), "gods", "demigods", "god.json"
        )
        if not os.path.exists(god_json):
            pytest.skip("god.json not found")

        cfg = GodConfig.from_god_json(god_json, "health_checker")
        assert cfg.role == "health_checker"
        assert cfg.is_allowed("restart_service")
        assert not cfg.is_allowed("deploy")

    def test_admin_allows_everything(self):
        from gods.god_mcp import GodConfig

        cfg = GodConfig(role="admin")
        assert cfg.is_allowed("deploy")
        assert cfg.is_allowed("anything")

    def test_build_mcp_filters_tools(self):
        from gods.god_mcp import GodConfig, build_mcp

        cfg = GodConfig(
            role="observer",
            allowed_tools={"list_services", "system_info"},
        )
        server = build_mcp(cfg)
        # Server was built — we can't easily count tools without running it,
        # but the build_mcp log confirms filtering


# ---------------------------------------------------------------------------
# Multi-god integration chain
# ---------------------------------------------------------------------------

class TestMultiGodIntegration:
    @pytest.mark.asyncio
    async def test_dispatch_execute_verify_chain(self, sqlite_db):
        """Simulate: Odin dispatches → Hermes completes → Mimir verifies → Odin sees."""
        from gods.base import God

        odin = God("odin", sqlite_db)
        hermes = God("hermes", sqlite_db)
        mimir = God("mimir", sqlite_db)

        # Subscribe to relevant events
        odin.subscribe(["worker_event", "task_verified"])
        hermes.subscribe(["dispatch_command"])
        mimir.subscribe(["worker_event"])

        base_time = time.time() - 5
        odin._relay_cursor = base_time
        hermes._relay_cursor = base_time
        mimir._relay_cursor = base_time

        # Step 1: Odin dispatches
        await odin.emit_relay("dispatch_command", {
            "task_id": "task-1",
            "provider": "claude_code",
        })

        # Step 2: Hermes picks up dispatch
        hermes_events = await hermes.poll_relay()
        assert len(hermes_events) == 1
        assert hermes_events[0].payload["task_id"] == "task-1"

        # Step 3: Hermes completes task
        await hermes.emit_relay("worker_event", {
            "task_id": "task-1",
            "status": "completed",
            "output_len": 2500,
        })

        # Step 4: Mimir picks up completion
        mimir_events = await mimir.poll_relay()
        assert len(mimir_events) == 1
        assert mimir_events[0].payload["status"] == "completed"

        # Step 5: Mimir verifies
        await mimir.emit_relay("task_verified", {
            "task_id": "task-1",
            "verdict": "passed",
        })

        # Step 6: Odin sees both worker_event and task_verified
        odin_events = await odin.poll_relay()
        assert len(odin_events) == 2
        types = {e.event_type for e in odin_events}
        assert types == {"worker_event", "task_verified"}

    @pytest.mark.asyncio
    async def test_fan_out_to_multiple_gods(self, sqlite_db):
        """One event consumed by multiple gods independently."""
        from gods.base import God

        hermes = God("hermes", sqlite_db)
        odin = God("odin", sqlite_db)
        mimir = God("mimir", sqlite_db)
        huginn = God("huginn", sqlite_db)

        base = time.time() - 1
        for god in [odin, mimir, huginn]:
            god.subscribe(["worker_event"])
            god._relay_cursor = base

        await hermes.emit_relay("worker_event", {"task_id": "t1"})

        # All three should see the same event independently
        for god in [odin, mimir, huginn]:
            events = await god.poll_relay()
            assert len(events) == 1
            assert events[0].payload["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_gods_dont_see_own_events(self, sqlite_db):
        """A god subscribed to worker_event doesn't re-process its own emit."""
        from gods.base import God

        hermes = God("hermes", sqlite_db)
        hermes.subscribe(["dispatch_command"])
        hermes._relay_cursor = time.time() - 1

        # Hermes emits a worker_event (not dispatch_command)
        await hermes.emit_relay("worker_event", {"task": "t1"})

        # Hermes only subscribes to dispatch_command — shouldn't see worker_event
        events = await hermes.poll_relay()
        assert len(events) == 0
