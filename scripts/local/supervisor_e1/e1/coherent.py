"""FIXTURE-ONLY coherent read and finish proof (plan 026 rev 5 §5 C7; E2a test-only).

NOT a production mechanism. The public API's event page and node view are independent reads,
so together they are not one snapshot. In E2a the facts are gathered with direct SQL inside one
REPEATABLE READ transaction on the disposable test database (via the harness's psql). A future
production proof needs an explicitly reviewed coherent-read mechanism; without one the outcome
is proof_missing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class CoherentFacts:
    node: dict[str, Any] | None      # plan_node_state row
    events: list[dict[str, Any]]     # ALL plan_attempt_events rows for the node, ordered by seq
    event_count: int                 # independent count inside the same snapshot
    complete: bool                   # len(events) == event_count


def read_coherent(psql: Callable[[str], str], node_id: str) -> CoherentFacts:
    """One statement inside one REPEATABLE READ transaction: the node row, every event, a count."""
    if not UUID.fullmatch(node_id):
        raise ValueError("node id must be a uuid")
    sql = (
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; "
        "SELECT json_build_object("
        f"'node', (SELECT row_to_json(s) FROM plan_node_state s WHERE node_id = '{node_id}'), "
        f"'events', (SELECT coalesce(json_agg(row_to_json(e) ORDER BY e.seq), '[]'::json) FROM plan_attempt_events e WHERE e.node_id = '{node_id}'), "
        f"'count', (SELECT count(*) FROM plan_attempt_events WHERE node_id = '{node_id}'))::text; "
        "COMMIT;"
    )
    out = [line for line in psql(sql).splitlines() if line.strip().startswith("{")]
    if len(out) != 1:
        return CoherentFacts(None, [], -1, False)
    doc = json.loads(out[0])
    events = doc.get("events") or []
    return CoherentFacts(doc.get("node"), events, int(doc.get("count", -1)), len(events) == int(doc.get("count", -1)))


def prove_finish(facts: CoherentFacts, held: dict[str, Any], pkg: Any) -> str:
    """proved | superseded | unconfirmed | intervening | proof_missing.

    proved:      exactly one attempt_finished event matches EVERY held fact (operation key, actor,
                 root, node, attempt id, epoch, executor reference, artifact, content revision and
                 both pins) and its nodeStateRevision equals the node's CURRENT revision, all in one
                 coherent snapshot.
    superseded:  that fully matching event exists but the revision has moved since.
    unconfirmed: no matching event and the node is still InProgress for this attempt at the held
                 expected revision. NOT proof the finish did not happen: it may still commit.
    intervening: no matching event and the node moved otherwise.
    proof_missing: incomplete/ambiguous facts, or a PARTIAL match. Absence is never inferred from
                 partial events.
    """
    if not facts.complete or facts.node is None:
        return "proof_missing"
    n = facts.node
    same_key = [e for e in facts.events if e.get("operation_key") == held["operationKey"]]
    if len(same_key) > 1:
        return "proof_missing"
    if not same_key:
        if (n.get("work_status") == "in_progress" and n.get("attempt_id") == held["attemptId"]
                and n.get("attempt_epoch") == held["attemptEpoch"] and n.get("state_revision") == held["expectedStateRevision"]):
            return "unconfirmed"
        return "intervening"
    e = same_key[0]
    expected = {
        "kind": "attempt_finished", "actor": held["actor"], "root_node_id": pkg.root_id, "node_id": pkg.node_id,
        "attempt_id": held["attemptId"], "attempt_epoch": held["attemptEpoch"], "executor_ref": held.get("executorRef"),
        "artifact_ref": held["artifactRef"], "content_revision": pkg.content_revision,
        "attempt_content_revision": pkg.content_revision, "attempt_prereq_digest": pkg.prereq_digest,
        "node_state_revision": held["expectedStateRevision"] + 1,
    }
    if any(e.get(k) != v for k, v in expected.items()):
        return "proof_missing"          # partial match: never claim proof
    return "proved" if n.get("state_revision") == e["node_state_revision"] else "superseded"
