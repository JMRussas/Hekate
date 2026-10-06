# Claim-envelope interoperability fixtures

Captured by codex-hekate on 2026-10-06 from the real plan-contract API built from clean commit `bb2af8b`, using a fresh disposable database. These are raw UTF-8 HTTP response bodies, saved without JSON reserialization. IDs, timestamps, actors, artifacts and evidence are generated test data; they contain no production data. The process-level HTTP suite passed 66 checks and dropped its database.

- `claim-claimed.json`: first claim, current in-progress attempt, null content value and attributes, completed/accepted prerequisite with attempt epoch 1.
- `claim-replayed.json`: same receipt and current attempt, replayed flag true.
- `claim-no-ready-work.json`: durable no-work receipt, nullable attempt/node/content/prerequisite fields, no current attempt.

The content digest is the opaque uppercase Hekate structured-content digest; prerequisite digest is lowercase and domain-separated. Neither is a raw rendered-text hash. These fixtures have no nested prerequisite epoch 0; test that valid boundary separately without describing a mutated fixture as an actual captured response. UUID ordering and timestamps are incidental; do not infer chronological ordering or authority from them.

Fixtures support ChatAgent's pure envelope/context seam and the proposed supervised fake-worker experiment in [023](../../023-coding-worker-adapter-proposal.md). They do not prove launch safety, execution idempotency, authorization, leases or recovery.
