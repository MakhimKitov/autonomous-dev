-- 0001 — the state block's first stone: the MVP's three tables in canonical
-- Postgres form (STORES.md "Schema — canonical DDL").
--
-- Deliberately the MVP *shape*, not the target one. `tasks` keeps `repo` and its
-- transcript columns, and `task_id` stays the bare issue number, so A3's
-- PostgresLedger answers the nine MVP protocol methods against this schema and
-- gate A7 can compare the two backends byte-for-byte. The shape then moves as
-- further numbered files: org tables + walls (A4), per-burst snapshots (A5),
-- composite identity + backfill (A6).
--
-- Already canonical here: timestamptz columns (ISO strings convert at the
-- adapter edge, A3), bigint identity keys, numeric money.
--
-- Deliberately absent, all A4's scope: status CHECKs, the
-- bursts_one_running_per_task partial unique index, the running ⟺ finished_at
-- CHECK, append-only grants, and the FKs into repositories/roles. Note for A4:
-- `bursts.task_id REFERENCES tasks (task_id)` cannot land until the write order
-- in bursts.execute_burst changes — today record_burst() runs before the first
-- upsert_task(), so a burst row precedes its task row.

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

-- No index for running_bursts() here: A4's bursts_one_running_per_task, the
-- wall, is a partial index on exactly this predicate and serves the query too.
-- bursts_started_since(): the rolling-window count behind the daily rail
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
