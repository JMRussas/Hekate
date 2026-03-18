#  Tests for the prompt renderer layer.
#
#  Covers: ContextEntry priority, budget truncation, all three renderers,
#  factory dispatch, provider_from_tier mapping.

import pytest

from backend.services.prompt_renderer import (
    BaseRenderer,
    ClaudeRenderer,
    ContextEntry,
    ContextType,
    FewShotExample,
    GeminiRenderer,
    OllamaRenderer,
    PromptSpec,
    RenderedPrompt,
    provider_from_tier,
    render_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(**overrides) -> PromptSpec:
    """Create a minimal PromptSpec with sensible defaults."""
    defaults = {
        "role": "task_executor",
        "identity": "You are a focused task executor.",
        "task_description": "Implement the login endpoint.",
    }
    defaults.update(overrides)
    return PromptSpec(**defaults)


def _make_context(tag: str, content: str, ctx_type: ContextType = ContextType.GENERIC) -> ContextEntry:
    return ContextEntry(type=ctx_type, tag=tag, content=content)


# ---------------------------------------------------------------------------
# ContextEntry
# ---------------------------------------------------------------------------

class TestContextEntry:
    def test_priority_from_type(self):
        entry = ContextEntry(type=ContextType.EXECUTION_RULES, tag="rules", content="x")
        assert entry.priority == 0

        entry = ContextEntry(type=ContextType.SIBLING_TASKS, tag="siblings", content="x")
        assert entry.priority == 8

    def test_priority_override(self):
        entry = ContextEntry(
            type=ContextType.SIBLING_TASKS, tag="siblings", content="x",
            priority_override=0,
        )
        assert entry.priority == 0

    def test_unknown_type_priority(self):
        entry = ContextEntry(type=ContextType.GENERIC, tag="misc", content="x")
        assert entry.priority == 9


# ---------------------------------------------------------------------------
# Budget truncation
# ---------------------------------------------------------------------------

class TestContextBudgeting:
    def test_all_entries_fit(self):
        renderer = BaseRenderer()
        entries = [
            _make_context("a", "short", ContextType.GENERIC),
            _make_context("b", "also short", ContextType.GENERIC),
        ]
        result = renderer._budget_context(entries, budget=1000)
        assert len(result) == 2

    def test_truncation_drops_low_priority(self):
        renderer = BaseRenderer()
        # High priority entry (100 chars)
        high = ContextEntry(
            type=ContextType.EXECUTION_RULES, tag="rules",
            content="x" * 100,
        )
        # Low priority entry (100 chars)
        low = ContextEntry(
            type=ContextType.SIBLING_TASKS, tag="siblings",
            content="y" * 100,
        )
        result = renderer._budget_context([low, high], budget=120)
        # High priority should be kept, low partially truncated or dropped
        assert len(result) >= 1
        assert result[0].type == ContextType.EXECUTION_RULES

    def test_partial_truncation(self):
        renderer = BaseRenderer()
        entry = ContextEntry(
            type=ContextType.GENERIC, tag="data",
            content="A" * 200,
        )
        result = renderer._budget_context([entry], budget=50)
        assert len(result) == 1
        assert "[...truncated...]" in result[0].content
        assert len(result[0].content) < 200

    def test_empty_entries(self):
        renderer = BaseRenderer()
        result = renderer._budget_context([], budget=1000)
        assert result == []

    def test_zero_budget(self):
        renderer = BaseRenderer()
        entry = _make_context("a", "content")
        result = renderer._budget_context([entry], budget=0)
        assert result == []

    def test_priority_ordering(self):
        renderer = BaseRenderer()
        low = ContextEntry(type=ContextType.SIBLING_TASKS, tag="low", content="L" * 50)
        mid = ContextEntry(type=ContextType.VERIFICATION_CRITERIA, tag="mid", content="M" * 50)
        high = ContextEntry(type=ContextType.EXECUTION_RULES, tag="high", content="H" * 50)
        # Pass in reverse priority order — budget should sort correctly
        result = renderer._budget_context([low, mid, high], budget=120)
        # High priority (0) should come first
        assert result[0].tag == "high"
        assert result[1].tag == "mid"
        # Low priority may be partially included or dropped
        if len(result) > 2:
            assert result[2].tag == "low"


# ---------------------------------------------------------------------------
# Claude Renderer
# ---------------------------------------------------------------------------

class TestClaudeRenderer:
    def test_identity_and_framing(self):
        spec = _make_spec()
        rendered = ClaudeRenderer().render(spec)
        assert "You are a focused task executor." in rendered.system_prompt
        assert "professional who values accuracy" in rendered.system_prompt

    def test_xml_tags_on_context(self):
        spec = _make_spec(context=[
            _make_context("project_setup", "Install deps first"),
        ])
        rendered = ClaudeRenderer().render(spec)
        assert "<project_setup>" in rendered.system_prompt
        assert "</project_setup>" in rendered.system_prompt

    def test_output_schema_before_context(self):
        spec = _make_spec(
            output_schema='{"result": "ok"}',
            context=[_make_context("ctx", "some context")],
        )
        rendered = ClaudeRenderer().render(spec)
        schema_pos = rendered.system_prompt.index("<output_format>")
        ctx_pos = rendered.system_prompt.index("<ctx>")
        assert schema_pos < ctx_pos

    def test_few_shot_in_user_message(self):
        spec = _make_spec(few_shot_examples=[
            FewShotExample(
                user_input="Add a column",
                expected_output="Write tool creates migration file",
                label="migration",
            ),
        ])
        rendered = ClaudeRenderer().render(spec)
        assert "<example" in rendered.user_message
        assert "Add a column" in rendered.user_message
        # Few-shot should NOT be in system prompt
        assert "<example" not in rendered.system_prompt

    def test_suppressions(self):
        spec = _make_spec(suppressions=["No markdown fences."])
        rendered = ClaudeRenderer().render(spec)
        assert "Do not apologize." in rendered.system_prompt
        assert "No markdown fences." in rendered.system_prompt

    def test_constraints_in_xml(self):
        spec = _make_spec(constraints=["Write files, not descriptions"])
        rendered = ClaudeRenderer().render(spec)
        assert "<constraints>" in rendered.system_prompt
        assert "Write files, not descriptions" in rendered.system_prompt

    def test_permissions(self):
        spec = _make_spec(permissions=["You may use destructive git operations."])
        rendered = ClaudeRenderer().render(spec)
        assert "destructive git operations" in rendered.system_prompt

    def test_flat_prompt_includes_both(self):
        spec = _make_spec()
        rendered = ClaudeRenderer().render(spec)
        assert rendered.system_prompt in rendered.flat_prompt
        assert rendered.user_message in rendered.flat_prompt

    def test_assistant_prefill_passed_through(self):
        spec = _make_spec(assistant_prefill="I'll start by reading")
        rendered = ClaudeRenderer().render(spec)
        assert rendered.assistant_prefill == "I'll start by reading"

    def test_tag_sanitization(self):
        spec = _make_spec(context=[
            ContextEntry(type=ContextType.GENERIC, tag="bad<tag>name", content="safe"),
        ])
        rendered = ClaudeRenderer().render(spec)
        assert "<bad<tag>name>" not in rendered.system_prompt
        assert "<bad_tag_name>" in rendered.system_prompt


# ---------------------------------------------------------------------------
# Gemini Renderer
# ---------------------------------------------------------------------------

class TestGeminiRenderer:
    def test_compact_system_prompt(self):
        spec = _make_spec(context=[
            _make_context("big_context", "X" * 500),
        ])
        rendered = GeminiRenderer().render(spec)
        # Context should NOT be in system prompt
        assert "big_context" not in rendered.system_prompt
        # Context should be in user message
        assert "### big_context" in rendered.user_message

    def test_numbered_constraints(self):
        spec = _make_spec(constraints=["Rule A", "Rule B"])
        rendered = GeminiRenderer().render(spec)
        assert "1. Rule A" in rendered.system_prompt
        assert "2. Rule B" in rendered.system_prompt

    def test_output_schema_in_system(self):
        spec = _make_spec(output_schema='{"verdict": "passed"}')
        rendered = GeminiRenderer().render(spec)
        assert '{"verdict": "passed"}' in rendered.system_prompt

    def test_few_shot_in_user_message(self):
        spec = _make_spec(few_shot_examples=[
            FewShotExample(user_input="test input", expected_output="test output"),
        ])
        rendered = GeminiRenderer().render(spec)
        assert "Example:" in rendered.user_message
        assert "Input: test input" in rendered.user_message

    def test_flat_prompt_separator(self):
        spec = _make_spec()
        rendered = GeminiRenderer().render(spec)
        assert "\n\n---\n\n" in rendered.flat_prompt

    def test_suppressions(self):
        spec = _make_spec()
        rendered = GeminiRenderer().render(spec)
        assert "Do not repeat the question." in rendered.system_prompt
        assert "Be concise." in rendered.system_prompt


# ---------------------------------------------------------------------------
# Ollama Renderer
# ---------------------------------------------------------------------------

class TestOllamaRenderer:
    def test_short_system_prompt(self):
        spec = _make_spec(
            constraints=["Only output JSON"],
            context=[_make_context("ctx", "X" * 500)],
        )
        rendered = OllamaRenderer().render(spec)
        # System prompt should be short — identity + constraints + suppressions
        assert len(rendered.system_prompt) < 500

    def test_few_shot_first_in_user_message(self):
        spec = _make_spec(
            few_shot_examples=[
                FewShotExample(user_input="in", expected_output="out"),
            ],
            context=[_make_context("ctx", "some context")],
        )
        rendered = OllamaRenderer().render(spec)
        # Few-shot should appear before context
        example_pos = rendered.user_message.index("Example:")
        ctx_pos = rendered.user_message.index("ctx:")
        assert example_pos < ctx_pos

    def test_flat_context(self):
        spec = _make_spec(context=[
            _make_context("section_a", "content A"),
        ])
        rendered = OllamaRenderer().render(spec)
        # Should be flat "tag: content", NOT XML
        assert "section_a: content A" in rendered.user_message
        assert "<section_a>" not in rendered.user_message

    def test_output_schema_at_end(self):
        spec = _make_spec(
            output_schema='{"result": "ok"}',
            context=[_make_context("ctx", "first")],
        )
        rendered = OllamaRenderer().render(spec)
        schema_pos = rendered.user_message.index("Expected format:")
        task_pos = rendered.user_message.index("Implement the login")
        # Schema should be before the task description (at end)
        assert schema_pos < task_pos

    def test_strict_budget(self):
        spec = _make_spec(context=[
            _make_context("big", "X" * 10_000, ContextType.GENERIC),
        ])
        rendered = OllamaRenderer().render(spec)
        # Content should be truncated to fit within 6k budget
        assert len(rendered.user_message) < 10_000

    def test_suppressions(self):
        spec = _make_spec()
        rendered = OllamaRenderer().render(spec)
        assert "No narration." in rendered.system_prompt


# ---------------------------------------------------------------------------
# Factory and provider mapping
# ---------------------------------------------------------------------------

class TestRenderPromptFactory:
    def test_dispatches_to_claude(self):
        spec = _make_spec()
        rendered = render_prompt(spec, "claude")
        # Claude signature: XML tags and constitutional framing
        assert "professional who values accuracy" in rendered.system_prompt

    def test_dispatches_to_gemini(self):
        spec = _make_spec()
        rendered = render_prompt(spec, "gemini")
        assert "Do not repeat the question." in rendered.system_prompt

    def test_dispatches_to_ollama(self):
        spec = _make_spec()
        rendered = render_prompt(spec, "ollama")
        assert "No narration." in rendered.system_prompt

    def test_unknown_provider_defaults_claude(self):
        spec = _make_spec()
        rendered = render_prompt(spec, "unknown_provider")
        assert "professional who values accuracy" in rendered.system_prompt


class TestProviderFromTier:
    def test_claude_code(self):
        assert provider_from_tier("claude_code") == "claude"

    def test_haiku(self):
        assert provider_from_tier("haiku") == "claude"

    def test_sonnet(self):
        assert provider_from_tier("sonnet") == "claude"

    def test_opus(self):
        assert provider_from_tier("opus") == "claude"

    def test_gemini_cli(self):
        assert provider_from_tier("gemini_cli") == "gemini"

    def test_codex_cli(self):
        assert provider_from_tier("codex_cli") == "gemini"

    def test_ollama(self):
        assert provider_from_tier("ollama") == "ollama"

    def test_unknown_defaults_claude(self):
        assert provider_from_tier("nonexistent") == "claude"


# ---------------------------------------------------------------------------
# Cross-renderer consistency
# ---------------------------------------------------------------------------

class TestCrossRenderer:
    """Ensure all renderers produce valid RenderedPrompt with required fields."""

    @pytest.mark.parametrize("provider", ["claude", "gemini", "ollama"])
    def test_all_renderers_produce_complete_output(self, provider):
        spec = _make_spec(
            context=[_make_context("ctx", "test context")],
            constraints=["Be accurate"],
            output_schema='{"status": "ok"}',
            few_shot_examples=[FewShotExample("in", "out")],
        )
        rendered = render_prompt(spec, provider)
        assert isinstance(rendered, RenderedPrompt)
        assert rendered.system_prompt
        assert rendered.user_message
        assert rendered.flat_prompt
        assert "Implement the login endpoint." in rendered.user_message

    @pytest.mark.parametrize("provider", ["claude", "gemini", "ollama"])
    def test_task_description_always_in_user_message(self, provider):
        spec = _make_spec(task_description="Build the API")
        rendered = render_prompt(spec, provider)
        assert "Build the API" in rendered.user_message

    @pytest.mark.parametrize("provider", ["claude", "gemini", "ollama"])
    def test_identity_always_in_system_prompt(self, provider):
        spec = _make_spec(identity="You are a code reviewer.")
        rendered = render_prompt(spec, provider)
        assert "You are a code reviewer." in rendered.system_prompt
