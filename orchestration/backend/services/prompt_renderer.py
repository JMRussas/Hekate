#  Orchestration Engine - Prompt Renderer
#
#  Model-agnostic prompt specification (PromptSpec) and per-provider
#  renderers that transform it into optimized prompt strings.
#
#  The semantic intent of a prompt is model-independent; the rendering
#  adapts to the receiving model's strengths and limitations.
#
#  Depends on: (none — pure data structures and formatting)
#  Used by:    cli_common.py, claude_agent.py, ollama_agent.py,
#              llm_router.py, planner.py, verifier.py, odin_prompts.py,
#              knowledge_extractor.py, sentinel/reasoner.py

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Context type enum — priority-ordered for budget truncation
# ---------------------------------------------------------------------------

class ContextType(str, Enum):
    """Context entry types, ordered by importance for budget truncation."""
    EXECUTION_RULES = "execution_rules"
    CSHARP_WORKER = "csharp_worker"
    PROJECT_KNOWLEDGE = "project_knowledge"
    HISTORICAL_RATIONALE = "historical_rationale"
    PLATFORM_CONTEXT = "platform_context"
    TASK_DESCRIPTION = "task_description"
    VERIFICATION_CRITERIA = "verification_criteria"
    DEPENDENCY_OUTPUT = "dependency_output"
    META_INSTRUCTIONS = "meta_instructions"
    SIBLING_TASKS = "sibling_tasks"
    GENERIC = "generic"


# Priority mapping: lower number = kept first during budget truncation
CONTEXT_PRIORITY: dict[ContextType, int] = {
    ContextType.EXECUTION_RULES: 0,
    ContextType.CSHARP_WORKER: 1,
    ContextType.PROJECT_KNOWLEDGE: 2,
    ContextType.HISTORICAL_RATIONALE: 2,
    ContextType.PLATFORM_CONTEXT: 3,
    ContextType.TASK_DESCRIPTION: 4,
    ContextType.VERIFICATION_CRITERIA: 5,
    ContextType.DEPENDENCY_OUTPUT: 6,
    ContextType.META_INSTRUCTIONS: 7,
    ContextType.SIBLING_TASKS: 8,
    ContextType.GENERIC: 9,
}


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ContextEntry:
    """A typed, tagged context block with priority for budget truncation."""
    type: ContextType
    tag: str            # XML tag name or section header
    content: str
    priority_override: int | None = None

    @property
    def priority(self) -> int:
        if self.priority_override is not None:
            return self.priority_override
        return CONTEXT_PRIORITY.get(self.type, 99)


@dataclass
class FewShotExample:
    """An input/output demonstration pair."""
    user_input: str
    expected_output: str
    label: str = ""


@dataclass
class PromptSpec:
    """Model-agnostic prompt specification.

    Constructed at every callsite that previously did string concatenation.
    Passed to a renderer to produce provider-optimized output.
    """
    role: str                   # e.g. "task_executor", "verifier", "planner", "overseer"
    identity: str               # System identity statement (first line of system prompt)
    task_description: str       # The main user message / task content

    # Structured context blocks (prioritized, budget-truncated per renderer)
    context: list[ContextEntry] = field(default_factory=list)

    # Behavioral rules
    constraints: list[str] = field(default_factory=list)

    # Expected output format (declared BEFORE task content to prime generation)
    output_schema: str = ""
    output_format: str = ""     # "json", "code", "text"

    # Model-specific tic suppression
    suppressions: list[str] = field(default_factory=list)

    # Explicit permission grants (Claude responds well to these)
    permissions: list[str] = field(default_factory=list)

    # Input/output demonstrations
    few_shot_examples: list[FewShotExample] = field(default_factory=list)

    # Completion anchoring — prefill the assistant turn (Claude API only)
    assistant_prefill: str = ""

    # Task metadata
    task_type: str = ""
    tools_available: list[str] = field(default_factory=list)


@dataclass
class RenderedPrompt:
    """The output of rendering. Ready to pass to any LLM call site."""
    system_prompt: str
    user_message: str
    assistant_prefill: str = ""     # Only used by Claude API
    flat_prompt: str = ""           # Single string for CLI executors (stdin pipe)


# ---------------------------------------------------------------------------
# Provider mapping
# ---------------------------------------------------------------------------

_TIER_TO_PROVIDER = {
    "claude_code": "claude",
    "haiku": "claude",
    "sonnet": "claude",
    "opus": "claude",
    "gemini_cli": "gemini",
    "codex_cli": "gemini",  # similar flat-prompt style
    "ollama": "ollama",
}


def provider_from_tier(tier_value: str) -> str:
    """Map a model tier string to a provider name for renderer selection.

    Matches the TIER_TO_PROVIDER logic in model_router.py.
    """
    return _TIER_TO_PROVIDER.get(tier_value, "claude")


# ---------------------------------------------------------------------------
# Base renderer
# ---------------------------------------------------------------------------

class BaseRenderer:
    """Shared logic for all renderers."""

    max_context_chars: int = 200_000

    def render(self, spec: PromptSpec) -> RenderedPrompt:
        raise NotImplementedError

    def _budget_context(
        self, entries: list[ContextEntry], budget: int | None = None,
    ) -> list[ContextEntry]:
        """Sort by priority, sanitize tags, truncate to fit within character budget."""
        if budget is None:
            budget = self.max_context_chars

        # Sanitize all tags upfront — prevent injection via any renderer
        for entry in entries:
            entry.tag = self._sanitize_tag(entry.tag)

        sorted_entries = sorted(entries, key=lambda e: e.priority)
        result: list[ContextEntry] = []
        used = 0

        for entry in sorted_entries:
            entry_len = len(entry.content)
            if used + entry_len <= budget:
                result.append(entry)
                used += entry_len
            elif used < budget:
                # Partial inclusion — truncate content to fit remaining budget
                remaining = budget - used
                truncated = ContextEntry(
                    type=entry.type,
                    tag=entry.tag,
                    content=entry.content[:remaining] + "\n[...truncated...]",
                    priority_override=entry.priority_override,
                )
                result.append(truncated)
                used = budget
                break
            else:
                break  # Budget exhausted

        return result

    @staticmethod
    def _sanitize_tag(tag: str) -> str:
        """Sanitize a tag name for XML use — prevent prompt injection."""
        return re.sub(r"[^a-zA-Z0-9_]", "_", tag)


# ---------------------------------------------------------------------------
# Claude renderer — XML tags, constitutional framing, full context
# ---------------------------------------------------------------------------

class ClaudeRenderer(BaseRenderer):
    """Optimized for Claude models.

    - XML tags on context (Claude was trained on this)
    - Constitutional framing and permission grants
    - Output schema placed before context (primes generation)
    - Full context budget (~100k tokens)
    - Suppresses RLHF hedging/apologizing
    """

    max_context_chars = 150_000

    def render(self, spec: PromptSpec) -> RenderedPrompt:
        system_parts: list[str] = []

        # Identity + constitutional framing
        system_parts.append(spec.identity)
        system_parts.append(
            "You are a professional who values accuracy and directness. "
            "It is appropriate and helpful to be direct here."
        )

        # Permission grants
        if spec.permissions:
            system_parts.append("\n".join(spec.permissions))

        # Output schema BEFORE context — primes the generation distribution
        if spec.output_schema:
            system_parts.append(
                f"<output_format>\n{spec.output_schema}\n</output_format>"
            )

        # Context entries in XML tags
        budgeted = self._budget_context(spec.context)
        for entry in budgeted:
            tag = self._sanitize_tag(entry.tag)
            system_parts.append(f"<{tag}>\n{entry.content}\n</{tag}>")

        # Constraints
        if spec.constraints:
            constraint_lines = "\n".join(f"- {c}" for c in spec.constraints)
            system_parts.append(
                f"<constraints>\n{constraint_lines}\n</constraints>"
            )

        # Suppressions — fight RLHF hedging
        all_suppressions = [
            "Do not apologize.",
            "Do not hedge.",
            "Do not restate the question.",
        ]
        if spec.suppressions:
            all_suppressions.extend(spec.suppressions)
        system_parts.append(" ".join(all_suppressions))

        system_prompt = "\n\n".join(system_parts)

        # User message — few-shot examples first, then task
        user_parts: list[str] = []
        if spec.few_shot_examples:
            for ex in spec.few_shot_examples:
                label = f' label="{ex.label}"' if ex.label else ""
                user_parts.append(
                    f"<example{label}>\n"
                    f"<input>{ex.user_input}</input>\n"
                    f"<output>{ex.expected_output}</output>\n"
                    f"</example>"
                )
        user_parts.append(spec.task_description)
        user_message = "\n\n".join(user_parts)

        return RenderedPrompt(
            system_prompt=system_prompt,
            user_message=user_message,
            assistant_prefill=spec.assistant_prefill,
            flat_prompt=f"{system_prompt}\n\n{user_message}",
        )


# ---------------------------------------------------------------------------
# Gemini renderer — compact system prompt, context in user message
# ---------------------------------------------------------------------------

class GeminiRenderer(BaseRenderer):
    """Optimized for Gemini models.

    - Compact system prompt (identity + numbered constraints only)
    - Context moved to user message as ### headers (not system prompt)
    - Moderate context budget (~8k tokens)
    - Suppressions: conciseness, no repetition
    """

    max_context_chars = 30_000

    def render(self, spec: PromptSpec) -> RenderedPrompt:
        # Compact system prompt — identity + output schema + constraints only
        system_parts: list[str] = [spec.identity]

        if spec.output_schema:
            system_parts.append(f"Output format: {spec.output_schema}")

        # Constraints as numbered list (Gemini responds well to this)
        if spec.constraints:
            numbered = "\n".join(
                f"{i + 1}. {c}" for i, c in enumerate(spec.constraints)
            )
            system_parts.append(f"Rules:\n{numbered}")

        # Suppressions
        all_suppressions = ["Do not repeat the question.", "Be concise."]
        if spec.suppressions:
            all_suppressions.extend(spec.suppressions)
        system_parts.append(" ".join(all_suppressions))

        system_prompt = "\n\n".join(system_parts)

        # User message — context as ### headers, then few-shot, then task
        user_parts: list[str] = []

        budgeted = self._budget_context(spec.context)
        for entry in budgeted:
            user_parts.append(f"### {entry.tag}\n{entry.content}")

        if spec.few_shot_examples:
            for ex in spec.few_shot_examples:
                label = f" ({ex.label})" if ex.label else ""
                user_parts.append(
                    f"Example{label}:\nInput: {ex.user_input}\n"
                    f"Output: {ex.expected_output}"
                )

        user_parts.append(spec.task_description)
        user_message = "\n\n".join(user_parts)

        return RenderedPrompt(
            system_prompt=system_prompt,
            user_message=user_message,
            flat_prompt=f"{system_prompt}\n\n---\n\n{user_message}",
        )


# ---------------------------------------------------------------------------
# Ollama renderer — minimal system prompt, few-shot critical, tight budget
# ---------------------------------------------------------------------------

class OllamaRenderer(BaseRenderer):
    """Optimized for local Ollama models (qwen, llama, mistral, etc.).

    - Very short system prompt (< 1k tokens)
    - Few-shot examples placed first in user message (critical for
      compensating weaker instruction-following)
    - Flat context (no XML nesting)
    - Strict context budget (~2k tokens)
    - Direct suppression (less RLHF to fight)
    """

    max_context_chars = 6_000

    def render(self, spec: PromptSpec) -> RenderedPrompt:
        # Minimal system prompt — identity + bullet constraints
        system_parts: list[str] = [spec.identity]

        if spec.constraints:
            system_parts.append(
                "\n".join(f"- {c}" for c in spec.constraints)
            )

        all_suppressions = ["Output only what was requested.", "No narration."]
        if spec.suppressions:
            all_suppressions.extend(spec.suppressions)
        system_parts.append(" ".join(all_suppressions))

        system_prompt = "\n".join(system_parts)

        # User message — few-shot FIRST (highest priority for weak models)
        user_parts: list[str] = []
        if spec.few_shot_examples:
            for ex in spec.few_shot_examples:
                label = f" ({ex.label})" if ex.label else ""
                user_parts.append(
                    f"Example{label}:\n"
                    f"Input: {ex.user_input}\n"
                    f"Output: {ex.expected_output}"
                )
            user_parts.append("---")

        # Context as flat key: value pairs
        budgeted = self._budget_context(spec.context)
        for entry in budgeted:
            user_parts.append(f"{entry.tag}: {entry.content}")

        # Output schema at end
        if spec.output_schema:
            user_parts.append(f"Expected format: {spec.output_schema}")

        user_parts.append(spec.task_description)
        user_message = "\n\n".join(user_parts)

        return RenderedPrompt(
            system_prompt=system_prompt,
            user_message=user_message,
            flat_prompt=f"{system_prompt}\n\n{user_message}",
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_RENDERERS: dict[str, type[BaseRenderer]] = {
    "claude": ClaudeRenderer,
    "gemini": GeminiRenderer,
    "ollama": OllamaRenderer,
}


def render_prompt(spec: PromptSpec, provider: str) -> RenderedPrompt:
    """Render a PromptSpec for a specific provider.

    Args:
        spec: Model-agnostic prompt specification.
        provider: Provider name ("claude", "gemini", "ollama").
            Unknown providers default to ClaudeRenderer.

    Returns:
        RenderedPrompt with system_prompt, user_message, and flat_prompt.
    """
    renderer_cls = _RENDERERS.get(provider, ClaudeRenderer)
    return renderer_cls().render(spec)
