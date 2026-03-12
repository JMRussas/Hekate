#  Orchestration Engine - Output Verifier
#
#  Verifies task output quality using a cheap model (Haiku).
#  Returns PASSED, GAPS_FOUND, or HUMAN_NEEDED.
#
#  Depends on: backend/config.py, backend/models/enums.py, utils/json_utils.py
#  Used by:    services/task_lifecycle.py

import json
import logging

from backend.config import API_TIMEOUT, VERIFICATION_MAX_TOKENS, VERIFICATION_MODEL
from backend.models.enums import VerificationResult
from backend.services.model_router import calculate_cost
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
    client,
    budget,
    project_id: str,
    task_id: str,
) -> dict:
    """Verify task output quality using a cheap model.

    Args:
        task_title: The task's title.
        task_description: What the task was supposed to do.
        output_text: The actual output produced.
        client: anthropic.AsyncAnthropic instance.
        budget: BudgetManager for recording verification cost.
        project_id: For cost attribution.
        task_id: For cost attribution.

    Returns:
        {"result": VerificationResult, "notes": str, "cost_usd": float}

    Raises:
        RuntimeError: If the budget is exhausted (caller should skip, not crash).
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

    response = await client.messages.create(
        model=VERIFICATION_MODEL,
        max_tokens=VERIFICATION_MAX_TOKENS,
        system=_VERIFICATION_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        timeout=API_TIMEOUT,
    )

    pt = response.usage.input_tokens
    ct = response.usage.output_tokens
    cost = calculate_cost(VERIFICATION_MODEL, pt, ct)

    await budget.record_spend(
        cost_usd=cost,
        prompt_tokens=pt,
        completion_tokens=ct,
        provider="anthropic",
        model=VERIFICATION_MODEL,
        purpose="verification",
        project_id=project_id,
        task_id=task_id,
    )

    # Parse response
    raw = "".join(
        block.text for block in response.content if block.type == "text"
    )

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

    return {"result": result, "notes": notes, "cost_usd": cost}
