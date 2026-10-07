"""supervised-task-spec.v0 (e1/task_spec.py): the closed loader. Pure; no clone, install or spawn."""

import hashlib
import json
from pathlib import Path

import pytest

from e1 import task_spec as T
from task_support import edited, make_repo, make_spec, pinned_node

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_the_frozen_ca012_spec_validates_byte_exact():
    raw = (FIXTURES / "ca012-spec-v0.json").read_bytes()
    spec = T.load(FIXTURES / "ca012-spec-v0.json")
    assert spec.sha256 == hashlib.sha256(raw).hexdigest() == "b919504a353bbc21c730caedbbab244c2e457e2ef2d249c5456d91b60c322d4e"
    assert len(spec.doc["oracle"]["baseline"]["cases"]) == 15 and spec.step("oracle")["argv"][0] == spec.doc["hashes"]["pinnedNodeExe"]["path"]


@pytest.fixture(scope="module")
def doc(tmp_path_factory):
    return make_spec(make_repo(tmp_path_factory.mktemp("spec")), pinned_node(tmp_path_factory.getbasetemp()))


def refused(d) -> str:
    with pytest.raises(T.SpecRefused) as e:
        T.parse(json.dumps(d).encode("utf-8"))
    return e.value.code


def test_the_fixture_spec_is_valid(doc):
    assert T.parse(json.dumps(doc).encode("utf-8")).doc == doc


def _oracle(d):
    return next(s for s in d["verify"]["steps"] if s["name"] == "oracle")


@pytest.mark.parametrize("mutate, code", [
    (lambda d: d.update(extra=1), "spec_shape"),
    (lambda d: d["worker"].update(extra=1), "spec_shape"),
    (lambda d: d.pop("metadata"), "spec_shape"),
    (lambda d: d.update(specVersion="supervised-task-spec.v1"), "spec_version"),
    (lambda d: d["deps"].update(timeoutS=1.5), "spec_float"),
    (lambda d: d["source"].update(anchorCommit=d["source"]["taskBaseCommit"]), "spec_value"),
    (lambda d: d["source"].update(repo="relative/repo"), "spec_value"),
    (lambda d: d["source"].update(taskBaseCommit="HEAD"), "spec_value"),
    (lambda d: _oracle(d)["argv"].__setitem__(0, "node"), "spec_argv"),
    (lambda d: d["oracle"]["baseline"]["argv"].__setitem__(0, "C:/other/node.exe"), "spec_argv"),
    (lambda d: _oracle(d)["argv"].append("C:/abs/test.ts"), "spec_value"),
    (lambda d: _oracle(d)["argv"].append("../outside.ts"), "spec_value"),
    (lambda d: _oracle(d)["argv"].append("a;b"), "spec_value"),
    (lambda d: _oracle(d)["argv"].append("a b"), "spec_value"),
    # F2 (msg 1659): flag values obey the same in-repo path rule as operands
    (lambda d: _oracle(d)["argv"].append("--config=../evil.config.ts"), "spec_value"),
    (lambda d: _oracle(d)["argv"].append("--root=../../.."), "spec_value"),
    (lambda d: _oracle(d)["argv"].append("--dir=C:/Users/x"), "spec_value"),
    (lambda d: _oracle(d)["argv"].append("--outputFile.json=../out.json"), "spec_value"),
    (lambda d: _oracle(d)["argv"].append("--root=/etc"), "spec_value"),
    (lambda d: _oracle(d)["argv"].append("--x=a/../b"), "spec_value"),
    (lambda d: _oracle(d)["argv"].append("-c=./x"), "spec_value"),
    (lambda d: d["allow"].append({"path": "tests/unit/value.test.ts", "status": "M", "mode": "100644"}), "spec_allow"),
    (lambda d: d["allow"].append({"path": "package-lock.json", "status": "M", "mode": "100644"}), "spec_allow"),
    (lambda d: d["allow"].append({"path": "package.json", "status": "M", "mode": "100644"}), "spec_allow"),
    (lambda d: d["allow"].append({"path": "node_modules/x.js", "status": "M", "mode": "100644"}), "spec_allow"),
    (lambda d: d["allow"].append({"path": "src/.eslintrc", "status": "M", "mode": "100644"}), "spec_allow"),
    (lambda d: d["allow"].append(dict(d["allow"][0])), "spec_allow"),
    (lambda d: d["allow"][0].update(status="A"), "spec_value"),
    (lambda d: d["allow"][0].update(mode="100755"), "spec_value"),
    (lambda d: d["verify"]["steps"][0].update(name="oracle"), "spec_value"),
    (lambda d: _oracle(d).update(name="tests"), "spec_value"),
    (lambda d: d["worker"].update(testCommand=d["worker"]["testCommand"] + " --bail"), "spec_worker"),
    (lambda d: d["worker"].update(budgetUsd="0.00"), "spec_value"),
    (lambda d: d["worker"].update(budgetUsd="1"), "spec_value"),
    (lambda d: d["worker"].update(maxRounds=3), "spec_value"),
    (lambda d: d["worker"].update(maxTurns=True), "spec_value"),
    (lambda d: d["oracle"]["baseline"]["cases"][2].update(failureFirstLine="x"), "spec_value"),
    (lambda d: d["oracle"]["baseline"]["cases"][0].update(failureFirstLine=None), "spec_value"),
    (lambda d: d["oracle"]["baseline"]["cases"][0].update(status="skipped"), "spec_value"),
    (lambda d: d["oracle"]["baseline"]["cases"][0].update(file="src/value.txt"), "spec_value"),
    (lambda d: d["oracle"]["baseline"]["cases"].append(dict(d["oracle"]["baseline"]["cases"][0])), "spec_value"),
    (lambda d: d["oracle"]["files"].append(dict(d["oracle"]["files"][0])), "spec_value"),
    (lambda d: d["deps"].update(network="online"), "spec_value"),
    (lambda d: d["deps"].update(kind="npm-install"), "spec_value"),
    (lambda d: d["hashes"]["pinnedNodeExe"].update(sha256="A" * 64), "spec_value"),
    (lambda d: d["metadata"].update(notes=["x"] * 17), "spec_value"),
])
def test_every_deviation_is_a_typed_refusal(doc, mutate, code):
    assert refused(edited(doc, mutate)) == code


@pytest.mark.parametrize("element", ["--reporter=json", "--noEmit", "-p", "--config=vitest.config.ts", "tests/unit/a.test.ts", "run"])
def test_in_repo_flag_values_and_operands_still_validate(doc, element):
    d = json.loads(json.dumps(doc))
    for argv in (_oracle(d)["argv"], d["oracle"]["baseline"]["argv"]):
        argv.append(element)
    d["worker"]["testCommand"] = " ".join(_oracle(d)["argv"])
    T.parse(json.dumps(d).encode("utf-8"))


def test_strict_json_size_and_unreadable(doc, tmp_path):
    raw = json.dumps(doc)
    for bad in (raw[:-1] + ', "extra": 1, "extra": 2}', raw.replace('"timeoutS": 60', '"timeoutS": NaN', 1), b"\xff"):
        with pytest.raises(T.SpecRefused) as e:
            T.parse(bad.encode("utf-8") if isinstance(bad, str) else bad)
        assert e.value.code == "spec_json"
    with pytest.raises(T.SpecRefused) as e:
        T.parse(b" " * (T.SPEC_MAX + 1))
    assert e.value.code == "spec_size"
    with pytest.raises(T.SpecRefused) as e:
        T.load(tmp_path / "missing.json")
    assert e.value.code == "spec_unreadable"
