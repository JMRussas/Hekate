"""Hard-coded mutation script.

No LLM here. We script the mutations the agent *would* emit, with delays
between them, so we can watch the renderer drive through plan -> execute ->
failure -> repair. If this demo doesn't read well, the substrate is wrong and
we'd be wasting the LLM's time before fixing it.

Run:    cd prototypes/canvas && uv run python demo_static.py
"""
from __future__ import annotations

from graph import Graph
from renderer import CanvasApp


# Each step is (delay_seconds_before, callable). Delays are deliberately
# generous so a viewer can read each transition. Tighten later.
def _script(g: Graph) -> list[tuple[float, callable]]:
    return [
        # ---------- planning phase ----------
        (0.5,  lambda: g.add_node("n1", "tool_call", {"tool": "read_file", "path": "test_calc.py"},
                                  intent="read the failing test to see what's expected")),
        (0.4,  lambda: g.add_node("n2", "tool_call", {"tool": "run_command", "cmd": "pytest test_calc.py -x"},
                                  intent="capture the actual failure")),
        (0.2,  lambda: g.connect("n1", "n2")),
        (0.4,  lambda: g.add_node("n3", "transform", {"shape": "parse traceback"},
                                  intent="extract assertion + file + line from pytest output")),
        (0.2,  lambda: g.connect("n2", "n3", from_port="result")),
        (0.4,  lambda: g.add_node("n4", "tool_call", {"tool": "read_file", "path": "calc.py"},
                                  intent="read the implementation under test")),
        (0.2,  lambda: g.connect("n3", "n4")),
        (0.4,  lambda: g.add_node("n5", "sub_agent", {"role": "diagnose"},
                                  intent="given symptom + impl, name the root cause")),
        (0.2,  lambda: g.connect("n3", "n5")),
        (0.2,  lambda: g.connect("n4", "n5")),
        (0.4,  lambda: g.add_node("n6", "check", {"asks": "does diagnosis explain the assertion?"},
                                  intent="self-check before proposing a fix")),
        (0.2,  lambda: g.connect("n5", "n6")),
        (0.4,  lambda: g.add_node("n7", "transform", {"shape": "one-line fix"},
                                  intent="produce the minimal patch")),
        (0.2,  lambda: g.connect("n6", "n7", from_port="pass")),
        (0.6,  lambda: g.mark_plan_complete()),

        # ---------- execution phase ----------
        (0.8,  lambda: g.set_status("n1", "running")),
        (1.0,  lambda: g.set_status("n1", "done", result="def test_add():\n  assert add(2,3)==5")),

        (0.4,  lambda: g.set_status("n2", "running")),
        (1.2,  lambda: g.set_status("n2", "done", result="AssertionError: assert -1 == 5 at test_calc.py:2")),

        (0.4,  lambda: g.set_status("n3", "running")),
        (0.8,  lambda: g.set_status("n3", "done", result="symptom: add(2,3) returned -1, expected 5")),

        (0.3,  lambda: g.set_status("n4", "running")),
        (0.9,  lambda: g.set_status("n4", "done", result="def add(a,b): return a-b")),

        (0.3,  lambda: g.set_status("n5", "running")),
        (1.4,  lambda: g.set_status("n5", "done", result="impl uses subtraction; should be addition")),

        (0.3,  lambda: g.set_status("n6", "running")),
        # ---------- failure + repair ----------
        (1.0,  lambda: g.set_status("n6", "failed", error="diagnosis is plausible but unverified — no test rerun")),

        # agent repairs the graph in place
        (1.2,  lambda: g.add_node("n8", "tool_call", {"tool": "run_command", "cmd": "python -c 'from calc import add; print(add(2,3))'"},
                                  intent="verify the diagnosis before committing to a fix")),
        (0.3,  lambda: g.connect("n5", "n8")),
        (0.3,  lambda: g.connect("n8", "n6")),

        (0.5,  lambda: g.set_status("n8", "running")),
        (1.0,  lambda: g.set_status("n8", "done", result="-1")),

        (0.3,  lambda: g.set_status("n6", "running")),
        (0.9,  lambda: g.set_status("n6", "done", result="verified: subtraction confirmed as root cause")),

        (0.3,  lambda: g.set_status("n7", "running")),
        (1.0,  lambda: g.set_status("n7", "done", result="def add(a,b): return a+b")),
    ]


class DemoApp(CanvasApp):
    def on_mount(self) -> None:
        # Schedule each mutation at its accumulated wall-clock offset using
        # Textual's native timers. set_timer fires once after `delay` seconds.
        t = 0.0
        for delay, action in _script(self.graph):
            t += delay
            self.set_timer(t, action)


def main() -> None:
    g = Graph()
    DemoApp(g, title="canvas — static demo").run()


if __name__ == "__main__":
    main()
