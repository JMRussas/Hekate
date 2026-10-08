"""D3 v1 (plan 044; root msgs 1825/1831) OFFLINE: a linear successor whose base is DERIVED from the predecessor's
accepted artifact by a pinned successor-recipe.v0, with no operator step. Fake CLI, disposable harness, tmp repos."""

import copy
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from e1 import plan_import as PI
from e1 import successor as S
from task_support import ORACLE_A, ORACLE_FILES, git, sha, sha_file, source_state, write_spec
from test_plan_run import OTHER_CASES, OTHER_TEST, Recorder, b_spec_doc, doc_bytes, driver, fx, npm_cache  # noqa: F401


def recipe_doc(fx, oracle_extra=(), replace=None) -> dict:
    """b's recipe: the b spec as a TEMPLATE (source = {repo} only) + the pinned oracle bytes."""
    tpl = b_spec_doc(fx, "0" * 40, "0" * 40)
    tpl["source"] = {"repo": fx.f["repo"]}
    other = fx.tmp / "oracle-other.json"
    other.write_text(OTHER_CASES, encoding="utf-8", newline="\n")
    oracle = [{"path": OTHER_TEST, "sha256": sha(OTHER_CASES.encode("utf-8")), "from": other.as_posix(), "replaces": None}]
    for path, text, replaces in oracle_extra:
        f = fx.tmp / f"oracle-{len(oracle)}.json"
        f.write_text(text, encoding="utf-8", newline="\n")
        oracle.append({"path": path, "sha256": sha(text.encode("utf-8")), "from": f.as_posix(), "replaces": replaces})
        tpl["oracle"]["files"].append({"path": path, "sha256": sha(text.encode("utf-8"))})
    return {"version": S.VERSION, "template": tpl, "oracle": oracle}


def write_recipe(fx, doc, name="recipe-b.json") -> tuple[str, str]:
    p = fx.tmp / name
    p.write_bytes(json.dumps(doc, indent=1).encode("utf-8"))
    return p.as_posix(), sha_file(p)


def chain(fx, recipe_path, recipe_sha, title="d3 chain") -> bytes:
    return doc_bytes([{"key": "a", "name": "set value", "spec": {"path": fx.a_path, "sha256": fx.a_spec.sha256}, "after": []},
                      {"key": "b", "name": "set other", "spec": {"recipe": {"path": recipe_path, "sha256": recipe_sha}}, "after": ["a"]}],
                     title=title)


# ------------------------------------------------------------------------ the zero-operator chain

def test_a_then_b_by_recipe_with_no_operator_step(fx, harness, setup, driver, tmp_path):
    path, rsha = write_recipe(fx, recipe_doc(fx))
    before = source_state(fx.f["repo"])
    plan = PI.import_plan(setup, harness.project_id, chain(fx, path, rsha))
    assert plan.applied[-1] == "edge:b<-a"
    r = driver(plan)
    assert (r.outcome, r.reason) == ("all_done", None)
    assert [(s.key, s.action, s.outcome) for s in r.steps] == [("a", "ran", "accepted"), ("b", "ran", "accepted")]
    x = r.nodes["a"]["artifactRef"]
    integ = tmp_path / "plan-run" / "b.integration"
    prov = json.loads((integ / "provenance.json").read_text(encoding="utf-8"))
    spec = json.loads((integ / "spec.resolved.json").read_text(encoding="utf-8"))
    assert spec["source"]["anchorCommit"] == x and spec["source"]["taskBaseCommit"] == prov["base"]
    assert prov["recipeSha256"] == rsha and prov["predecessor"]["artifactRef"] == x
    assert prov["resolvedSpecSha256"] == sha_file(integ / "spec.resolved.json")
    assert git(integ / "repo", "rev-parse", f"{prov['base']}^") == x                     # the base sits on a's artifact
    assert git(integ / "repo", "diff", "--name-status", x, prov["base"]) == f"A\t{OTHER_TEST}"
    assert source_state(fx.f["repo"]) == before                                           # the primary repo is never edited
    # deterministic: re-materializing the same inputs elsewhere reproduces the same base sha
    again = S.materialize(S.load_recipe(path, rsha), rsha, prov["predecessor"] | {"repo": prov["predecessor"]["repo"]}, x,
                          tmp_path / "again", "plan/x/b")
    assert again.doc["source"]["taskBaseCommit"] == prov["base"]


def test_a_replaced_oracle_needs_its_exact_expected_old_blob(fx, harness, setup, driver, tmp_path):
    """M: an existing file may be replaced ONLY when its blob at the artifact is exactly the pinned old one."""
    new_a = ORACLE_FILES[ORACLE_A].replace('"value file loads"', '"value file still loads"')
    good = recipe_doc(fx, oracle_extra=[(ORACLE_A, new_a, sha(ORACLE_FILES[ORACLE_A].encode("utf-8")))])
    path, rsha = write_recipe(fx, good)
    plan = PI.import_plan(setup, harness.project_id, chain(fx, path, rsha, "d3 replace ok"))
    r = driver(plan)
    assert (r.outcome, r.reason) == ("all_done", None)
    prov = json.loads((tmp_path / "plan-run" / "b.integration" / "provenance.json").read_text(encoding="utf-8"))
    names = git(tmp_path / "plan-run" / "b.integration" / "repo", "diff", "--name-status", r.nodes["a"]["artifactRef"], prov["base"])
    assert sorted(names.splitlines()) == sorted([f"A\t{OTHER_TEST}", f"M\t{ORACLE_A}"])


@pytest.mark.parametrize("variant, code", [
    ("new_file_exists", "oracle_conflict"),            # replaces null, but the path exists at the artifact
    ("wrong_old_blob", "oracle_conflict"),             # replaces names a blob that is not the one there
])
def test_an_oracle_conflict_stops_before_any_spend(fx, harness, setup, driver, tmp_path, variant, code):
    new_a = ORACLE_FILES[ORACLE_A].replace('"value file loads"', '"value file still loads"')
    replaces = None if variant == "new_file_exists" else "0" * 64
    path, rsha = write_recipe(fx, recipe_doc(fx, oracle_extra=[(ORACLE_A, new_a, replaces)]))
    plan = PI.import_plan(setup, harness.project_id, chain(fx, path, rsha, f"d3 {variant}"))
    r = driver(plan)
    assert (r.outcome, r.reason) == ("needs_operator", code) and r.detail["detail"]["path"] == ORACLE_A
    assert r.nodes["a"]["acceptance"] == "accepted" and r.nodes["b"]["work"] == "todo"
    assert not (tmp_path / "plan-run" / "b").exists()                                    # no preflight clone, no claim


# ------------------------------------------------------------------------ tamper: recipe, oracle bytes, predecessor ref

def test_a_tampered_recipe_or_oracle_input_stops_before_any_clone(fx, harness, setup, driver, tmp_path):
    path, rsha = write_recipe(fx, recipe_doc(fx))
    plan = PI.import_plan(setup, harness.project_id, chain(fx, path, rsha, "d3 tamper"))
    Path(path).write_bytes(Path(path).read_bytes() + b"\n")                               # the recipe changed after import
    r = driver(plan)
    assert (r.outcome, r.reason) == ("needs_operator", "recipe_tamper") and r.nodes["a"]["acceptance"] == "accepted"
    assert not (tmp_path / "plan-run" / "b.integration").exists() and not (tmp_path / "plan-run" / "b").exists()


def test_tampered_oracle_bytes_stop(fx, harness, setup, driver, tmp_path):
    doc = recipe_doc(fx)
    path, rsha = write_recipe(fx, doc)
    plan = PI.import_plan(setup, harness.project_id, chain(fx, path, rsha, "d3 oracle tamper"))
    Path(doc["oracle"][0]["from"]).write_text('{"cases": []}\n', encoding="utf-8")
    r = driver(plan)
    assert (r.outcome, r.reason) == ("needs_operator", "recipe_tamper") and "from" in r.detail["detail"]


def test_a_moved_predecessor_ref_is_artifact_unavailable(fx, harness, setup, driver, tmp_path):
    doc = recipe_doc(fx)
    path, rsha = write_recipe(fx, doc)
    good = Path(path).read_bytes()
    plan = PI.import_plan(setup, harness.project_id, chain(fx, path, rsha, "d3 moved ref"))
    Path(path).write_bytes(good + b" ")                                                   # run 1 stops at b (a accepted)
    r1 = driver(plan)
    assert (r1.reason, r1.nodes["a"]["acceptance"]) == ("recipe_tamper", "accepted")
    Path(path).write_bytes(good)                                                          # the recipe is fine again
    a_repo = tmp_path / "plan-run" / "a" / "repo"
    ev = json.loads(next((tmp_path / "plan-run" / "a").glob("pilot-*/evidence.json")).read_text(encoding="utf-8"))
    ref = ev["ownedRefs"][0].split()[1]
    git(a_repo, "update-ref", ref, git(a_repo, "rev-parse", f"{r1.nodes['a']['artifactRef']}^"))   # the ref no longer = X
    r2 = driver(plan)
    assert (r2.outcome, r2.reason) == ("needs_operator", "artifact_unavailable") and r2.nodes["b"]["work"] == "todo"


# ------------------------------------------------------------------------ import

def test_a_recipe_node_must_be_linear_and_valid_before_any_write(fx):
    path, rsha = write_recipe(fx, recipe_doc(fx))
    two = json.loads(chain(fx, path, rsha))
    two["nodes"].insert(1, {"key": "z", "name": "z", "spec": None, "after": []})
    two["nodes"][2]["after"] = ["a", "z"]
    rec = Recorder()
    with pytest.raises(PI.ImportRefused) as e:
        PI.import_plan(rec, str(uuid.uuid4()), json.dumps(two).encode("utf-8"))
    assert e.value.code == "import_value" and "LINEAR" in str(e.value.detail) and rec.calls == []
    bad = recipe_doc(fx)
    bad["template"]["source"]["anchorCommit"] = "1" * 40                                  # the template must NOT carry a base
    bpath, bsha = write_recipe(fx, bad, "recipe-bad.json")
    with pytest.raises(PI.ImportRefused) as e:
        PI.import_plan(rec, str(uuid.uuid4()), chain(fx, bpath, bsha))
    assert e.value.code == "recipe_invalid" and e.value.detail["code"] == "recipe_shape" and rec.calls == []
    wrong = recipe_doc(fx)
    wrong["oracle"][0]["sha256"] = "0" * 64                                               # pin != the template's oracle file
    wpath, wsha = write_recipe(fx, wrong, "recipe-wrong.json")
    with pytest.raises(PI.ImportRefused) as e:
        PI.import_plan(rec, str(uuid.uuid4()), chain(fx, wpath, wsha))
    assert e.value.code == "recipe_invalid" and e.value.detail["code"] == "recipe_oracle" and rec.calls == []
