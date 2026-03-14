#  Orchestration Engine - Output Verifier
#
#  Verifies task output quality using call_llm (CLI providers or Ollama).
#  Returns PASSED, GAPS_FOUND, or HUMAN_NEEDED.
#
#  Depends on: backend/config.py, backend/models/enums.py,
#              backend/services/llm_router.py, backend/utils/json_utils.py
#  Used by:    services/task_lifecycle.py

import json
import logging

from backend.models.enums import VerificationResult
from backend.services.llm_router import call_llm
from backend.utils.json_utils import extract_json_object

logger = logging.getLogger("orchestration.verifier")

_VERIFICATION_PROMPT = """\
You are a task output verifier. Given a task description and the output produced,
assess whether the output is acceptable.

<criteria>
1. Substantiveness: Is the output real content, or is it empty/stub/placeholder?
2. Relevance: Does the output address the task description?
3. Completeness: Does the output cover the key aspects of what was asked?
</criteria>

<verdict_rules>
- "passed": Output is substantive, relevant, and reasonably complete.
- "gaps_found": Output is empty, a stub, placeholder, off-topic, or missing key aspects.
  The task should be retried with feedback.
- "human_needed": Output has fundamental issues that require human judgment
  (e.g., ambiguous requirements, conflicting instructions, needs domain expertise).
</verdict_rules>

Respond with ONLY a JSON object (no markdown):
{
  "verdict": "passed" | "gaps_found" | "human_needed",
  "notes": "Brief explanation of your assessment"
}
"""


async def verify_output(
    task_title: str,
    task_description: str,
    output_text: str,
    *,
    budget,
    project_id: str,
    task_id: str,
) -> dict:
    """Verify task output quality using call_llm (CLI providers / Ollama).

    Routes through llm_router with task_type="simple" so it prefers
    Gemini > Ollama > Codex — cheap/free providers suitable for classification.

    Args:
        task_title: The task's title.
        task_description: What the task was supposed to do.
        output_text: The actual output produced.
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

    user_msg = (
        f"## Task: {task_title}\n\n"
        f"### Description\n{task_description}\n\n"
        f"### Output\n{truncated}"
    )

    try:
        llm_response = await call_llm(
            _VERIFICATION_PROMPT,
            user_msg,
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
