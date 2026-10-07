"""A FAKE `claude -p` for HK-ISSUE-007 adapter tests (no model, no network). It reads the prompt from
stdin, picks a scenario from a `FAKE-SCENARIO: <name>` line in it, and emits Claude-style stream-json.

It first PROVES the adapter's safety flags reached the process: it exits 3 unless --max-budget-usd,
--max-turns, --permission-mode, --strict-mcp-config with an empty --mcp-config and --allowedTools are
present, and --dangerously-skip-permissions is absent."""

import json
import os
import subprocess
import sys
import time

argv = sys.argv[1:]


def flag(name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


if ("--dangerously-skip-permissions" in argv or flag("--max-budget-usd") is None or flag("--max-turns") is None
        or flag("--permission-mode") not in ("acceptEdits", "default", "plan") or "--strict-mcp-config" not in argv
        or flag("--mcp-config") != '{"mcpServers":{}}' or "--allowedTools" not in argv or "-p" not in argv
        or flag("--tools") is None):
    sys.exit(3)

if os.path.exists("FAKE_MODE") and open("FAKE_MODE", encoding="utf-8").read().strip() == "stdin_block":
    # NEVER read stdin (the supervisor's large prompt must not block it), keep a child alive, never exit.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    with open("child.pid", "w", encoding="utf-8") as f:
        f.write(str(child.pid))
    time.sleep(300)
    sys.exit(0)

prompt = sys.stdin.read()
scenario = next((ln.split(":", 1)[1].strip() for ln in prompt.splitlines() if ln.startswith("FAKE-SCENARIO:")), "happy")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def say(text):
    emit({"type": "assistant", "message": {"role": "assistant", "model": "fake-runtime-model-1",
                                            "content": [{"type": "text", "text": text}]}})


def act(obj):
    return "HEKATE-ACT " + json.dumps(obj)


def init():
    emit({"type": "system", "subtype": "init", "model": flag("--model"), "budget": flag("--max-budget-usd")})


def result():
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "done"})


def edit(name="hello.txt", text="hello\n"):
    with open(name, "w", encoding="utf-8") as f:
        f.write(text)


if scenario == "happy":
    init()
    say("Starting.\n" + act({"kind": "worker_ack", "seq": 1}))
    edit()
    say(act({"kind": "worker_progress", "seq": 2, "checkpointId": 1, "evidence": "wrote hello.txt"}))
    result()
elif scenario.startswith("calc_"):
    # The pilot_real disposable task: implement add() in calc.py. calc_ok | calc_bad | calc_bad_then_ok | calc_touch_test
    rnd = next((int(ln.split(":", 1)[1]) for ln in prompt.splitlines() if ln.startswith("Round:")), 1)
    init()
    say(act({"kind": "worker_ack", "seq": 1}))
    good = scenario == "calc_ok" or (scenario == "calc_bad_then_ok" and rnd >= 2) or scenario == "calc_touch_test"
    edit("calc.py", "def add(a, b):\n    return a + b\n" if good else "def add(a, b):\n    return a - b\n")
    if scenario == "calc_touch_test":
        edit("test_calc.py", "import unittest\n\n\nclass T(unittest.TestCase):\n    pass\n")
    # Run the AUTO-APPROVED test command exactly as configured (Bash(<cmd>) in --allowedTools), like a real
    # worker checking its work: its leftovers must not reach the artifact diff (repro A, msg 1548).
    bash = next((a[len("Bash("):-1] for a in argv if a.startswith("Bash(") and a.endswith(")")), None)
    if bash:
        words = bash.split()
        if words and words[0] == "python":
            words[0] = sys.executable
        subprocess.run(words, capture_output=True, timeout=120)
    say(act({"kind": "worker_progress", "seq": 2, "checkpointId": 1, "evidence": f"edited calc.py round {rnd}"}))
    result()
elif scenario == "no_result":
    init()
    say(act({"kind": "worker_ack", "seq": 1}))
    edit()
elif scenario == "result_error":
    init()
    say(act({"kind": "worker_ack", "seq": 1}))
    edit()
    emit({"type": "result", "subtype": "error_max_budget_usd", "is_error": True})
elif scenario == "result_is_error":
    init()
    say(act({"kind": "worker_ack", "seq": 1}))
    edit()
    emit({"type": "result", "subtype": "success", "is_error": True})
elif scenario == "result_malformed":
    init()
    say(act({"kind": "worker_ack", "seq": 1}))
    edit()
    emit({"type": "result", "subtype": "success", "is_error": "no"})
elif scenario == "result_twice":
    init()
    say(act({"kind": "worker_ack", "seq": 1}))
    edit()
    result()
    result()
elif scenario == "no_ack":
    init()
    edit()
    result()
elif scenario == "quoted":
    init()
    # markers in a tool call, a tool result and a user turn must NOT count
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write",
                                                        "input": {"content": act({"kind": "worker_ack", "seq": 1})}}]}})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": act({"kind": "worker_ack", "seq": 1})}]}})
    emit({"type": "user", "message": {"content": [{"type": "text", "text": act({"kind": "worker_ack", "seq": 1})}]}})
    # malformed assistant markers are refused and counted
    say("HEKATE-ACT {not json\n" + act({"kind": "worker_ack", "seq": 7}) + "\n" + act({"kind": "launched", "seq": 1}))
    edit()
    result()
elif scenario == "no_output":
    time.sleep(120)
elif scenario == "stall":
    init()
    time.sleep(120)
elif scenario == "child":
    init()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    with open("child.pid", "w", encoding="utf-8") as f:
        f.write(str(child.pid))
    say(act({"kind": "worker_ack", "seq": 1}))
    time.sleep(300)
elif scenario == "nonzero":
    init()
    sys.exit(2)
elif scenario == "empty":
    init()
    say(act({"kind": "worker_ack", "seq": 1}))
    result()
elif scenario == "self_commit":
    init()
    say(act({"kind": "worker_ack", "seq": 1}))
    edit()
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.name=w", "-c", "user.email=w@x", "commit", "-qm", "worker commit"], check=True)
    result()
elif scenario == "big_line":
    init()
    sys.stdout.write("x" * (3 << 20) + "\n")
    sys.stdout.flush()
    time.sleep(60)
elif scenario == "stderr_flood":
    init()
    say(act({"kind": "worker_ack", "seq": 1}))
    sys.stderr.write("E" * (1 << 20))
    sys.stderr.flush()
    edit()
    result()
else:
    sys.exit(4)
