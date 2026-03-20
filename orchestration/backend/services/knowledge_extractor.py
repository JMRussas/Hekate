#  Orchestration Engine - Knowledge Extractor
#
#  Extracts reusable findings from completed task output using Haiku.
#  Findings are project-scoped and deduplicated by content hash.
#
#  Depends on: config.py, models/enums.py, db/connection.py,
#              services/model_router.py, utils/json_utils.py
#  Used by:    services/task_lifecycle.py

import hashlib
import json
import logging
import time
import uuid

from backend.config import (
    API_TIMEOUT,
    KNOWLEDGE_EXTRACTION_MAX_TOKENS,
    KNOWLEDGE_EXTRACTION_MODEL,
    KNOWLEDGE_MIN_OUTPUT_LENGTH,
)
from backend.models.enums import ConfidenceLevel, FindingCategory
from backend.services.model_router import calculate_cost
from backend.services.prompt_renderer import PromptSpec
from backend.utils.json_utils import extract_json_object

logger = logging.getLogger("orchestration.knowledge")

_VALID_CATEGORIES = {c.value for c in FindingCategory}
_VALID_CONFIDENCE = {c.value for c in ConfidenceLevel}

# Cap task output sent to extraction model to control cost
_MAX_OUTPUT_CHARS = 4000

_EXTRACTOR_IDENTITY = (
    "You are a causal reasoning analyst specialising in software engineering post-mortems. "
    "Given a task description and its execution output, extract findings that explain "
    "WHY approaches succeeded or failed — not just what happened. "
    "Every finding must include the causal chain: what was attempted, what outcome occurred, "
    "and what root cause drove that outcome. "
    "Your findings should help future tasks avoid repeated mistakes and replicate successes."
)

_EXTRACTOR_CONSTRAINTS = [
    "Focus on CAUSATION: why did something work or fail? What was the root cause?",
    "For each finding, answer: What was tried? Did it work? WHY did it work or fail?",
    "If an approach failed, capture the failure mode, the root cause, and what was learned.",
    "If an approach succeeded, capture what made it work, what preconditions were required, and whether the success is reproducible under different conditions.",
    "Categories: constraint, decision, discovery, reference, gotcha, architecture.",
    "Only extract findings that are REUSABLE — skip task-specific implementation details.",
    "Each finding should be self-contained (understandable without reading the full output).",
    "If there are NO reusable findings, return an empty array.",
    "Keep each finding concise (1-3 sentences).",
    'For "rationale": explain the causal chain — what was the trigger, what was the mechanism, and what was the effect. Frame it as actionable guidance for future tasks. Never write just "important" or "useful". Write "Unknown — insufficient evidence in output" if truly unknown.',
    'For "alternatives_considered": describe other approaches that were tried or rejected, and why they were abandoned. Include what trade-offs were evaluated. If none apparent, use empty string.',
    'For "confidence": high = directly observed cause-and-effect in this execution output, medium = inferred from output patterns or partial stack traces, low = speculative based on circumstantial evidence.',
]

_EXTRACTOR_OUTPUT_SCHEMA = """\
{"findings": [{"category": "constraint|decision|discovery|reference|gotcha|architecture", \
"content": "What was found — the observable fact (1-3 sentences)", \
"rationale": "Causal chain: trigger → mechanism → effect. Why this matters for future tasks", \
"alternatives_considered": "Other approaches tried/rejected and why they were abandoned", \
"confidence": "high|medium|low"}]}"""


def _build_extraction_spec(user_msg: str) -> PromptSpec:
    """Build a PromptSpec for knowledge extraction."""
    return PromptSpec(
        role="knowledge_extractor",
        identity=_EXTRACTOR_IDENTITY,
        task_description=user_msg,
        constraints=_EXTRACTOR_CONSTRAINTS,
        output_schema=_EXTRACTOR_OUTPUT_SCHEMA,
        output_format="json",
        suppressions=["Do not include markdown fences."],
    )


async def extract_knowledge(
    *,
    task_title: str,
    task_description: str,
    output_text: str,
    client,
    budget,
    project_id: str,
    task_id: str,
    db,
) -> list[dict]:
    """Extract reusable findings from task output and persist them.

    Returns list of newly created finding dicts (may be empty).
    Never raises — returns [] on any failure.
    """
    if not output_text or len(output_text.strip()) < KNOWLEDGE_MIN_OUTPUT_LENGTH:
        return []

    # Skip extraction if budget is exhausted — task output is already paid for
    if not await budget.can_spend(0.001):
        logger.warning("Budget exhausted, skipping knowledge extraction for task %s", task_id)
        return []

    try:
        return await _do_extract(
            task_title=task_title,
            task_description=task_description,
            output_text=output_text,
            client=client,
            budget=budget,
            project_id=project_id,
            task_id=task_id,
            db=db,
        )
    except Exception as e:
        logger.warning("Knowledge extraction failed for task %s: %s", task_id, e)
        return []


async def _do_extract(
    *,
    task_title,
    task_description,
    output_text,
    client,
    budget,
    project_id,
    task_id,
    db,
) -> list[dict]:
    user_msg = (
        f"## Task: {task_title}\n\n"
        f"### Description\n{task_description}\n\n"
        f"### Execution Output\n{output_text[:_MAX_OUTPUT_CHARS]}\n\n"
        f"### Extraction Focus\n"
        f"Analyze the output above for causal insights. For each finding:\n"
        f"1. What approach was tried and what was the outcome (success/failure)?\n"
        f"2. WHY did it succeed or fail? Identify the root cause, not just the symptom.\n"
        f"3. What constraints, preconditions, or environmental factors were discovered?\n"
        f"4. What should future tasks know to avoid repeating this mistake or replicate this success?\n"
        f"5. Were alternative approaches considered or attempted? Why were they chosen or rejected?"
    )

    spec = _build_extraction_spec(user_msg)

    if client:
        # Use Anthropic SDK directly when API key is available
        from backend.services.prompt_renderer import render_prompt
        rendered = render_prompt(spec, "claude")
        response = await client.messages.create(
            model=KNOWLEDGE_EXTRACTION_MODEL,
            max_tokens=KNOWLEDGE_EXTRACTION_MAX_TOKENS,
            system=rendered.system_prompt,
            messages=[{"role": "user", "content": rendered.user_message}],
            timeout=API_TIMEOUT,
        )

        pt = response.usage.input_tokens
        ct = response.usage.output_tokens
        cost = calculate_cost(KNOWLEDGE_EXTRACTION_MODEL, pt, ct)

        await budget.record_spend(
            cost_usd=cost,
            prompt_tokens=pt,
            completion_tokens=ct,
            provider="anthropic",
            model=KNOWLEDGE_EXTRACTION_MODEL,
            purpose="knowledge_extraction",
            project_id=project_id,
            task_id=task_id,
        )

        raw = "".join(block.text for block in response.content if block.type == "text")
    else:
        # No API key — fall back to CLI providers via llm_router (re-renders per provider)
        from backend.services.llm_router import call_llm
        llm_response = await call_llm(
            spec=spec, task_type="simple",
        )
        raw = llm_response.text

        await budget.record_spend(
            cost_usd=llm_response.cost_usd,
            prompt_tokens=llm_response.prompt_tokens,
            completion_tokens=llm_response.completion_tokens,
            provider=llm_response.provider or "unknown",
            model=llm_response.model or "default",
            purpose="knowledge_extraction",
            project_id=project_id,
            task_id=task_id,
        )

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, AttributeError):
        # Fallback: extract JSON from markdown fences / trailing commas
        parsed = extract_json_object(raw)

    if not parsed or not isinstance(parsed, dict):
        logger.debug("Could not parse knowledge extraction response: %s", raw[:200])
        return []

    findings = parsed.get("findings", [])

    if not isinstance(findings, list):
        return []

    created = []
    now = time.time()
    for f in findings:
        content = (f.get("content") or "").strip()
        category = f.get("category", "discovery")
        if not content:
            continue
        if category not in _VALID_CATEGORIES:
            category = "discovery"

        rationale = (f.get("rationale") or "").strip()
        alternatives = (f.get("alternatives_considered") or "").strip()
        confidence = (f.get("confidence") or "medium").strip().lower()
        if confidence not in _VALID_CONFIDENCE:
            confidence = "medium"

        content_hash = hashlib.sha256(content.lower().encode()).hexdigest()[:32]
        finding_id = uuid.uuid4().hex[:12]

        try:
            await db.execute_write(
                "INSERT INTO project_knowledge "
                "(id, project_id, task_id, category, content, content_hash, "
                "rationale, alternatives_considered, confidence, "
                "source_task_title, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
                "ON CONFLICT DO NOTHING",
                (finding_id, project_id, task_id, category, content,
                 content_hash, rationale, alternatives, confidence,
                 task_title, now),
            )
            created.append({
                "id": finding_id,
                "category": category,
                "content": content,
                "rationale": rationale,
                "alternatives_considered": alternatives,
                "confidence": confidence,
            })
        except Exception as e:
            logger.debug("Failed to insert finding: %s", e)

    if created:
        logger.info(
            "Extracted %d finding(s) from task %s (project %s)",
            len(created), task_id, project_id,
        )

    return created
