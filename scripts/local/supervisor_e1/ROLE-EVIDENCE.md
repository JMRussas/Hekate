# Managed role evidence manifest (`hekate-role-evidence.v0`)

`e1/role_evidence.py` is a pure, stdlib-only bundle builder/verifier for future managed role wiring. It does no file,
network, DB or clock access. The host supplies named exact bytes and strict metadata; the module measures and hashes
them. It adds no DB schema and no event system.

## Interface

```python
built = build_manifest(identity, {"stdout.txt": Payload(data, "text/plain", "stdout", "restricted_raw")},
                       max_file_bytes=..., max_total_bytes=...)      # raises EvidenceRefused(code)
built.body      # canonical manifest bytes
built.digest    # sha256 hex of exactly built.body (no self-hash inside the manifest)
built.manifest()  # fresh dict copy on every call

result = verify_manifest(body, {"stdout.txt": data}, expected={...}, max_file_bytes=..., max_total_bytes=...,
                         expected_digest=None)                      # VerifyResult(ok, code, name); never raises
```

Identity fields (all required, exact keys, unknown fields refused): `planRoot`, `taskId` (lowercase UUIDs), `runId`,
`attemptId`, `epoch` (positive attempt epoch), `stateRevision` and `contentRevision` (positive; distinct from the
attempt epoch, which is never used as a content revision), integers in `1..2**53-1` (safe JS range),
`claimKey`/`operationKey` (id or `null`),
`role{id,version,definitionSha256}`, `binding{binding,provider,model}` (`"unknown"` where the host cannot attest),
`limits` (<=16 non-negative int counters), `source{revision,snapshotSha256}`, `reviewerRefs` (<=8 ids), `linkage`.

`linkage` is `unlinked` (e.g. direct CLI streams without claim receipts; `claimKey` must be `null`) or
`host_asserted`. `operationKey` is operation correlation (e.g. a manual transition), allowed on unlinked attempts; a
`claimKey` names a native trace claim receipt, which an unlinked attempt does not have, so it is refused there. Native trace linkage is never claimed. Task/attempt identity is correlation data only and grants no
authority; the manifest says `authority: none`.

Payload entries carry name, content type, purpose, access (`restricted_raw` | `review_export`), byte count, sha256.

## Bounds

Caller supplies `max_file_bytes` / `max_total_bytes` (hard ceilings 16 MiB / 64 MiB). Count <= 32, names are bare
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}` (no `..`, trailing dot, separators or case-fold duplicates), manifest <= 64 KiB.
Lengths are compared with budgets before any hashing; verify checks the manifest size before parsing. Counters must be
real ints (no bool/float/NaN) and mapping keys must be strings. Inputs are never mutated or coerced.

## Verify

Checks, in order: budgets, expected identity shape, payload names/sizes, manifest size, strict parse (duplicate keys,
NaN, unknown fields, wrong schema refused), canonical re-encoding equality, optional expected digest, expected identity
(`expected` must name planRoot, taskId, runId, attemptId, epoch, stateRevision, contentRevision; other keys compared when given), exact
membership (missing and extra refused), lengths, then sha256. Failures are fixed codes plus at most a validated payload
name; no content appears in errors.

## Limitations

- Same-machine SHA-256 does not authenticate an actor and does not prove evidence is immutable or unaltered by someone
  who can rewrite both manifest and payloads. Pin `expected_digest` out of band if that matters.
- Verification is byte/identity consistency only. `host_asserted` linkage is the host's claim; the verifier cannot
  establish it from manifest metadata.
- `review_export` is a label. It is not proof of sanitizing; no provider-secret or private-reasoning scrubbing is
  claimed, and `restricted_raw` payloads stay restricted. Payload bytes are arbitrary and never interpreted.
- Canonical form is `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` UTF-8,
  versioned to this schema only; no RFC 8785 or cross-language equivalence is claimed.

## Tests

From `scripts/local/supervisor_e1`: `uv run --python 3.13.13 pytest tests/test_role_evidence.py`
