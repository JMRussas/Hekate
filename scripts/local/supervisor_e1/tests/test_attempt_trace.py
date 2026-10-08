"""The attempt trace (root GO 2441; contract trace-contract-001 rev 2): each round retains the exact prompt and ONE
ordered JSONL file of the worker's stdout and stderr, referenced from launch_intent before spawn and finalized in
exited / RunEvidence. Exercised end to end against the FAKE CLI (tests/fake_cli.py): no model, no network."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from e1 import cli_worker as W
from test_cli_worker import FailingSink, Sink, make_cfg, order, repo, run  # noqa: F401 (repo is a fixture)


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def by_stream(recs, stream):
    return [r for r in recs if r["stream"] == stream]


def file_meta(path: Path) -> tuple[int, str]:
    b = path.read_bytes()
    return len(b), hashlib.sha256(b).hexdigest()


def test_a_normal_attempt_retains_prompt_conversation_and_order(tmp_path, repo):
    w, sink, rep, run_dir = run(tmp_path, repo, "trace")
    assert (rep.status, rep.reason) == ("ok", None) and "result_captured" in sink.kinds()      # acceptance path unchanged
    kinds = sink.kinds()
    intent = dict(sink.records)["launch_intent"]
    assert kinds.index("launch_intent") < kinds.index("launched")
    assert intent["trace"] == {"version": "hekate-attempt-trace.v0", "executionKind": "claude-cli", "runDir": str(run_dir),
                               "prompt": "attempt-r1.prompt.txt", "trace": "attempt-r1.trace.jsonl"}

    prompt = (run_dir / "attempt-r1.prompt.txt").read_bytes()
    assert prompt == w.prompt(order("trace", Sink())).encode("utf-8")          # the exact stdin bytes
    assert b"FAKE-SCENARIO: trace" in prompt and b"HEKATE-ACT" in prompt

    path = run_dir / "attempt-r1.trace.jsonl"
    recs = records(path)
    assert [r["seq"] for r in recs] == list(range(len(recs)))
    assert all(set(r) == {"seq", "tMs", "stream", "text", "cut", "redacted"} for r in recs)
    assert [r["tMs"] for r in recs] == sorted(r["tMs"] for r in recs)
    out = [json.loads(r["text"]) for r in by_stream(recs, "stdout")]
    types = [e["type"] for e in out]
    assert types[0] == "system" and types[-1] == "result" and "user" in types

    # the assistant's visible text and its tool call/result are retained ...
    texts = [b["text"] for e in out if e["type"] == "assistant" for b in e["message"]["content"] if b["type"] == "text"]
    assert any(t.startswith("I will write hello.txt.") for t in texts)
    tool = [b for e in out if e["type"] == "assistant" for b in e["message"]["content"] if b["type"] == "tool_use"]
    assert tool == [{"type": "tool_use", "id": "tu_1", "name": "Write", "input": {"file_path": "hello.txt", "content": "hello\n"}}]
    res = [b for e in out if e["type"] == "user" for b in e["message"]["content"]]
    assert res == [{"type": "tool_result", "tool_use_id": "tu_1", "content": "File written"}]
    # ... but private reasoning never is, and a mixed record keeps its visible content
    raw = path.read_text(encoding="utf-8")
    assert "PRIVATE-REASONING" not in raw and "PRIVATE-CODEX-REASONING" not in raw
    mixed = next(r for r in by_stream(recs, "stdout") if "I will write hello.txt" in r["text"])
    assert mixed["redacted"] is True and [b["type"] for b in json.loads(mixed["text"])["message"]["content"]] == ["text"]
    reasoning = next(r for r in by_stream(recs, "stdout") if '"reasoning"' in r["text"])
    assert reasoning["redacted"] is True and json.loads(reasoning["text"])["item"] == {"id": "r1", "type": "reasoning"}
    assert not any(r["redacted"] for r in by_stream(recs, "stdout") if r is not mixed and r is not reasoning)

    # stderr is retained IN TIME ORDER with stdout; an unterminated last line is flushed at close
    err = by_stream(recs, "stderr")
    assert [r["text"] for r in err] == ["warn: first stderr line", "partial stderr without newline at the end"]
    first_err, first_assistant = recs.index(err[0]), recs.index(mixed)
    assert recs.index(by_stream(recs, "stdout")[0]) < first_err < first_assistant
    assert [r["text"] for r in by_stream(recs, "hekate")] == ["exit:0"] and recs[-1]["text"] == "exit:0"

    # the journal and the evidence describe exactly the retained bytes
    final = dict(sink.records)["exited"]["trace"]
    n, sha = file_meta(path)
    assert final == {"prompt": {"bytes": len(prompt), "sha256": hashlib.sha256(prompt).hexdigest()},
                     "trace": {"bytes": n, "sha256": sha, "records": len(recs), "capped": False, "writeError": False},
                     "complete": True}
    assert w.evidence[1].trace == {"ref": intent["trace"], "final": final}
    assert json.loads(json.dumps(vars(w.evidence[1])))["trace"]["final"] == final          # reaches evidence.json


def test_a_killed_attempt_records_the_kill_and_still_matches_its_file(tmp_path, repo):
    w, sink, rep, run_dir = run(tmp_path, repo, "stall", inactivity_timeout_s=2)
    assert (rep.status, rep.reason) == ("failed", "inactivity")
    recs = records(run_dir / "attempt-r1.trace.jsonl")
    notes = [r["text"] for r in by_stream(recs, "hekate")]
    assert notes[0] == "killed:inactivity" and notes[1].startswith("exit:") and len(notes) == 2
    assert json.loads(by_stream(recs, "stdout")[0]["text"])["type"] == "system"
    final = dict(sink.records)["exited"]["trace"]
    assert (final["trace"]["bytes"], final["trace"]["sha256"]) == file_meta(run_dir / "attempt-r1.trace.jsonl")
    assert final["complete"] is True


def test_the_existing_stdout_cap_bounds_retention_with_one_note(tmp_path, repo):
    w, sink, rep, run_dir = run(tmp_path, repo, "trace", stdout_lines_max=3)
    assert (rep.status, rep.reason) == ("failed", "stdout_cap")                    # unchanged supervisor behaviour
    recs = records(run_dir / "attempt-r1.trace.jsonl")
    assert len(by_stream(recs, "stdout")) == 3
    assert [r["text"] for r in by_stream(recs, "hekate")][:2] == ["stdout_cap_reached", "killed:stdout_cap"]
    assert dict(sink.records)["exited"]["trace"]["trace"]["capped"] is True


def test_the_existing_stderr_keep_bounds_retained_stderr_but_not_the_full_digest(tmp_path, repo):
    w, sink, rep, run_dir = run(tmp_path, repo, "trace", stderr_keep=30)
    assert rep.status == "ok"
    recs = records(run_dir / "attempt-r1.trace.jsonl")
    first = b"warn: first stderr line" + os.linesep.encode()
    prefix = (b"partial stderr without newline at the end"[:30 - len(first)]).decode()       # raw bytes, delimiters counted
    assert [(r["text"], r["cut"]) for r in by_stream(recs, "stderr")] == [("warn: first stderr line", False), (prefix, True)]
    assert [r["text"] for r in by_stream(recs, "hekate")].count("stderr_cap_reached") == 1
    ex = dict(sink.records)["exited"]
    full = b"warn: first stderr line" + os.linesep.encode() + b"partial stderr without newline at the end"   # text-mode stderr
    assert ex["stderrBytes"] == len(full) and ex["stderrSha256"] == hashlib.sha256(full).hexdigest()   # full stream
    assert ex["trace"]["trace"]["sha256"] != ex["stderrSha256"] and ex["trace"]["trace"]["capped"] is True


def test_an_over_long_stderr_line_is_cut_once_and_the_next_line_survives(tmp_path, repo):
    w, sink, rep, run_dir = run(tmp_path, repo, "trace_long_err", stderr_keep=1 << 20)
    assert rep.status == "ok"
    err = by_stream(records(run_dir / "attempt-r1.trace.jsonl"), "stderr")
    assert [(r["cut"], len(r["text"])) for r in err] == [(False, len("warn: first stderr line")), (True, W.TRACE_LINE_CUT),
                                                         (False, len("partial stderr without newline at the end"))]
    assert err[1]["text"] == "L" * W.TRACE_LINE_CUT and err[2]["text"] == "partial stderr without newline at the end"


def test_a_flood_of_empty_stderr_lines_stops_at_the_cap(tmp_path, repo):
    """Root review 2452 R1: delimiters count toward stderr_keep, so empty lines cannot write unbounded records."""
    w, sink, rep, run_dir = run(tmp_path, repo, "trace_blank_flood", stderr_keep=64)
    assert (rep.status, rep.reason) == ("ok", None)                                  # the task outcome is unchanged
    recs = records(run_dir / "attempt-r1.trace.jsonl")
    assert len(by_stream(recs, "stderr")) <= 64 and all(r["text"] == "" for r in by_stream(recs, "stderr"))
    assert [r["text"] for r in by_stream(recs, "hekate")] == ["stderr_cap_reached", "exit:0"]
    assert dict(sink.records)["exited"]["stderrBytes"] >= 200_000                    # the full-stream digest still counts all


def test_visible_lone_surrogate_text_next_to_dropped_thinking_is_retained_losslessly(tmp_path, repo):
    """Root review 2452 R2: re-serialising a redacted event must not fail on a lone surrogate, and must not stop the
    stdout reader (the ACK in the same message is still delivered and the round captured)."""
    w, sink, rep, run_dir = run(tmp_path, repo, "trace_surrogate")
    assert (rep.status, rep.reason) == ("ok", None) and [a["kind"] for a in sink.acts] == ["worker_ack"]
    path = run_dir / "attempt-r1.trace.jsonl"
    path.read_bytes().decode("ascii")                                                  # every record is ASCII-escaped JSON
    rec = next(r for r in by_stream(records(path), "stdout") if r["redacted"])
    content = json.loads(rec["text"])["message"]["content"]
    assert [b["type"] for b in content] == ["text"] and content[0]["text"].startswith("odd \ud800 char\n")
    final = dict(sink.records)["exited"]["trace"]
    assert final["complete"] is True and final["trace"]["writeError"] is False


def test_a_write_failure_is_reported_and_never_complete(tmp_path, repo, monkeypatch):
    real = W.AttemptTrace._write
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 3 and self.f is not None:
            self.f.close()                                                             # the next write raises ValueError
        return real(self, *a, **kw)
    monkeypatch.setattr(W.AttemptTrace, "_write", flaky)
    w, sink, rep, run_dir = run(tmp_path, repo, "trace")
    assert (rep.status, rep.reason) == ("ok", None)                                  # retention never changes the outcome
    final = dict(sink.records)["exited"]["trace"]
    assert final["trace"]["writeError"] is True and final["complete"] is False
    assert (final["trace"]["bytes"], final["trace"]["sha256"]) == file_meta(run_dir / "attempt-r1.trace.jsonl")


def test_an_aborted_attempt_has_no_exited_but_a_finalized_incomplete_trace(tmp_path, repo):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    w = W.CliWorker(make_cfg(repo, run_dir, total_timeout_s=120, first_output_timeout_s=60, inactivity_timeout_s=60))
    sink = FailingSink("act")
    with pytest.raises(RuntimeError):
        w(order("trace", sink))
    assert "exited" not in sink.kinds() and dict(sink.records)["launch_intent"]["trace"]["trace"] == "attempt-r1.trace.jsonl"
    path = run_dir / "attempt-r1.trace.jsonl"
    final = w.evidence[1].trace["final"]
    assert final["complete"] is False and (final["trace"]["bytes"], final["trace"]["sha256"]) == file_meta(path)
    assert [r["text"] for r in by_stream(records(path), "hekate")][-2:] == ["killed:supervisor_error", "trace_incomplete:supervisor_error"]


def test_an_existing_trace_file_is_never_overwritten_and_nothing_launches(tmp_path, repo):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "attempt-r1.trace.jsonl").write_text("older\n", encoding="utf-8")
    sink = Sink()
    w = W.CliWorker(make_cfg(repo, run_dir))
    rep = w(order("happy", sink))
    assert (rep.status, rep.reason) == ("failed", "trace_setup_failed") and sink.records == [] and w.evidence[1].pid is None
    assert (run_dir / "attempt-r1.trace.jsonl").read_text(encoding="utf-8") == "older\n"


def test_the_execution_kind_labels_the_trace_and_is_validated(tmp_path, repo):
    w, sink, rep, _ = run(tmp_path, repo, "happy", execution_kind="fake-cli")
    assert dict(sink.records)["launch_intent"]["trace"]["executionKind"] == "fake-cli"
    with pytest.raises(W.CliRefused) as e:
        W.CliWorker(make_cfg(repo, tmp_path, execution_kind="codex"))
    assert e.value.code == "config_execution_kind"
