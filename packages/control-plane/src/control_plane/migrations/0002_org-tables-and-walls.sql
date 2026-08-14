-- 0002 — the org tables, and the walls (A4, #15).
--
-- 0001 built the MVP's three tables with nothing forbidden. This file adds the
-- tables the platform's own state lives in (repositories, roles, subscriptions,
-- audit, events, memory_entries) and then turns STORES.md's "Walls in the
-- schema" table from prose into constraints: statuses enumerated, one running
-- burst per task, a burst that is running has no finish time, burst rows point
-- at a real task and a real role, three tables append-only by grant, pipeline
-- role rows frozen by trigger.
--
-- New tables land in their full declared shape — nothing is writing to them, so
-- a column costs nothing to include and a later ALTER on a live table is what we
-- would otherwise be buying. Additions to `tasks` and `bursts` are the opposite,
-- and get only what a wall in this file needs; the rest waits for its stage
-- (schema.sql's header lists which).
--
-- Two-step constraints. Anything added to a live table lands `NOT VALID` here
-- and is validated in 0003: `ADD CONSTRAINT` alone takes a lock while it scans
-- the whole table, which on a live database is the two-second ALTER that takes
-- the application down for a minute. `VALIDATE CONSTRAINT` does that scan
-- without blocking writers, and belongs in its own transaction. squawk enforces
-- the first half (`constraint-missing-not-valid`, `adding-foreign-key-constraint`).
-- The second half is on us: Atlas reports a database with 0003 unapplied as
-- *synced* — measured, not assumed — because it compares constraints without
-- reading pg_constraint.convalidated. What catches a forgotten VALIDATE is
-- test_no_constraint_is_left_unvalidated in tests/test_schema_walls.py.
--
-- Deliberately not here:
--   * `tasks.repo` gets no foreign key into repositories — it holds a filesystem
--     path in the MVP shape, and becomes `repository` at A6 (#17), which is the
--     migration that can point it at a registered product.
--   * the `roles_pipeline_guard` trigger does not guard INSERT. `platform_app`
--     has no write grant on roles at all, so the runtime cannot add a trust slot;
--     the admin path is migrations, which are reviewed code. A fifth pipeline
--     role is a platform release by design (TO-BE §4), not a runtime edit.
--   * no DELETE grant anywhere: retention and pruning are platform_admin's.

-- ---------------------------------------------------------------- org tables

CREATE TABLE repositories (
    repository      text PRIMARY KEY,          -- 'owner/name'
    team            text NOT NULL,
    state           text NOT NULL CONSTRAINT repositories_state_check CHECK (state IN ('active', 'paused')),
    review_enabled  boolean NOT NULL,
    review_provider text NOT NULL,
    e2e_enabled     boolean NOT NULL,
    test_env_refs   jsonb NOT NULL,            -- name → secret ref, never a value
    concurrency     integer NOT NULL,
    daily_bursts    integer NOT NULL,
    usage_share     numeric NOT NULL,
    last_burst_at   timestamptz,               -- least-recently-served scheduling
    claimed_by      text,                      -- observation lease (E2)
    claimed_until   timestamptz,
    registered_at   timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE roles (
    role            text PRIMARY KEY,
    kind            text NOT NULL CONSTRAINT roles_kind_check CHECK (kind IN ('pipeline', 'custom')),
    prompt_preamble text NOT NULL,
    tools           jsonb NOT NULL,            -- must be ⊆ the code allowlist, checked at load
    model           text NOT NULL,
    max_budget_usd  numeric NOT NULL,
    max_turns       integer NOT NULL,
    mount_mode      text NOT NULL,
    identity        text NOT NULL CONSTRAINT roles_identity_check CHECK (identity IN ('none', 'automation')),
    sink            text NOT NULL CONSTRAINT roles_sink_check CHECK (sink IN ('issue_comment', 'ledger_artifact', 'event')),
    trigger         jsonb,                     -- schedule/event; machinery lands at T4
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE subscriptions (
    subscription_id text PRIMARY KEY,
    member          text NOT NULL,
    secret_ref      text NOT NULL,             -- env:NAME / file:PATH — a name, never a value
    cap_tokens      bigint NOT NULL,           -- per rolling window
    expires_at      timestamptz NOT NULL,
    status          text NOT NULL CONSTRAINT subscriptions_status_check CHECK (status IN ('active', 'expired', 'revoked')),
    backoff_until   timestamptz,
    registered_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor  text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    reason text NOT NULL DEFAULT '',
    at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memory_entries (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope             text NOT NULL CONSTRAINT memory_entries_scope_check CHECK (scope IN ('product', 'platform', 'role')),
    repository        text CONSTRAINT memory_entries_repository_fk REFERENCES repositories (repository),
    role              text CONSTRAINT memory_entries_role_fk REFERENCES roles (role),
    claim             text NOT NULL,
    evidence          text NOT NULL,
    proposed_by_burst text CONSTRAINT memory_entries_burst_fk REFERENCES bursts (burst_id),  -- provenance
    status            text NOT NULL CONSTRAINT memory_entries_status_check CHECK (status IN ('proposed', 'active', 'retired')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT memory_entries_product_scope_check CHECK ((scope = 'product') = (repository IS NOT NULL)),
    CONSTRAINT memory_entries_role_scope_check CHECK ((scope = 'role') = (role IS NOT NULL))
);

-- ------------------------------------------------------- the pipeline roles
--
-- The four trust slots, seeded (TO-BE §4). The values are what the engine does
-- *today*, not aspirations, so that D5 (#31) — which makes the row the source of
-- these parameters — changes where they come from without changing behavior:
--
--   * `identity` is 'none' for every pipeline row, and that is the wall, not an
--     omission. A pipeline role's credential is engine code keyed on the role
--     name (the dev App mints a token, the reviewer gets none — REVIEWERS.md);
--     the column can only ever name the shared automation App, so flipping one
--     of these rows to 'automation' would hand a funnel slot the wrong identity.
--   * `tools` is '["*"]' — no narrowing. There is no per-role tool allowlist in
--     code yet; every burst runs the SDK's suite under walls.py's deny floor.
--     D5 introduces named narrowing, and a row's list must then be ⊆ the code
--     allowlist. Widening stays impossible: the floor is code.
--   * `model`, `max_budget_usd`, `max_turns` are today's effective defaults
--     (runner.DEFAULT_MODEL, SchedulerRails.burst_budget_usd/burst_max_turns).
--   * `mount_mode` mirrors what execute_burst does: the dev writes, the reviewer
--     and tester get the repo read-only. `monitor` is declared read-only ahead of
--     T4 rather than inheriting the code's default for an unknown role.
--   * `prompt_preamble` is empty because no such preamble exists yet — the
--     scheduler assembles prompts in full.
INSERT INTO roles (role, kind, prompt_preamble, tools, model, max_budget_usd, max_turns,
                   mount_mode, identity, sink)
VALUES
    ('dev',      'pipeline', '', '["*"]', 'claude-sonnet-5', 2.00, 100, 'rw', 'none', 'issue_comment'),
    ('reviewer', 'pipeline', '', '["*"]', 'claude-sonnet-5', 2.00, 100, 'ro', 'none', 'issue_comment'),
    ('tester',   'pipeline', '', '["*"]', 'claude-sonnet-5', 2.00, 100, 'ro', 'none', 'issue_comment'),
    ('monitor',  'pipeline', '', '["*"]', 'claude-sonnet-5', 2.00, 100, 'ro', 'none', 'event');

-- WALL: pipeline role rows are frozen. The schema's only trigger, and one of the
-- two walls a schema diff cannot see (the other is the grants below) — both are
-- tested by provocation instead, in tests/test_schema_walls.py.
--
-- Frozen: `kind` (a pipeline row must not become custom), `identity`, `sink`,
-- `tools` (the wall-bearing fields of TO-BE §4), and `role` itself — renaming a
-- seeded row is the same escape as editing it, since pipeline identity is code
-- keyed on that name. Everything else (preamble, model, budget, turns, mount)
-- stays editable: that is the point of roles-as-rows.
--
-- Deletion is refused too. A row that can be dropped and re-inserted is not
-- frozen, and `bursts.role` points here, so a delete would also orphan history.
--
-- The error is raised as restrict_violation (23001), so callers see the wall as
-- an integrity error like any other constraint rather than a bespoke class.
CREATE FUNCTION roles_pipeline_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'role "%" is a seeded pipeline role and cannot be deleted', OLD.role
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.role, NEW.kind, NEW.identity, NEW.sink, NEW.tools)
       IS DISTINCT FROM (OLD.role, OLD.kind, OLD.identity, OLD.sink, OLD.tools) THEN
        RAISE EXCEPTION
            'role "%" is a seeded pipeline role: role, kind, identity, sink and tools are frozen',
            OLD.role
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER roles_pipeline_guard
    BEFORE UPDATE OR DELETE ON roles
    FOR EACH ROW WHEN (OLD.kind = 'pipeline')
    EXECUTE FUNCTION roles_pipeline_guard();

-- ------------------------------------------------------ walls on live tables

-- WALL: sessions are single-writer — at most one running burst per task, as a
-- constraint rather than a scheduler promise. A partial unique index, not a
-- UNIQUE constraint: the constraint form would take ACCESS EXCLUSIVE and cannot
-- be partial. It serves running_bursts() as well, which is why 0001 left that
-- query without an index of its own.
--
-- Not CONCURRENTLY, for two reasons that both point the same way. The runner
-- wraps each file in a transaction and Postgres forbids CONCURRENTLY inside one;
-- and a concurrent build that fails leaves an INVALID index behind, which
-- enforces nothing while looking present. For a wall, "created or not created" is
-- the only acceptable pair of outcomes. The plain build blocks writes to bursts
-- for the length of it — acceptable on this table at this size, and the reason
-- the suppression is here at the statement rather than in .squawk.toml, where it
-- would quietly cover the next index too.
-- squawk-ignore require-concurrent-index-creation
CREATE UNIQUE INDEX bursts_one_running_per_task ON bursts (task_id)
    WHERE status = 'running';

ALTER TABLE tasks
    ADD CONSTRAINT tasks_status_check CHECK (status IN
        ('open', 'claimed', 'working', 'awaiting_review', 'blocked', 'done')) NOT VALID;

-- `kind` gets no CHECK on purpose: it is scheduler vocabulary (work, review,
-- test, assess …) and a new kind must not need a migration.
ALTER TABLE bursts
    ADD CONSTRAINT bursts_status_check CHECK (status IN
        ('running', 'succeeded', 'failed', 'killed')) NOT VALID,
    -- WALL: a burst finalizes exactly once. The store layer's CAS
    -- (UPDATE … WHERE status = 'running') is what makes the second finalize a
    -- no-op; this is what makes it structural — no statement can leave a row
    -- half-finished, running with an end time or ended without one.
    ADD CONSTRAINT bursts_running_finished_check
        CHECK ((status = 'running') = (finished_at IS NULL)) NOT VALID,
    ADD CONSTRAINT bursts_task_fk FOREIGN KEY (task_id) REFERENCES tasks (task_id) NOT VALID,
    ADD CONSTRAINT bursts_role_fk FOREIGN KEY (role) REFERENCES roles (role) NOT VALID;

-- ----------------------------------------------------------------- grants
--
-- Two roles (STORES.md): platform_admin owns the schema and runs migrations and
-- retention; platform_app is what replicas connect as, and its grants are walls.
--
-- The role is ensured here rather than assumed because the grants are part of the
-- schema: `migrate` has to be the one command that makes any database ready —
-- CI's from-zero job and every throwaway test database included. It is created
-- without a password: the schema owns what the role may do, the operator owns
-- how it logs in (docker/postgres/init/010-roles.sql for the dev rig). Where the
-- role already exists this is a no-op and its credential is untouched.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'platform_app') THEN
        CREATE ROLE platform_app NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO platform_app;

-- The runtime's own state: read, insert, update. Never delete.
GRANT SELECT, INSERT, UPDATE ON tasks, bursts TO platform_app;

-- WALL: violations, audit and events are append-only — a grant, not a promise.
-- The runtime that records evidence cannot revise it, and the outbox it writes
-- cannot be un-written. Pruning is platform_admin's, on a retention horizon.
GRANT SELECT, INSERT ON violations, audit, events TO platform_app;

-- Read-only for the runtime: registration, role composition and the
-- subscription pool are operator verbs, and each grows its own grant in the
-- stage that ships it (C1, D5, F1). memory_entries is read-only for the same
-- reason — proposals arrive through the provenance gate, not from a burst.
GRANT SELECT ON repositories, roles, subscriptions, memory_entries TO platform_app;
