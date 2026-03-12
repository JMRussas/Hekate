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
from backend.models.enums import FindingCategory
from backend.services.model_router import calculate_cost
from backend.utils.json_utils import extract_json_object

logger = logging.getLogger("orchestration.knowledge")

_VALID_CATEGORIES = {c.value for c in FindingCategory}

# Cap task output sent to extraction model to control cost
_MAX_OUTPUT_CHARS = 4000

_EXTRACTION_PROMPT = """\
You are a knowledge extraction assistant. Given a task description and its output,
identify any reusable findings that would help OTHER tasks in the same project.

<finding_categories>
1. Constraints: limitations discovered ("X must be Y", "API limits to N")
2. Decisions: choices made with rationale ("chose X over Y because...")
3. Discoveries: API behavior, library quirks, undocumented features
4. References: useful URLs, documentation pointers, code patterns found
5. Gotchas: things that don't work as expected, pitfalls encountered
6. Architecture: structural choices, data flow patterns, component relationships
</finding_categories>

<rules>
- Only extract findings that are REUSABLE — skip task-specific implementation details.
- Each finding should be self-contained (understandable without reading the full output).
- If there are NO reusable findings, return an empty array.
- Keep each finding concise (1-3 sentences).
</rules>

Respond with ONLY a JSON object (no markdown):
{
  "findings": [
    {"category": "constraint|decision|discovery|reference|gotcha|architecture", "content": "..."},
    ...
  ]
}
"""


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
        f"### Output\n{output_text[:_MAX_OUTPUT_CHARS]}"
    )

    response = await client.messages.create(
        model=KNOWLEDGE_EXTRACTION_MODEL,
        max_tokens=KNOWLEDGE_EXTRACTION_MAX_TOKENS,
        system=_EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
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

        content_hash = hashlib.sha256(content.lower().encode()).hexdigest()[:32]
        finding_id = uuid.uuid4().hex[:12]

        try:
            await db.execute_write(
                "INSERT OR IGNORE INTO project_knowledge "
                "(id, project_id, task_id, category, content, content_hash, "
                "source_task_title, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (finding_id, project_id, task_id, category, content,
                 content_hash, task_title, now),
            )
            created.append({
                "id": finding_id,
                "category": category,
                "content": content,
            })
        except Exception as e:
            logger.debug("Failed to insert finding: %s", e)

    if created:
        logger.info(
            "Extracted %d finding(s) from task %s (project %s)",
            len(created), task_id, project_id,
        )

    return created
