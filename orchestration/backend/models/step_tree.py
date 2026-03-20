#  Orchestration Engine - Step Tree Schema
#
#  Deterministic execution skeleton for task execution. The tree owns
#  control flow; the model owns judgment at the leaves.
#
#  Three tiers of tree design:
#    1. Fully static — every branch predetermined
#    2. Static with model-evaluated conditions — model picks branches
#    3. Bounded dynamic — model generates sub-steps within template constraints
#
#  Depends on: (none — pure data structures)
#  Used by:    services/tree_runner.py, services/decomposer.py,
#              services/planner.py, services/cli_common.py

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepOutput:
    """A named output variable that a step must produce."""
    name: str
    description: str
    required: bool = True
    # JSON schema fragment for validation (e.g. {"type": "array", "items": {"type": "string"}})
    schema: dict[str, Any] | None = None


@dataclass
class BranchCondition:
    """A condition the model evaluates to pick the next step.

    Conditions are evaluated in order. The first one the model judges
    true wins. If none match, falls through to ``fallback_step_id``.
    """
    condition: str          # Natural language condition for model to evaluate
    target_step_id: str     # Where to go if true


@dataclass
class StepNode:
    """A single node in the execution tree.

    The model executes ``instruction`` and must produce all ``outputs``.
    The tree runner validates outputs, then follows edges to the next step.
    """
    id: str
    instruction: str                            # What the model must do
    inputs: list[str] = field(default_factory=list)   # Variable names from prior steps
    outputs: list[StepOutput] = field(default_factory=list)
    # If None, linear — proceeds to next step in list order
    next_step_id: str | None = None
    # If set, model evaluates conditions to pick branch
    branch_conditions: list[BranchCondition] | None = None
    # Fallback when no branch condition matches
    fallback_step_id: str | None = None
    # For loop nodes — max times this step can execute
    max_iterations: int = 1
    # If True, model can generate sub-steps (bounded dynamic planning)
    allow_substeps: bool = False
    # Template constraint for sub-step generation
    substep_template: str | None = None
    # Max sub-steps the model can generate
    max_substeps: int = 5


@dataclass
class StepTree:
    """The complete execution tree for a task.

    Walked by the tree runner. Each step gets progressive injection —
    only its instruction + typed inputs from prior steps. The model
    never sees the full tree.
    """
    steps: list[StepNode]
    entry_step_id: str
    # The state accumulation object — grows as steps complete
    variables: dict[str, Any] = field(default_factory=dict)
    # Global max steps to prevent runaway execution
    max_total_steps: int = 20

    def get_step(self, step_id: str) -> StepNode | None:
        """Look up a step by ID."""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def step_index(self, step_id: str) -> int:
        """Return the list index of a step, or -1 if not found."""
        for i, step in enumerate(self.steps):
            if step.id == step_id:
                return i
        return -1

    def next_linear_step(self, current_id: str) -> StepNode | None:
        """Return the next step in list order after current_id."""
        idx = self.step_index(current_id)
        if idx < 0 or idx + 1 >= len(self.steps):
            return None
        return self.steps[idx + 1]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict for storage."""
        return {
            "entry_step_id": self.entry_step_id,
            "max_total_steps": self.max_total_steps,
            "variables": self.variables,
            "steps": [
                {
                    "id": s.id,
                    "instruction": s.instruction,
                    "inputs": s.inputs,
                    "outputs": [
                        {
                            "name": o.name,
                            "description": o.description,
                            "required": o.required,
                            **({"schema": o.schema} if o.schema else {}),
                        }
                        for o in s.outputs
                    ],
                    **({"next_step_id": s.next_step_id} if s.next_step_id else {}),
                    **({"branch_conditions": [
                        {"condition": bc.condition, "target_step_id": bc.target_step_id}
                        for bc in s.branch_conditions
                    ]} if s.branch_conditions else {}),
                    **({"fallback_step_id": s.fallback_step_id} if s.fallback_step_id else {}),
                    **({"max_iterations": s.max_iterations} if s.max_iterations != 1 else {}),
                    **({"allow_substeps": True, "substep_template": s.substep_template,
                        "max_substeps": s.max_substeps} if s.allow_substeps else {}),
                }
                for s in self.steps
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepTree:
        """Deserialize from a JSON-compatible dict."""
        steps = []
        for sd in data.get("steps", []):
            outputs = [
                StepOutput(
                    name=o["name"],
                    description=o.get("description", ""),
                    required=o.get("required", True),
                    schema=o.get("schema"),
                )
                for o in sd.get("outputs", [])
            ]
            branch_conditions = None
            if sd.get("branch_conditions"):
                branch_conditions = [
                    BranchCondition(
                        condition=bc["condition"],
                        target_step_id=bc["target_step_id"],
                    )
                    for bc in sd["branch_conditions"]
                ]
            steps.append(StepNode(
                id=sd["id"],
                instruction=sd["instruction"],
                inputs=sd.get("inputs", []),
                outputs=outputs,
                next_step_id=sd.get("next_step_id"),
                branch_conditions=branch_conditions,
                fallback_step_id=sd.get("fallback_step_id"),
                max_iterations=sd.get("max_iterations", 1),
                allow_substeps=sd.get("allow_substeps", False),
                substep_template=sd.get("substep_template"),
                max_substeps=sd.get("max_substeps", 5),
            ))
        return cls(
            steps=steps,
            entry_step_id=data["entry_step_id"],
            variables=data.get("variables", {}),
            max_total_steps=data.get("max_total_steps", 20),
        )

    @classmethod
    def from_json(cls, raw: str) -> StepTree:
        """Parse from a JSON string."""
        return cls.from_dict(json.loads(raw))
