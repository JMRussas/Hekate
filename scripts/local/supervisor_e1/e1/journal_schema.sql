-- FIXTURE-ONLY supervisor journal schema (plan 028 §9, E2b-a). Applied ONLY to the harness's
-- disposable hekate_plan_e1_* database. Not a production migration; nothing here is added to
-- PlanStoreSchema. The guards below are integrity checks for the well-behaved path, NOT
-- authorization: any credentialed database user can set the session flags or disable triggers.

CREATE SCHEMA supervisor_journal;

-- Singleton: totals over ALL writers plus the SHARED caps (one row, so writers cannot diverge).
CREATE TABLE supervisor_journal.global_usage (
    id                 int PRIMARY KEY CHECK (id = 1),
    count              bigint NOT NULL CHECK (count >= 0),
    bytes              bigint NOT NULL CHECK (bytes >= 0),
    per_stream         int NOT NULL CHECK (per_stream > 0),
    unresolved_streams int NOT NULL CHECK (unresolved_streams > 0),
    global_records     bigint NOT NULL CHECK (global_records > 0),
    global_bytes       bigint NOT NULL CHECK (global_bytes > 0),
    compact_after      double precision NOT NULL CHECK (compact_after > 0),
    summary_retention  double precision NOT NULL CHECK (summary_retention > 0),
    notify_max         int NOT NULL CHECK (notify_max > 0)
);

CREATE TABLE supervisor_journal.writer_usage (
    writer_id  text PRIMARY KEY CHECK (length(writer_id) BETWEEN 1 AND 256),
    epoch      bigint NOT NULL CHECK (epoch >= 1),
    unresolved int NOT NULL CHECK (unresolved >= 0)
);

CREATE TABLE supervisor_journal.streams (
    root           text NOT NULL,
    claim_key      text NOT NULL,
    writer_id      text NOT NULL REFERENCES supervisor_journal.writer_usage(writer_id) ON DELETE RESTRICT,
    state          text NOT NULL CHECK (state IN ('active', 'resolved', 'compacted')),
    count          int NOT NULL CHECK (count >= 0),
    bytes          bigint NOT NULL CHECK (bytes >= 0),
    reserved       int NOT NULL CHECK (reserved >= 0),
    fault_reserved int NOT NULL CHECK (fault_reserved >= 0),
    outstanding    jsonb NOT NULL DEFAULT '[]'::jsonb,
    next_seq       bigint NOT NULL CHECK (next_seq >= 1),
    head_hash      text NOT NULL,
    resolved_at    double precision,
    PRIMARY KEY (root, claim_key)
);

CREATE TABLE supervisor_journal.records (
    record_id     uuid PRIMARY KEY,
    root          text NOT NULL,
    claim_key     text NOT NULL,
    seq           bigint NOT NULL CHECK (seq >= 1),
    kind          text NOT NULL,
    writer_id     text NOT NULL,
    writer_epoch  bigint NOT NULL,
    at_json       text NOT NULL,          -- exact JSON of the fake clock value (no float re-rendering)
    data          text NOT NULL,          -- canonical JSON payload (or the bounded invalidPayload fallback)
    version       int NOT NULL,
    intent_digest text NOT NULL,          -- idempotency identity (writer, stream, kind, original payload, expected seq)
    prev_hash     text NOT NULL,
    record_hash   text NOT NULL,
    budget_bytes  int NOT NULL,           -- logical bytes charged to the caps (E2a record + durable metadata)
    UNIQUE (root, claim_key, seq),
    FOREIGN KEY (root, claim_key) REFERENCES supervisor_journal.streams(root, claim_key) ON DELETE RESTRICT
);

CREATE TABLE supervisor_journal.summaries (
    root        text NOT NULL,
    claim_key   text NOT NULL,
    writer_id   text NOT NULL,
    resolved_at double precision NOT NULL,
    summary     text NOT NULL,
    head_hash   text NOT NULL,            -- the chain head is kept after the records are deleted
    PRIMARY KEY (root, claim_key),
    FOREIGN KEY (root, claim_key) REFERENCES supervisor_journal.streams(root, claim_key) ON DELETE RESTRICT
);

-- Operator epoch bumps (append-only audit). Fences future JOURNAL appends only.
CREATE TABLE supervisor_journal.takeovers (
    id                 bigserial PRIMARY KEY,
    writer_id          text NOT NULL,
    from_epoch         bigint NOT NULL,
    to_epoch           bigint NOT NULL CHECK (to_epoch = from_epoch + 1),
    reconciliation_ref text NOT NULL CHECK (length(btrim(reconciliation_ref)) > 0),
    at_json            text NOT NULL
);

-- Records / summaries / takeovers: never UPDATEd. DELETE only inside a compaction transaction
-- (SET LOCAL supervisor_journal.compaction = 'on' | 'evict'), re-checked against the stream:
--   'on'    : stream resolved/compacted and old enough (D for records, R for summaries); the adapter
--             additionally skips streams whose chain does not validate (corrupt streams are retained);
--   'evict' : stream resolved/compacted (admission under global pressure; age not required).
CREATE FUNCTION supervisor_journal.guard_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    mode   text := coalesce(current_setting('supervisor_journal.compaction', true), '');
    st     record;
    now_   double precision;
    g      record;
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'SJ409: % is append-only (TRUNCATE refused)', TG_TABLE_NAME;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'SJ409: % rows are immutable', TG_TABLE_NAME;
    END IF;
    IF TG_TABLE_NAME = 'takeovers' THEN
        RAISE EXCEPTION 'SJ409: takeovers are append-only';
    END IF;
    IF mode NOT IN ('on', 'evict') THEN
        RAISE EXCEPTION 'SJ409: DELETE on % outside a compaction transaction', TG_TABLE_NAME;
    END IF;
    SELECT s.state, s.resolved_at INTO st FROM supervisor_journal.streams s
     WHERE s.root = OLD.root AND s.claim_key = OLD.claim_key;
    IF st.state IS NULL OR st.state NOT IN ('resolved', 'compacted') OR st.resolved_at IS NULL THEN
        RAISE EXCEPTION 'SJ409: stream (%, %) is not resolved', OLD.root, OLD.claim_key;
    END IF;
    IF mode = 'on' THEN
        now_ := current_setting('supervisor_journal.now')::double precision;
        SELECT compact_after, summary_retention INTO g FROM supervisor_journal.global_usage WHERE id = 1;
        IF TG_TABLE_NAME = 'records' AND now_ - st.resolved_at < g.compact_after THEN
            RAISE EXCEPTION 'SJ409: stream (%, %) resolved too recently to compact', OLD.root, OLD.claim_key;
        END IF;
        IF TG_TABLE_NAME = 'summaries' AND now_ - st.resolved_at < g.summary_retention THEN
            RAISE EXCEPTION 'SJ409: summary (%, %) is within retention', OLD.root, OLD.claim_key;
        END IF;
    END IF;
    RETURN OLD;
END $$;

CREATE TRIGGER trg_sj_records_guard BEFORE UPDATE OR DELETE ON supervisor_journal.records
    FOR EACH ROW EXECUTE FUNCTION supervisor_journal.guard_append_only();
CREATE TRIGGER trg_sj_records_truncate BEFORE TRUNCATE ON supervisor_journal.records
    FOR EACH STATEMENT EXECUTE FUNCTION supervisor_journal.guard_append_only();
CREATE TRIGGER trg_sj_summaries_guard BEFORE UPDATE OR DELETE ON supervisor_journal.summaries
    FOR EACH ROW EXECUTE FUNCTION supervisor_journal.guard_append_only();
CREATE TRIGGER trg_sj_summaries_truncate BEFORE TRUNCATE ON supervisor_journal.summaries
    FOR EACH STATEMENT EXECUTE FUNCTION supervisor_journal.guard_append_only();
CREATE TRIGGER trg_sj_takeovers_guard BEFORE UPDATE OR DELETE ON supervisor_journal.takeovers
    FOR EACH ROW EXECUTE FUNCTION supervisor_journal.guard_append_only();
CREATE TRIGGER trg_sj_takeovers_truncate BEFORE TRUNCATE ON supervisor_journal.takeovers
    FOR EACH STATEMENT EXECUTE FUNCTION supervisor_journal.guard_append_only();

-- Stream rows are mutable bookkeeping (counters, state), but a stream is DELETEd only when its
-- summary has been dropped inside a compaction transaction, and never TRUNCATEd.
CREATE FUNCTION supervisor_journal.guard_streams() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'SJ409: streams TRUNCATE refused';
    END IF;
    IF coalesce(current_setting('supervisor_journal.compaction', true), '') NOT IN ('on', 'evict')
       OR OLD.state <> 'compacted' THEN
        RAISE EXCEPTION 'SJ409: stream (%, %) cannot be deleted', OLD.root, OLD.claim_key;
    END IF;
    RETURN OLD;
END $$;

CREATE TRIGGER trg_sj_streams_delete BEFORE DELETE ON supervisor_journal.streams
    FOR EACH ROW EXECUTE FUNCTION supervisor_journal.guard_streams();
CREATE TRIGGER trg_sj_streams_truncate BEFORE TRUNCATE ON supervisor_journal.streams
    FOR EACH STATEMENT EXECUTE FUNCTION supervisor_journal.guard_streams();
CREATE TRIGGER trg_sj_global_truncate BEFORE TRUNCATE ON supervisor_journal.global_usage
    FOR EACH STATEMENT EXECUTE FUNCTION supervisor_journal.guard_streams();
CREATE TRIGGER trg_sj_writers_truncate BEFORE TRUNCATE ON supervisor_journal.writer_usage
    FOR EACH STATEMENT EXECUTE FUNCTION supervisor_journal.guard_streams();
