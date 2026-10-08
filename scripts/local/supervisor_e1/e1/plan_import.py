"""plan-import.v0 (plan 042; root msg 1793): a CLOSED roadmap slice imported into PlanStore as ONE managed plan of
dependent task nodes. TEST-SCOPED: P1 runs only against the disposable harness database.

PlanStore is the only authority: nodes, edges, readiness and acceptance live there; this module keeps no status.
It builds the plan only through the existing contract routes (create plan, add child, add dependency; the
plan's default gate is Accepted) and the operator content route (pin a spec).

- EVERY input is validated before ANY write: the closed JSON shape, keys, names, the dependency graph (known
  keys, no self edge, acyclic, 1..8 nodes) and every given task spec (loadable, valid supervised-task-spec.v0,
  sha256 equal to the declared one). A node may declare its spec as `null` (pending): with operator-prepared
  bases (D3 v0) a successor's spec can only be frozen after its predecessor is accepted and integrated.
- IDENTITIES ARE DETERMINISTIC: root = uuid5(project, sha256(import bytes)), node = uuid5(root, key). PlanStore
  does NOT replay an operation key once the revision has moved on (plan_exists / stale_revision, msg 1793 probe),
  so the import RECONCILES against the authoritative view: an existing step equal to the document is skipped, a
  missing one is applied at the current revision, anything else (other content, extra nodes or edges) is a typed
  `import_conflict` with no write. A re-import or a resume after a crash in the middle of an import therefore
  completes or no-ops; it can never duplicate a plan, a node or an edge.
- A node's spec reference is its CONTENT VALUE (D2): `task-spec.v0 sha256=<64 hex> path=<absolute path>`, or
  `task-spec.v0 pending`. No new attribute key and no PlanStore change.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from e1 import consumer as C
from e1 import task_spec as T

VERSION = "plan-import.v0"
MAX_BYTES = 64 << 10
NODES_MAX = 8
KEY = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PENDING = "task-spec.v0 pending"
SPEC_REF = re.compile(r"^task-spec\.v0 sha256=([0-9a-f]{64}) path=(\S.{0,398})$")
RECIPE_REF = re.compile(r"^task-recipe\.v0 sha256=([0-9a-f]{64}) path=(\S.{0,398})$")
NS = uuid.UUID("6b1e7a50-6f0e-4f7a-9a52-3b0c2e1f0d42")          # plan-import.v0 namespace (fixed)


class ImportRefused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(f"{code}: {detail}" if detail is not None else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class NodeDecl:
    key: str
    name: str
    spec_path: str | None
    spec_sha256: str | None
    after: tuple[str, ...]
    kind: str | None = None                  # "spec" | "recipe" (D3 v1) | None (pending)


@dataclass(frozen=True)
class ImportDoc:
    title: str
    nodes: tuple[NodeDecl, ...]
    sha256: str                              # of the exact import bytes


@dataclass(frozen=True)
class ImportedPlan:
    """The deterministic identities of one import into one project (derived, never stored as status)."""
    doc: ImportDoc
    project_id: str
    root: str
    node_ids: dict[str, str]                 # key -> node id
    after: dict[str, tuple[str, ...]]        # key -> predecessor keys
    applied: tuple[str, ...] = ()            # the steps THIS call applied (evidence only)

    def key_of(self, node_id: str) -> str | None:
        return next((k for k, v in self.node_ids.items() if v == node_id), None)


def spec_value(path: str | None, sha: str | None) -> str:
    return PENDING if path is None else f"task-spec.v0 sha256={sha} path={path}"


def node_value(n: NodeDecl) -> str:
    if n.kind == "recipe":
        return f"task-recipe.v0 sha256={n.spec_sha256} path={n.spec_path}"
    return spec_value(n.spec_path, n.spec_sha256)


def parse_node_ref(value: Any) -> tuple[str, str, str] | None:
    """("spec" | "recipe", sha256, path), or None when pending. Anything else is not a plan-run node."""
    if value == PENDING:
        return None
    for kind, rx in (("spec", SPEC_REF), ("recipe", RECIPE_REF)):
        m = rx.fullmatch(value) if isinstance(value, str) else None
        if m:
            return kind, m.group(1), m.group(2)
    raise ImportRefused("node_value_not_a_spec_ref", value if isinstance(value, str) else type(value).__name__)


def parse_spec_value(value: Any) -> tuple[str, str] | None:
    """(sha256, path) of a pinned spec reference; None when pending. Anything else is not a plan-run node."""
    if value == PENDING:
        return None
    m = SPEC_REF.fullmatch(value) if isinstance(value, str) else None
    if not m:
        raise ImportRefused("node_value_not_a_spec_ref", value if isinstance(value, str) else type(value).__name__)
    return m.group(1), m.group(2)


def _abs(p: Any, where: str) -> str:
    if not isinstance(p, str) or not 3 <= len(p) <= 398 or any(c.isspace() for c in p):
        raise ImportRefused("import_value", f"{where}: an absolute path without whitespace")
    if not (PurePosixPath(p).is_absolute() or re.match(r"^[A-Za-z]:/", p)) or "\\" in p:
        raise ImportRefused("import_value", f"{where}: an absolute forward-slash path")
    return p


def load_spec(path: str, sha: str) -> T.TaskSpec:
    """The spec file must load, validate and hash to the pinned sha256 (re-checked whenever it is used)."""
    try:
        spec = T.load(Path(path))
    except T.SpecRefused as e:
        raise ImportRefused("spec_invalid", {"path": path, "code": e.code}) from None
    if spec.sha256 != sha:
        raise ImportRefused("spec_sha_mismatch", {"path": path, "want": sha, "got": spec.sha256})
    return spec


def parse(raw: bytes) -> ImportDoc:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_BYTES:
        raise ImportRefused("import_size")
    try:
        d = C.strict_loads(bytes(raw), "plan-import")
    except C.Refused as e:
        raise ImportRefused("import_json", e.code) from None
    if not isinstance(d, dict) or set(d) != {"version", "title", "nodes"} or d["version"] != VERSION:
        raise ImportRefused("import_shape", "keys must be exactly {version, title, nodes}, version plan-import.v0")
    if not isinstance(d["title"], str) or not 1 <= len(d["title"]) <= 200:
        raise ImportRefused("import_value", "title")
    nodes = d["nodes"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= NODES_MAX:
        raise ImportRefused("import_value", f"nodes must hold 1..{NODES_MAX}")
    decls: list[NodeDecl] = []
    for i, n in enumerate(nodes):
        if not isinstance(n, dict) or set(n) != {"key", "name", "spec", "after"}:
            raise ImportRefused("import_shape", f"nodes[{i}] keys must be exactly {{key, name, spec, after}}")
        if not isinstance(n["key"], str) or not KEY.fullmatch(n["key"]):
            raise ImportRefused("import_value", f"nodes[{i}].key")
        if not isinstance(n["name"], str) or not 1 <= len(n["name"]) <= 200:
            raise ImportRefused("import_value", f"nodes[{i}].name")
        spec, kind = n["spec"], "spec"
        if isinstance(spec, dict) and set(spec) == {"recipe"}:          # D3 v1: a successor recipe
            spec, kind = spec["recipe"], "recipe"
        if spec is None and kind == "spec":
            path = sha = kind = None
        elif isinstance(spec, dict) and set(spec) == {"path", "sha256"}:
            path = _abs(spec["path"], f"nodes[{i}].spec.path")
            if not isinstance(spec["sha256"], str) or not HEX64.fullmatch(spec["sha256"]):
                raise ImportRefused("import_value", f"nodes[{i}].spec.sha256")
            sha = spec["sha256"]
        else:
            raise ImportRefused("import_shape", f"nodes[{i}].spec must be null, {{path, sha256}} or {{recipe: {{path, sha256}}}}")
        after = n["after"]
        if not isinstance(after, list) or not all(isinstance(a, str) for a in after) or len(set(after)) != len(after):
            raise ImportRefused("import_value", f"nodes[{i}].after must be a list of distinct keys")
        if kind == "recipe" and len(after) != 1:
            raise ImportRefused("import_value", f"nodes[{i}]: a recipe node is LINEAR: exactly one predecessor (D3 v1)")
        decls.append(NodeDecl(n["key"], n["name"], path, sha, tuple(after), kind))
    keys = [n.key for n in decls]
    if len(set(keys)) != len(keys):
        raise ImportRefused("import_value", "duplicate node key")
    for n in decls:
        for a in n.after:
            if a not in keys:
                raise ImportRefused("import_graph", f"{n.key} after unknown {a}")
            if a == n.key:
                raise ImportRefused("import_graph", f"{n.key} after itself")
    indeg = {n.key: len(n.after) for n in decls}                          # Kahn: the graph must be acyclic
    succ = {k: [n.key for n in decls if k in n.after] for k in keys}
    queue, seen = [k for k in keys if indeg[k] == 0], 0
    while queue:
        k = queue.pop()
        seen += 1
        for s in succ[k]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    if seen != len(keys):
        raise ImportRefused("import_graph", "cycle")
    return ImportDoc(d["title"], tuple(decls), hashlib.sha256(bytes(raw)).hexdigest())


def validate(doc: ImportDoc) -> dict[str, T.TaskSpec]:
    """Every declared spec, loaded and hash-checked (before any write)."""
    from e1 import successor as S
    for n in doc.nodes:
        if n.kind == "recipe":
            try:
                S.load_recipe(n.spec_path, n.spec_sha256)
            except S.SuccessorStop as e:
                raise ImportRefused("recipe_invalid", {"node": n.key, "code": e.code, "detail": e.detail}) from None
    return {n.key: load_spec(n.spec_path, n.spec_sha256) for n in doc.nodes if n.kind == "spec"}


def identities(doc: ImportDoc, project_id: str) -> ImportedPlan:
    try:
        project = str(uuid.UUID(project_id))
    except (ValueError, TypeError):
        raise ImportRefused("import_value", "project id") from None
    root = str(uuid.uuid5(NS, f"{VERSION}|{project}|{doc.sha256}"))
    ids = {n.key: str(uuid.uuid5(uuid.UUID(root), n.key)) for n in doc.nodes}
    return ImportedPlan(doc, project, root, ids, {n.key: n.after for n in doc.nodes})


def _ok(resp, what: str) -> dict[str, Any]:
    if resp.status != 200:
        raise ImportRefused(f"{what}_refused", {"status": resp.status, "code": resp.code})
    return resp.body


def _content_matches(n: NodeDecl, got: dict[str, Any]) -> bool:
    """As declared (revision 1), or, for a node declared pending only, an operator pin at a later revision."""
    if got.get("contentRevision") == 1 and got.get("value") == node_value(n):
        return True
    if n.kind is None and isinstance(got.get("contentRevision"), int) and got["contentRevision"] > 1:
        try:
            return parse_node_ref(got.get("value")) is not None
        except ImportRefused:
            return False
    return False


def _op(doc: ImportDoc, step: str) -> str:
    return f"pi-{doc.sha256[:24]}-{step}"


def _check_view(doc: ImportDoc, plan: ImportedPlan, v: dict[str, Any]) -> tuple[dict[str, Any], set[tuple[str, str]]]:
    """The authoritative view against the document: anything existing that is NOT exactly declared (or an authorized
    operator pin of a node declared pending) is import_conflict. Returns (nodes by id, existing edges)."""
    if v.get("projectId") != plan.project_id:
        raise ImportRefused("import_conflict", "root exists in another project")
    by_id = {n["id"]: n for n in v["nodes"]}
    root_node = by_id.get(plan.root)
    if root_node is None or root_node.get("name") != doc.title:
        raise ImportRefused("import_conflict", "root name")
    expected_ids = set(plan.node_ids.values()) | {plan.root}
    if set(by_id) - expected_ids:
        raise ImportRefused("import_conflict", {"unexpectedNodes": sorted(set(by_id) - expected_ids)})
    want_edges = {(plan.node_ids[n.key], plan.node_ids[a]) for n in doc.nodes for a in n.after}
    have_edges = {(e["successorId"], e["predecessorId"]) for e in v["dependencies"]}
    if have_edges - want_edges or any(e.get("gate") not in (None, "accepted") for e in v["dependencies"]):
        raise ImportRefused("import_conflict", "unexpected dependency edges or gates")
    # Every existing node must be EXACTLY the declared one; otherwise nothing is written. The ONE authorized drift
    # (review 1899a): a node DECLARED pending may carry an operator pin, a valid spec/recipe ref at contentRevision > 1.
    for order, n in enumerate(doc.nodes):
        got = by_id.get(plan.node_ids[n.key])
        if got is not None and not (got.get("parentId") == plan.root and got.get("nodeType") == "task" and got.get("name") == n.name
                                    and got.get("siblingOrder") == order and got.get("contentAttributes") == {}
                                    and _content_matches(n, got)):
            raise ImportRefused("import_conflict", {"node": n.key})
    return by_id, have_edges


def import_plan(setup, project_id: str, raw: bytes) -> ImportedPlan:
    """Validate everything, then create or reconcile the plan. Returns the deterministic identities."""
    doc = parse(raw)
    validate(doc)
    plan = identities(doc, project_id)
    applied: list[str] = []
    view = setup.plan(plan.root)
    if view.status == 404:
        _ok(setup.create_plan(plan.root, plan.project_id, doc.title, _op(doc, "root")), "create_plan")
        applied.append("root")
        view = setup.plan(plan.root)
    v = _ok(view, "plan")
    by_id, have_edges = _check_view(doc, plan, v)
    for order, n in enumerate(doc.nodes):
        if plan.node_ids[n.key] in by_id:
            continue
        rev = _ok(setup.plan(plan.root), "plan")
        root_rev = next(x["stateRevision"] for x in rev["nodes"] if x["id"] == plan.root)
        _ok(setup.add_child(plan.root, plan.node_ids[n.key], n.name, order, _op(doc, f"n-{n.key}"), root_rev,
                            value=node_value(n)), "add_child")
        applied.append(f"node:{n.key}")
    for n in doc.nodes:
        for a in n.after:
            edge = (plan.node_ids[n.key], plan.node_ids[a])
            if edge in have_edges:
                continue
            cur = _ok(setup.plan(plan.root), "plan")
            succ_rev = next(x["stateRevision"] for x in cur["nodes"] if x["id"] == edge[0])
            _ok(setup.add_dependency(edge[0], edge[1], _op(doc, f"e-{n.key}-{a}"), succ_rev), "add_dependency")
            applied.append(f"edge:{n.key}<-{a}")
    return ImportedPlan(doc, plan.project_id, plan.root, plan.node_ids, plan.after, tuple(applied))


def attach_plan(setup, project_id: str, raw: bytes) -> ImportedPlan:
    """A LATER run of the SAME plan file on a persistent store (root msg 1901): re-derive the identities from the
    bytes and VERIFY the authoritative plan with NO write. Missing nodes or edges are plan_drift; anything else not
    exactly declared (except an operator pin of a pending node) is import_conflict; an absent plan is plan_missing."""
    doc = parse(raw)
    validate(doc)
    plan = identities(doc, project_id)
    view = setup.plan(plan.root)
    if view.status == 404:
        raise ImportRefused("plan_missing", plan.root)
    by_id, have_edges = _check_view(doc, plan, _ok(view, "plan"))
    missing = [f"node:{n.key}" for n in doc.nodes if plan.node_ids[n.key] not in by_id]
    missing += [f"edge:{n.key}<-{a}" for n in doc.nodes for a in n.after if (plan.node_ids[n.key], plan.node_ids[a]) not in have_edges]
    if missing:
        raise ImportRefused("plan_drift", missing)
    return plan


def pin_spec(setup, plan: ImportedPlan, key: str, path: str) -> str:
    """OPERATOR action (D3 v0): pin a frozen spec on a not-yet-started node by revising its content value.
    The spec is loaded and validated first; the node must be Todo with no attempt. Returns the new value."""
    if key not in plan.node_ids:
        raise ImportRefused("pin_unknown_node", key)
    _abs(path, "spec path")
    spec = T.load(Path(path)) if Path(path).is_file() else None
    if spec is None:
        raise ImportRefused("spec_invalid", {"path": path})
    node = next((n for n in _ok(setup.plan(plan.root), "plan")["nodes"] if n["id"] == plan.node_ids[key]), None)
    if node is None or node["work"] != "todo" or node["attemptEpoch"] != 0:
        raise ImportRefused("pin_not_todo", {"node": key, "work": node and node["work"]})
    value = spec_value(path, spec.sha256)
    _ok(setup.revise(node["id"], value, node["contentRevision"], f"pin-{plan.root[:8]}-{key}-{spec.sha256[:16]}",
                     node["stateRevision"]), "pin")
    return value
