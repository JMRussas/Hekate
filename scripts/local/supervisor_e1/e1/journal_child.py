"""E2b-a child process (TEST-ONLY): owns its OWN database session so the parent can kill it at a
crash point. Reads one JSON command per stdin line and answers one JSON line per command:

  {"op": "open", "dsn": ..., "writer": ..., "now": ...}
  {"op": "append", "root": ..., "key": ..., "kind": ..., "data": {...}}       -> {"ok": seq} only AFTER a confirmed commit
  {"op": "effect", "name": ...}                                             -> {"effect": name} (a fixture marker, no I/O)
  {"op": "hold", "root": ..., "key": ..., "kind": ..., "data": {...}}         -> {"holding": pid} with the record INSERTed
                                                                               but NOT committed; then blocks forever
  {"op": "idle"}                                                            -> {"idle": pid}; then blocks forever

No worker, provider or process launch: effects are markers only.
"""

from __future__ import annotations

import json
import sys
import threading

from e1.durable import DurableJournal
from e1.evidence import JournalRefused


def say(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    dj: DurableJournal | None = None
    for line in sys.stdin:
        cmd = json.loads(line)
        op = cmd["op"]
        try:
            if op == "open":
                dj = DurableJournal(cmd["dsn"], cmd["writer"], now=cmd.get("now", 0.0)).open()
                say({"opened": dj.backend_pid()})
            elif op == "append":
                rec = dj.append(cmd["root"], cmd["key"], cmd["kind"], cmd["data"])
                say({"ok": rec.seq})
            elif op == "effect":
                say({"effect": cmd["name"]})
            elif op == "hold":
                def block(stage, _dj):
                    if stage == "before_commit":
                        say({"holding": _dj.backend_pid()})
                        threading.Event().wait()            # killed here, inside the open transaction
                dj.fault = block
                dj.append(cmd["root"], cmd["key"], cmd["kind"], cmd["data"])
                say({"unexpected": "committed"})
            elif op == "idle":
                say({"idle": dj.backend_pid() if dj else None})
                threading.Event().wait()
        except JournalRefused as e:
            say({"refused": e.code})


if __name__ == "__main__":
    main()
