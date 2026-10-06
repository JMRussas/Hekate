// CodeStoragePoc - Plan node contracts v1: store schema and write fence
//
// Additive, idempotent DDL for managed plans, created ONLY when PlanContractGate is
// Enabled. Existing tables are not altered. Fence triggers reject writes to managed plan
// nodes from every path that is not the plan-contract store (HTTP, repository, raw SQL,
// MCP tools, seeders) with SQLSTATE HP409 / "managed_plan_protected: ...".
//
// Trust boundary: the store enables writes with SET LOCAL hekate.plan_contract='on'
// inside its own transaction. Any client holding full database credentials can do the
// same or disable triggers; this is an integrity fence for well-behaved code paths, not
// product authentication.
//
// Depends on: Npgsql
// Used by:    Api/Program (when enabled), PlanStore, PlanStore.LiveTests

using Npgsql;

namespace CodeStoragePoc.PlanContracts;

public static class PlanStoreSchema
{
    /// <summary>Custom SQLSTATE raised by the fence; mapped to HTTP 409 managed_plan_protected.</summary>
    public const string FenceSqlState = "HP409";

    /// <summary>Attribute keys anyone may write on a managed node (presentation only).</summary>
    public static readonly IReadOnlyList<string> PresentationAttributeKeys = ["priority", "target_date", "display_color", "notes"];

    public static async Task Ensure(string connectionString, string graphName = "code_graph")
    {
        await using var conn = new NpgsqlConnection(connectionString);
        await conn.OpenAsync();

        // The plan graph projection needs AGE and the graph to exist; fail honestly otherwise.
        await using (var check = new NpgsqlCommand(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'age') AND " +
            "EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = @g)", conn))
        {
            check.Parameters.AddWithValue("g", graphName);
            bool ok;
            try { ok = (bool)(await check.ExecuteScalarAsync())!; }
            catch (PostgresException) { ok = false; }
            if (!ok)
                throw new PlanContractConfigurationException(
                    $"The plan-contract store requires the AGE extension and graph '{graphName}' in this database. Refusing to start.");
        }

        await using var tx = await conn.BeginTransactionAsync();
        await using (var cmd = new NpgsqlCommand(Ddl, conn, tx)) await cmd.ExecuteNonQueryAsync();
        await tx.CommitAsync();
    }

    private static string SqlList(IEnumerable<string> values) => string.Join(", ", values.Select(v => $"'{v}'"));

    private static readonly string Ddl = $$"""
        CREATE TABLE IF NOT EXISTS public.managed_plans (
            root_node_id      uuid PRIMARY KEY REFERENCES public.nodes(id) ON DELETE RESTRICT,
            project_id        uuid NOT NULL REFERENCES public.projects(id),
            default_gate      text NOT NULL DEFAULT 'accepted' CHECK (default_gate IN ('completed', 'accepted')),
            contract_version  text NOT NULL,
            created_at        timestamptz NOT NULL DEFAULT now(),
            created_by        text NOT NULL
        );

        CREATE TABLE IF NOT EXISTS public.plan_node_state (
            node_id               uuid PRIMARY KEY REFERENCES public.nodes(id) ON DELETE RESTRICT,
            root_node_id          uuid NOT NULL REFERENCES public.managed_plans(root_node_id) ON DELETE RESTRICT,
            content_revision      bigint NOT NULL CHECK (content_revision >= 1),
            state_revision        bigint NOT NULL CHECK (state_revision >= 0),
            work_status           text NOT NULL CHECK (work_status IN ('todo', 'in_progress', 'done', 'cancelled')),
            attempt_id            text,
            attempt_epoch         bigint NOT NULL DEFAULT 0 CHECK (attempt_epoch >= 0),
            artifact_ref          text,
            acc_decision          text CHECK (acc_decision IN ('accepted', 'rejected')),
            acc_content_revision  bigint,
            acc_artifact_ref      text,
            acc_attempt_id        text,
            acc_attempt_epoch     bigint,
            acc_decided_by        text,
            acc_evidence_ref      text,
            last_op_key           text,
            last_op_fingerprint   text,
            updated_at            timestamptz NOT NULL DEFAULT now(),
            updated_by            text
        );
        CREATE INDEX IF NOT EXISTS idx_plan_node_state_root ON public.plan_node_state(root_node_id);

        CREATE TABLE IF NOT EXISTS public.plan_dependencies (
            root_node_id    uuid NOT NULL REFERENCES public.managed_plans(root_node_id) ON DELETE RESTRICT,
            predecessor_id  uuid NOT NULL REFERENCES public.plan_node_state(node_id) ON DELETE RESTRICT,
            successor_id    uuid NOT NULL REFERENCES public.plan_node_state(node_id) ON DELETE RESTRICT,
            gate            text CHECK (gate IS NULL OR gate IN ('completed', 'accepted')),
            created_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (predecessor_id, successor_id),
            CHECK (predecessor_id <> successor_id)
        );
        CREATE INDEX IF NOT EXISTS idx_plan_dependencies_root ON public.plan_dependencies(root_node_id);

        -- Plan 016: executor reference + append-only attempt provenance audit.
        ALTER TABLE public.plan_node_state ADD COLUMN IF NOT EXISTS executor_ref text;
        ALTER TABLE public.managed_plans ADD COLUMN IF NOT EXISTS event_seq bigint NOT NULL DEFAULT 0 CHECK (event_seq >= 0);

        CREATE TABLE IF NOT EXISTS public.plan_attempt_events (
            root_node_id               uuid NOT NULL REFERENCES public.managed_plans(root_node_id) ON DELETE RESTRICT,
            seq                        bigint NOT NULL CHECK (seq >= 1),
            node_id                    uuid NOT NULL REFERENCES public.plan_node_state(node_id) ON DELETE RESTRICT,
            node_state_revision        bigint NOT NULL,
            kind                       text NOT NULL CHECK (kind IN ('attempt_started', 'attempt_reopened', 'attempt_finished',
                                           'attempt_released', 'attempt_cancelled', 'work_restored', 'decision_recorded', 'content_revised')),
            work_from                  text NOT NULL,
            work_to                    text NOT NULL,
            content_revision           bigint NOT NULL,
            attempt_id                 text,
            attempt_epoch              bigint NOT NULL,
            executor_ref               text,
            artifact_ref               text,
            decision                   text CHECK (decision IS NULL OR decision IN ('accepted', 'rejected')),
            reviewed_content_revision  bigint,
            evidence_ref               text,
            content_digest             text,
            actor                      text NOT NULL,
            operation_key              text NOT NULL,
            recorded_at                timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (root_node_id, seq),
            UNIQUE (node_id, node_state_revision)
        );
        CREATE INDEX IF NOT EXISTS idx_plan_attempt_events_node ON public.plan_attempt_events(node_id, seq);

        -- Plan 019 (3b1): attempt pins, pin provenance on events, durable claim receipts.
        -- Additive only; existing rows keep NULL pins (no backfill).
        ALTER TABLE public.plan_node_state ADD COLUMN IF NOT EXISTS attempt_content_revision bigint;
        ALTER TABLE public.plan_node_state ADD COLUMN IF NOT EXISTS attempt_prereq_digest text;
        ALTER TABLE public.plan_attempt_events ADD COLUMN IF NOT EXISTS attempt_content_revision bigint;
        ALTER TABLE public.plan_attempt_events ADD COLUMN IF NOT EXISTS attempt_prereq_digest text;
        ALTER TABLE public.plan_attempt_events ADD COLUMN IF NOT EXISTS claim_key text;

        CREATE TABLE IF NOT EXISTS public.plan_claim_receipts (
            root_node_id         uuid NOT NULL REFERENCES public.managed_plans(root_node_id) ON DELETE RESTRICT,
            claim_key            text NOT NULL CHECK (claim_key ~ '^[A-Za-z0-9._~-]{1,128}$' AND claim_key NOT IN ('.', '..')),
            request_fingerprint  text NOT NULL,
            outcome              text NOT NULL CHECK (outcome IN ('claimed', 'no_ready_work')),
            node_id              uuid REFERENCES public.plan_node_state(node_id) ON DELETE RESTRICT,
            attempt_id           text,
            attempt_epoch        bigint,
            executor_ref         text,
            content_revision     bigint,
            content_digest       text,
            content_snapshot     jsonb,
            prereq_digest        text,
            prereq_snapshot      jsonb,
            event_seq            bigint,
            actor                text NOT NULL,
            created_at           timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (root_node_id, claim_key),
            FOREIGN KEY (root_node_id, event_seq) REFERENCES public.plan_attempt_events(root_node_id, seq) ON DELETE RESTRICT,
            CHECK (CASE outcome
                WHEN 'claimed' THEN node_id IS NOT NULL AND attempt_id IS NOT NULL AND attempt_epoch IS NOT NULL
                    AND content_revision IS NOT NULL AND content_digest IS NOT NULL AND content_snapshot IS NOT NULL
                    AND prereq_digest IS NOT NULL AND prereq_snapshot IS NOT NULL AND event_seq IS NOT NULL
                ELSE node_id IS NULL AND attempt_id IS NULL AND attempt_epoch IS NULL AND executor_ref IS NULL
                    AND content_revision IS NULL AND content_digest IS NULL AND content_snapshot IS NULL
                    AND prereq_digest IS NULL AND prereq_snapshot IS NULL AND event_seq IS NULL END)
        );

        CREATE OR REPLACE FUNCTION public.hekate_plan_contract_active() RETURNS boolean
            LANGUAGE sql STABLE AS $$ SELECT coalesce(current_setting('hekate.plan_contract', true), '') = 'on' $$;

        CREATE OR REPLACE FUNCTION public.hekate_is_managed(nid uuid) RETURNS boolean
            LANGUAGE sql STABLE AS $$ SELECT nid IS NOT NULL AND EXISTS (SELECT 1 FROM public.plan_node_state WHERE node_id = nid) $$;

        -- True when start (inclusive) or any ancestor is a managed plan node. The walk is
        -- complete (no depth limit, so it never fails open on deep trees) and cycle-safe:
        -- UNION over the id alone drops already-visited ids, so a parent cycle terminates.
        CREATE OR REPLACE FUNCTION public.hekate_in_managed_tree(start uuid) RETURNS boolean
            LANGUAGE sql STABLE AS $$
            WITH RECURSIVE up(id) AS (
                SELECT start WHERE start IS NOT NULL
                UNION
                SELECT n.parent_id
                FROM public.nodes n JOIN up ON n.id = up.id
                WHERE n.parent_id IS NOT NULL)
            SELECT EXISTS (SELECT 1 FROM up JOIN public.plan_node_state s ON s.node_id = up.id) $$;

        CREATE OR REPLACE FUNCTION public.hekate_plan_fence(msg text) RETURNS void
            LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '{{FenceSqlState}}', MESSAGE = 'managed_plan_protected: ' || msg;
        END $$;

        CREATE OR REPLACE FUNCTION public.hekate_guard_nodes() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF public.hekate_plan_contract_active() THEN
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF public.hekate_is_managed(OLD.id) THEN
                    PERFORM public.hekate_plan_fence(format('node %s cannot be deleted outside the plan contract', OLD.id));
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'INSERT' THEN
                -- Any structural node anywhere below a managed node (directly or through an
                -- unmanaged intermediary) must be created through the plan contract.
                IF NEW.node_type IN ('plan', 'plan_phase', 'plan_step', 'task', 'milestone')
                   AND public.hekate_in_managed_tree(NEW.parent_id) THEN
                    PERFORM public.hekate_plan_fence(format('structural node under managed plan (parent %s) must be created through the plan contract', NEW.parent_id));
                END IF;
                RETURN NEW;
            END IF;
            -- UPDATE
            IF public.hekate_is_managed(OLD.id) AND (
                   NEW.id IS DISTINCT FROM OLD.id OR NEW.value IS DISTINCT FROM OLD.value
                OR NEW.node_type IS DISTINCT FROM OLD.node_type OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.parent_id IS DISTINCT FROM OLD.parent_id OR NEW.file_id IS DISTINCT FROM OLD.file_id) THEN
                PERFORM public.hekate_plan_fence(format('node %s content/structure can only change through the plan contract', OLD.id));
            END IF;
            IF NEW.parent_id IS DISTINCT FROM OLD.parent_id
               AND (public.hekate_in_managed_tree(NEW.parent_id) OR public.hekate_in_managed_tree(OLD.parent_id)) THEN
                PERFORM public.hekate_plan_fence(format('node %s cannot be moved into or out of a managed plan tree', OLD.id));
            END IF;
            -- An unmanaged node below a managed node cannot become structural (it would join the
            -- plan tree without being registered).
            IF NEW.node_type IS DISTINCT FROM OLD.node_type
               AND NEW.node_type IN ('plan', 'plan_phase', 'plan_step', 'task', 'milestone')
               AND public.hekate_in_managed_tree(NEW.parent_id) THEN
                PERFORM public.hekate_plan_fence(format('node %s cannot become structural inside a managed plan tree', OLD.id));
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION public.hekate_guard_node_attributes() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF public.hekate_plan_contract_active() THEN
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP IN ('UPDATE', 'DELETE') AND public.hekate_is_managed(OLD.node_id)
               AND (OLD.key NOT IN ({{SqlList(PresentationAttributeKeys)}}) OR TG_OP = 'UPDATE' AND NEW.node_id IS DISTINCT FROM OLD.node_id) THEN
                PERFORM public.hekate_plan_fence(format('attribute %s of managed node %s is protected', OLD.key, OLD.node_id));
            END IF;
            IF TG_OP IN ('UPDATE', 'INSERT') AND public.hekate_is_managed(NEW.node_id)
               AND (NEW.key NOT IN ({{SqlList(PresentationAttributeKeys)}}) OR TG_OP = 'UPDATE' AND NEW.node_id IS DISTINCT FROM OLD.node_id) THEN
                PERFORM public.hekate_plan_fence(format('attribute %s of managed node %s is protected', NEW.key, NEW.node_id));
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END $$;

        CREATE OR REPLACE FUNCTION public.hekate_guard_plan_tables() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT public.hekate_plan_contract_active() THEN
                PERFORM public.hekate_plan_fence(format('%s can only be written by the plan contract store', TG_TABLE_NAME));
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END $$;

        CREATE OR REPLACE FUNCTION public.hekate_guard_truncate() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT public.hekate_plan_contract_active() AND EXISTS (SELECT 1 FROM public.managed_plans) THEN
                PERFORM public.hekate_plan_fence(format('TRUNCATE %s is blocked while managed plans exist', TG_TABLE_NAME));
            END IF;
            RETURN NULL;
        END $$;

        -- Audit events and claim receipts are immutable even for the store (its write flag only
        -- permits INSERT).
        CREATE OR REPLACE FUNCTION public.hekate_guard_events_immutable() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM public.hekate_plan_fence(format('%s is append-only', TG_TABLE_NAME));
            RETURN NULL;
        END $$;

        CREATE OR REPLACE FUNCTION public.hekate_guard_event_seq() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.event_seq < OLD.event_seq THEN
                PERFORM public.hekate_plan_fence(format('event_seq of plan %s cannot decrease', OLD.root_node_id));
            END IF;
            RETURN NEW;
        END $$;

        DROP TRIGGER IF EXISTS trg_hekate_guard_events_insert ON public.plan_attempt_events;
        CREATE TRIGGER trg_hekate_guard_events_insert BEFORE INSERT ON public.plan_attempt_events
            FOR EACH ROW EXECUTE FUNCTION public.hekate_guard_plan_tables();
        DROP TRIGGER IF EXISTS trg_hekate_guard_events_immutable ON public.plan_attempt_events;
        CREATE TRIGGER trg_hekate_guard_events_immutable BEFORE UPDATE OR DELETE ON public.plan_attempt_events
            FOR EACH ROW EXECUTE FUNCTION public.hekate_guard_events_immutable();
        DROP TRIGGER IF EXISTS trg_hekate_guard_events_truncate ON public.plan_attempt_events;
        CREATE TRIGGER trg_hekate_guard_events_truncate BEFORE TRUNCATE ON public.plan_attempt_events
            FOR EACH STATEMENT EXECUTE FUNCTION public.hekate_guard_events_immutable();
        DROP TRIGGER IF EXISTS trg_hekate_guard_receipts_insert ON public.plan_claim_receipts;
        CREATE TRIGGER trg_hekate_guard_receipts_insert BEFORE INSERT ON public.plan_claim_receipts
            FOR EACH ROW EXECUTE FUNCTION public.hekate_guard_plan_tables();
        DROP TRIGGER IF EXISTS trg_hekate_guard_receipts_immutable ON public.plan_claim_receipts;
        CREATE TRIGGER trg_hekate_guard_receipts_immutable BEFORE UPDATE OR DELETE ON public.plan_claim_receipts
            FOR EACH ROW EXECUTE FUNCTION public.hekate_guard_events_immutable();
        DROP TRIGGER IF EXISTS trg_hekate_guard_receipts_truncate ON public.plan_claim_receipts;
        CREATE TRIGGER trg_hekate_guard_receipts_truncate BEFORE TRUNCATE ON public.plan_claim_receipts
            FOR EACH STATEMENT EXECUTE FUNCTION public.hekate_guard_events_immutable();
        DROP TRIGGER IF EXISTS trg_hekate_guard_event_seq ON public.managed_plans;
        CREATE TRIGGER trg_hekate_guard_event_seq BEFORE UPDATE ON public.managed_plans
            FOR EACH ROW EXECUTE FUNCTION public.hekate_guard_event_seq();

        DROP TRIGGER IF EXISTS trg_hekate_guard_nodes ON public.nodes;
        CREATE TRIGGER trg_hekate_guard_nodes BEFORE INSERT OR UPDATE OR DELETE ON public.nodes
            FOR EACH ROW EXECUTE FUNCTION public.hekate_guard_nodes();
        DROP TRIGGER IF EXISTS trg_hekate_guard_node_attributes ON public.node_attributes;
        CREATE TRIGGER trg_hekate_guard_node_attributes BEFORE INSERT OR UPDATE OR DELETE ON public.node_attributes
            FOR EACH ROW EXECUTE FUNCTION public.hekate_guard_node_attributes();

        DROP TRIGGER IF EXISTS trg_hekate_guard_managed_plans ON public.managed_plans;
        CREATE TRIGGER trg_hekate_guard_managed_plans BEFORE INSERT OR UPDATE OR DELETE ON public.managed_plans
            FOR EACH ROW EXECUTE FUNCTION public.hekate_guard_plan_tables();
        DROP TRIGGER IF EXISTS trg_hekate_guard_plan_node_state ON public.plan_node_state;
        CREATE TRIGGER trg_hekate_guard_plan_node_state BEFORE INSERT OR UPDATE OR DELETE ON public.plan_node_state
            FOR EACH ROW EXECUTE FUNCTION public.hekate_guard_plan_tables();
        DROP TRIGGER IF EXISTS trg_hekate_guard_plan_dependencies ON public.plan_dependencies;
        CREATE TRIGGER trg_hekate_guard_plan_dependencies BEFORE INSERT OR UPDATE OR DELETE ON public.plan_dependencies
            FOR EACH ROW EXECUTE FUNCTION public.hekate_guard_plan_tables();

        DROP TRIGGER IF EXISTS trg_hekate_guard_truncate_nodes ON public.nodes;
        CREATE TRIGGER trg_hekate_guard_truncate_nodes BEFORE TRUNCATE ON public.nodes
            FOR EACH STATEMENT EXECUTE FUNCTION public.hekate_guard_truncate();
        DROP TRIGGER IF EXISTS trg_hekate_guard_truncate_attrs ON public.node_attributes;
        CREATE TRIGGER trg_hekate_guard_truncate_attrs BEFORE TRUNCATE ON public.node_attributes
            FOR EACH STATEMENT EXECUTE FUNCTION public.hekate_guard_truncate();
        DROP TRIGGER IF EXISTS trg_hekate_guard_truncate_plans ON public.managed_plans;
        CREATE TRIGGER trg_hekate_guard_truncate_plans BEFORE TRUNCATE ON public.managed_plans
            FOR EACH STATEMENT EXECUTE FUNCTION public.hekate_guard_truncate();
        DROP TRIGGER IF EXISTS trg_hekate_guard_truncate_state ON public.plan_node_state;
        CREATE TRIGGER trg_hekate_guard_truncate_state BEFORE TRUNCATE ON public.plan_node_state
            FOR EACH STATEMENT EXECUTE FUNCTION public.hekate_guard_truncate();
        DROP TRIGGER IF EXISTS trg_hekate_guard_truncate_deps ON public.plan_dependencies;
        CREATE TRIGGER trg_hekate_guard_truncate_deps BEFORE TRUNCATE ON public.plan_dependencies
            FOR EACH STATEMENT EXECUTE FUNCTION public.hekate_guard_truncate();
        """;
}
