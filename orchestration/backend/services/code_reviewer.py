#  Orchestration Engine - Code Reviewer
#
#  Reviews task output like a senior developer reviewing a junior's PR.
#  Returns structured feedback with file:line references and a verdict.
#  Used in the execute → review → iterate → commit cycle.
#
#  Depends on: services/llm_router.py, utils/json_utils.py
#  Used by:    services/task_lifecycle.py

import json
import logging

from backend.services.llm_router import call_llm
from backend.utils.json_utils import extract_json_object

logger = logging.getLogger("orchestration.reviewer")

_REVIEW_PROMPT = """\
You are a senior software engineer reviewing code written by a junior developer.
Review the code changes with the same rigor you'd apply to a pull request.

<review_criteria>
1. **Correctness**: Does the code do what the task asks? Are there logic bugs?
2. **Security**: SQL injection, command injection, XSS, hardcoded secrets, unsafe deserialization?
3. **Error handling**: Are failures handled gracefully? Missing null checks, uncaught exceptions?
4. **Edge cases**: Off-by-one errors, empty inputs, concurrent access, resource cleanup?
5. **Code quality**: Dead code, unused imports, unclear naming, overly complex logic?
</review_criteria>

<review_rules>
- Focus on **bugs and security issues**, not style preferences.
- If the code is correct and handles errors properly, approve it. Don't nitpick.
- A task that produces working code with minor style issues should be APPROVED.
- Only request changes for actual bugs, security holes, or missing error handling.
- Be specific: reference the file and describe exactly what's wrong and how to fix it.
</review_rules>

<response_format>
Respond with ONLY a JSON object (no markdown fences):
{
  "verdict": "approved" | "changes_requested",
  "issues": [
    {
      "severity": "error" | "warning",
      "file": "path/to/file.py",
      "description": "What's wrong and how to fix it"
    }
  ],
  "summary": "One-sentence overall assessment"
}

- "approved": Code is correct, safe, and handles errors. Ship it.
- "changes_requested": Found bugs or security issues that must be fixed.
- Only include issues with severity "error" for things that MUST be fixed.
- "warning" issues are suggestions — they don't block approval.
</response_format>
"""


async def review_code(
    task_title: str,
    task_description: str,
    output_text: str,
    diff_text: str | None = None,
    *,
    task_type: str = "",
    iteration: int = 0,
    prior_feedback: str | None = None,
) -> dict:
    """Review task output like a senior dev reviewing a junior's code.

    Args:
        task_title: What the task was supposed to do.
        task_description: Full task description.
        output_text: The agent's output/response text.
        diff_text: Git diff of changes made (if available).
        task_type: Task type for context.
        iteration: Current review iteration (0 = first review).
        prior_feedback: Feedback from previous review iteration.

    Returns:
        {
            "verdict": "approved" | "changes_requested",
            "issues": [...],
            "summary": str,
        }
    """
    # Build the review context
    parts = [f"## Task: {task_title}\n"]
    parts.append(f"**Type**: {task_type or 'code'}\n")
    parts.append(f"### Description\n{task_description}\n")

    if iteration > 0 and prior_feedback:
        parts.append(
            f"### Previous Review Feedback (iteration {iteration})\n"
            f"The developer was asked to fix these issues:\n{prior_feedback}\n"
        )

    # Prefer diff over raw output — it's what a reviewer actually looks at
    _MAX_CHARS = 12000
    if diff_text and diff_text.strip():
        truncated = diff_text[:_MAX_CHARS]
        if len(diff_text) > _MAX_CHARS:
            truncated += "\n\n[... diff truncated ...]"
        parts.append(f"### Code Changes (git diff)\n```\n{truncated}\n```")
    else:
        truncated = (output_text or "(empty)")[:_MAX_CHARS]
        if output_text and len(output_text) > _MAX_CHARS:
            truncated += "\n\n[... output truncated ...]"
        parts.append(f"### Output\n{truncated}")

    user_msg = "\n".join(parts)

    try:
        llm_response = await call_llm(
            _REVIEW_PROMPT,
            user_msg,
            task_type="simple",
        )
    except RuntimeError:
        logger.warning("All LLM providers failed for code review of %s", task_title)
        return {
            "verdict": "approved",
            "issues": [],
            "summary": "Review skipped: all LLM providers unavailable",
        }

    raw = llm_response.text
    parsed = extract_json_object(raw)
    if not parsed:
        logger.warning("Code review returned unparseable response: %s", raw[:200])
        return {
            "verdict": "approved",
            "issues": [],
            "summary": "Review skipped: unparseable LLM response",
        }

    verdict = parsed.get("verdict", "approved")
    if verdict not in ("approved", "changes_requested"):
        verdict = "approved"

    issues = parsed.get("issues", [])
    # Only block on error-severity issues
    error_issues = [i for i in issues if i.get("severity") == "error"]
    if verdict == "changes_requested" and not error_issues:
        verdict = "approved"

    return {
        "verdict": verdict,
        "issues": issues,
        "summary": parsed.get("summary", ""),
    }


def format_review_feedback(review_result: dict) -> str:
    """Format review result into human/LLM-readable feedback for iteration."""
    parts = [f"## Code Review: {review_result['summary']}\n"]

    for issue in review_result.get("issues", []):
        severity = issue.get("severity", "warning").upper()
        file_ref = issue.get("file", "")
        desc = issue.get("description", "")
        parts.append(f"- **[{severity}]** {file_ref}: {desc}")

    return "\n".join(parts)
