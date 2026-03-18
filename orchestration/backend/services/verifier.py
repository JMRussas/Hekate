#  Orchestration Engine - Output Verifier
#
#  Verifies task output quality using call_llm (CLI providers or Ollama).
#  Returns PASSED, GAPS_FOUND, or HUMAN_NEEDED.
#
#  Depends on: backend/config.py, backend/models/enums.py,
#              backend/services/llm_router.py, backend/services/prompt_renderer.py,
#              backend/utils/json_utils.py
#  Used by:    services/task_lifecycle.py

import json
import logging

from backend.models.enums import VerificationResult
from backend.services.llm_router import call_llm
from backend.services.prompt_renderer import ContextEntry, ContextType, FewShotExample, PromptSpec
from backend.utils.json_utils import extract_json_object

logger = logging.getLogger("orchestration.verifier")

_VERIFIER_IDENTITY = (
    "You are a task output verifier. Given a task description and the output "
    "produced, assess whether the output is acceptable."
)

_VERIFIER_CONSTRAINTS = [
    "Substantiveness: Is the output real content, or is it empty/stub/placeholder?",
    "Relevance: Does the output address the task description?",
    "Completeness: Does the output cover the key aspects of what was asked?",
    "For CODE tasks, the executor MUST have used Write/Edit tools to create or "
    "modify files. A text description of code is NOT the same as writing code — "
    "mark as gaps_found if no files were written.",
    "Evaluate ONLY against the stated task description. Ignore unrelated content.",
    "Pre-existing failures in unrelated modules are NOT gaps.",
    "Consider the task type and tools when judging completeness.",
]

_VERIFIER_OUTPUT_SCHEMA = """\
Respond with ONLY a JSON object (no markdown):
{"verdict": "passed" | "gaps_found" | "human_needed", "notes": "Brief explanation"}

Verdict rules:
- "passed": Substantive, relevant, complete. For code tasks, files were written/edited.
- "gaps_found": Empty, stub, off-topic, missing key aspects, or code task without files written.
- "human_needed": Fundamental issues requiring human judgment."""

_VERIFIER_FEW_SHOT = FewShotExample(
    user_input='Task "Add login endpoint" / Output: "Here is how you would implement a login endpoint..."',
    expected_output='{"verdict": "gaps_found", "notes": "Code task produced text description only, no files were written"}',
    label="code task without file writes",
)


async def verify_output(
    task_title: str,
    task_description: str,
    output_text: str,
    *,
    task_type: str = "",
    tools: list[str] | None = None,
    platform_context: str | None = None,
    budget,
    project_id: str,
    task_id: str,
) -> dict:
    """Verify task output quality using call_llm (CLI providers / Ollama).

    Routes through llm_router with task_type="simple" so it prefers
    Gemini > Ollama > Codex — cheap/free providers suitable for classification.
    Uses PromptSpec so the prompt is re-rendered per provider in the fallback chain.

    Args:
        task_title: The task's title.
        task_description: What the task was supposed to do.
        output_text: The actual output produced.
        task_type: Task type (e.g., "code", "research") for scoped evaluation.
        tools: Tools available to the task, for context.
        budget: BudgetManager for recording verification cost.
        project_id: For cost attribution.
        task_id: For cost attribution.

    Returns:
        {"result": VerificationResult, "notes": str, "cost_usd": float}
    """
    # Skip verification if budget is exhausted — output is already paid for
    if not await budget.can_spend(0.001):
        logger.warning("Budget exhausted, skipping verification for task %s", task_id)
        return {"result": VerificationResult.SKIPPED, "notes": "Skipped: budget exhausted", "cost_usd": 0.0}

    # Truncate long output to control verification cost
    _MAX_OUTPUT_CHARS = 8000
    truncated = (output_text or "(empty)")[:_MAX_OUTPUT_CHARS]
    if output_text and len(output_text) > _MAX_OUTPUT_CHARS:
        truncated += "\n\n[... output truncated for verification ...]"

    task_meta = f"**Task type**: {task_type or 'unknown'}"
    if tools:
        task_meta += f"\n**Tools**: {', '.join(tools)}"

    platform_section = ""
    if platform_context:
        platform_section = (
            f"\n### Platform Requirements\n"
            f"Verify output correctness against these platform-specific rules:\n"
            f"{platform_context}\n"
        )

    user_msg = (
        f"## Task: {task_title}\n\n"
        f"{task_meta}\n\n"
        f"### Description\n{task_description}\n"
        f"{platform_section}\n"
        f"### Output\n{truncated}"
    )

    # Build PromptSpec — re-rendered per provider in the fallback chain
    spec = PromptSpec(
        role="verifier",
        identity=_VERIFIER_IDENTITY,
        task_description=user_msg,
        constraints=_VERIFIER_CONSTRAINTS,
        output_schema=_VERIFIER_OUTPUT_SCHEMA,
        output_format="json",
        suppressions=["Do not include markdown fences."],
        few_shot_examples=[_VERIFIER_FEW_SHOT],
        task_type=task_type,
    )

    try:
        llm_response = await call_llm(
            spec=spec,
            task_type="simple",
        )
    except RuntimeError:
        # All providers failed — don't crash the task, escalate to human review
        logger.warning("All LLM providers failed for verification of task %s", task_id)
        return {
            "result": VerificationResult.HUMAN_NEEDED,
            "notes": "Verification skipped: all LLM providers unavailable",
            "cost_usd": 0.0,
        }

    # Record audit trail (CLI providers report $0, Ollama is free)
    await budget.record_spend(
        cost_usd=llm_response.cost_usd,
        prompt_tokens=llm_response.prompt_tokens,
        completion_tokens=llm_response.completion_tokens,
        provider=llm_response.provider or "unknown",
        model=llm_response.model or "default",
        purpose="verification",
        project_id=project_id,
        task_id=task_id,
    )

    raw = llm_response.text

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, AttributeError):
        # Fallback: extract JSON from markdown fences / trailing commas
        parsed = extract_json_object(raw)

    if parsed and isinstance(parsed, dict):
        verdict_str = parsed.get("verdict", "passed")
        notes = parsed.get("notes", "")
    else:
        # If we can't parse, escalate to human review (don't silently pass)
        logger.warning("Could not parse verification response, escalating to human review: %s", raw[:200])
        verdict_str = "human_needed"
        notes = "Verification response was not parseable JSON — escalated to human review"

    # Map to enum
    verdict_map = {
        "passed": VerificationResult.PASSED,
        "gaps_found": VerificationResult.GAPS_FOUND,
        "human_needed": VerificationResult.HUMAN_NEEDED,
    }
    result = verdict_map.get(verdict_str, VerificationResult.HUMAN_NEEDED)

    return {"result": result, "notes": notes, "cost_usd": llm_response.cost_usd}
