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


# ------------------------------------------------------------------------ same-repository lineage (root msg 1860)

def test_a_recipe_targeting_another_repository_is_refused_before_any_clone(fx, harness, setup, driver, tmp_path):
    from task_support import make_repo
    other = make_repo(tmp_path / "elsewhere")
    doc = recipe_doc(fx)
    doc["template"]["source"]["repo"] = other["repo"]
    path, rsha = write_recipe(fx, doc)
    plan = PI.import_plan(setup, harness.project_id, chain(fx, path, rsha, "d3 other repo"))
    r = driver(plan)
    assert (r.outcome, r.reason) == ("needs_operator", "repo_lineage_mismatch")
    from e1 import plan_run as PR
    got = r.detail["detail"]
    assert PR.same_repo(got["recipeRepo"], other["repo"]) and PR.same_repo(got["predecessorRepo"], fx.f["repo"])
    assert r.nodes["a"]["acceptance"] == "accepted" and r.nodes["b"]["work"] == "todo"
    assert not (tmp_path / "plan-run" / "b.integration").exists() and not (tmp_path / "plan-run" / "b").exists()


THIRD_TEST = "tests/unit/third.test.ts"
THIRD_CASES = '{"cases": [{"name": "value is 43", "expect": "43", "source": "src/value.txt"}, ' \
              '{"name": "third loads", "expect": "*", "source": "src/value.txt"}]}\n'


def third_recipe(fx) -> dict:
    """c, after the RECIPE node b: src/value.txt 42 -> 43, its own new oracle; the same original repository."""
    d = recipe_doc(fx)
    tpl = d["template"]
    node = fx.node.as_posix()
    argv = [node, "node_modules/vitest/vitest.mjs", "run", "--reporter=json", THIRD_TEST]
    tpl["task"] = {"text": "Set src/value.txt to 43.", "criteria": "The third oracle passes."}
    tpl["allow"] = [{"path": "src/value.txt", "status": "M", "mode": "100644"}]
    tpl["oracle"]["files"] = [{"path": THIRD_TEST, "sha256": sha(THIRD_CASES.encode("utf-8"))}]
    tpl["oracle"]["baseline"].update(argv=argv, cases=[
        {"file": THIRD_TEST, "fullName": "value is 43", "status": "failed", "failureFirstLine": "AssertionError: expected '42' to be '43'"},
        {"file": THIRD_TEST, "fullName": "third loads", "status": "passed", "failureFirstLine": None}])
    tpl["verify"]["steps"][1]["argv"] = argv
    tpl["worker"]["testCommand"] = " ".join(argv)
    f = fx.tmp / "oracle-third.json"
    f.write_text(THIRD_CASES, encoding="utf-8", newline="\n")
    d["oracle"] = [{"path": THIRD_TEST, "sha256": sha(THIRD_CASES.encode("utf-8")), "from": f.as_posix(), "replaces": None}]
    return d


def test_a_three_node_chain_through_a_recipe_predecessor_in_one_run(fx, harness, setup, driver, tmp_path):
    """c's predecessor b is itself a recipe node: b is bound through ITS resolved spec + provenance (spec_ran), and
    the lineage check uses b's recipe template repository, not b's integration clone."""
    bpath, bsha = write_recipe(fx, recipe_doc(fx))
    cpath, csha = write_recipe(fx, third_recipe(fx), "recipe-c.json")
    raw = doc_bytes([{"key": "a", "name": "set value", "spec": {"path": fx.a_path, "sha256": fx.a_spec.sha256}, "after": []},
                     {"key": "b", "name": "set other", "spec": {"recipe": {"path": bpath, "sha256": bsha}}, "after": ["a"]},
                     {"key": "c", "name": "set 43", "spec": {"recipe": {"path": cpath, "sha256": csha}}, "after": ["b"]}],
                    title="d3 three")
    plan = PI.import_plan(setup, harness.project_id, raw)
    r = driver(plan, scenarios={"a": "value_ok", "b": "other_ok", "c": "value43_ok"})
    assert (r.outcome, r.reason) == ("all_done", None)
    assert [(s.key, s.outcome) for s in r.steps] == [("a", "accepted"), ("b", "accepted"), ("c", "accepted")]
    pc = json.loads((tmp_path / "plan-run" / "c.integration" / "provenance.json").read_text(encoding="utf-8"))
    pb = json.loads((tmp_path / "plan-run" / "b.integration" / "provenance.json").read_text(encoding="utf-8"))
    assert pc["predecessor"]["artifactRef"] == r.nodes["b"]["artifactRef"]
    assert pc["predecessor"]["specSha256"] == pb["resolvedSpecSha256"]                # b bound via its resolved spec
    repo = tmp_path / "plan-run" / "c.integration" / "repo"
    assert git(repo, "rev-parse", f"{pc['base']}^") == r.nodes["b"]["artifactRef"]
    assert git(repo, "merge-base", "--is-ancestor", r.nodes["a"]["artifactRef"], pc["base"]) == ""   # a -> b -> c lineage


@pytest.mark.parametrize("corrupt, reason", [
    (lambda p: p.write_text(json.dumps(dict(json.loads(p.read_text(encoding="utf-8")), recipeSha256="0" * 64)), encoding="utf-8"),
     "predecessor_provenance"),                                    # b's provenance no longer names b's recipe pin
    (lambda p: p.write_text("{not json", encoding="utf-8"), "predecessor_evidence"),          # unreadable record (R4)
])
def test_a_recipe_predecessors_own_provenance_is_checked(fx, harness, setup, driver, tmp_path, corrupt, reason):
    """Review 1866 R3/R4: c's predecessor b is a recipe node; b's resolved spec is trusted ONLY through a provenance
    that names b's recipe pin and the resolved spec sha."""
    bpath, bsha = write_recipe(fx, recipe_doc(fx))
    cpath, csha = write_recipe(fx, third_recipe(fx), "recipe-c.json")
    good_c = Path(cpath).read_bytes()
    raw = doc_bytes([{"key": "a", "name": "set value", "spec": {"path": fx.a_path, "sha256": fx.a_spec.sha256}, "after": []},
                     {"key": "b", "name": "set other", "spec": {"recipe": {"path": bpath, "sha256": bsha}}, "after": ["a"]},
                     {"key": "c", "name": "set 43", "spec": {"recipe": {"path": cpath, "sha256": csha}}, "after": ["b"]}],
                    title=f"d3 prov {reason}")
    plan = PI.import_plan(setup, harness.project_id, raw)
    Path(cpath).write_bytes(good_c + b" ")                         # run 1 stops at c, after a and b are accepted
    scen = {"a": "value_ok", "b": "other_ok", "c": "value43_ok"}
    r1 = driver(plan, scenarios=scen)
    assert (r1.reason, r1.nodes["b"]["acceptance"]) == ("recipe_tamper", "accepted")
    Path(cpath).write_bytes(good_c)
    corrupt(tmp_path / "plan-run" / "b.integration" / "provenance.json")
    r2 = driver(plan, scenarios=scen)
    assert (r2.outcome, r2.reason) == ("needs_operator", reason) and r2.nodes["c"]["work"] == "todo"
    assert not (tmp_path / "plan-run" / "c.integration").exists()




def test_a_stopped_integration_is_a_precise_operator_stop_and_is_kept(fx, harness, setup, driver, tmp_path, monkeypatch):
    """Root msg 1901: a materialization that stops AFTER creating b.integration leaves it behind; every re-run stops
    with the PRECISE reason integration_exists and the directory is KEPT (automatic archive/retry is deferred)."""
    path, rsha = write_recipe(fx, recipe_doc(fx))
    plan = PI.import_plan(setup, harness.project_id, chain(fx, path, rsha, "d3 stopped integration"))
    real = S.materialize

    def clone_fails(recipe, recipe_sha, pred, artifact, out, branch):
        Path(out).mkdir(parents=True)
        (Path(out) / "partial.txt").write_text("evidence of the failed attempt", encoding="utf-8")
        raise S.SuccessorStop("integration_clone_failed", "simulated")
    monkeypatch.setattr(S, "materialize", clone_fails)
    r1 = driver(plan)
    assert (r1.reason, r1.nodes["a"]["acceptance"], r1.nodes["b"]["work"]) == ("integration_clone_failed", "accepted", "todo")
    monkeypatch.setattr(S, "materialize", real)                                   # only this patch (npm env stays)
    r2 = driver(plan)                                                                  # the SAME run root
    assert (r2.outcome, r2.reason) == ("needs_operator", "integration_exists") and r2.nodes["b"]["work"] == "todo"
    kept = tmp_path / "plan-run" / "b.integration" / "partial.txt"
    assert kept.read_text(encoding="utf-8") == "evidence of the failed attempt"          # nothing deleted or renamed
