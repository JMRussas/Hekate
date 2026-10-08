"""Local coordinator CLI (root msgs 1915/1926): CREATE the coordinator's own new database. LOCAL development only.

  create --state-dir D [--api-port N]   LocalStore.create: a NEW guarded hekate_coord_* database in the owned
                                        hekate-local container, its marker, the exclusive locator D/coordinator.json,
                                        PlanStore via the Api, the project row and the journal; then verify and stop.

It never adopts, repairs, migrates or drops a database, and launches no model. Before any effect it refuses:
  state_dir_required | state_dir_in_repo (the locator must never be tracked) | locator_exists | api_port_reserved
  (5108 belongs to the disposable harness) | api_port_invalid | api_port_in_use (checked first, so a busy port never
  leaves a half-created database behind).
A failure AFTER the database exists keeps the database and its locator for operator inspection (plan 043 §5,
review 1907 F3); recovery is: inspect, drop that coordinator database, delete its locator, create anew.
The created store is stopped before exit (its Api and lock); the database is kept. Use it with
`plan_cli run --store local --state-dir D`.

Exit codes: 0 created; 1 create failed after createdb (the database is kept; with no locator the result says a
hekate_coord_* database may exist unnamed); 2 refused before any effect (including no owned container).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from e1 import harness as HZ
from e1 import local_store as LS

PROG = "local_cli"
HARNESS_PORT = 5108
DEFAULT_PORT = 5109


class Refused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(code)
        self.code, self.detail = code, detail


def in_git_work_tree(p: Path) -> Path | None:
    """The enclosing work tree root if `p` (or the nearest existing parent) lies inside one, else None."""
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


def check_create(a: argparse.Namespace) -> tuple[Path, int]:
    if a.state_dir is None:
        raise Refused("state_dir_required", "--state-dir")
    state = a.state_dir.resolve()
    repo = in_git_work_tree(state)
    if repo is not None:
        raise Refused("state_dir_in_repo", {"stateDir": str(state), "workTree": str(repo)})
    if (state / LS.LOCATOR).exists():
        raise Refused("locator_exists", str(state / LS.LOCATOR))
    port = a.api_port
    if port == HARNESS_PORT:
        raise Refused("api_port_reserved", port)
    if not 1024 <= port <= 65535:
        raise Refused("api_port_invalid", port)
    if not HZ._port_free(port):
        raise Refused("api_port_in_use", port)
    return state, port


def create(state: Path, port: int, creator: Callable[..., Any]) -> tuple[int, dict[str, Any]]:
    try:
        store = creator(state, api_port=port)
    except Exception as e:  # noqa: BLE001 -- typed result; a partial database and its locator are KEPT
        kept = (state / LS.LOCATOR).exists()
        first = (str(e).splitlines() or [""])[0]
        if isinstance(e, LS.LocalStoreRefused) and e.code == "container" and not kept:
            return 2, {"refused": "container", "detail": first[:200]}      # before createdb: nothing was created
        return 1, {"outcome": "create_failed", "code": getattr(e, "code", type(e).__name__), "detail": first[:200],
                   "locatorKept": kept, "locator": str(state / LS.LOCATOR) if kept else None,
                   "note": ("the partially created coordinator database and its locator are kept for inspection; to retry, "
                            "drop that database, delete the locator, then create again") if kept else
                           ("a hekate_coord_* database may exist WITHOUT a locator (createdb runs before the marker and the "
                            "locator); list hekate_coord_* databases in the owned container before retrying")}
    try:
        return 0, {"outcome": "created", "db": store.loc.db, "projectId": store.loc.project_id, "apiPort": store.loc.api_port,
                   "stateDir": str(state), "locator": str(state / LS.LOCATOR),
                   "next": f"plan_cli run --store local --state-dir {state.as_posix()} ..."}
    finally:
        store.stop()                                          # the Api and the lock; the database is kept


def main(argv: list[str], *, creator: Callable[..., Any] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=PROG, description="Create the local coordinator's own new database (local development only).")
    ap.add_argument("command", choices=("create",))
    ap.add_argument("--state-dir", type=Path, help="a directory OUTSIDE any git work tree; it receives coordinator.json")
    ap.add_argument("--api-port", type=int, default=DEFAULT_PORT, help=f"the coordinator Api port (default {DEFAULT_PORT}; not {HARNESS_PORT})")
    a = ap.parse_args(argv)
    try:
        state, port = check_create(a)
    except Refused as e:
        print(json.dumps({"refused": e.code, "detail": e.detail}, default=str))
        return 2
    code, out = create(state, port, creator or LS.LocalStore.create)
    print(json.dumps(out, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
