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

from backend.config import PLANNING_MODEL, cfg
from backend.exceptions import BudgetExhaustedError, NotFoundError, PlanParseError
from backend.models.enums import (
    PlanningRigor,
    PlanStatus,
    ProjectStatus,
    ReassessmentOutcome,
)
from backend.models.schemas import ReassessmentResult, WaveReassessmentContext
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
- For Alembic migrations (in backend/migrations/versions/):
  - Filename MUST be 'NNN_description.py' where NNN is the next 3-digit number.
  - The `revision_id` variable in the file MUST be the 'NNN' string.
  - The `down_revision` variable MUST match the `revision_id` of the previous migration.
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
- Include rationale: explain WHY this approach was chosen, what alternatives were considered and rejected, and what constraints or dependencies drove the decision. This captures decision context so future tasks and revisions understand the reasoning.
- When a feature has well-known implementation patterns (window resizing, drag-and-drop, undo/redo, virtual scrolling, etc.), plan to replicate the established pattern rather than speculating about difficulty. Reference the standard approach in the task description so the implementer knows what to follow.
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
      "affected_files": ["src/auth.ts", "db/schema.sql"],
      "rationale": "Why this approach was chosen, alternatives considered, and driving constraints"
    }"""

_RIGOR_SUFFIX_L0 = """Produce a high-level roadmap of epics. Do NOT decompose into individual tasks — \
each epic represents a major body of work that will be planned separately at L1-L3 when the user is ready.

{
  "summary": "Brief summary of the overall vision",
  "epics": [
    {
      "title": "Epic title (e.g. 'Authentication System', 'Data Pipeline')",
      "description": "What this epic delivers and why it matters",
      "scope": "What's in scope and what's explicitly out of scope",
      "estimated_complexity": "small|medium|large|xlarge",
      "depends_on": [],
      "success_criteria": "How you know this epic is done"
    }
  ],
  "open_questions": [
    {
      "question": "An ambiguity or decision that affects the roadmap",
      "proposed_answer": "How you propose to handle it",
      "impact": "What changes if the answer differs"
    }
  ]
}

Epic guidelines:
- 3-10 epics that represent major deliverables or milestones.
- Each epic should be independently plannable at L1-L3 later.
- Order by dependency — earlier epics should not depend on later ones.
- depends_on indices are 0-based across all epics.
- estimated_complexity guides effort sizing (small=days, medium=1-2 weeks, large=2-4 weeks, xlarge=month+).

Respond with ONLY the JSON roadmap, no markdown fences or explanation."""

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
    PlanningRigor.L0: _RIGOR_SUFFIX_L0,
    PlanningRigor.L1: _RIGOR_SUFFIX_L1,
    PlanningRigor.L2: _RIGOR_SUFFIX_L2,
    PlanningRigor.L3: _RIGOR_SUFFIX_L3,
}


def _build_system_prompt(rigor: PlanningRigor) -> str:
    """Build the full system prompt for the given planning rigor level."""
    return _PLANNING_PREAMBLE + _RIGOR_SUFFIXES[rigor]


# ---------------------------------------------------------------------------
# Game development planning strategy
# ---------------------------------------------------------------------------

_GAMEDEV_PLANNING_PREAMBLE = """You are a game development architect for an AI orchestration engine. Your job is to decompose game requirements into a multi-sprint development plan.

Requirements are numbered [R1], [R2], etc. for traceability.

<game_dev_strategy>
Games are built in SPRINTS. Each sprint follows a proven Foundation → Features → Content → Polish progression.

Sprint decomposition rules:
- Sprint 1 is always Foundation: project scaffold, basic rendering, player entity, input handling, test level.
- Each subsequent sprint adds ONE major system (combat, inventory, networking, AI, etc.).
- Each sprint has testable exit criteria proving the sprint is done.
- Content sprints (adding items, enemies, levels, etc.) should use game_content task type for bulk data.
- Integration sprints wire systems together — always sequential, always complex tier.
- Polish sprints are parallelizable refinements (audio, VFX, UI animations, accessibility).

Phase structure within each sprint:
- Phase 1 "Infrastructure": Independent pieces, parallelizable across multiple agents.
- Phase 2 "Integration": Wire infrastructure together, sequential, reads Phase 1 outputs.
- Phase 3 "Polish": Independent refinements, parallelizable.

Task assignment heuristics:
- Multi-file architecture, system design, hub wiring → claude_code (game_code complex)
- Data models, config, content definitions, stat blocks → gemini_cli (game_content)
- Isolated utilities, pure algorithms, single-file helpers → codex_cli (game_code simple)
- UI layout, screen implementation → claude_code (game_ui)
- Game design documents, design decisions → gemini_cli (game_design)
- Build verification, integration tests → claude_code (game_build_verify)

Rationale requirement:
- Every task MUST include a rationale field explaining why this approach was chosen, what alternatives were considered and rejected, and what constraints drove the decision.

Anti-patterns to avoid:
- Don't create a single massive "Implement game" task — decompose into focused work units.
- Don't assign UI/rendering tasks to agents without engine knowledge context.
- Don't parallelize changes to the same core file (e.g., Game.cs, main hub class).
- Don't skip the Foundation sprint — it establishes patterns all later sprints depend on.
- Always include a Game Design Document task in Wave 0 of the first sprint.
- Don't assign multi-file architecture work to gemini_cli or codex_cli.
- Don't assign bulk content/data entry to claude_code.
</game_dev_strategy>

<task_types>
- game_design: Game design documents, system design, level design, narrative design
- game_content: Stat blocks, item definitions, enemy data, dialogue, level configs
- game_code: Gameplay code, systems, algorithms, engine integration
- game_ui: UI screens, HUD, menus, overlays (platform-specific)
- game_build_verify: Build verification, compilation check, integration test
- code: General programming (non-game-specific utilities, tools, scripts)
- research: Information gathering (API docs, engine capabilities, asset formats)
- documentation: READMEs, CLAUDE.md, architecture docs, sprint retrospectives
</task_types>

<available_tools>
- search_knowledge: Semantic search across engine docs, game design patterns, platform APIs
- lookup_type: Exact keyword/type name lookup in RAG databases
- local_llm: Free local LLM for drafts, summaries, sub-tasks
- generate_image: Queue image generation via ComfyUI
- read_file: Read files from the project workspace
- write_file: Write files to the project workspace
</available_tools>

"""

_GAMEDEV_TASK_SCHEMA = """{
      "title": "Short task title",
      "description": "Detailed description with enough context for a fresh AI agent...",
      "task_type": "game_design|game_content|game_code|game_ui|game_build_verify|code|research|documentation",
      "complexity": "simple|medium|complex",
      "depends_on": [],
      "tools_needed": ["search_knowledge", "lookup_type", "local_llm", "generate_image", "read_file", "write_file"],
      "requirement_ids": ["R1", "R3"],
      "verification_criteria": "How to verify this task was completed correctly",
      "affected_files": ["game/Combat.cs", "assets/monsters.json"],
      "rationale": "Why this approach was chosen, alternatives considered, and driving constraints"
    }"""

# Platform-specific context blocks injected into the game dev preamble
_PLATFORM_CONTEXTS = {
    "noz": """<platform_context platform="noz">
Target: NoZ engine (C#/.NET 10, custom 2D engine)
Language: C# only
UI Framework: Immediate-mode (NoZ.UI)
Rendering: WebGPU via SDL3, sprite-based with shader pipeline
Build: dotnet build / dotnet run
Template: Use noz-game-template for scaffolding (cp + setup.py)
Asset Pipeline: Compiled shaders (.wgsl), textures (.png → compiled), sounds (.wav → compiled)
Testing: dotnet test, visual UI capture via tools/ui_capture.py
Key Constraints:
- UI.Scene must be last/only child in its container
- Button IDs must be globally unique per frame
- No LINQ in Update/render hot paths (GC pressure)
- Graphics.Draw only inside UI.Scene callback
- Max 8192 UI elements per frame
- Frame time budget: <11ms
RAG Databases: noz, gamedesign
</platform_context>""",

    "highrise": """<platform_context platform="highrise">
Target: Highrise.game (mobile social metaverse)
Language: Lua only (Highrise custom flavor, NOT standard Lua)
UI Framework: UXML (structure) + USS (styling) + Lua (interactivity)
Rendering: Unity-based 3D, mobile-first, low-poly required
Build: No CLI build — Unity Editor required for upload
Template: Assets/Scripts/ organized by feature, Modules/ for shared utilities

Script Types (--!Type annotation required on every script):
- Server: Runs on server only (Storage, Inventory, Payments access)
- Client: Runs on client only (Audio, Input, UI, PlayerPrefs access)
- ClientAndServer: Split execution (self:ClientAwake/self:ServerUpdate, etc.)
- Module: Shared utility loaded via require("ModuleName")
- UI: UI interactivity with --!Bind variable binding to UXML elements

Networking: Event.new("name") with :FireServer(), :FireClient(), :FireAllClients()
Special Globals: self, client, server, scene, defer()
Annotations: --!Type(), --!SerializeField, --!Bind
Lifecycle: Awake/Start/Update/FixedUpdate/LateUpdate + Client/Server prefixed variants

Services: Audio (client), Chat (both), Input (client), Inventory (server),
Localization (both), Payments (both), PlayerPrefs (client), Storage (server), Time (both), UI (client)

Pre-built Scripts: PlayerCharacterController, ThirdPersonCamera, FirstPersonCamera,
SideScrollerCamera, RTSCamera, PlayMusic, PlaySound, MoveObject, RotateObject

Key Constraints:
- NO C# scripts allowed
- Storage API: server-side only, rate-limited, values under 100KB
- Inventory/Payments: server-side only, rate-limited
- Audio: client-side only
- Mobile-first: optimize for phone performance
- Publishing: manual via Unity Editor + Creator Portal (no CLI)
RAG Databases: highrise, gamedesign
</platform_context>""",

    "noz_continuing": """<platform_context platform="noz_continuing">
Target: Existing NoZ engine project (continuing development)
Language: C# only
IMPORTANT: This is a CONTINUATION of an existing game, not a new project.
- Read existing code before making changes — match established patterns.
- Check CLAUDE.md and .claude/ docs for conventions and gotchas.
- Do NOT restructure the project or change naming conventions.
- New systems must integrate with the existing Game.cs hub pattern.
- Respect existing anti-patterns documentation — check .claude/anti-patterns.md.
- Check .claude/decision-log.md before re-litigating settled architectural decisions.
- Skip the Foundation sprint — the project is already scaffolded.
RAG Databases: noz, gamedesign
</platform_context>""",
}


def _build_gamedev_system_prompt(rigor: PlanningRigor, platform: str | None = None) -> str:
    """Build the full system prompt for game dev planning."""
    prompt = _GAMEDEV_PLANNING_PREAMBLE

    # Inject platform context if specified
    if platform and platform in _PLATFORM_CONTEXTS:
        prompt += _PLATFORM_CONTEXTS[platform] + "\n\n"

    # Use game-dev-specific task schema in the rigor suffix
    rigor_suffix = _RIGOR_SUFFIXES[rigor]
    # Replace the generic task schema with game dev task schema
    rigor_suffix = rigor_suffix.replace(_TASK_SCHEMA, _GAMEDEV_TASK_SCHEMA)

    return prompt + rigor_suffix


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
- Every task MUST include a rationale field explaining why this approach was chosen, what alternatives were considered, and what constraints drove the decision.
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
      "affected_files": ["src/Services/MyService.cs"],
      "rationale": "Why this approach was chosen, alternatives considered, and driving constraints"
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


# ---------------------------------------------------------------------------
# Wave Reassessment (Athena Loop)
# ---------------------------------------------------------------------------

_REASSESSMENT_PROMPT = """You are an expert project manager AI. Your task is to evaluate the progress of a software project after a "wave" of tasks has completed and decide if the project plan needs to be adjusted.

You will be given a JSON object containing:
1. `project_id`: The ID of the project.
2. `wave_number`: The wave number that just finished.
3. `task_outcomes`: A list of tasks in the wave, their status (completed/failed), and a summary of their output.
4. `knowledge_findings`: Discoveries made during the wave (e.g., API limitations, new requirements).
5. `sentinel_observations`: Automated checks and observations about the project state.
6. `original_plan`: The complete original project plan.

Based on this context, you must decide on the next course of action. Your response must be a JSON object with the following structure:
{
  "outcome": "continue_as_planned" | "replan_remaining" | "escalate_to_human",
  "rationale": "A detailed explanation for your decision. Explain what factors led to this outcome.",
  "suggested_changes": ["A list of specific, high-level changes to make if you are recommending a replan."]
}

Possible outcomes:
- `continue_as_planned`: The project is on track. The remaining waves in the original plan are still valid. Use this if the completed wave was successful and no new information invalidates the existing plan.
- `replan_remaining`: The project has deviated significantly, or new information requires a change in direction. The remaining waves should be replanned. Use this if tasks failed, new knowledge invalidates assumptions, or a better path has been discovered. Provide a high-level list of `suggested_changes` for the new plan.
- `escalate_to_human`: The project is in a state that requires human intervention. This could be due to critical failures, unresolvable ambiguities, or a fundamental problem with the project's goals. Clearly explain why human help is needed in the `rationale`.

Analyze the inputs carefully. Are there failed tasks? Do the knowledge findings contradict the plan's assumptions? Are the sentinel observations indicating a problem? Is the project drifting from its original requirements?

Your analysis in the `rationale` is critical. It will be used to inform the human project manager or the replanning AI.

Respond with ONLY the JSON object, with no markdown fences or other text.
"""


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
        comments: Optional[list[dict]] = None,
        previous_plan: Optional[dict] = None,
    ) -> dict:
        """Generate a structured plan for a project using CLI providers.

        Routes through llm_router (CLI subprocess) instead of Anthropic API.
        Zero cost on subscription billing.

        Args:
            project_id: The project to plan for.
            provider: Optional explicit provider (gemini, claude, codex). Defaults to fallback chain.
            comments: Optional list of human review comments ({"author", "content"}) to fold into re-planning.
            previous_plan: Optional previous plan JSON to provide context for revision.

        Returns the plan dict and updates the database.
        """
        db = self._db

        # Get project
        row = await db.fetchone("SELECT * FROM projects WHERE id = $1", (project_id,))
        if not row:
            raise NotFoundError(f"Project {project_id} not found")

        requirements = row["requirements"]
        project_name = row["name"]

        # Budget gate — refuse to plan if budget is already exhausted.
        # Plan generation itself is $0 (CLI subscription billing), but
        # generating a plan leads to task execution which may use paid tiers
        # (Haiku/Sonnet/Opus). Block early to avoid plans the user can't execute.
        # Uses nominal $0.01 because can_spend short-circuits on 0.0.
        _BUDGET_CHECK_ESTIMATE = 0.01
        if not await self._budget.can_spend(_BUDGET_CHECK_ESTIMATE):
            raise BudgetExhaustedError("Global budget limit exceeded")
        if not await self._budget.can_spend_project(project_id, _BUDGET_CHECK_ESTIMATE):
            raise BudgetExhaustedError(
                f"Project {project_id} has exceeded its per-project budget limit"
            )

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
        elif config.get("project_type") == "game_dev":
            platform = config.get("platform")
            system_prompt = _build_gamedev_system_prompt(rigor, platform)
        else:
            system_prompt = _build_system_prompt(rigor)

        # Update project status
        await db.execute_write(
            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
            (ProjectStatus.PLANNING, time.time(), project_id),
        )

        # Number requirements for traceability (paragraph-based splitting)
        req_blocks = parse_requirements(requirements)
        if req_blocks:
            numbered = "\n".join(f"[R{i+1}] {block}" for i, block in enumerate(req_blocks))
        else:
            numbered = requirements
        user_msg = f"Project: {project_name}\n\nRequirements:\n{numbered}"

        # Inject previous plan + human review comments for re-planning
        if previous_plan:
            user_msg += (
                "\n\n<previous_plan>\n"
                "A previous version of this plan was generated and reviewed by a human. "
                "Use it as a starting point — keep what works, fix what was called out.\n"
                f"{json.dumps(previous_plan, indent=2)}\n"
                "</previous_plan>"
            )

        if comments:
            comment_block = "\n".join(
                f"- [{c.get('author', 'reviewer')}]: {c['content']}" for c in comments
            )
            user_msg += (
                "\n\n<human_review_comments>\n"
                "The human reviewer left these comments on the previous plan. "
                "Address each one. If the comment mentions a well-known pattern or solved problem, "
                "plan to replicate the established approach rather than inventing a new one.\n"
                f"{comment_block}\n"
                "</human_review_comments>"
            )

        try:
            llm_response = await call_llm(
                system_prompt,
                user_msg,
                provider=provider,
                model=cfg("llm.planning_model"),
                task_type="planning",
            )

            response_text = llm_response.text
            if not response_text:
                raise PlanParseError("LLM returned an empty response")

            # Record spend for audit trail — $0 on CLI subscription billing
            await self._budget.record_spend(
                cost_usd=0.0,
                prompt_tokens=0,
                completion_tokens=0,
                provider=llm_response.provider or provider or "unknown",
                model=llm_response.model or "default",
                purpose="plan_generation",
                project_id=project_id,
            )

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
                "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
                (ProjectStatus.DRAFT, time.time(), project_id),
            )
            raise

        # Determine plan version
        version_row = await db.fetchone(
            "SELECT COALESCE(MAX(version), 0) as v FROM plans WHERE project_id = $1",
            (project_id,),
        )
        version = (version_row["v"] if version_row else 0) + 1

        # Supersede any previous draft plans
        await db.execute_write(
            "UPDATE plans SET status = $1 WHERE project_id = $2 AND status = $3",
            (PlanStatus.SUPERSEDED, project_id, PlanStatus.DRAFT),
        )

        # Store the plan — cost is $0 on subscription billing
        plan_id = uuid.uuid4().hex[:12]
        model_used = f"{llm_response.provider}/{llm_response.model or 'default'}"
        now = time.time()
        await db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, prompt_tokens, "
            "completion_tokens, cost_usd, plan_json, status, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            (plan_id, project_id, version, model_used, 0, 0, 0.0,
             json.dumps(plan_data), PlanStatus.DRAFT, now),
        )

        # Update project status back to draft (awaiting approval)
        await db.execute_write(
            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
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

    async def evaluate_wave_reassessment(
        self, context: WaveReassessmentContext
    ) -> ReassessmentResult:
        """
        Calls an LLM to evaluate the outcome of a wave and decide on the next step.

        Args:
            context: The context of the completed wave.

        Returns:
            A ReassessmentResult object with the outcome and rationale.
        """
        logger.info(
            "Evaluating wave %d for project %s for reassessment.",
            context.wave_number,
            context.project_id,
        )

        user_message = context.model_dump_json(indent=2)

        try:
            # Using Haiku for this evaluation as it's fast and good at structured JSON output.
            llm_response = await call_llm(
                system_prompt=_REASSESSMENT_PROMPT,
                user_message=user_message,
                provider="claude",  # Assuming 'claude' provider can route to Haiku
                model="haiku",
                task_type="simple",  # This is more of a classification/extraction task
            )

            response_text = llm_response.text
            if not response_text:
                raise PlanParseError("LLM returned an empty response for reassessment.")

            # Record spend for audit trail — $0 on CLI subscription billing
            await self._budget.record_spend(
                cost_usd=0.0,
                prompt_tokens=0,
                completion_tokens=0,
                provider=llm_response.provider or "claude",
                model=llm_response.model or "haiku",
                purpose="wave_reassessment",
                project_id=context.project_id,
            )

            # Parse the JSON response
            try:
                result_data = json.loads(response_text)
            except json.JSONDecodeError:
                result_data = extract_json_object(response_text)
                if result_data is None:
                    raise PlanParseError(
                        f"Failed to parse reassessment JSON from {llm_response.provider} response"
                    )

            # Validate with Pydantic schema
            return ReassessmentResult(**result_data)

        except Exception as e:
            logger.error(
                "Wave reassessment failed for project %s, wave %d: %s",
                context.project_id,
                context.wave_number,
                e,
                exc_info=True,
            )
            # Fallback: if evaluation fails, escalate to human to be safe.
            return ReassessmentResult(
                outcome=ReassessmentOutcome.ESCALATE_TO_HUMAN,
                rationale=(
                    f"The automated wave reassessment process failed with an error: {e}. "
                    "Human review is required to determine the next steps for this project."
                ),
            )


async def generate_plan(
    project_id: str,
    *,
    db,
    budget,
    provider: Optional[str] = None,
    comments: Optional[list[dict]] = None,
    previous_plan: Optional[dict] = None,
) -> dict:
    """Convenience wrapper for backward compatibility with tests and direct callers."""
    return await PlannerService(db=db, budget=budget).generate(
        project_id, provider=provider, comments=comments, previous_plan=previous_plan,
    )
