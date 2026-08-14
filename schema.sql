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
-- functions, or privileges, so three of STORES.md's walls are deliberately absent
-- here and are verified by behavior instead: the `roles_pipeline_guard` trigger,
-- the append-only grants on violations/audit/events, and anything else the
-- database enforces procedurally. A4 (#15) tests those by provoking them, which
-- is a better proof than a schema comparison anyway. Do not read this file as the
-- whole truth about the database — read it as the whole truth about its tables,
-- columns, indexes and constraints.
--
-- Shape note: this is the MVP shape, matching what 0001 produces, not STORES.md's
-- Stage-A target. It converges as the stage lands — org tables and walls at A4
-- (#15), per-burst snapshots at A5 (#16), composite identity at A6 (#17) — and at
-- that point this file and STORES.md's DDL block are the same thing, so the block
-- collapses into a pointer here.

CREATE TABLE tasks (
    task_id             text PRIMARY KEY,
    repo                text NOT NULL,
    status              text NOT NULL,
    session_id          text,
    transcript_key      text,
    transcript_checksum text,
    pr_number           integer,
    attempts            integer NOT NULL DEFAULT 0,
    review_rounds       integer NOT NULL DEFAULT 0,
    next_burst_kind     text,
    last_review_id      bigint,
    last_burst_at       timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- tasks_by_status(): WHERE status IN (…) ORDER BY created_at
CREATE INDEX tasks_status_created ON tasks (status, created_at);

CREATE TABLE bursts (
    burst_id      text PRIMARY KEY,
    task_id       text NOT NULL,
    kind          text NOT NULL,
    role          text NOT NULL,
    status        text NOT NULL,
    trace_id      text NOT NULL DEFAULT '',
    session_id    text,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    exit_code     integer,
    model         text,
    cost_usd      numeric,
    input_tokens  bigint,
    output_tokens bigint,
    num_turns     integer
);

-- bursts_started_since(): the rolling-window count behind the daily rail.
-- running_bursts() has no index of its own: A4's bursts_one_running_per_task,
-- the wall, is a partial index on exactly that predicate and serves the query.
CREATE INDEX bursts_started_at ON bursts (started_at);

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




