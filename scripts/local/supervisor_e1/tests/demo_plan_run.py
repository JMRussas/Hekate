"""RUNNABLE DEMONSTRATION of plan-run v0 (plan 042), OFFLINE: a disposable harness database, the FAKE CLI, fake
node tools and temporary git repos. No model and no live database.

    uv run python tests/demo_plan_run.py            # D3 v0: the manual operator handoff
    uv run python tests/demo_plan_run.py --recipe   # D3 v1: a -> b by recipe, ONE run, no operator step

It walks the whole slice and prints each step:
  import a 2-node roadmap slice (a, then b after a)  ->  run_plan: a is claimed, worked, independently verified,
  accepted; b is blocked on its pending spec (a clear operator stop)  ->  OPERATOR: forward a's accepted artifact
  into the source repo, freeze b's spec on it, pin it  ->  run_plan: b is claimed (its receipt pins a's artifact),
  verified, accepted  ->  all_done from the authoritative PlanStore view.
"""

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

from e1 import plan_import as PI  # noqa: E402
from e1 import plan_run as PR  # noqa: E402
from e1.acts import E2C_BOUNDS  # noqa: E402
from e1.acts_durable import ActsJournal, install_acts  # noqa: E402
from e1.durable import install  # noqa: E402
from e1.handoff_durable import install_handoff  # noqa: E402
from e1.harness import Harness  # noqa: E402
from e1.wire import SetupClient, SupervisorClient  # noqa: E402
from task_support import make_repo, make_spec, pinned_node, sha_file, write_spec  # noqa: E402
from test_plan_run import FAKE, PYEXE, b_spec_doc, integrate_and_prepare_b, two_nodes  # noqa: E402


def say(*a):
    print("[plan-run demo]", *a, flush=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="planrun-"))
    cache = tmp / "npm-cache" / "_cacache"
    cache.mkdir(parents=True)
    import os
    os.environ["npm_config_cache"] = str(cache.parent)
    f = make_repo(tmp)
    node = pinned_node(tmp)
    a_doc = make_spec(f, node)
    a_spec = write_spec(a_doc, tmp / "spec-a.json")

    class Fx:
        pass
    fx = Fx()
    fx.f, fx.node, fx.a_doc, fx.a_spec, fx.a_path, fx.tmp = f, node, a_doc, a_spec, (tmp / "spec-a.json").as_posix(), tmp
    h = Harness()
    h.start()
    ok = False
    try:
        install(h.dsn, E2C_BOUNDS)
        install_acts(h.dsn)
        install_handoff(h.dsn)
        setup, client = SetupClient(h.base_url), SupervisorClient(h.base_url)
        recipe_mode = "--recipe" in sys.argv                                  # D3 v1: zero operator handoff
        if recipe_mode:
            from test_plan_run_d3 import chain, recipe_doc, write_recipe
            rpath, rsha = write_recipe(fx, recipe_doc(fx))
            plan = PI.import_plan(setup, h.project_id, chain(fx, rpath, rsha))
        else:
            plan = PI.import_plan(setup, h.project_id, two_nodes(fx))
        say("imported", plan.root, "nodes", plan.node_ids, "applied", list(plan.applied))
        aj = ActsJournal(h.dsn, "plan-run-demo", now=1000.0).open()
        run = lambda: PR.run_plan(plan, tmp / "plan-run", setup=setup, client=client, aj=aj, executable=(PYEXE, FAKE),  # noqa: E731
                                  executable_sha256=sha_file(FAKE), execution_kind="fake-cli", root_go="demo", timeouts=(120, 60, 60),
                                  task_suffix=lambda k: "\nFAKE-SCENARIO: " + ("value_ok" if k == "a" else "other_ok") + "\n")
        try:
            if recipe_mode:
                r = run()
                say("ONE run, no operator step:", r.outcome, r.reason, [(s.key, s.action, s.outcome) for s in r.steps])
                prov = json.loads((tmp / "plan-run" / "b.integration" / "provenance.json").read_text(encoding="utf-8"))
                say(f"b's base {prov['base'][:12]} was derived from a's accepted artifact {prov['predecessor']['artifactRef'][:12]}"
                    f" + the pinned oracle (recipe {prov['recipeSha256'][:12]}); resolved spec {prov['resolvedSpecSha256'][:12]}")
                say("final:", {k: (s["work"], s["acceptance"]) for k, s in r.nodes.items()})
                ok = r.outcome == "all_done"
            else:
                r1 = run()
                say("run 1:", r1.outcome, r1.reason, r1.detail, [(s.key, s.action, s.outcome) for s in r1.steps])
                art = r1.nodes["a"]["artifactRef"]
                ev = json.loads(Path(r1.steps[0].evidence).read_text(encoding="utf-8"))
                ref = next(r.split()[1] for r in ev["ownedRefs"] if r.startswith(art))
                anchor, base = integrate_and_prepare_b(fx, ref, tmp / "plan-run" / "a" / "repo")
                write_spec(b_spec_doc(fx, anchor, base), tmp / "spec-b.json")
                PI.pin_spec(setup, plan, "b", (tmp / "spec-b.json").as_posix())
                say(f"OPERATOR: forwarded a's accepted artifact {art[:12]} into the source repo; b's base {base[:12]} is on it; b's spec pinned")
                r2 = run()
                say("run 2:", r2.outcome, r2.reason, [(s.key, s.action, s.outcome) for s in r2.steps])
                say("final:", {k: (s["work"], s["acceptance"]) for k, s in r2.nodes.items()})
                ok = r2.outcome == "all_done"
        finally:
            aj.close()
    finally:
        h.stop(keep_work=not ok)
    say("RESULT", "PASS" if ok else "FAIL", "(evidence under", tmp, ")")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
