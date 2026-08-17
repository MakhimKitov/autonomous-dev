-- The ledger's schema as desired state — the executable copy of STORES.md's
-- "Schema — canonical DDL", and the input Atlas diffs the migration directory
-- against (see atlas.hcl, and ARCHITECTURE.md §12 for why).
--
-- HOW THIS FILE IS USED. Nothing applies it. It is the *statement* of what the
-- schema should be; `packages/control-plane/src/control_plane/migrations/` is the
-- ordered path that gets a database there, and `control-plane migrate` is what
-- walks it. To change the schema: edit this file, ask Atlas for the difference,
-- and save the answer as the next numbered migration:
--
--   atlas schema diff --env local --var dev_url="docker://postgres/18/dev"
--
-- CI then asserts the two agree — a migrated database diffed against this file
-- must come back empty. That check is the entire point: before it existed, "the
-- DDL is authoritative" was a claim about a markdown block that nothing verified.
--
-- WHAT THIS FILE CANNOT SAY. Atlas's community version does not read triggers,
-- functions, privileges, or table data, so four of the walls are deliberately
-- absent here and are verified by behavior instead: the `roles_pipeline_guard`
-- trigger, the append-only grants on violations/audit/events, the rest of
-- `platform_app`'s grants, and the seeded pipeline role rows. A4 (#15) provokes
-- each of them in `tests/test_schema_walls.py`, which is a better proof than a
-- schema comparison anyway. Do not read this file as the whole truth about the
-- database — read it as the whole truth about its tables, columns, indexes and
-- constraints.
--
-- One more blind spot, and a sharp one: a constraint added to a live table lands
-- `NOT VALID` and is validated in a later migration (0002 → 0003). Atlas calls a
-- database with that second step missing *synced* — it compares the constraints
-- but not pg_constraint.convalidated, so a wall enforced only for new rows looks
-- identical to one that holds for all of them. The declarations below are the
-- validated form; test_no_constraint_is_left_unvalidated is what makes the
-- database match them.
--
-- Shape note: still converging on STORES.md's Stage-A target. Landed: org tables
-- and walls (A4), the session tree (A5). Outstanding: composite task identity —
-- `repository` + `issue_number` + the one-task-per-issue UNIQUE — at A6 (#17), the
-- task/repository claim columns at E1/E2 (#32, #33), the `org_state` STOP row at
-- E3 (#34), `bursts.pinned` with the golden-replay harness that needs it at G4
-- (#45), and `bursts.subscription_id` with the pool at F (#38–41). All but
-- `org_state` are additions to a *live* table, which is why they wait for the
-- stage that needs them rather than landing speculatively here.

-- repositories — one row per registered repository; implements TO-BE §2's
-- product model (product:repository is 1:1 by DR). Defaults are materialized,
-- not inherited: registration fills every policy/quota column from platform
-- config; the row is the entire truth, no NULL-means-inherit. Empty until C1
-- (#23) ships `product register`; `tasks.repo` gains its foreign key at A6,
-- when that column becomes `repository` and holds 'owner/name'.
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

-- roles — ceilings in code, composition in data (TO-BE §4). The four pipeline
-- rows are seeded by migration and their wall-bearing fields are frozen by the
-- roles_pipeline_guard trigger. `identity` never accepts a pipeline App:
-- pipeline identity is engine code keyed on the seeded role name, which is why
-- every seeded row carries 'none'.
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

-- subscriptions — the ref-only pool (TO-BE §4). Usage is derived from bursts;
-- backoff_until is the pool's only mutable state, set by CAS on rate limiting,
-- cleared by time. Empty until F1 (#38).
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

-- tasks — one GitHub issue worked to completion. The id is derived, never
-- invented: '{owner}/{repo}#{issue_number}', so it is stable across replicas and
-- joins the GitHub world for free. `repository` holds the product slug; the
-- platform's clone path is a runtime argument and belongs in no row.
--
-- No foreign key into repositories yet, and that is a decision rather than an
-- omission: nothing before C1 (#23) can legitimately create a `repositories` row,
-- so requiring one here would make every task uninsertable in the meantime. C1
-- ships `product register` and adds the reference.
CREATE TABLE tasks (
    task_id         text PRIMARY KEY,          -- '{owner}/{repo}#{issue_number}'
    repository      text NOT NULL,             -- 'owner/name'
    issue_number    integer NOT NULL,
    status          text NOT NULL CONSTRAINT tasks_status_check CHECK (status IN
                      ('open', 'claimed', 'working', 'awaiting_review', 'blocked', 'done')),
    session_id      text,
    pr_number       integer,
    attempts        integer NOT NULL DEFAULT 0,
    review_rounds   integer NOT NULL DEFAULT 0,
    next_burst_kind text,
    last_review_id  bigint,
    last_burst_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- tasks_by_status(): WHERE status IN (…) ORDER BY created_at
CREATE INDEX tasks_status_created ON tasks (status, created_at);

-- WALL: one task per issue. The derived task_id already makes a duplicate hard to
-- represent; this makes it impossible, including for two replicas that compute the
-- same id at the same moment.
CREATE UNIQUE INDEX tasks_one_per_issue ON tasks (repository, issue_number);

-- bursts — history-preserving, not append-only: a row transitions while running
-- and is finalized exactly once (the CAS in the store layer, with the
-- running ⟺ finished_at CHECK behind it). `kind` is deliberately unconstrained:
-- it is scheduler vocabulary, and new kinds must not need a migration.
CREATE TABLE bursts (
    burst_id              text PRIMARY KEY,
    task_id               text NOT NULL CONSTRAINT bursts_task_fk REFERENCES tasks (task_id),
    kind                  text NOT NULL,
    role                  text NOT NULL CONSTRAINT bursts_role_fk REFERENCES roles (role),
    status                text NOT NULL CONSTRAINT bursts_status_check CHECK (status IN
                            ('running', 'succeeded', 'failed', 'killed')),
    trace_id              text NOT NULL DEFAULT '',
    -- the session tree (A5): the parent node this burst resumed from, and the
    -- snapshot it left behind. Self-referencing, so lineage is a walk in SQL.
    resumed_from_burst_id text CONSTRAINT bursts_resumed_from_fk REFERENCES bursts (burst_id),
    session_id            text,
    session_key           text,   -- archive key of this burst's snapshot
    session_checksum      text,   -- sha256; the ledger is the checksum authority
    started_at            timestamptz NOT NULL DEFAULT now(),
    finished_at           timestamptz,
    exit_code             integer,
    model                 text,
    cost_usd              numeric,
    input_tokens          bigint,
    output_tokens         bigint,
    num_turns             integer,
    CONSTRAINT bursts_running_finished_check CHECK ((status = 'running') = (finished_at IS NULL))
);

-- bursts_started_since(): the rolling-window count behind the daily rail.
CREATE INDEX bursts_started_at ON bursts (started_at);

-- latest_archived_burst(): the leaf of a task's session tree, materialized into
-- the next burst's volume. Partial, because only a burst that archived something
-- is a candidate — reviewer and tester bursts never are.
CREATE INDEX bursts_session_leaf ON bursts (task_id, started_at) WHERE session_key IS NOT NULL;

-- WALL: sessions are single-writer — at most one running burst per task, as a
-- constraint, not a scheduler promise. Any claiming bug becomes a constraint
-- violation, never a doubled burst. Serves running_bursts() too, which is why
-- that query has no index of its own.
CREATE UNIQUE INDEX bursts_one_running_per_task ON bursts (task_id)
    WHERE status = 'running';

-- violations — wall blocks and tripwire firings; the experiment's primary
-- dataset. Append-only by grant. No foreign keys on purpose: a violation is
-- evidence and outlives whatever it was recorded against.
CREATE TABLE violations (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id  text NOT NULL,
    burst_id text NOT NULL,
    tool     text NOT NULL,
    reason   text NOT NULL,
    detail   text NOT NULL DEFAULT '',
    at       timestamptz NOT NULL DEFAULT now()
);

-- violations(task_id) — the experiment's primary dataset, always read by task
CREATE INDEX violations_task ON violations (task_id);

-- audit — every operator mutation (actor, action, target, reason), starting
-- with `product register` (TO-BE §2). Append-only by grant.
CREATE TABLE audit (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor  text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    reason text NOT NULL DEFAULT '',
    at     timestamptz NOT NULL DEFAULT now()
);

-- events — the outbox (STORES.md). id is the consumer's replay cursor; rows are
-- written in the same statement as the mutation they announce. Append-only by
-- grant; pruning is platform_admin's.
CREATE TABLE events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- memory_entries — a datum, not an instruction (TO-BE §9): auto-class content
-- under the provenance gate, scope-filtered at prompt assembly.
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
