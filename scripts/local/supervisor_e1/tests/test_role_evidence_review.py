"""Independent lead checks for evidence correlation with PlanStore semantics."""
import copy
import pytest
from e1 import role_evidence as RE
from test_role_evidence import IDENT, BUDGET, pl


def identity():
    return {**copy.deepcopy(IDENT), "contentRevision": 4}


def test_content_revision_is_required_separately_from_attempt_epoch():
    value = identity()
    value.pop("contentRevision")
    with pytest.raises(RE.EvidenceRefused):
        RE.build_manifest(value, {"out.txt": pl()}, **BUDGET)


def test_expected_content_revision_mismatch_is_refused():
    value = identity()
    built = RE.build_manifest(value, {"out.txt": pl()}, **BUDGET)
    expected = {k: value[k] for k in ("planRoot", "taskId", "runId", "attemptId", "epoch", "stateRevision", "contentRevision")}
    expected["contentRevision"] = 5
    result = RE.verify_manifest(built.body, {"out.txt": b"hello"}, expected=expected, **BUDGET)
    assert not result.ok and result.code == "identity_mismatch"


def test_exact_javascript_integer_boundary_is_refused():
    value = copy.deepcopy(IDENT)
    value["epoch"] = 2**53
    with pytest.raises(RE.EvidenceRefused):
        RE.build_manifest(value, {"out.txt": pl()}, **BUDGET)


def test_manual_operation_key_does_not_imply_claim_receipt():
    value = identity()
    value["operationKey"] = "manual-transition-1"
    built = RE.build_manifest(value, {"out.txt": pl()}, **BUDGET)
    assert built.manifest()["identity"]["linkage"] == "unlinked"
    assert built.manifest()["identity"]["claimKey"] is None
