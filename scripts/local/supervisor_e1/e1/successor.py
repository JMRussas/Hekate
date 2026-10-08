"""D3 v1 (plan 044; root msgs 1825/1831): a LINEAR successor's base is derived from its predecessor's ACCEPTED artifact,
with no operator step. TEST-SCOPED, offline.

successor-recipe.v0 (a closed file, pinned in the node value at import, validated before any write):
  {"version": "successor-recipe.v0",
   "template": <supervised-task-spec.v0 with source = {"repo"} only: NO anchorCommit / taskBaseCommit>,
   "oracle": [{"path", "sha256", "from": <absolute file with the exact bytes>, "replaces": null | <sha256 of the EXPECTED old blob>}]}
- `oracle` covers EXACTLY the template's oracle.files (same paths and sha256). `replaces: null` = a NEW file (it must be
  absent at the predecessor's artifact); otherwise the file must exist there with EXACTLY that old blob (msg 1831 ii).
  No unchecked overwrite.

materialize() for the expected recipe node, given its ONE accepted predecessor:
  1. bind the predecessor: its PlanStore artifact X == the run-owned ref in its OWNED clone == its evidence; X's parent
     == the base of the spec it ran with (pinned or resolved), whose sha256 == the evidence's specSha256 (msg 1831 i);
  2. re-hash the recipe and every `from` file (tamper -> stop) BEFORE any clone;
  3. an owned integration repo `<run_root>/<key>.integration`: `git clone --no-local` of the template's source repo
     (read only), fetch EXACTLY that ref from the predecessor's owned clone, check it equals X, detach at X;
  4. write the oracle files (new / exact-replace only), commit with a FIXED identity and date (deterministic base
     sha), on branch plan/<root8>/<key> in the OWNED repo only;
  5. resolve the frozen spec = template + source{repo: <integration repo>, anchorCommit: X, taskBaseCommit: base},
     written EXCLUSIVELY with provenance.json. The existing checks then apply unchanged (ancestry, preflight's
     anchor..base == exactly the oracle files, the claim pins). Never touches the primary repo; never pushes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from e1 import cli_worker as W
from e1 import consumer as C
from e1 import task_spec as T

VERSION = "successor-recipe.v0"
RECIPE_MAX = 64 << 10
FIXED_IDENTITY = {"GIT_AUTHOR_NAME": "hekate-plan-run", "GIT_AUTHOR_EMAIL": "hekate-plan-run@hekate.local",
                  "GIT_COMMITTER_NAME": "hekate-plan-run", "GIT_COMMITTER_EMAIL": "hekate-plan-run@hekate.local",
                  "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"}


class SuccessorStop(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(f"{code}: {detail}" if detail is not None else code)
        self.code, self.detail = code, detail


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _git(cwd: Path | str, *args: str, env: dict | None = None, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=text, timeout=600,
                          env={**W.git_env(), **(env or {})})


def parse_recipe(raw: bytes) -> dict[str, Any]:
    """The closed recipe, validated: the template is a valid v0 spec once given SOME anchor/base."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > RECIPE_MAX:
        raise SuccessorStop("recipe_size")
    try:
        d = C.strict_loads(bytes(raw), "recipe")
    except C.Refused as e:
        raise SuccessorStop("recipe_json", e.code) from None
    if not isinstance(d, dict) or set(d) != {"version", "template", "oracle"} or d["version"] != VERSION:
        raise SuccessorStop("recipe_shape", "keys must be exactly {version, template, oracle}")
    tpl = d["template"]
    if not isinstance(tpl, dict) or not isinstance(tpl.get("source"), dict) or set(tpl["source"]) != {"repo"}:
        raise SuccessorStop("recipe_shape", "template.source must be exactly {repo}")
    probe = copy.deepcopy(tpl)
    probe["source"] = {"repo": tpl["source"]["repo"], "anchorCommit": "1" * 40, "taskBaseCommit": "2" * 40}
    try:
        T.validate(probe)
    except T.SpecRefused as e:
        raise SuccessorStop("recipe_template", e.code) from None
    want = {f["path"]: f["sha256"] for f in tpl["oracle"]["files"]}
    oracle = d["oracle"]
    if not isinstance(oracle, list) or len(oracle) != len(want):
        raise SuccessorStop("recipe_oracle", "must cover exactly the template's oracle.files")
    for o in oracle:
        if not isinstance(o, dict) or set(o) != {"path", "sha256", "from", "replaces"}:
            raise SuccessorStop("recipe_shape", "oracle entries are exactly {path, sha256, from, replaces}")
        if want.get(o["path"]) != o["sha256"]:
            raise SuccessorStop("recipe_oracle", o.get("path"))
        if not isinstance(o["from"], str) or not Path(o["from"]).is_absolute():
            raise SuccessorStop("recipe_oracle", "from must be an absolute path")
        if o["replaces"] is not None and not (isinstance(o["replaces"], str) and T.HEX64.fullmatch(o["replaces"])):
            raise SuccessorStop("recipe_oracle", "replaces must be null or a sha256")
    if len({o["path"] for o in oracle}) != len(oracle):
        raise SuccessorStop("recipe_oracle", "duplicate path")
    return d


def load_recipe(path: str, pinned_sha: str) -> dict[str, Any]:
    """Load, re-hash against the pin, validate, and re-hash every oracle `from` file (tamper -> stop)."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise SuccessorStop("recipe_unreadable", path) from None
    if sha(raw) != pinned_sha:
        raise SuccessorStop("recipe_tamper", {"path": path, "want": pinned_sha, "got": sha(raw)})
    d = parse_recipe(raw)
    for o in d["oracle"]:
        try:
            got = sha(Path(o["from"]).read_bytes())
        except OSError:
            raise SuccessorStop("recipe_tamper", {"from": o["from"], "unreadable": True}) from None
        if got != o["sha256"]:
            raise SuccessorStop("recipe_tamper", {"from": o["from"], "want": o["sha256"], "got": got})
    return d


def bind_predecessor(pred_root: Path, artifact: str, ran_spec: T.TaskSpec) -> dict[str, Any]:
    """The predecessor's accepted artifact, bound to its OWN run: one pilot run dir, its evidence (specSha256 ==
    the spec it ran with), its run-owned ref resolving to X in its owned clone, and X's parent == that spec's base."""
    runs = sorted(Path(pred_root).glob("pilot-*/evidence.json"))
    if len(runs) != 1:
        raise SuccessorStop("predecessor_evidence", {"found": len(runs)})
    raw = runs[0].read_bytes()
    ev = json.loads(raw)
    if ev.get("specSha256") != ran_spec.sha256 or ev.get("outcome") != "accepted":
        raise SuccessorStop("predecessor_evidence", {"specSha256": ev.get("specSha256"), "outcome": ev.get("outcome")})
    refs = [r.split() for r in ev.get("ownedRefs") or [] if r.split()[0] == artifact]
    if len(refs) != 1:
        raise SuccessorStop("artifact_unavailable", {"artifact": artifact, "ownedRefs": ev.get("ownedRefs")})
    ref = refs[0][1]
    repo = Path(pred_root) / "repo"
    got = _git(repo, "rev-parse", "--verify", "-q", ref).stdout.strip()
    if got != artifact:
        raise SuccessorStop("artifact_unavailable", {"ref": ref, "resolves": got})
    if _git(repo, "rev-parse", f"{artifact}^").stdout.strip() != ran_spec.doc["source"]["taskBaseCommit"]:
        raise SuccessorStop("predecessor_base_mismatch", {"artifact": artifact})
    return {"ref": ref, "repo": str(repo), "evidence": str(runs[0]), "evidenceSha256": sha(raw), "specSha256": ran_spec.sha256}


def materialize(recipe: dict[str, Any], recipe_sha: str, pred: dict[str, Any], artifact: str, out: Path, branch: str) -> T.TaskSpec:
    """Build the successor's base in an OWNED integration repo and resolve its frozen spec (see module doc)."""
    out = Path(out)
    if out.exists():
        raise SuccessorStop("integration_exists", str(out))
    out.mkdir(parents=True)
    repo = out / "repo"
    tpl = recipe["template"]
    c = subprocess.run(["git", "clone", "-c", "core.longpaths=true", "--no-local", "--no-checkout", "-q", tpl["source"]["repo"], str(repo)],
                       capture_output=True, text=True, timeout=900, env=W.git_env())
    if c.returncode != 0:
        raise SuccessorStop("integration_clone_failed", c.stderr[-2000:])
    f = _git(repo, "fetch", "-q", pred["repo"], f"{pred['ref']}:refs/plan-run/predecessor")
    if f.returncode != 0 or _git(repo, "rev-parse", "refs/plan-run/predecessor").stdout.strip() != artifact:
        raise SuccessorStop("artifact_unavailable", {"fetch": f.stderr[-2000:]})
    if _git(repo, "checkout", "-q", "--detach", artifact).returncode != 0:
        raise SuccessorStop("integration_checkout_failed")
    for o in recipe["oracle"]:
        old = _git(repo, "show", f"{artifact}:{o['path']}", text=False)
        old_sha = sha(old.stdout) if old.returncode == 0 else None
        if o["replaces"] is None and old_sha is not None:
            raise SuccessorStop("oracle_conflict", {"path": o["path"], "exists": old_sha})
        if o["replaces"] is not None and old_sha != o["replaces"]:
            raise SuccessorStop("oracle_conflict", {"path": o["path"], "want": o["replaces"], "got": old_sha})
        data = Path(o["from"]).read_bytes()
        if sha(data) != o["sha256"]:
            raise SuccessorStop("recipe_tamper", {"from": o["from"]})
        target = repo / o["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if _git(repo, "add", "--", o["path"]).returncode != 0:
            raise SuccessorStop("integration_commit_failed", o["path"])
    msg = f"plan-run successor base\n\nrecipe {recipe_sha}\npredecessor {artifact}\n"
    if _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", msg, env=FIXED_IDENTITY).returncode != 0:
        raise SuccessorStop("integration_commit_failed")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if _git(repo, "branch", "-f", branch, base).returncode != 0:
        raise SuccessorStop("integration_commit_failed", "branch")
    doc = copy.deepcopy(tpl)
    doc["source"] = {"repo": repo.as_posix(), "anchorCommit": artifact, "taskBaseCommit": base}
    spec_raw = json.dumps(doc, indent=1, sort_keys=True).encode("utf-8") + b"\n"
    with open(out / "spec.resolved.json", "xb") as fh:
        fh.write(spec_raw)
    spec = T.load(out / "spec.resolved.json")
    prov = {"recipeSha256": recipe_sha, "predecessor": dict(pred, artifactRef=artifact),
            "oracle": [{"path": o["path"], "sha256": o["sha256"], "replaces": o["replaces"]} for o in recipe["oracle"]],
            "integrationRepo": repo.as_posix(), "branch": branch, "base": base, "resolvedSpecSha256": spec.sha256}
    with open(out / "provenance.json", "x", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(prov, indent=1, sort_keys=True))
    return spec
