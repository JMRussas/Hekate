#  Orchestration Engine - Planner
#
#  Uses Claude to generate structured plans from requirements.
#
#  Depends on: backend/config.py, services/model_router.py, utils/json_utils.py
#  Used by:    routes/projects.py, container.py

import json
import logging
import time
import uuid
from typing import Optional

from backend.config import PLANNING_MODEL
from backend.exceptions import NotFoundError, PlanParseError
from backend.models.enums import PlanningRigor, PlanStatus, ProjectStatus
from backend.services.llm_router import call_llm
from backend.utils.json_utils import extract_json_object, parse_requirements

logger = logging.getLogger("orchestration.planner")

# Backward-compat alias for external importers
_extract_json_object = extract_json_object


# ---------------------------------------------------------------------------
# Planner system prompt: preamble (shared) + rigor-specific suffix
# ---------------------------------------------------------------------------

_PLANNING_PREAMBLE = """You are a project planner for an AI orchestration engine. Your job is to analyze requirements and produce a structured execution plan.

Requirements are numbered [R1], [R2], etc. for traceability.

<task_guidelines>
- Break work into small, focused tasks. Each task should be completable in a single AI conversation.
- Keep task descriptions self-contained — include enough context for a fresh AI instance.
- Use "depends_on" to reference task indices (0-based) for ordering dependencies.
- Prefer simple tasks when possible — they use cheaper models.
- Use task_type "research" for information gathering that can run on a free local model.
- Use task_type "analysis" for summarization/comparison that can run locally.
- Use task_type "asset" for image/visual generation (uses ComfyUI).
- Use task_type "code" for writing code or technical implementation.
- Use task_type "integration" for combining outputs from other tasks.
- Use task_type "documentation" for writing docs, READMEs, etc.
- Order tasks so independent work can run in parallel.
- Map each task to the requirement IDs it satisfies using requirement_ids.
- Include verification_criteria: a concrete check to confirm task completion.
- Include affected_files: list of files this task will create or modify (best guess).
</task_guidelines>

<available_tools>
- search_knowledge: Semantic search across code and documentation RAG databases
- lookup_type: Exact keyword/type name lookup in RAG databases
- local_llm: Free local LLM for drafts, summaries, sub-tasks
- generate_image: Queue image generation via ComfyUI
- read_file: Read files from the project workspace
- write_file: Write files to the project workspace
</available_tools>

"""

_TASK_SCHEMA = """{
      "title": "Short task title",
      "description": "Detailed description...",
      "task_type": "code|research|analysis|asset|integration|documentation",
      "complexity": "simple|medium|complex",
      "depends_on": [],
      "tools_needed": ["search_knowledge", "lookup_type", "local_llm", "generate_image", "read_file", "write_file"],
      "requirement_ids": ["R1", "R3"],
      "verification_criteria": "How to verify this task was completed correctly",
      "affected_files": ["src/auth.ts", "db/schema.sql"]
    }"""

_RIGOR_SUFFIX_L1 = f"""Produce a JSON plan with this exact structure:
{{
  "summary": "Brief summary of what will be built",
  "tasks": [
    {_TASK_SCHEMA}
  ]
}}

- Aim for 3-15 tasks. Too few means tasks are too large; too many means overhead.

Respond with ONLY the JSON plan, no markdown fences or explanation."""

_RIGOR_SUFFIX_L2 = f"""Produce a JSON plan organized into phases. Each phase groups related tasks into a logical stage of work.

{{
  "summary": "Brief summary of what will be built",
  "phases": [
    {{
      "name": "Phase name (e.g. 'Foundation', 'Core Logic', 'Integration')",
      "description": "What this phase accomplishes and why it comes at this point",
      "tasks": [
        {_TASK_SCHEMA}
      ]
    }}
  ],
  "open_questions": [
    {{
      "question": "An ambiguity or decision in the requirements",
      "proposed_answer": "How you propose to handle it",
      "impact": "What changes if the answer differs"
    }}
  ]
}}

Phase guidelines:
- Group related tasks into 2-5 phases that represent logical stages of work.
- Name phases clearly: "Research & Discovery", "Core Implementation", "Integration & Testing", etc.
- Earlier phases should have no dependencies on later phases.
- depends_on indices are GLOBAL across all phases (0-based from the first task in the first phase).
- Aim for 3-15 total tasks across all phases.

Open questions:
- Surface 1-5 ambiguities, assumptions, or decisions that could affect the plan.
- Each must include a proposed_answer so the user can approve or override quickly.

Respond with ONLY the JSON plan, no markdown fences or explanation."""

_RIGOR_SUFFIX_L3 = f"""Produce a thorough JSON plan organized into phases with risk analysis and test strategy.

{{
  "summary": "Brief summary of what will be built",
  "phases": [
    {{
      "name": "Phase name (e.g. 'Foundation', 'Core Logic', 'Integration')",
      "description": "What this phase accomplishes and why it comes at this point",
      "tasks": [
        {_TASK_SCHEMA}
      ]
    }}
  ],
  "open_questions": [
    {{
      "question": "An ambiguity or decision in the requirements",
      "proposed_answer": "How you propose to handle it",
      "impact": "What changes if the answer differs"
    }}
  ],
  "risk_assessment": [
    {{
      "risk": "Description of a technical or schedule risk",
      "likelihood": "low|medium|high",
      "impact": "low|medium|high",
      "mitigation": "How to reduce or handle this risk"
    }}
  ],
  "test_strategy": {{
    "approach": "Overall testing approach description",
    "test_tasks": ["Task titles that represent test/verification work"],
    "coverage_notes": "What areas need testing and how"
  }}
}}

Phase guidelines:
- Group related tasks into 2-5 phases that represent logical stages of work.
- Name phases clearly: "Research & Discovery", "Core Implementation", "Integration & Testing", etc.
- Earlier phases should have no dependencies on later phases.
- depends_on indices are GLOBAL across all phases (0-based from the first task in the first phase).
- Aim for 5-15 total tasks across all phases.

Open questions:
- Surface 1-5 ambiguities, assumptions, or decisions that could affect the plan.
- Each must include a proposed_answer so the user can approve or override quickly.

Risk assessment:
- Identify 2-5 technical, integration, or scope risks.
- Be concrete — reference specific requirements or tasks.

Test strategy:
- Describe the overall approach to verifying the work.
- Reference specific tasks that perform testing/verification.
- Note coverage gaps the user should be aware of.

You may optionally begin your response with a <thinking> block to reason through dependencies, risks, and trade-offs before producing the plan. After your reasoning (if any), output the JSON plan with no markdown fences."""

_RIGOR_SUFFIXES = {
    PlanningRigor.L1: _RIGOR_SUFFIX_L1,
    PlanningRigor.L2: _RIGOR_SUFFIX_L2,
    PlanningRigor.L3: _RIGOR_SUFFIX_L3,
}


def _build_system_prompt(rigor: PlanningRigor) -> str:
    """Build the full system prompt for the given planning rigor level."""
    return _PLANNING_PREAMBLE + _RIGOR_SUFFIXES[rigor]


# ---------------------------------------------------------------------------
# C# Reflection-based decomposition strategy
# ---------------------------------------------------------------------------

_CSHARP_PLANNING_PREAMBLE = """You are a C# code architect for an AI orchestration engine. Your job is to decompose a feature request into method-level implementation tasks using reflected type metadata from the target assembly.

You will receive:
1. The feature requirements (numbered [R1], [R2], etc.)
2. A reflected type map showing existing classes, methods, properties, and constructors from the .NET assembly.

<strategy>
- Each task implements exactly ONE method body. The method signature is already defined.
- Tasks are organized into phases, one phase per class being modified or created.
- Each task receives: the target method signature, injected dependencies (constructor params), and available sibling methods.
- The AI worker will output ONLY the method body — no class wrapper, no using statements.
- A final assembly task per class stitches method bodies into the class file and runs dotnet build.
- Keep each method under 50 lines of logic. If a method needs more, split into private helpers and add those as separate tasks.
</strategy>

<rules>
- Use the reflected type map strictly. Do not invent classes or interfaces that don't exist.
- If a new class is needed, create a "scaffold" task that generates the class shell first.
- Map depends_on to the task indices (0-based, global across phases) of methods that must complete before this one.
- For new methods on existing classes, include the existing method signatures in available_methods.
- For methods that modify shared state, note potential concurrency concerns in the description.
</rules>

"""

_CSHARP_TASK_SCHEMA = """{
      "title": "ClassName.MethodName",
      "description": "What this method does, including behavioral contract and edge cases",
      "task_type": "csharp_method",
      "complexity": "simple|medium|complex",
      "depends_on": [],
      "target_class": "Namespace.ClassName",
      "target_signature": "public async Task<bool> MethodName(ParamType param)",
      "available_methods": ["signatures of other methods in the same class or injected services"],
      "constructor_params": ["IDbContext db", "ILogger logger"],
      "requirement_ids": ["R1"],
      "verification_criteria": "How to verify this method works correctly",
      "affected_files": ["src/Services/MyService.cs"]
    }"""

_CSHARP_RIGOR_SUFFIX = f"""Produce a JSON plan organized into phases. Each phase corresponds to one class being modified or created.

{{
  "summary": "Brief summary of the feature being implemented",
  "phases": [
    {{
      "name": "ClassName (e.g. 'UserService', 'OrderValidator')",
      "description": "What this class does and why these methods are needed",
      "tasks": [
        {_CSHARP_TASK_SCHEMA}
      ]
    }}
  ],
  "open_questions": [
    {{
      "question": "An ambiguity or decision in the requirements",
      "proposed_answer": "How you propose to handle it",
      "impact": "What changes if the answer differs"
    }}
  ],
  "assembly_config": {{
    "new_files": ["Paths to new .cs files that need to be created"],
    "modified_files": ["Paths to existing .cs files that will be modified"]
  }}
}}

Phase guidelines:
- One phase per class. Phase name = class name.
- Within a phase, order tasks so independent methods come first.
- depends_on indices are GLOBAL across all phases (0-based from the first task in the first phase).
- After all method tasks in a phase, the system will auto-create an assembly task to stitch and build.

Open questions:
- Surface 1-5 ambiguities about the requirements or existing code structure.

Respond with ONLY the JSON plan, no markdown fences or explanation."""


def _build_csharp_system_prompt(type_map: str) -> str:
    """Build the system prompt for C# reflection-based planning."""
    return (
        _CSHARP_PLANNING_PREAMBLE
        + f"<reflected_types>\n{type_map}\n</reflected_types>\n\n"
        + _CSHARP_RIGOR_SUFFIX
    )


class PlannerService:
    """Injectable service that generates plans from project requirements."""

    def __init__(self, *, db, budget, tool_registry=None):
        self._db = db
        self._budget = budget
        self._tool_registry = tool_registry

    async def _get_csharp_type_map(self, config: dict) -> str | None:
        """Run .NET reflection to get the type map for C# planning.

        Reads assembly_path or csproj_path from project config.
        Returns formatted type map string, or None if reflection fails/unavailable.
        """
        assembly_path = config.get("assembly_path")
        csproj_path = config.get("csproj_path")

        if not assembly_path and not csproj_path:
            logger.warning("csharp_reflection strategy requires assembly_path or csproj_path in config")
            return None

        try:
            from backend.tools.dotnet_reflection import (
                build_project,
                format_type_map,
                reflect_assembly,
            )

            # Build from csproj if needed
            if csproj_path and not assembly_path:
                success, result = await build_project(csproj_path)
                if not success:
                    logger.warning("C# build failed: %s", result)
                    return None
                assembly_path = result

            ns_filter = config.get("namespace_filter")
            data = await reflect_assembly(assembly_path, ns_filter)
            return format_type_map(data)
        except Exception as e:
            logger.warning("C# reflection failed, falling back to generic planner: %s", e)
            return None

    async def generate(
        self,
        project_id: str,
        provider: Optional[str] = None,
        client=None,  # Deprecated — kept for backward compat, ignored
    ) -> dict:
        """Generate a structured plan for a project using CLI providers.

        Routes through llm_router (CLI subprocess) instead of Anthropic API.
        Zero cost on subscription billing.

        Args:
            project_id: The project to plan for.
            provider: Optional explicit provider (gemini, claude, codex). Defaults to fallback chain.

        Returns the plan dict and updates the database.
        """
        db = self._db

        # Get project
        row = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not row:
            raise NotFoundError(f"Project {project_id} not found")

        requirements = row["requirements"]
        project_name = row["name"]

        # Read planning rigor from project config
        config = json.loads(row["config_json"]) if row["config_json"] else {}
        rigor_str = config.get("planning_rigor", "L2")
        try:
            rigor = PlanningRigor(rigor_str)
        except ValueError:
            rigor = PlanningRigor.L2

        # Check for C# reflection decomposition strategy
        decomposition_strategy = config.get("decomposition_strategy")
        csharp_type_map = None
        if decomposition_strategy == "csharp_reflection":
            csharp_type_map = await self._get_csharp_type_map(config)

        if csharp_type_map is not None:
            system_prompt = _build_csharp_system_prompt(csharp_type_map)
        else:
            system_prompt = _build_system_prompt(rigor)

        # Update project status
        await db.execute_write(
            "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
            (ProjectStatus.PLANNING, time.time(), project_id),
        )

        # Number requirements for traceability (paragraph-based splitting)
        req_blocks = parse_requirements(requirements)
        if req_blocks:
            numbered = "\n".join(f"[R{i+1}] {block}" for i, block in enumerate(req_blocks))
        else:
            numbered = requirements
        user_msg = f"Project: {project_name}\n\nRequirements:\n{numbered}"

        try:
            llm_response = await call_llm(
                system_prompt,
                user_msg,
                provider=provider,
                task_type="planning",
            )

            response_text = llm_response.text
            if not response_text:
                raise PlanParseError("LLM returned an empty response")

            # Parse the plan JSON
            try:
                plan_data = json.loads(response_text)
            except json.JSONDecodeError:
                plan_data = extract_json_object(response_text)
                if plan_data is None:
                    raise PlanParseError(
                        f"Failed to parse plan JSON from {llm_response.provider} response"
                    )

        except Exception:
            # Reset project status so it's not stuck in PLANNING
            await db.execute_write(
                "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
                (ProjectStatus.DRAFT, time.time(), project_id),
            )
            raise

        # Determine plan version
        version_row = await db.fetchone(
            "SELECT COALESCE(MAX(version), 0) as v FROM plans WHERE project_id = ?",
            (project_id,),
        )
        version = (version_row["v"] if version_row else 0) + 1

        # Supersede any previous draft plans
        await db.execute_write(
            "UPDATE plans SET status = ? WHERE project_id = ? AND status = ?",
            (PlanStatus.SUPERSEDED, project_id, PlanStatus.DRAFT),
        )

        # Store the plan — cost is $0 on subscription billing
        plan_id = uuid.uuid4().hex[:12]
        model_used = f"{llm_response.provider}/{llm_response.model or 'default'}"
        now = time.time()
        await db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, prompt_tokens, "
            "completion_tokens, cost_usd, plan_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, project_id, version, model_used, 0, 0, 0.0,
             json.dumps(plan_data), PlanStatus.DRAFT, now),
        )

        # Update project status back to draft (awaiting approval)
        await db.execute_write(
            "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
            (ProjectStatus.DRAFT, time.time(), project_id),
        )

        return {
            "plan_id": plan_id,
            "version": version,
            "plan": plan_data,
            "model_used": model_used,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
        }


async def generate_plan(
    project_id: str,
    *,
    db,
    budget,
    provider: Optional[str] = None,
    client=None,  # Deprecated — ignored
) -> dict:
    """Convenience wrapper for backward compatibility with tests and direct callers."""
    return await PlannerService(db=db, budget=budget).generate(project_id, provider=provider)
