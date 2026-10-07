-- FIXTURE-ONLY E2d candidate store (plan 032 rev 6 §3a). Applied ONLY after journal_schema.sql and
-- acts_schema.sql in the harness's disposable hekate_plan_e1_* database. Not a production migration.
-- A candidate grants NO authority; only the committed review_rebind record (in
-- supervisor_journal.records) does. Rows are immutable: a conflicting identity can never overwrite one.

CREATE TABLE supervisor_journal.e2d_candidates (
    handoff_id       uuid PRIMARY KEY,
    root             text NOT NULL,
    claim_key        text NOT NULL,
    review_id        text NOT NULL CHECK (length(review_id) = 64),
    candidate_digest text NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    -- Storage bounds allow for JSON escaping (up to 6 bytes per delivered byte, e.g. a NUL as a six-character escape); the
    -- delivered-byte caps (032 §3: required 64 KiB, total 96 KiB) are enforced before storage.
    manifest         text NOT NULL CHECK (octet_length(manifest) <= 1048576),
    envelope         text NOT NULL CHECK (octet_length(envelope) <= 1048576),
    task             text NOT NULL CHECK (octet_length(task) <= 1048576),    -- the exact H1-shaped Task bytes (text + instructions)
    prepared_at_json text NOT NULL,
    FOREIGN KEY (root, claim_key) REFERENCES supervisor_journal.streams(root, claim_key) ON DELETE CASCADE
);

CREATE FUNCTION supervisor_journal.guard_e2d() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'SJ409: % TRUNCATE refused', TG_TABLE_NAME;
    END IF;
    IF TG_OP = 'DELETE' AND coalesce(current_setting('supervisor_journal.compaction', true), '') IN ('on', 'evict') THEN
        RETURN OLD;                                   -- leaves only with its stream, inside compaction
    END IF;
    RAISE EXCEPTION 'SJ409: % rows are immutable', TG_TABLE_NAME;
END $$;

CREATE TRIGGER trg_e2d_candidates_guard BEFORE UPDATE OR DELETE ON supervisor_journal.e2d_candidates
    FOR EACH ROW EXECUTE FUNCTION supervisor_journal.guard_e2d();
CREATE TRIGGER trg_e2d_candidates_truncate BEFORE TRUNCATE ON supervisor_journal.e2d_candidates
    FOR EACH STATEMENT EXECUTE FUNCTION supervisor_journal.guard_e2d();
