"""Tests for gods/demigod.py — parse_pick, template, state machine basics."""

import asyncio

import pytest

from gods.demigod import (
    Action, State, Role, RunContext, Pick,
    parse_pick, build_prompt, _template, DemigodResult,
)


# ---------------------------------------------------------------------------
# _template
# ---------------------------------------------------------------------------

class TestTemplate:
    def test_string_substitution(self):
        assert _template("{name}:{port}", {"name": "Hades", "port": 5201}) == "Hades:5201"

    def test_leaves_unknown_placeholders(self):
        assert _template("{known} and {unknown}", {"known": "yes"}) == "yes and {unknown}"

    def test_no_false_substitution_on_json(self):
        result = _template('{"key": "value"}', {})
        assert result == '{"key": "value"}'

    def test_int_substitution(self):
        assert _template("port={port}", {"port": 5201}) == "port=5201"

    def test_bool_substitution(self):
        assert _template("flag={flag}", {"flag": True}) == "flag=True"

    def test_dict_substitution(self):
        result = _template("data={data}", {"data": {"a": 1}})
        assert '"a": 1' in result

    def test_empty_context(self):
        assert _template("no change", {}) == "no change"


# ---------------------------------------------------------------------------
# parse_pick
# ---------------------------------------------------------------------------

class TestParsePick:
    @pytest.fixture
    def actions(self):
        return [
            Action(name="check_all", description="check everything"),
            Action(name="all_healthy", description="everything is fine"),
            Action(name="inspect", description="look at one", param="service_name"),
        ]

    def test_exact_match(self, actions):
        assert parse_pick("all_healthy", actions).action.name == "all_healthy"
        assert parse_pick("check_all", actions).action.name == "check_all"
        assert parse_pick("inspect", actions).action.name == "inspect"

    def test_case_insensitive(self, actions):
        assert parse_pick("ALL_HEALTHY", actions).action.name == "all_healthy"
        assert parse_pick("Check_All", actions).action.name == "check_all"

    def test_space_normalization(self, actions):
        assert parse_pick("all healthy", actions).action.name == "all_healthy"
        assert parse_pick("check all", actions).action.name == "check_all"

    def test_param_extraction(self, actions):
        p = parse_pick("inspect:HekateOrchestration", actions)
        assert p.action.name == "inspect"
        assert p.param_value == "HekateOrchestration"

    def test_param_preserves_case(self, actions):
        p = parse_pick("inspect:HekateContextStore", actions)
        assert p.param_value == "HekateContextStore"

    def test_number_match(self, actions):
        assert parse_pick("1", actions).action.name == "check_all"
        assert parse_pick("2", actions).action.name == "all_healthy"
        assert parse_pick("3", actions).action.name == "inspect"

    def test_number_with_param(self, actions):
        p = parse_pick("3:MyService", actions)
        assert p.action.name == "inspect"
        assert p.param_value == "MyService"

    def test_prefix_match(self, actions):
        p = parse_pick("inspect the service", actions)
        assert p.action.name == "inspect"

    def test_substring_longest_wins(self, actions):
        # "all_healthy" is longer than "check_all" — should win for "all healthy"
        p = parse_pick("all healthy services are fine", actions)
        assert p.action.name == "all_healthy"

    def test_think_tag_stripping(self, actions):
        p = parse_pick("<think>hmm</think>check_all", actions)
        assert p.action.name == "check_all"

    def test_nested_think_tags(self, actions):
        p = parse_pick("<think>one</think><think>two</think>inspect:Foo", actions)
        assert p.action.name == "inspect"
        assert p.param_value == "Foo"

    def test_no_match_returns_none(self, actions):
        assert parse_pick("gibberish_xyz", actions) is None

    def test_empty_response(self, actions):
        assert parse_pick("", actions) is None

    def test_whitespace_only(self, actions):
        assert parse_pick("   ", actions) is None

    def test_hyphen_normalization(self, actions):
        assert parse_pick("check-all", actions).action.name == "check_all"


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_includes_system_prompt(self):
        role = Role(
            name="test", description="t",
            system_prompt="You are a test.",
            states={"s": State(name="s", prompt="Pick.",
                               actions=[Action(name="act", description="do")])},
        )
        sys_p, user_p = build_prompt(role, role.states["s"], {"info": "hello"})
        assert "You are a test." in sys_p
        assert "CURRENT STATE: s" in sys_p

    def test_includes_context(self):
        role = Role(
            name="t", description="t", system_prompt="sys",
            states={"s": State(name="s", prompt="go",
                               actions=[Action(name="a", description="d")])},
        )
        _, user_p = build_prompt(role, role.states["s"], {"my_key": "my_val"})
        assert "my_key: my_val" in user_p

    def test_includes_actions(self):
        role = Role(
            name="t", description="t", system_prompt="sys",
            states={"s": State(name="s", prompt="go", actions=[
                Action(name="first", description="do first"),
                Action(name="second", description="do second"),
            ])},
        )
        _, user_p = build_prompt(role, role.states["s"], {})
        assert "1. first" in user_p
        assert "2. second" in user_p

    def test_param_hint_in_action(self):
        role = Role(
            name="t", description="t", system_prompt="sys",
            states={"s": State(name="s", prompt="go", actions=[
                Action(name="inspect", description="look", param="target"),
            ])},
        )
        sys_p, user_p = build_prompt(role, role.states["s"], {})
        assert "inspect:<target>" in user_p
        assert "action_name:parameter_value" in sys_p

    def test_correction_appended(self):
        role = Role(
            name="t", description="t", system_prompt="sys",
            states={"s": State(name="s", prompt="go",
                               actions=[Action(name="a", description="d")])},
        )
        _, user_p = build_prompt(role, role.states["s"], {}, correction="bad answer")
        assert "bad answer" in user_p
        assert "invalid" in user_p.lower()


# ---------------------------------------------------------------------------
# RunContext
# ---------------------------------------------------------------------------

class TestRunContext:
    def test_defaults(self):
        ctx = RunContext(role_name="test")
        assert ctx.params == {}
        assert ctx.last_result == {}
        assert ctx.history == []
        assert ctx.step == 0
        assert not ctx.cancelled

    def test_cancel(self):
        ctx = RunContext(role_name="test")
        assert not ctx.cancelled
        ctx.cancel()
        assert ctx.cancelled

    def test_params_mutable(self):
        ctx = RunContext(role_name="test")
        ctx.params["service_name"] = "HekateOrchestration"
        assert ctx.params["service_name"] == "HekateOrchestration"


# ---------------------------------------------------------------------------
# Role validation
# ---------------------------------------------------------------------------

class TestRoleStructure:
    def test_health_checker_role(self):
        from gods.roles.health_checker import build_health_checker_role
        role = build_health_checker_role()
        assert role.name == "health_checker"
        assert role.initial_state == "check_all"
        assert len(role.states) == 3
        assert "check_all" in role.states
        assert "inspect" in role.states
        assert "verify" in role.states

    def test_batch_health_checker_role(self):
        from gods.roles.health_checker import build_batch_health_checker_role
        role = build_batch_health_checker_role()
        assert role.name == "batch_health_checker"
        assert role.max_steps == 30
        assert "scan" in role.states
        assert "fixing" in role.states

    def test_all_states_have_actions_or_terminal(self):
        from gods.roles.health_checker import (
            build_health_checker_role,
            build_batch_health_checker_role,
        )
        for builder in [build_health_checker_role, build_batch_health_checker_role]:
            role = builder()
            for name, state in role.states.items():
                assert state.actions or state.terminal, (
                    f"State '{name}' in role '{role.name}' has no actions and is not terminal"
                )

    def test_no_dangling_transitions(self):
        from gods.roles.health_checker import (
            build_health_checker_role,
            build_batch_health_checker_role,
        )
        for builder in [build_health_checker_role, build_batch_health_checker_role]:
            role = builder()
            valid_states = set(role.states.keys())
            for name, state in role.states.items():
                for action in state.actions:
                    if action.next_state:
                        assert action.next_state in valid_states, (
                            f"Action '{action.name}' in state '{name}' transitions to "
                            f"unknown state '{action.next_state}'"
                        )


# ---------------------------------------------------------------------------
# Batch tracker (_mark_fixed)
# ---------------------------------------------------------------------------

class TestBatchTracker:
    @pytest.mark.asyncio
    async def test_marks_service_fixed(self):
        from gods.roles.health_checker import _mark_fixed
        ctx = RunContext(role_name="batch")
        ctx.params["service_name"] = "HekateOrchestration"

        result = await _mark_fixed(ctx)
        assert "HekateOrchestration" in ctx.params["_fixed_services"]
        assert result["marked"] == "HekateOrchestration"

    @pytest.mark.asyncio
    async def test_accumulates_fixed_services(self):
        from gods.roles.health_checker import _mark_fixed
        ctx = RunContext(role_name="batch")

        ctx.params["service_name"] = "ServiceA"
        await _mark_fixed(ctx)
        ctx.params["service_name"] = "ServiceB"
        await _mark_fixed(ctx)

        fixed = ctx.params["_fixed_services"]
        assert "ServiceA" in fixed
        assert "ServiceB" in fixed

    @pytest.mark.asyncio
    async def test_no_duplicates(self):
        from gods.roles.health_checker import _mark_fixed
        ctx = RunContext(role_name="batch")

        ctx.params["service_name"] = "ServiceA"
        await _mark_fixed(ctx)
        await _mark_fixed(ctx)

        parts = ctx.params["_fixed_services"].split(",")
        assert parts.count("ServiceA") == 1
