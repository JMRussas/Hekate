"""The pilot's post-compose export hook and the opt-in real-H1 review source (e1/pilot_export.py), against
the disposable harness database. The REAL pinned H1 is never called here: the real-H1 source is wired with
an injected stand-in `build`, which is labelled INJECTED (never chatagent-h1) and exportable only with
test_only. These tests prove the plumbing and the derived provenance, not ChatAgent parity."""

import datetime
import json
import subprocess
from pathlib import Path

import pytest

from e1 import consumer as C
from e1 import export as X
from e1 import pilot as P
from e1 import pilot_export as PE
from test_pilot_dryrun import Verdicts, pilot  # noqa: F401 (fixture)

WORKTREE = Path(__file__).resolve().parent


def exporter(p, tmp_path, **over) -> PE.Exporter:
    (tmp_path / "exports").mkdir(exist_ok=True)
    kw = dict(out_root=tmp_path / "exports", run_id="t", hekate_repo=WORKTREE, dsn=p.aj.dsn, execution_kind=p.execution_kind,
              source=p.review_source, test_only=True)
    kw.update(over)
    return PE.Exporter(**kw)


class StandInH1:
    """Stands in for e1.h1_bridge.build: renders the task from the options it is given, as H1 does."""

    def __init__(self):
        self.calls = []

    def __call__(self, options):
        self.calls.append(options)
        text = "REVIEW TASK from claim bytes sha " + C.H.sha(options["response"])
        return {"ok": True, "text": text, "suppliedSha256": C.H.sha(text),
                "context": {"messages": [{"role": "user", "content": text}], "systemInstruction": options["systemInstruction"],
                            "roleInstructions": options["roleInstructions"]},
                "_runtime": {"node": "x", "version": "v0", "h1Commit": "f" * 40}}


# ------------------------------------------------------------------------ the hook

def test_each_round_exports_one_verified_directory_and_the_run_continues(pilot, tmp_path):
    p = pilot(Verdicts("rejected", "accepted"))
    p.exporter = ex = exporter(p, tmp_path)
    res = p.run()
    assert (res.outcome, len(res.rounds)) == ("accepted", 2)
    assert [e["round"] for e in ex.published] == [1, 2]
    for e, rec in zip(ex.published, res.rounds):
        out = Path(e["dir"])
        X.verify(out)
        exp = json.loads((out / "expected.json").read_bytes())
        prov = json.loads((out / "provenance.json").read_bytes())
        assert exp["viewDigest"] == rec.view_digest == prov["viewDigest"] == e["viewDigest"]
        assert exp["candidateDigest"] == rec.candidate_digest == prov["candidateDigest"]
        assert prov["attemptEpoch"] == rec.attempt_epoch and prov["synthetic"] is True and exp["h1Builder"] == X.STUB_H1
        # a SIMULATED round has no adapter record: the derived worker says so
        assert prov["worker"] == {"kind": "fake", "requestedModel": None, "reportedModels": [], "reportedModelsAuthenticated": False}
        assert prov["h1Bridge"] == {"chatagentCommit": None, "nodeVersion": None}


def test_an_export_failure_is_a_typed_stop_before_review(pilot, tmp_path):
    rv = Verdicts("accepted")
    p = pilot(rv)
    p.exporter = exporter(p, tmp_path)
    (tmp_path / "exports" / "export-r1").mkdir()                                  # exclusive publish refuses
    res = p.run()
    assert (res.outcome, res.reason, res.detail) == ("needs_operator", "export_failed", "out_exists")
    assert rv.orders == [] and res.rounds[0].decision is None


def test_a_non_real_composition_is_refused_unless_test_only(pilot, tmp_path):
    p = pilot(Verdicts("accepted"))
    p.exporter = exporter(p, tmp_path, test_only=False)
    res = p.run()
    assert (res.reason, res.detail) == ("export_failed", "h1_builder")


# ------------------------------------------------------------------------ review 1593 #1: identity, not a name

def test_an_injected_builder_is_never_labelled_chatagent_h1(pilot, tmp_path):
    h1 = StandInH1()
    p = pilot(Verdicts("accepted"))
    p.review_source = src = PE.RealH1ReviewSource(build=h1)
    p.exporter = ex = exporter(p, tmp_path)
    res = p.run()
    assert res.outcome == "accepted" and src.name == PE.INJECTED and src.is_real is False
    out = Path(ex.published[0]["dir"])
    assert json.loads((out / "expected.json").read_bytes())["h1Builder"] == X.STUB_H1
    prov = json.loads((out / "provenance.json").read_bytes())
    assert prov["synthetic"] is True and prov["h1Bridge"] == {"chatagentCommit": None, "nodeVersion": None}
    # the plumbing: the EXACT retained claim bytes reach H1, and composition re-ran the same input
    claim = json.loads(h1.calls[0]["response"])
    assert claim["receipt"]["claimKey"] == res.rounds[0].claim_key and claim["receipt"]["attemptEpoch"] == 1
    assert len(h1.calls) >= 2 and all(c["response"] == h1.calls[0]["response"] for c in h1.calls)
    # ONE actual capture time at prepare, reused unchanged by every composition call (budget loop included)
    stamp = h1.calls[0]["capturedAtIso"]
    assert stamp.endswith("Z") and all(c["capturedAtIso"] == stamp for c in h1.calls)
    assert json.loads((out / "delivery" / "h1-input.json").read_bytes())["capturedAtIso"] == stamp
    assert json.loads((out / "delivery" / "h1-input.json").read_bytes())["response"] == h1.calls[0]["response"]
    task = json.loads((out / "delivery" / "task.bin").read_bytes())
    assert task["text"].startswith("REVIEW TASK from claim bytes") and task["instructions"]["system"] == PE.SYSTEM


def test_real_only_by_identity_with_the_pinned_bridge_and_a_captured_runtime():
    from e1 import h1_bridge
    src = PE.RealH1ReviewSource()                                         # the pinned bridge, NOT called here
    assert src._build is h1_bridge.build and src.is_real is False and src.name == PE.INJECTED      # no runtime yet
    src.runtime = {"node": "n", "version": "v24.21.0", "h1Commit": h1_bridge.H1_COMMIT}
    assert src.is_real is True and src.name == X.REAL_H1
    assert src.bridge() == {"chatagentCommit": h1_bridge.H1_COMMIT, "nodeVersion": "v24.21.0"}
    other = PE.RealH1ReviewSource(build=StandInH1())
    other.runtime = dict(src.runtime)
    assert other.is_real is False and other.name == PE.INJECTED                                    # identity decides


def test_a_failing_h1_is_a_typed_stop(pilot):
    p = pilot(Verdicts("accepted"))
    p.review_source = PE.RealH1ReviewSource(build=lambda options: {"ok": False, "kind": "refused", "code": "INVALID_RESPONSE"})
    res = p.run()
    assert (res.outcome, res.reason, res.detail) == ("needs_operator", "review_task_unavailable", "RuntimeError")


# ------------------------------------------------------------------------ review 1593 #3 and #4

def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_tree_clean_counts_untracked_files_and_needs_a_real_head(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q")
    (r / "a.txt").write_text("a", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "a")
    sha, clean = PE.git_identity(r)
    assert sha == git(r, "rev-parse", "HEAD") and clean is True
    (r / "untracked.py").write_text("x", encoding="utf-8")
    assert PE.git_identity(r) == (sha, False)                                     # untracked => not clean
    empty = tmp_path / "e"
    empty.mkdir()
    git(empty, "init", "-q")
    with pytest.raises(X.ExportRefused) as e:                                     # no HEAD => refused, never ""
        PE.git_identity(empty)
    assert e.value.code == "hekate_identity"


def test_captured_at_is_the_actual_utc_time():
    src = PE.RealH1ReviewSource(build=StandInH1())
    before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    opts = src.options(b'{"x":1}')
    after = datetime.datetime.now(datetime.timezone.utc)
    t = datetime.datetime.fromisoformat(opts["capturedAtIso"].replace("Z", "+00:00"))
    assert opts["capturedAtIso"].endswith("Z") and before <= t <= after


def test_the_default_source_is_the_unchanged_stub(pilot):
    p = pilot(Verdicts("accepted"))
    assert type(p.review_source) is P.ReviewTaskSource and p.review_source.name == "stub" and p.exporter is None
