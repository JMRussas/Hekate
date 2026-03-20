#  Orchestration Engine - Step Tree Templates
#
#  Built-in deterministic execution trees for known task patterns.
#  Templates are selected by task_type + complexity during decomposition.
#  Tasks can override these with planner-generated step trees.
#
#  Depends on: models/step_tree.py
#  Used by:    services/decomposer.py

from __future__ import annotations

from typing import Any

from backend.models.step_tree import StepTree


# ---------------------------------------------------------------------------
# Template: Code Task (read → plan → implement → verify)
# ---------------------------------------------------------------------------
# For medium/complex code tasks. The model reads existing code, plans
# its approach, writes the implementation, then self-verifies.

CODE_TASK_TREE: dict[str, Any] = {
    "entry_step_id": "read_context",
    "max_total_steps": 10,
    "variables": {},
    "steps": [
        {
            "id": "read_context",
            "instruction": (
                "Read the files listed in the task description and any files they import or depend on. "
                "Understand the existing code structure, naming conventions, and patterns. "
                "Identify the exact locations where changes need to be made."
            ),
            "inputs": [],
            "outputs": [
                {"name": "existing_code_summary", "description": "Summary of relevant existing code, patterns, and conventions", "required": True},
                {"name": "change_locations", "description": "List of files and locations that need modification", "required": True},
            ],
        },
        {
            "id": "plan_changes",
            "instruction": (
                "Based on the existing code context, plan the exact changes needed. "
                "For each file, describe what will be added, modified, or removed. "
                "Consider edge cases, error handling, and consistency with existing patterns."
            ),
            "inputs": ["existing_code_summary", "change_locations"],
            "outputs": [
                {"name": "change_plan", "description": "Detailed plan of changes per file", "required": True},
                {"name": "risk_notes", "description": "Potential risks or gotchas to watch for", "required": False},
            ],
        },
        {
            "id": "implement",
            "instruction": (
                "Execute the change plan. Write or edit each file using the Write/Edit tools. "
                "Follow the existing code conventions identified in step 1. "
                "Do NOT describe what you would write — actually write the code to disk."
            ),
            "inputs": ["change_plan", "existing_code_summary"],
            "outputs": [
                {"name": "files_written", "description": "List of files that were created or modified", "required": True},
                {"name": "implementation_notes", "description": "Any deviations from the plan and why", "required": False},
            ],
        },
        {
            "id": "verify",
            "instruction": (
                "Read back the files you wrote and verify they are syntactically correct "
                "and consistent with the change plan. Check for: missing imports, "
                "typos in variable names, unclosed brackets, incorrect indentation. "
                "If you find issues, fix them immediately using Edit."
            ),
            "inputs": ["files_written", "change_plan"],
            "outputs": [
                {"name": "verification_result", "description": "PASS or list of issues found and fixed", "required": True},
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Template: Integration Task (gather → merge → validate)
# ---------------------------------------------------------------------------
# For tasks that combine outputs from multiple prior tasks into a
# cohesive whole (e.g., wiring up components, connecting endpoints).

INTEGRATION_TASK_TREE: dict[str, Any] = {
    "entry_step_id": "gather_outputs",
    "max_total_steps": 8,
    "variables": {},
    "steps": [
        {
            "id": "gather_outputs",
            "instruction": (
                "Review the outputs from dependency tasks provided in your inputs. "
                "Identify the interfaces, contracts, and integration points between them. "
                "Note any mismatches or gaps that need to be bridged."
            ),
            "inputs": [],
            "outputs": [
                {"name": "integration_points", "description": "List of interfaces/contracts that need to connect", "required": True},
                {"name": "gaps_found", "description": "Mismatches or missing pieces between component outputs", "required": True},
            ],
        },
        {
            "id": "bridge_gaps",
            "instruction": (
                "For each gap or mismatch identified, write the bridging code — adapters, "
                "type conversions, configuration wiring, import statements, etc. "
                "Use Write/Edit tools to make actual changes."
            ),
            "inputs": ["integration_points", "gaps_found"],
            "outputs": [
                {"name": "files_modified", "description": "List of files touched during integration", "required": True},
                {"name": "wiring_summary", "description": "How components are now connected", "required": True},
            ],
        },
        {
            "id": "validate_integration",
            "instruction": (
                "Read the modified files and trace the integration path end-to-end. "
                "Verify that imports resolve, types match across boundaries, and the "
                "integration points identified in step 1 are all connected. "
                "Fix any issues found."
            ),
            "inputs": ["files_modified", "integration_points"],
            "outputs": [
                {"name": "validation_result", "description": "PASS or list of remaining issues", "required": True},
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Template: Research Task (search → synthesize → report)
# ---------------------------------------------------------------------------
# For research/analysis tasks that need structured information gathering.

RESEARCH_TASK_TREE: dict[str, Any] = {
    "entry_step_id": "search",
    "max_total_steps": 6,
    "variables": {},
    "steps": [
        {
            "id": "search",
            "instruction": (
                "Search for information relevant to the task description. "
                "Use search_knowledge and lookup_type tools if available. "
                "Read relevant source files. Cast a wide net — gather more than you need."
            ),
            "inputs": [],
            "outputs": [
                {"name": "raw_findings", "description": "All relevant information found, with sources", "required": True},
                {"name": "open_questions", "description": "Questions that couldn't be answered from available sources", "required": False},
            ],
        },
        {
            "id": "synthesize",
            "instruction": (
                "Analyze the raw findings. Identify patterns, contradictions, and key insights. "
                "Organize into a structured analysis that directly addresses the task requirements."
            ),
            "inputs": ["raw_findings", "open_questions"],
            "outputs": [
                {"name": "analysis", "description": "Structured analysis addressing the task requirements", "required": True},
                {"name": "recommendations", "description": "Actionable recommendations based on findings", "required": False},
            ],
        },
        {
            "id": "report",
            "instruction": (
                "Produce the final deliverable. Format the analysis and recommendations "
                "into a clear, concise report that downstream tasks can consume."
            ),
            "inputs": ["analysis", "recommendations"],
            "outputs": [
                {"name": "report", "description": "Final research report", "required": True},
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Template registry — maps (task_type, complexity) to step trees
# ---------------------------------------------------------------------------

# Only medium/complex tasks get step trees by default.
# Simple tasks are single-shot — the overhead isn't worth it.
_TEMPLATE_REGISTRY: dict[tuple[str, str], dict[str, Any]] = {
    ("code", "medium"): CODE_TASK_TREE,
    ("code", "complex"): CODE_TASK_TREE,
    ("integration", "medium"): INTEGRATION_TASK_TREE,
    ("integration", "complex"): INTEGRATION_TASK_TREE,
    ("research", "complex"): RESEARCH_TASK_TREE,
    ("analysis", "complex"): RESEARCH_TASK_TREE,
    # Game task types reuse code template
    ("game_code", "medium"): CODE_TASK_TREE,
    ("game_code", "complex"): CODE_TASK_TREE,
    ("game_ui", "medium"): CODE_TASK_TREE,
    ("game_ui", "complex"): CODE_TASK_TREE,
}


def get_template(task_type: str, complexity: str) -> dict[str, Any] | None:
    """Look up a built-in step tree template for a task type + complexity.

    Returns None if no template exists (task runs single-shot).
    """
    return _TEMPLATE_REGISTRY.get((task_type, complexity))


def resolve_step_tree(
    task_data: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve the step tree for a task during decomposition.

    Priority:
    1. Planner-provided steps (task_data["steps"]) — highest priority
    2. Built-in template based on task_type + complexity
    3. None — task runs single-shot

    Returns the step tree dict or None.
    """
    # Planner-provided steps take precedence
    planner_steps = task_data.get("steps")
    if planner_steps and isinstance(planner_steps, list) and len(planner_steps) > 0:
        # Build a StepTree from the planner's step list
        return {
            "entry_step_id": planner_steps[0].get("id", "step_0"),
            "max_total_steps": len(planner_steps) * 2,  # Allow some headroom for branches
            "variables": {},
            "steps": planner_steps,
        }

    # Fall back to built-in template
    task_type = task_data.get("task_type", "")
    complexity = task_data.get("complexity", "simple")
    return get_template(task_type, complexity)
