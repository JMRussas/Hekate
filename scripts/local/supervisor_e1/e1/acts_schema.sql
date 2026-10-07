-- FIXTURE-ONLY E2c additions (plan 030 rev 7 §12). Applied ONLY after journal_schema.sql in the
-- harness's disposable hekate_plan_e1_* database. Not a production migration.
--
-- Act RECORDS live in supervisor_journal.records (the E2b-a rules: reservations, required outcomes,
-- idempotent ids, hash chain, fencing, corrupt-stream retention). Bookkeeping is DERIVED from those
-- validated records. Only two diagnostic, bounded tables are new:
--   e2c_counters: per (stream, key, name) saturating u32 counters (counter-only outcomes write no record);
--   e2c_queue:    at most one operator-queue entry per (stream, key, reason).
-- The guards are integrity checks for the well-behaved path, NOT authorization.

CREATE TABLE supervisor_journal.e2c_counters (
    root       text NOT NULL,
    claim_key  text NOT NULL,
    stream_key text NOT NULL CHECK (length(stream_key) BETWEEN 1 AND 128),
    name       text NOT NULL CHECK (name IN ('malformed', 'stale', 'foreign_session', 'conflict', 'out_of_order', 'not_novel',
                                             'novelty_unknown', 'progress_overflow', 'duplicate', 'observation_overflow')),
    value      bigint NOT NULL CHECK (value BETWEEN 0 AND 4294967295),
    saturated  boolean NOT NULL DEFAULT false,
    PRIMARY KEY (root, claim_key, stream_key, name),
    FOREIGN KEY (root, claim_key) REFERENCES supervisor_journal.streams(root, claim_key) ON DELETE CASCADE
);

CREATE TABLE supervisor_journal.e2c_queue (
    root       text NOT NULL,
    claim_key  text NOT NULL,
    stream_key text NOT NULL CHECK (length(stream_key) BETWEEN 1 AND 128),
    reason     text NOT NULL CHECK (length(reason) BETWEEN 1 AND 64),
    at_json    text NOT NULL,
    PRIMARY KEY (root, claim_key, stream_key, reason),
    FOREIGN KEY (root, claim_key) REFERENCES supervisor_journal.streams(root, claim_key) ON DELETE CASCADE
);

-- runId -> its one dispatch, across ALL streams (030 §8: a runId is never reused for another
-- AttemptKey). Inserted in the same route A transaction as the dispatch_intent, after the
-- global -> writer -> stream locks; the primary key enforces uniqueness without any scan, but
-- ONLY while the row is retained: it is deleted with its stream on compaction/eviction (msg 1190).
CREATE TABLE supervisor_journal.e2c_runs (
    run_id     text PRIMARY KEY CHECK (length(run_id) BETWEEN 1 AND 256),
    root       text NOT NULL,
    claim_key  text NOT NULL,
    exec_id    text NOT NULL CHECK (length(exec_id) = 64),
    FOREIGN KEY (root, claim_key) REFERENCES supervisor_journal.streams(root, claim_key) ON DELETE CASCADE
);

-- Counters only grow (or saturate); queue and run entries are immutable. Rows leave only with their
-- stream, inside a compaction transaction. TRUNCATE is refused.
CREATE FUNCTION supervisor_journal.guard_e2c() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'SJ409: % TRUNCATE refused', TG_TABLE_NAME;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF coalesce(current_setting('supervisor_journal.compaction', true), '') NOT IN ('on', 'evict') THEN
            RAISE EXCEPTION 'SJ409: DELETE on % outside a compaction transaction', TG_TABLE_NAME;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_TABLE_NAME IN ('e2c_queue', 'e2c_runs') THEN
        RAISE EXCEPTION 'SJ409: % rows are immutable', TG_TABLE_NAME;
    END IF;
    IF (NEW.root, NEW.claim_key, NEW.stream_key, NEW.name) IS DISTINCT FROM (OLD.root, OLD.claim_key, OLD.stream_key, OLD.name)
       OR NEW.value < OLD.value OR (OLD.saturated AND NOT NEW.saturated) THEN
        RAISE EXCEPTION 'SJ409: e2c counters only grow';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_e2c_counters_guard BEFORE UPDATE OR DELETE ON supervisor_journal.e2c_counters
    FOR EACH ROW EXECUTE FUNCTION supervisor_journal.guard_e2c();
CREATE TRIGGER trg_e2c_counters_truncate BEFORE TRUNCATE ON supervisor_journal.e2c_counters
    FOR EACH STATEMENT EXECUTE FUNCTION supervisor_journal.guard_e2c();
CREATE TRIGGER trg_e2c_queue_guard BEFORE UPDATE OR DELETE ON supervisor_journal.e2c_queue
    FOR EACH ROW EXECUTE FUNCTION supervisor_journal.guard_e2c();
CREATE TRIGGER trg_e2c_queue_truncate BEFORE TRUNCATE ON supervisor_journal.e2c_queue
    FOR EACH STATEMENT EXECUTE FUNCTION supervisor_journal.guard_e2c();
CREATE TRIGGER trg_e2c_runs_guard BEFORE UPDATE OR DELETE ON supervisor_journal.e2c_runs
    FOR EACH ROW EXECUTE FUNCTION supervisor_journal.guard_e2c();
CREATE TRIGGER trg_e2c_runs_truncate BEFORE TRUNCATE ON supervisor_journal.e2c_runs
    FOR EACH STATEMENT EXECUTE FUNCTION supervisor_journal.guard_e2c();
