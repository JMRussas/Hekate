"""Pure recovery projection (HK-ISSUE-015 manifest, first increment; root msgs 2309/2310/2311). OFFLINE, no effects.

project(observation) -> (body_bytes, sha256_hex) turns ONE writer's caller-supplied, sanitized journal observation into
a deterministic listing of every stream and intent with a CONSERVATIVE status. It reads no database, HTTP endpoint,
file or clock, writes nothing, takes no lock, grants nothing and mutates no input.

CONTRACT (frozen by root review 2311):
- Input `hekate-journal-observation.v0`, strict: an unknown key anywhere, a bool where an int is required, a non-finite
  number, a pattern violation or a bound violation refuses (ProjectionRefused) -- nothing is silently dropped.
  Record `fields` carry ONLY: operator_resolution `decision`, `reconciliationRef`; any kind `invalidPayload: true`.
- Record `seq` is CONTIGUOUS from 1 (a gap could hide an intent) unless the stream is flagged `corrupt`, in which case
  it carries no records.
- Hashes are CALLER-OBSERVED strings, not a verified chain (sanitized records cannot recreate record hashes): the output
  says `integrity: caller_observed_unverified` and `authority: none`. Nothing here proves loaded code or authority.
- Pairing and closure reuse the shared rules: FIFO pairing over the whole prefix (evidence.unanswered_intents, checked
  against this module's own answer map); an ACTIVE stream follows handoff.pending_effects exactly (the LAST
  operator_resolution, if terminal by its own payload, closes the intents recorded BEFORE it, even when later intents
  are unanswered; an invalid or non-terminal LAST resolution closes nothing and never revives an earlier one); a
  RESOLVED stream follows evidence.final_resolution (legacy post-resolution intents stay visible as
  post_terminal_unknown).
- Effects are never "none": answered -> observed_outcome_semantics_only (unknown if the answering record is degraded),
  closed -> operator_asserted_not_attested, everything else -> unknown. A released reservation never implies no effects.
- complete = at least one stream observed AND every stream listed (compacted or unlistable streams cannot be complete).
- Output canonical JSON over 1 MiB refuses the WHOLE projection; the sha256 is returned beside the body, never inside.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from typing import Any

from e1.evidence import INTENTS, KINDS, TERMINALS, Record, final_resolution, is_terminal_resolution, unanswered_intents

INPUT_SCHEMA = "hekate-journal-observation.v0"
OUTPUT_SCHEMA = "hekate-recovery-projection.v0"
STREAMS_MAX = 64
PER_STREAM_MAX = 4096
OUTPUT_MAX = 1 << 20
TAIL_MAX = 8
AFTER_IDS_MAX = 8
TERMINALS_PER_INTENT_MAX = max(len(t) for t in INTENTS.values())

HEX64 = re.compile(r"^[0-9a-f]{64}$")
IDENT = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")              # writer id, root, claim key: printable, bounded
CODE = re.compile(r"^[a-z0-9_:.-]{1,64}$")
DECISION = re.compile(r"^[a-z_]{1,32}$")
REF = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
STATES = ("active", "resolved", "compacted")


class ProjectionRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# --- strict input validation ---------------------------------------------------------------------------------------

def _schema(cond: bool, detail: str) -> None:
    if not cond:
        raise ProjectionRefused("input_schema", detail)


def _keys(obj: Any, required: set[str], optional: set[str], where: str) -> None:
    _schema(isinstance(obj, dict), f"{where} must be an object")
    got = set(obj)
    _schema(required <= got and got <= required | optional, f"{where} keys {sorted(got)}")


def _int(v: Any, lo: int, hi: int, where: str) -> int:
    _schema(isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi, f"{where} must be an int in [{lo}, {hi}]")
    return v


def _str(v: Any, pattern: re.Pattern, where: str) -> str:
    _schema(isinstance(v, str) and bool(pattern.fullmatch(v)), f"{where} has an invalid value")
    return v


def _fields(kind: str, f: Any, where: str) -> dict[str, Any]:
    allowed = {"invalidPayload"} | ({"decision", "reconciliationRef"} if kind == "operator_resolution" else set())
    _keys(f, set(), allowed, where)
    out: dict[str, Any] = {}
    if "invalidPayload" in f:
        _schema(f["invalidPayload"] is True, f"{where}.invalidPayload must be true when present")
        out["invalidPayload"] = True
    if "decision" in f:
        out["decision"] = _str(f["decision"], DECISION, f"{where}.decision")
    if "reconciliationRef" in f:
        out["reconciliationRef"] = _str(f["reconciliationRef"], REF, f"{where}.reconciliationRef")
    return out


def _validate(obs: Any) -> dict[str, Any]:
    _keys(obs, {"schema", "writer", "bounds", "streams"}, set(), "observation")
    _schema(obs["schema"] == INPUT_SCHEMA, "schema")
    _keys(obs["writer"], {"id", "epoch"}, set(), "writer")
    writer = {"id": _str(obs["writer"]["id"], IDENT, "writer.id"), "epoch": _int(obs["writer"]["epoch"], 1, 2**63 - 1, "writer.epoch")}
    _keys(obs["bounds"], {"perStream"}, set(), "bounds")
    per_stream = _int(obs["bounds"]["perStream"], 1, PER_STREAM_MAX, "bounds.perStream")
    _schema(isinstance(obs["streams"], list), "streams must be a list")
    if len(obs["streams"]) > STREAMS_MAX:
        raise ProjectionRefused("input_overflow", f"{len(obs['streams'])} streams > {STREAMS_MAX}")
    streams, seen = [], set()
    for i, s in enumerate(obs["streams"]):
        w = f"streams[{i}]"
        _keys(s, {"root", "claimKey", "state", "outstanding", "headHash", "resolvedAt", "corrupt", "records", "summary"}, set(), w)
        root, ck = _str(s["root"], IDENT, f"{w}.root"), _str(s["claimKey"], IDENT, f"{w}.claimKey")
        if (root, ck) in seen:
            raise ProjectionRefused("input_order", f"duplicate stream {w}")
        seen.add((root, ck))
        _schema(s["state"] in STATES, f"{w}.state")
        _str(s["headHash"], HEX64, f"{w}.headHash")
        ra = s["resolvedAt"]
        if ra is not None:
            _schema(isinstance(ra, (int, float)) and not isinstance(ra, bool) and math.isfinite(ra) and ra >= 0, f"{w}.resolvedAt")
        if (ra is None) != (s["state"] == "active"):
            raise ProjectionRefused("input_state", f"{w}: resolvedAt must be null exactly when active")
        _schema(isinstance(s["outstanding"], list) and len(s["outstanding"]) <= per_stream, f"{w}.outstanding")
        outstanding = []
        for j, o in enumerate(s["outstanding"]):
            _schema(isinstance(o, list) and 1 <= len(o) <= TERMINALS_PER_INTENT_MAX
                    and all(isinstance(k, str) and k in TERMINALS for k in o), f"{w}.outstanding[{j}]")
            outstanding.append(list(o))
        corrupt = s["corrupt"]
        if corrupt is not None:
            _str(corrupt, CODE, f"{w}.corrupt")
        _schema(isinstance(s["records"], list), f"{w}.records must be a list")
        if len(s["records"]) > per_stream:
            raise ProjectionRefused("input_overflow", f"{w}: {len(s['records'])} records > perStream {per_stream}")
        if (s["state"] == "compacted" or corrupt is not None) and s["records"]:
            raise ProjectionRefused("input_state", f"{w}: a compacted or corrupt stream carries no records")
        if s["state"] != "compacted" and corrupt is None and not s["records"]:
            raise ProjectionRefused("input_state", f"{w}: a readable stream has records")
        records = []
        for j, r in enumerate(s["records"]):
            rw = f"{w}.records[{j}]"
            _keys(r, {"seq", "kind", "hash", "fields"}, set(), rw)
            seq = _int(r["seq"], 1, 2**63 - 1, f"{rw}.seq")
            if seq != j + 1:
                raise ProjectionRefused("input_order", f"{rw}: seq {seq} is not contiguous from 1")
            _schema(isinstance(r["kind"], str) and r["kind"] in KINDS, f"{rw}.kind")
            records.append({"seq": seq, "kind": r["kind"], "hash": _str(r["hash"], HEX64, f"{rw}.hash"),
                            "fields": _fields(r["kind"], r["fields"], f"{rw}.fields")})
        summary = s["summary"]
        if (summary is not None) != (s["state"] == "compacted"):
            raise ProjectionRefused("input_state", f"{w}: summary must be present exactly when compacted")
        if summary is not None:
            _keys(summary, {"kindsTail", "resolution"}, set(), f"{w}.summary")
            tail = summary["kindsTail"]
            _schema(isinstance(tail, list) and len(tail) <= TAIL_MAX and all(isinstance(k, str) and k in KINDS for k in tail),
                    f"{w}.summary.kindsTail")
            res = summary["resolution"]
            if res is not None:
                _keys(res, {"decision", "reconciliationRef"}, set(), f"{w}.summary.resolution")
                res = {"decision": _str(res["decision"], DECISION, f"{w}.summary.resolution.decision"),
                       "reconciliationRef": _str(res["reconciliationRef"], REF, f"{w}.summary.resolution.reconciliationRef")}
            summary = {"kindsTail": list(tail), "resolution": res}
        streams.append({"root": root, "claimKey": ck, "state": s["state"], "headHash": s["headHash"], "outstanding": outstanding,
                        "corrupt": corrupt, "records": records, "summary": summary})
    return {"writer": writer, "streams": sorted(streams, key=lambda x: (x["root"], x["claimKey"]))}


# --- projection --------------------------------------------------------------------------------------------------

def _records(writer: str, s: dict[str, Any]) -> list[Record]:
    return [Record(r["seq"], r["kind"], writer, s["root"], s["claimKey"], 0.0,
                   json.dumps(r["fields"], sort_keys=True, separators=(",", ":"))) for r in s["records"]]


def _pairing(records: list[Record]) -> tuple[list[tuple[Record, list[str]]], dict[int, list[Record]]]:
    """FIFO pairing with the answering records kept; its unanswered set must equal the shared unanswered_intents."""
    open_: list[tuple[Record, list[str], list[Record]]] = []
    for r in records:
        if r.kind in INTENTS:
            open_.append((r, list(INTENTS[r.kind]), []))
        for o in open_:
            if r.kind in o[1]:
                o[1].remove(r.kind)
                o[2].append(r)
                break
    mine = [(r.seq, rest) for r, rest, _ in open_ if rest]
    shared = [(r.seq, rest) for r, rest in unanswered_intents(records)]
    if mine != shared:                                   # one pairing rule; a divergence is a bug, never a status
        raise AssertionError("pairing diverged from evidence.unanswered_intents")
    return [(r, rest) for r, rest, _ in open_], {r.seq: answers for r, _, answers in open_}


def _project_stream(writer: str, s: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"root": s["root"], "claimKey": s["claimKey"], "state": s["state"], "headHash": s["headHash"],
                           "status": "listed", "reason": None, "finalResolution": None, "intents": [], "summary": None}
    if s["corrupt"] is not None:
        return dict(out, status="unlistable", reason=s["corrupt"])
    if s["state"] == "compacted":
        return dict(out, status="compacted_no_enumeration", reason="records_compacted", summary=s["summary"])
    records = _records(writer, s)
    paired, answers = _pairing(records)
    resolutions = [r for r in records if r.kind == "operator_resolution"]
    last = resolutions[-1] if resolutions else None
    closing = last if last is not None and is_terminal_resolution([last]) else None
    remaining = sorted(t for _, rest in paired for t in rest)
    held = sorted(t for o in s["outstanding"] for t in o)
    post_terminal: set[int] = set()
    if s["state"] == "active":
        if held != remaining:
            return dict(out, status="unlistable", reason="reservation_mismatch")
    else:
        f = final_resolution(records)
        out["finalResolution"] = {"decision": closing.payload.get("decision") if closing else None, "problem": f.problem,
                                  "afterIds": list(f.after_ids[:AFTER_IDS_MAX]), "afterCount": f.after_count}
        if held:
            return dict(out, status="unlistable", reason="reservation_mismatch")
        if f.problem == "no_terminal_resolution" and remaining:
            return dict(out, status="unlistable", reason="resolved_without_closure")
        if f.problem == "resolution_not_final":
            post_terminal = {r.seq for r, rest in paired if rest and r.seq > closing.seq}
    intents = []
    for r, rest in paired:
        if not rest:
            degraded = any(a.degraded for a in answers[r.seq])
            status, effects = "answered", "unknown" if degraded else "observed_outcome_semantics_only"
        elif r.seq in post_terminal:
            status, effects = "post_terminal_unknown", "unknown"
        elif closing is not None and r.seq < closing.seq:
            status, effects = f"closed:{closing.payload['decision']}", "operator_asserted_not_attested"
        else:
            status, effects = "open_unknown", "unknown"
        intents.append({"id": f"{r.kind}@{r.seq}", "kind": r.kind, "seq": r.seq, "status": status, "awaiting": list(rest),
                        "effects": effects})
    return dict(out, intents=intents)


def project(observation: Any) -> tuple[bytes, str]:
    """(canonical body bytes, sha256 hex of exactly those bytes). Pure; refuses (ProjectionRefused) instead of truncating."""
    obs = _validate(observation)
    writer = obs["writer"]["id"]
    streams = [_project_stream(writer, s) for s in obs["streams"]]
    by_status = Counter(i["status"].split(":")[0] for s in streams for i in s["intents"])
    body = {"schema": OUTPUT_SCHEMA, "integrity": "caller_observed_unverified", "authority": "none",
            "writer": obs["writer"],
            "complete": bool(streams) and all(s["status"] == "listed" for s in streams),
            "streams": streams,
            "counts": {"streams": len(streams), "listed": sum(s["status"] == "listed" for s in streams),
                       "compacted": sum(s["status"] == "compacted_no_enumeration" for s in streams),
                       "unlistable": sum(s["status"] == "unlistable" for s in streams),
                       "intents": dict(sorted(by_status.items()))}}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > OUTPUT_MAX:
        raise ProjectionRefused("projection_overflow", f"{len(raw)} bytes > {OUTPUT_MAX}")
    return raw, hashlib.sha256(raw).hexdigest()
