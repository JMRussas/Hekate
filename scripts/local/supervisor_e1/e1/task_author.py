"""Task authoring v0 (root GO 1984; schema 1985, agreed in 1987/1989): ONE pinned supervised-task-spec.v0 from a
Claude-authored draft and a reviewed target-repo profile, proven before it is called ready.

  draft --draft T.json --profile P.json --reference R.patch --out DIR

  hekate-task-profile.v0 (reviewed, in the target repo): repo, the trusted tool pins (node, npm-cli), the trusted
      lock pins (package-lock, vitest and tsc entries), offline deps, the oracle runner argv with ONE "{oracle}"
      token, the other verify steps and the default worker bounds.
  hekate-task-draft.v0 (the judgment fields only): anchor/base commits, task text/criteria, allow paths, oracle
      paths, optional worker overrides, metadata.
  R.patch: a throwaway reference implementation, used ONLY to prove the spec is satisfiable. It is kept under
      evidence/ and never enters the package or any worker prompt.

Before any effect (exit 2, nothing created): bounded strict inputs; the tool pins re-hashed BEFORE any subprocess;
the base's package-lock blob equals the profile pin (profile_lock_mismatch); the draft composes to a spec that the
UNCHANGED task_spec.parse accepts. Then, in the exclusive DIR (exit 1 = not ready, evidence kept):
  1. capture: a LABELLED provisional spec (one placeholder case, parsed by the unchanged task_spec.parse, confined
     to evidence/capture/) clones and runs the oracle at the base with the existing run_baseline. The report must
     be a normal one: no suite-level error, every failure an AssertionError, at least one failing case.
  2. the final spec (the captured cases) is written to evidence/spec.json and loaded by the unchanged task_spec.load;
  3. the UNCHANGED task_runner.preflight proves it in evidence/proof/;
  4. the reference patch is committed on the base in that owned clone and the REAL verifier
     (SpecVerifier.check_artifact) must ACCEPT it: oracle, every step, the allow list, a clean tree.
Only then is package/spec.json written (the same bytes) and authoring.json says ready true. package/ exists only
for a ready package. authoring.json is written last in every outcome.

It CALLS no worker, model, harness, PlanStore or plan launch (review 2002 F3: it imports e1.pilot for the Verdict
type and e1.task_runner for the shared checks, and those modules import the launch code, but nothing here invokes it).
Dependencies install with `npm ci --offline` only (the profile must say offline).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from e1 import consumer as C
from e1 import pilot as P
from e1 import task_runner as TR
from e1 import task_spec as T

PROFILE_VERSION = "hekate-task-profile.v0"
DRAFT_VERSION = "hekate-task-draft.v0"
PROFILE_VERSION_V1 = "hekate-task-profile.v1"     # v0 plus `formatter` and up to 4 extra steps; composes supervised-task-spec.v1
DRAFT_VERSION_V1 = "hekate-task-draft.v1"         # v0 plus `formatPaths` (a subset of `allow`); the only draft a v1 profile accepts
AUTHORING_VERSION = "hekate-task-authoring.v0"
ORACLE_TOKEN = "{oracle}"
PROVISIONAL = "(capture: provisional)"
ASSERTION = "AssertionError"
WORKER_KEYS = ("model", "budgetUsd", "maxRounds", "maxTurns")
AUTHOR = ("-c", "user.name=hekate-author", "-c", "user.email=hekate-author@hekate.local", "-c", "commit.gpgsign=false")


class AuthorRefused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(f"{code}: {detail}" if detail is not None else code)
        self.code, self.detail = code, detail


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_input(path: Path, what: str) -> bytes:
    """Bounded like a spec (task_spec.SPEC_MAX)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(T.SPEC_MAX + 1)
    except OSError:
        raise AuthorRefused(f"{what}_unreadable", str(path)) from None
    if len(raw) > T.SPEC_MAX:
        raise AuthorRefused(f"{what}_size", f"at most {T.SPEC_MAX} bytes")
    return raw


def _strict(raw: bytes, what: str) -> dict[str, Any]:
    try:
        d = C.strict_loads(raw, what)
        T._no_float(d)
    except (C.Refused, RecursionError):
        raise AuthorRefused(f"{what}_json") from None
    except T.SpecRefused as e:
        raise AuthorRefused(f"{what}_invalid", str(e)) from None
    return d


def _checked(what: str, fn, *a):
    """task_spec's own field validators, mapped to this input's typed refusal."""
    try:
        return fn(*a)
    except T.SpecRefused as e:
        raise AuthorRefused(f"{what}_invalid", str(e)) from None


def parse_profile(raw: bytes) -> dict[str, Any]:
    """The SHAPE of the profile. Every value is re-validated by task_spec.parse once composed."""
    d = _strict(raw, "profile")
    v1 = isinstance(d, dict) and d.get("version") == PROFILE_VERSION_V1
    _checked("profile", T._exact, d, ("version", "repo", "tools", "lock", "deps", "oracleRunner", "steps", "worker")
             + (("formatter",) if v1 else ()), "profile")
    if d["version"] not in (PROFILE_VERSION, PROFILE_VERSION_V1):
        raise AuthorRefused("profile_version")
    if v1:                                                                  # the SHAPE; task_spec re-validates every value
        fm = _checked("profile", T._exact, d["formatter"], ("prettierEntry", "config", "timeoutS", "outputKeepBytes"), "formatter")
        _checked("profile", T._exact, fm["prettierEntry"], ("path", "version", "sha256"), "formatter.prettierEntry")
        _checked("profile", T._exact, fm["config"], ("path", "sha256"), "formatter.config")
    _checked("profile", T._abs_path, d["repo"], "repo")
    tools = _checked("profile", T._exact, d["tools"], ("pinnedNodeExe", "npmCli"), "tools")
    for k in ("pinnedNodeExe", "npmCli"):
        t = _checked("profile", T._exact, tools[k], ("path", "version", "sha256"), f"tools.{k}")
        _checked("profile", T._abs_path, t["path"], f"tools.{k}.path")
        _checked("profile", T._str, t["sha256"], f"tools.{k}.sha256", 64, 64, T.HEX64)
    lock = _checked("profile", T._exact, d["lock"], ("packageLock", "vitestEntry", "tscEntry"), "lock")
    for k, v in lock.items():
        _checked("profile", T._str, v, f"lock.{k}", 64, 64, T.HEX64)
    deps = _checked("profile", T._exact, d["deps"], ("kind", "network", "timeoutS", "outputKeepBytes"), "deps")
    if deps["network"] != "offline":
        raise AuthorRefused("profile_not_offline", deps["network"])        # v0: offline dependencies only (root msg 1987)
    run = _checked("profile", T._exact, d["oracleRunner"], ("argv", "timeoutS", "reportMaxBytes", "outputKeepBytes"), "oracleRunner")
    if not isinstance(run["argv"], list) or run["argv"].count(ORACLE_TOKEN) != 1:
        raise AuthorRefused("profile_invalid", "oracleRunner.argv must hold the token {oracle} exactly once")
    steps = d["steps"]
    if not isinstance(steps, list) or len(steps) > (4 if v1 else 3):
        raise AuthorRefused("profile_invalid", f"steps must hold 0..{4 if v1 else 3} steps (the oracle step is added first)")
    for i, s in enumerate(steps):
        _checked("profile", T._exact, s, ("name", "argv", "timeoutS", "outputKeepBytes"), f"steps[{i}]")
        if s["name"] == "oracle" or ORACLE_TOKEN in (s["argv"] if isinstance(s["argv"], list) else []):
            raise AuthorRefused("profile_invalid", f"steps[{i}]: the oracle step is the runner's; the name and token are reserved")
    _checked("profile", T._exact, d["worker"], WORKER_KEYS, "worker")
    return d


def parse_draft(raw: bytes, v1: bool = False) -> dict[str, Any]:
    d = _strict(raw, "draft")
    _checked("draft", T._exact, d, ("version", "source", "task", "allow", "oracle", "worker", "metadata")
             + (("formatPaths",) if v1 else ()), "draft")
    if d["version"] != (DRAFT_VERSION_V1 if v1 else DRAFT_VERSION):
        raise AuthorRefused("draft_version")
    if v1:
        fp = d["formatPaths"]
        if not isinstance(fp, list) or not fp:
            raise AuthorRefused("draft_invalid", "formatPaths must be a non-empty list of allow paths")
        for i, p in enumerate(fp):
            _checked("draft", T._rel_path, p, f"formatPaths[{i}]")
        if not set(fp) <= set(d["allow"] if isinstance(d["allow"], list) else []):
            raise AuthorRefused("draft_invalid", "formatPaths must be a subset of allow")
    src = _checked("draft", T._exact, d["source"], ("anchorCommit", "taskBaseCommit"), "source")
    for k in ("anchorCommit", "taskBaseCommit"):
        _checked("draft", T._str, src[k], f"source.{k}", 40, 40, T.HEX40)
    _checked("draft", T._exact, d["task"], ("text", "criteria"), "task")
    for k in ("allow", "oracle"):
        if not isinstance(d[k], list) or not d[k]:
            raise AuthorRefused("draft_invalid", f"{k} must be a non-empty list of repository paths")
        for i, p in enumerate(d[k]):
            _checked("draft", T._rel_path, p, f"{k}[{i}]")
    if set(d["allow"]) & set(d["oracle"]):                                  # review 1989 R-a: cheap, before any effect
        raise AuthorRefused("draft_allow_overlaps_oracle", sorted(set(d["allow"]) & set(d["oracle"])))
    if not isinstance(d["worker"], dict) or set(d["worker"]) - set(WORKER_KEYS):
        raise AuthorRefused("draft_invalid", f"worker overrides only {list(WORKER_KEYS)}")
    _checked("draft", T._exact, d["metadata"], ("issue", "title", "notes"), "metadata")
    return d


def oracle_argv(profile: dict[str, Any], oracle: list[str]) -> list[str]:
    out: list[str] = []
    for a in profile["oracleRunner"]["argv"]:
        out.extend(oracle if a == ORACLE_TOKEN else [a])
    return out


def compose(profile: dict[str, Any], draft: dict[str, Any], oracle_files: list[dict[str, str]],
            cases: list[dict[str, Any]], expected_exit: int) -> bytes:
    """The supervised-task-spec.v0 bytes. Every value comes from the profile, the draft or a computed hash/capture."""
    run, argv = profile["oracleRunner"], oracle_argv(profile, draft["oracle"])
    v1 = profile["version"] == PROFILE_VERSION_V1
    doc = {
        "specVersion": T.SPEC_VERSION_V1 if v1 else T.SPEC_VERSION,
        "source": {"repo": profile["repo"], **draft["source"]},
        "task": draft["task"],
        "allow": [{"path": p, "status": "M", "mode": "100644"} for p in draft["allow"]],
        "oracle": {"files": oracle_files,
                   "baseline": {"argv": argv, "timeoutS": run["timeoutS"], "reportMaxBytes": run["reportMaxBytes"],
                                "expectedExit": expected_exit, "cases": cases}},
        "verify": {"steps": [{"name": "oracle", "argv": argv, "timeoutS": run["timeoutS"], "outputKeepBytes": run["outputKeepBytes"]},
                             *profile["steps"]]},
        "worker": {"testCommand": " ".join(argv), **{k: draft["worker"].get(k, profile["worker"][k]) for k in WORKER_KEYS}},
        "hashes": {**profile["tools"], **profile["lock"]},
        "deps": profile["deps"],
        "metadata": draft["metadata"],
        **({"formatter": {**profile["formatter"], "paths": draft["formatPaths"]}} if v1 else {}),
    }
    return (json.dumps(doc, indent=1) + "\n").encode("utf-8")


def check_tool_pins(profile: dict[str, Any]) -> None:
    """The trusted tool pins, re-hashed BEFORE any subprocess (git included)."""
    for k, t in profile["tools"].items():
        try:
            got = TR.sha256_file(Path(t["path"]))
        except OSError:
            raise AuthorRefused("profile_tool_unreadable", k) from None
        if got != t["sha256"]:
            raise AuthorRefused("profile_tool_mismatch", {"tool": k, "sha256": got})


def capture(cspec: T.TaskSpec, root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The oracle's cases at the base, run by the existing run_baseline under the provisional spec (only here)."""
    root.mkdir(parents=True)
    TR.make_npmrc(root)
    TR.check_pins(cspec)
    repo = TR.owned_clone(cspec, root)
    b, ev, wt = TR.run_baseline(cspec, repo, root)
    try:
        report = json.loads(b.head.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AuthorRefused("capture_report_not_json", ev) from None
    got, counts = TR.report_cases(report, wt)                              # refuses a suite-level error / a foreign file
    cases = [{"file": f, "fullName": n, "status": s, "failureFirstLine": first} for f, n, s, first in sorted(got, key=str)]
    failed = [c for c in cases if c["status"] == "failed"]
    if any(c["status"] not in ("passed", "failed") for c in cases):
        raise AuthorRefused("capture_case_status", [c for c in cases if c["status"] not in ("passed", "failed")][:5])
    if not failed:
        raise AuthorRefused("oracle_never_fails", "no oracle case fails at the base")
    if any(not (c["failureFirstLine"] or "").startswith(ASSERTION) for c in failed):
        raise AuthorRefused("baseline_not_assertion", [c for c in failed if not (c["failureFirstLine"] or "").startswith(ASSERTION)][:5])
    if b.rc != 1 or counts["numFailedTests"] != len(failed) or counts["numPassedTests"] != len(cases) - len(failed):
        raise AuthorRefused("capture_inconsistent", dict(ev, counts=counts, cases=len(cases), failed=len(failed)))
    return cases, dict(ev, cases=len(cases), failed=len(failed))


def prove_reference(spec: T.TaskSpec, repo: Path, proof: Path, patch: Path) -> dict[str, Any]:
    """The reference patch, committed on the base in the proof's owned clone, through the REAL verifier."""
    base = spec.doc["source"]["taskBaseCommit"]
    wt = proof / "reference-wt"
    add = TR._git("worktree", "add", "--detach", str(wt), base, cwd=repo)
    if add.returncode != 0:
        raise AuthorRefused("reference_worktree_failed", TR.git_stderr(add))
    ap = TR._git("apply", "--index", str(patch), cwd=wt)
    if ap.returncode != 0:
        raise AuthorRefused("reference_apply_failed", TR.git_stderr(ap))
    cm = TR._git(*AUTHOR, "commit", "-q", "-m", "task-author reference (proof only; never a worker artifact)", cwd=wt)
    if cm.returncode != 0:
        raise AuthorRefused("reference_commit_failed", TR.git_stderr(cm))
    art = TR._git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    run_dir = proof / "reference"
    run_dir.mkdir()
    rep: dict[str, Any] = {"mode": "reference-proof", "round": 1, "artifactRef": art, "specSha256": spec.sha256}

    def verdict(decision: str, why: str) -> P.Verdict:
        rep.update(decision=decision, why=why)
        return P.Verdict(decision, json.dumps(rep, sort_keys=True, default=str))

    TR.SpecVerifier(spec, repo, lambda: run_dir, proof).check_artifact(art, 1, rep, verdict)
    return rep


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(data)


RECORD_FAILED = "authoring_record_failed"    # authoring.json not written AFTER the folder exists (review 2005)


def prepare(draft_path: Path, profile_path: Path, reference_path: Path, out: Path) -> dict[str, Any]:
    """Everything before any effect. Raises AuthorRefused only; nothing is created."""
    raw_d, raw_p, raw_r = (read_input(draft_path, "draft"), read_input(profile_path, "profile"),
                           read_input(reference_path, "reference"))
    profile = parse_profile(raw_p)
    d = parse_draft(raw_d, profile["version"] == PROFILE_VERSION_V1)
    check_tool_pins(profile)                                               # BEFORE any subprocess
    if out.exists():
        raise AuthorRefused("out_exists", str(out))
    repo, base = Path(profile["repo"]), d["source"]["taskBaseCommit"]
    for key, code in (("anchorCommit", "anchor_not_found"), ("taskBaseCommit", "base_not_found")):   # review 2002 F2
        if TR._git("cat-file", "-e", f"{d['source'][key]}^{{commit}}", cwd=repo).returncode != 0:
            raise AuthorRefused(code, d["source"][key])
    lock = TR.blob_sha256(repo, base, "package-lock.json")
    if lock != profile["lock"]["packageLock"]:
        raise AuthorRefused("profile_lock_mismatch", lock)
    oracle_files = []
    for p in d["oracle"]:
        h = TR.blob_sha256(repo, base, p)
        if h is None:
            raise AuthorRefused("oracle_missing_at_base", p)
        oracle_files.append({"path": p, "sha256": h})
    provisional = [{"file": d["oracle"][0], "fullName": PROVISIONAL, "status": "failed", "failureFirstLine": PROVISIONAL}]
    capture_bytes = compose(profile, d, oracle_files, provisional, 1)
    try:
        cspec = T.parse(capture_bytes)                                     # the UNCHANGED parser; no bypass
    except T.SpecRefused as e:
        raise AuthorRefused("draft_spec_invalid", str(e)) from None
    return {"raw": (raw_d, raw_p, raw_r), "profile": profile, "draft": d, "oracleFiles": oracle_files,
            "captureBytes": capture_bytes, "captureSpec": cspec}


def draft(draft_path: Path, profile_path: Path, reference_path: Path, out: Path) -> tuple[int, dict[str, Any]]:
    out = Path(out).resolve()
    # ---- before any effect: exit 2. An unforeseen error here (e.g. a git timeout) is a typed refusal; nothing is created.
    try:
        pre = prepare(draft_path, profile_path, reference_path, out)
        out.mkdir(parents=True, exist_ok=False)
    except AuthorRefused:
        raise
    except Exception as e:                                                 # review 2005: the type name only
        raise AuthorRefused("pre_effect_error", type(e).__name__) from None
    # ---- effects: the exclusive DIR; exit 1 when not ready, evidence kept ----
    (raw_d, raw_p, raw_r), profile, d = pre["raw"], pre["profile"], pre["draft"]
    oracle_files, capture_bytes, cspec = pre["oracleFiles"], pre["captureBytes"], pre["captureSpec"]
    ev_dir = out / "evidence"
    rec: dict[str, Any] = {"version": AUTHORING_VERSION, "ready": False,
                           "inputs": {"draft": {"path": str(draft_path), "sha256": _sha(raw_d)},
                                      "profile": {"path": str(profile_path), "sha256": _sha(raw_p)},   # review 1989 R-b
                                      "reference": {"path": str(reference_path), "sha256": _sha(raw_r)}},
                           "captureSpec": {"path": str(ev_dir / "capture" / "capture-spec.json"), "sha256": cspec.sha256,
                                           "label": "PROVISIONAL: capture only; never a task spec"}}
    stage = "capture"
    try:
        _write_new(ev_dir / "capture" / "capture-spec.json", capture_bytes)
        cases, rec["capture"] = capture(cspec, ev_dir / "capture" / "run")
        stage = "final_spec"
        final_bytes = compose(profile, d, oracle_files, cases, 1)
        _write_new(ev_dir / "spec.json", final_bytes)
        final = T.load(ev_dir / "spec.json")                               # the UNCHANGED loader on the written bytes
        rec["spec"] = {"sha256": final.sha256, "cases": len(cases), "failed": rec["capture"]["failed"]}
        stage = "preflight"
        pf = TR.preflight(final, ev_dir / "proof")                         # UNCHANGED: must prove the captured cases
        rec["preflight"] = {"ok": pf["ok"], "cases": pf["baseline"]["cases"], "failed": pf["baseline"]["failed"],
                            "path": str(ev_dir / "proof" / TR.PREFLIGHT)}
        stage = "reference"
        _write_new(ev_dir / "reference.patch", raw_r)
        ref = prove_reference(final, Path(pf["repo"]), ev_dir / "proof", ev_dir / "reference.patch")
        _write_new(ev_dir / "reference.json", json.dumps(ref, indent=1, sort_keys=True, default=str).encode("utf-8"))
        rec["reference"] = {"decision": ref.get("decision"), "why": ref.get("why"), "artifactRef": ref.get("artifactRef"),
                            "path": str(ev_dir / "reference.json")}
        if ref.get("decision") != "accepted":
            raise AuthorRefused(f"reference_{ref.get('decision')}", ref.get("why"))
        stage = "package"
        _write_new(out / "package" / "spec.json", final_bytes)             # ONLY for a ready package; the same bytes
        if T.load(out / "package" / "spec.json").sha256 != final.sha256:
            raise AuthorRefused("package_mismatch")
        rec["package"] = {"spec": str(out / "package" / "spec.json"), "sha256": final.sha256}
        rec["ready"] = True
    except (AuthorRefused, TR.PreflightRefused, T.SpecRefused, OSError) as e:
        rec["notReady"] = {"stage": stage, "code": getattr(e, "code", type(e).__name__),
                           "detail": getattr(e, "detail", str(e))}
    except Exception as e:                       # review 2002 F1: anything else fails closed as a typed not-ready
        rec["notReady"] = {"stage": stage, "code": "unexpected_error", "detail": type(e).__name__}
    try:
        _write_new(out / "authoring.json", json.dumps(rec, indent=1, sort_keys=True, default=str).encode("utf-8"))   # LAST
    except Exception as e:                       # review 2005: the folder exists, so this is NOT "no effects"
        rec.update(ready=False, recordFailed={"code": RECORD_FAILED, "detail": type(e).__name__, "out": str(out)})
    return (0 if rec["ready"] else 1), rec


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="task_author", description="Author ONE pinned task spec and prove it (no model, no run).")
    ap.add_argument("command", choices=("draft",))
    ap.add_argument("--draft", type=Path, required=True)
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--reference", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        code, rec = draft(a.draft, a.profile, a.reference, a.out)
    except AuthorRefused as e:                                             # before any effect: nothing was created
        print(json.dumps({"refused": e.code, "detail": e.detail, "effects": "none"}, default=str))
        return 2
    print(json.dumps(rec, indent=1, default=str))
    return code

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
