"""handoff-export.v0 publisher (e1/export.py) OFFLINE: built from the ACCEPTED golden delivery bytes
(fixtures/e2e-consumer-v0) composed with wanted=[] by the accepted consumer and the H1-SHAPED stub, so the
export is labelled a stub that the ChatAgent consumer refuses (never real parity). No DB, no H1, no network."""

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from e1 import consumer as C
from e1 import export as X

GOLD = Path(__file__).resolve().parents[1] / "fixtures" / "e2e-consumer-v0"


def rd(rel: str) -> bytes:
    return (GOLD / rel).read_bytes()


def golden_parts():
    w = json.loads(rd("delivery/wrapper.json"))
    d = C.Delivery(w["wrapper"], w["codec"], w["candidateDigest"], rd("delivery/manifest.bin"), rd("delivery/envelope.bin"),
                   rd("delivery/task.bin"), rd("delivery/receipt.json"), rd("delivery/h1-input.json"))
    fresh = C.Fresh(**json.loads(rd("inputs/fresh.json")))
    p = json.loads(rd("inputs/policy-allow.json"))
    policy = C.PolicyStub(p["principal"], tuple(p["rules"]))
    destination = json.loads(rd("inputs/request.json"))["destination"]
    text = json.loads(d.task)["text"]
    comp = C.compose(d, fresh, policy=policy, destination=destination, h1=C.h1_stub(text), wanted=[])
    return d, fresh, policy, destination, comp


def declared(comp, d, fresh, **over):
    ident = fresh.review_identity
    base = {"hekateCommit": "0" * 40, "hekateTreeClean": True, "runId": "test", "planRoot": ident["rootId"],
            "nodeId": ident["nodeId"], "attemptId": ident["attemptId"], "attemptEpoch": ident["attemptEpoch"],
            "candidateDigest": d.candidate_digest, "recordId": fresh.record_id, "viewDigest": comp.view_digest,
            "h1Bridge": {"chatagentCommit": None, "nodeVersion": None},
            "worker": {"kind": "fake", "requestedModel": "fake-model", "reportedModels": [], "reportedModelsAuthenticated": False},
            "reviewer": {"kind": "deterministic-verifier", "inputSha256": None}, "synthetic": True}
    base.update(over)
    return base


@pytest.fixture
def parts():
    return golden_parts()


def inputs(parts, **over):
    d, fresh, policy, destination, comp = parts
    kw = dict(delivery=d, fresh=fresh, policy=policy, destination=destination, composition=comp, h1_builder=X.STUB_H1,
              provenance=declared(comp, d, fresh))
    kw.update(over)
    return X.ExportInputs(**kw)


# ------------------------------------------------------------------------ the layout and the index

def test_publishes_exactly_thirteen_files_and_an_index_written_last(parts, tmp_path):
    out = tmp_path / "export"
    hashes = X.publish(out, X.export_files(inputs(parts), allow_stub=True))
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert files == sorted(X.FILES + (X.INDEX,)) and len(X.FILES) == 13
    index = (out / X.INDEX).read_bytes()
    lines = index.decode("utf-8").split("\n")
    assert lines[-1] == "" and len(lines) == 14                       # 13 LF-terminated lines, itself excluded
    assert [ln.split("  ", 1)[1] for ln in lines[:-1]] == sorted(X.FILES)
    for ln in lines[:-1]:
        digest, rel = ln.split("  ", 1)
        assert digest == hashlib.sha256((out / rel).read_bytes()).hexdigest() == hashes[rel]
    # INDEX was created last: its mtime is not older than any listed file
    assert all(os.stat(out / X.INDEX).st_mtime_ns >= os.stat(out / rel).st_mtime_ns for rel in X.FILES)


def test_exact_delivery_bytes_and_the_parts_are_carried(parts, tmp_path):
    d, fresh, policy, destination, comp = parts
    out = tmp_path / "export"
    X.publish(out, X.export_files(inputs(parts), allow_stub=True))
    for rel, raw in (("delivery/manifest.bin", d.manifest), ("delivery/envelope.bin", d.envelope), ("delivery/task.bin", d.task),
                     ("delivery/receipt.json", d.receipt), ("delivery/h1-input.json", d.h1_input)):
        assert (out / rel).read_bytes() == raw
    assert json.loads((out / "delivery/wrapper.json").read_bytes()) == {"wrapper": C.WRAPPER, "codec": C.CODEC,
                                                                         "candidateDigest": d.candidate_digest}
    assert json.loads((out / "request.json").read_bytes()) == {"destination": destination, "wanted": []}
    assert json.loads((out / "retrieval.json").read_bytes()) == []
    assert json.loads((out / "policy.json").read_bytes()) == {"principal": policy.principal, "rules": list(policy.rules)}
    assert (out / "view-part.txt").read_bytes() == comp.part.encode("utf-8")
    f = json.loads((out / "fresh.json").read_bytes())
    assert f["candidate_digest"] == fresh.candidate_digest and f["review_identity"] == fresh.review_identity


def test_expected_matches_the_producers_own_composition_and_names_the_stub(parts, tmp_path):
    d, fresh, policy, destination, comp = parts
    out = tmp_path / "export"
    X.publish(out, X.export_files(inputs(parts), allow_stub=True))
    e = json.loads((out / "expected.json").read_bytes())
    assert e == {"version": "handoff-expectation.v0", "h1Builder": X.STUB_H1, "viewDigest": comp.view_digest,
                 "viewPartSha256": hashlib.sha256(comp.part.encode("utf-8")).hexdigest(),
                 "reservationTokens": comp.view["reservationTokens"], "viewCost": comp.view_cost,
                 "h1SuppliedSha256": comp.h1["suppliedSha256"], "candidateDigest": d.candidate_digest}
    assert e["viewCost"] == int(e["reservationTokens"]) == len(comp.part.encode("utf-8")) + 16
    assert e["h1Builder"] != "chatagent-h1"                       # the consumer refuses it: never real parity


def test_provenance_is_closed_declared_and_bound(parts, tmp_path):
    out = tmp_path / "export"
    X.publish(out, X.export_files(inputs(parts), allow_stub=True))
    p = json.loads((out / "provenance.json").read_bytes())
    assert p["exportVersion"] == "handoff-export.v0" and p["synthetic"] is True
    assert p["worker"]["reportedModelsAuthenticated"] is False and p["reviewer"] == {"kind": "deterministic-verifier",
                                                                                       "inputSha256": None}


def test_the_published_bytes_recompose_to_the_same_view(parts, tmp_path):
    """Round trip: the consumer, fed ONLY the exported files, reproduces view-part.txt byte for byte."""
    out = tmp_path / "export"
    X.publish(out, X.export_files(inputs(parts), allow_stub=True))
    r = lambda rel: (out / rel).read_bytes()                                         # noqa: E731
    w = json.loads(r("delivery/wrapper.json"))
    d = C.Delivery(w["wrapper"], w["codec"], w["candidateDigest"], r("delivery/manifest.bin"), r("delivery/envelope.bin"),
                   r("delivery/task.bin"), r("delivery/receipt.json"), r("delivery/h1-input.json"))
    p = json.loads(r("policy.json"))
    req = json.loads(r("request.json"))
    again = C.compose(d, C.Fresh(**json.loads(r("fresh.json"))), policy=C.PolicyStub(p["principal"], tuple(p["rules"])),
                      destination=req["destination"], h1=C.h1_stub(json.loads(d.task)["text"]), wanted=req["wanted"])
    assert again.part.encode("utf-8") == r("view-part.txt") and again.view_digest == json.loads(r("expected.json"))["viewDigest"]


# ------------------------------------------------------------------------ exclusivity and self-verification

def test_an_existing_output_is_never_overwritten(parts, tmp_path):
    out = tmp_path / "export"
    out.mkdir()
    (out / "keep.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(X.ExportRefused) as e:
        X.publish(out, X.export_files(inputs(parts), allow_stub=True))
    assert e.value.code == "out_exists" and (out / "keep.txt").read_text(encoding="utf-8") == "user data"


@pytest.mark.parametrize("damage, code", [("byte", "verify_index"), ("extra", "verify_layout"), ("missing", "verify_layout"),
                                          ("nested_extra", "verify_layout")])
def test_verify_detects_any_change_after_indexing(parts, tmp_path, damage, code):
    out = tmp_path / "export"
    X.publish(out, X.export_files(inputs(parts), allow_stub=True))
    if damage == "byte":
        p = out / "policy.json"
        b = bytearray(p.read_bytes())
        b[5] ^= 1
        p.write_bytes(bytes(b))
    elif damage == "extra":
        (out / "notes.txt").write_text("x", encoding="utf-8")
    elif damage == "missing":
        (out / "retrieval.json").unlink()
    else:
        (out / "delivery" / "extra.bin").write_bytes(b"x")
    with pytest.raises(X.ExportRefused) as e:
        X.verify(out)
    assert e.value.code == code


# ------------------------------------------------------------------------ refusals before anything is written

def test_a_stub_composition_is_refused_outside_tests(parts):
    with pytest.raises(X.ExportRefused) as e:
        X.export_files(inputs(parts))
    assert e.value.code == "h1_builder"
    with pytest.raises(X.ExportRefused):
        X.export_files(inputs(parts, h1_builder="something-else"), allow_stub=True)


@pytest.mark.parametrize("over, code", [
    ({"wanted": ({"kind": "binding", "seq": 1},)}, "first_export_scope"),
    ({"retrieval": ({"request": {}, "result": None},)}, "first_export_scope"),
    ({"destination": "elsewhere"}, "destination_mismatch"),
])
def test_scope_and_binding_refusals(parts, over, code):
    with pytest.raises(X.ExportRefused) as e:
        X.export_files(inputs(parts, **over), allow_stub=True)
    assert e.value.code == code


def test_fresh_and_provenance_must_name_this_handoff(parts):
    d, fresh, policy, destination, comp = parts
    other = C.Fresh(**dict(vars(fresh), candidate_digest="f" * 64))
    with pytest.raises(X.ExportRefused) as e:
        X.export_files(inputs(parts, fresh=other), allow_stub=True)
    assert e.value.code == "fresh_mismatch"
    with pytest.raises(X.ExportRefused) as e:
        X.export_files(inputs(parts, provenance=declared(comp, d, fresh, viewDigest="0" * 64)), allow_stub=True)
    assert e.value.code == "provenance_binding"


@pytest.mark.parametrize("mutate, code", [
    (lambda p: p.pop("runId"), "provenance_fields"),
    (lambda p: p.update(extra=1), "provenance_fields"),
    (lambda p: p["worker"].update(reportedModelsAuthenticated=True), "provenance_worker"),
    (lambda p: p["worker"].update(kind="gpt"), "provenance_worker"),
    (lambda p: p["reviewer"].update(kind="human"), "provenance_reviewer"),
    (lambda p: p.update(h1Bridge={"x": 1}), "provenance_h1_bridge"),
    (lambda p: p.update(synthetic="no"), "provenance_synthetic"),
])
def test_provenance_is_a_closed_declared_set(parts, mutate, code):
    d, fresh, policy, destination, comp = parts
    prov = copy.deepcopy(declared(comp, d, fresh))
    mutate(prov)
    with pytest.raises(X.ExportRefused) as e:
        X.export_files(inputs(parts, provenance=prov), allow_stub=True)
    assert e.value.code == code


def test_a_float_anywhere_is_refused_before_writing(parts):
    """The consumer refuses JSON floats (codec_unsupported); the producer refuses them first (review 1593)."""
    d, fresh, policy, destination, comp = parts
    floaty = C.Fresh(**dict(vars(fresh), basis={"snapshot": "s", "revision": 1.5}))
    with pytest.raises(X.ExportRefused) as e:
        X.export_files(inputs(parts, fresh=floaty), allow_stub=True)
    assert e.value.code == "float_value"


def test_every_cap_is_the_consumers(parts):
    """A file above the consumer's read cap is refused before writing (the receipt cap is 4 KiB)."""
    d, fresh, policy, destination, comp = parts
    big = C.Delivery(d.wrapper, d.codec, d.candidate_digest, d.manifest, d.envelope, d.task, d.receipt + b" " * 5000, d.h1_input)
    with pytest.raises(X.ExportRefused) as e:
        X.export_files(inputs(parts, delivery=big), allow_stub=True)
    assert e.value.code == "file_cap"
