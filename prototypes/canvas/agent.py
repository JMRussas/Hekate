"""Entry point — runs the planning agent against the seed failing-test task.

Most of the logic is in agent_source.py + launch.py. This file just defines
the task and hands an AgentSource to the launcher.

Run:    cd prototypes/canvas
        uv run python agent.py
"""
from __future__ import annotations

from agent_source import AgentSource
from launch import run


TASK = """\
Plan (do not execute) the following work as a graph:

A test is failing. Diagnose the root cause and produce a one-line fix.

Symptom: `pytest test_calc.py` reports `AssertionError: assert -1 == 5` at test_calc.py:2.
Test source: `def test_add(): assert add(2, 3) == 5`.
The implementation in calc.py is unknown to you.

When the plan is complete, call mark_plan_complete.
"""


def main() -> None:
    run(AgentSource(TASK))


if __name__ == "__main__":
    main()
