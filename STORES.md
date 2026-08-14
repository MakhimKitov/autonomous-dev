# Stores — the state block

The platform's persistence as `TO-BE.md` commits it: a **Postgres ledger** and an
**object-store session archive** behind the MVP's two protocols (`Ledger`,
`SessionArchive` — `store.py`). This doc is the subsystem contract for PHASE-1
Stage A: the canonical schema, the query discipline, and the archive's key scheme.
The SQLite + local-directory backends stay as the single-node variant behind
presence toggles (`LEDGER_DSN` / `ARCHIVE_URL` unset ⇒ today's behavior); the
Postgres DDL below is authoritative and the SQLite schema derives from it.

## Two stores, two protocols

| Store | Owns | Service backend | Single-node variant |
|---|---|---|---|
| **Ledger** | queryable run state: repositories, tasks, bursts, violations, audit, subscriptions, events, memory, roles | `PostgresLedger` (A3) | `SqliteLedger`, as built |
| **SessionArchive** | immutable transcript snapshots, one per burst | S3-compatible bucket (A5) | local directory |

Rules that hold across both, carried over from the MVP:

- **Agents hold no store credentials** — all ledger/archive I/O happens in the
  control plane (or, at T1, through per-object presigned URLs), never inside the
  burst (`ARCHITECTURE.md` §6).
- **No secret values.** Ledger columns hold refs (`env:NAME`, `file:PATH`) —
  names resolved at spawn, never values (`TO-BE.md` §4).
- The ledger is the **queryable index** over state whose truth lives elsewhere
  (GitHub owns issue/PR facts, the archive owns transcripts); it is the
  *authority* only for run state — claims, attempts, lineage, accounting.

## The discipline — transactional, but not interactive

No transaction ever spans application logic, network I/O, or think-time
(`TO-BE.md` §5 DR). Every ledger interaction is one of four single-statement
shapes; locks live for microseconds inside statements, and a dead replica leaves
expired leases — visible, debuggable data — never held locks.

**Claim — a lease is data.** Work is claimed by writing who and until when:

```sql
UPDATE tasks SET claimed_by = $replica, claimed_until = now() + $lease
WHERE task_id = (
    SELECT task_id FROM tasks
    WHERE status = 'claimed'
      AND (claimed_until IS NULL OR claimed_until < now())
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1)
RETURNING *;
```

Anyone may reclaim a lapsed lease; renewal is the same statement guarded by
`claimed_by = $replica`. The `claimed_by`/`claimed_until` pair is the one lease
pattern; `tasks` carries it now, `repositories` carries it for observation ticks
(fanned out at E2).

**Transition — compare-and-set.** Rowcount decides, never a read-then-write:

```sql
UPDATE tasks SET status = 'working', updated_at = now()
WHERE task_id = $1 AND status = 'claimed';
```

**Mutation + outbox — one data-modifying CTE.** A state change and its event
commit atomically or not at all:

```sql
WITH moved AS (
    UPDATE tasks SET status = 'blocked', updated_at = now()
    WHERE task_id = $1 AND status = 'working'
    RETURNING task_id, repository)
INSERT INTO events (kind, payload)
SELECT 'task_blocked', jsonb_build_object('task_id', task_id, 'repository', repository)
FROM moved;
```

**Aggregate — derive, don't mirror.** Anything computable from history is a
query, never a maintained counter: subscription usage is a rolling-window
aggregate over `bursts`, product daily counts likewise (§12's
ledger-as-metrics rule). The only aggregate *decision* — admission — runs under
a short advisory lock fencing in-memory math and single-statement writes,
never held across I/O.

**One clock.** Time enters comparisons only as the database's `now()` — lease
expiry, backoff, windows all use the ledger's clock, so replica clock skew
cannot corrupt a decision. Application code stamps nothing it compares.

## Timestamps — timestamptz at the edge

Postgres columns are `timestamptz`; the Python vocabulary stays ISO-8601 UTC
strings (the MVP's dataclasses, unchanged). Conversion happens only in the store
layer — at parameter bind and row load — so callers never learn which backend
they're on. SQLite keeps ISO `TEXT`: the same strings, lexically ordered the
same way. The DB gets real temporal types (indexing, `claimed_until < now()`
arithmetic in SQL); the app keeps doing no time math at all.

## Schema — canonical DDL

Two database roles: **`platform_admin`** owns the schema, runs migrations and
retention; **`platform_app`** is what replicas connect as, and its grants are
walls (below). Migrations are numbered SQL files applied by the
`control-plane migrate` verb against `schema_migrations(version, applied_at)`
(A2).

**The executable copy of this section is `schema.sql`**, and CI proves the two
agree — a migrated database diffed against that file must come back empty
(`ARCHITECTURE.md` §12, "Schema authoring"). Until Stage A lands, `schema.sql`
holds the MVP shape rather than the target below, and converges as A4–A6 land; at
A6 they are the same thing and this block becomes a pointer to the file. Three
walls are outside what a schema diff can see — the `roles_pipeline_guard` trigger
and the append-only grants — and are verified by provoking them instead.

```sql
-- repositories — one row per registered repository; implements TO-BE §2's
-- product model (product:repository is 1:1 by DR). Defaults are materialized,
-- not inherited: registration fills every policy/quota column from platform
-- config; the row is the entire truth, no NULL-means-inherit.
CREATE TABLE repositories (
    repository      text PRIMARY KEY,          -- 'owner/name'
    team            text NOT NULL,
    state           text NOT NULL CHECK (state IN ('active', 'paused')),
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

-- roles — ceilings in code, composition in data (TO-BE §4). Pipeline rows
-- (dev, reviewer, tester, monitor) are seeded by migration; wall-bearing
-- fields of pipeline rows are frozen by the roles_pipeline_guard trigger.
-- `identity` never accepts a pipeline App: pipeline identity is engine code
-- keyed on the seeded role name, not a column value.
CREATE TABLE roles (
    role            text PRIMARY KEY,
    kind            text NOT NULL CHECK (kind IN ('pipeline', 'custom')),
    prompt_preamble text NOT NULL,
    tools           jsonb NOT NULL,            -- must be ⊆ the code allowlist, checked at load
    model           text NOT NULL,
    max_budget_usd  numeric NOT NULL,
    max_turns       integer NOT NULL,
    mount_mode      text NOT NULL,
    identity        text NOT NULL CHECK (identity IN ('none', 'automation')),
    sink            text NOT NULL,             -- enumerated, platform-implemented set
    trigger         jsonb,                     -- schedule/event; machinery lands at T4
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- subscriptions — the ref-only pool (TO-BE §4). Usage is derived from bursts;
-- backoff_until is the pool's only mutable state, set by CAS on rate limiting,
-- cleared by time.
CREATE TABLE subscriptions (
    subscription_id text PRIMARY KEY,
    member          text NOT NULL,
    secret_ref      text NOT NULL,             -- env:NAME / file:PATH — a name, never a value
    cap_tokens      bigint NOT NULL,           -- per rolling window
    expires_at      timestamptz NOT NULL,
    status          text NOT NULL CHECK (status IN ('active', 'expired', 'revoked')),
    backoff_until   timestamptz,
    registered_at   timestamptz NOT NULL DEFAULT now()
);

-- tasks — one GitHub issue worked to completion. The id is derived, never
-- invented: '{owner}/{repo}#{issue_number}', so it is stable across replicas
-- and joins the GitHub world for free.
CREATE TABLE tasks (
    task_id         text PRIMARY KEY,          -- '{owner}/{repo}#{issue_number}'
    repository      text NOT NULL REFERENCES repositories (repository),
    issue_number    integer NOT NULL,
    status          text NOT NULL CHECK (status IN
                      ('open', 'claimed', 'working', 'awaiting_review', 'blocked', 'done')),
    pr_number       integer,
    attempts        integer NOT NULL DEFAULT 0,
    review_rounds   integer NOT NULL DEFAULT 0,
    next_burst_kind text,
    last_review_id  bigint,
    last_burst_at   timestamptz,
    claimed_by      text,                      -- the lease pattern (above)
    claimed_until   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (repository, issue_number)
);

-- bursts — history-preserving, not append-only: a row transitions while
-- running and is finalized exactly once (the CAS below). Lineage columns make
-- session history a tree (TO-BE §6): resumed_from_burst_id names the parent
-- node; session_key/checksum name this burst's own archived snapshot.
CREATE TABLE bursts (
    burst_id              text PRIMARY KEY,
    task_id               text NOT NULL REFERENCES tasks (task_id),
    kind                  text NOT NULL,       -- scheduler vocabulary; deliberately unconstrained
    role                  text NOT NULL REFERENCES roles (role),
    status                text NOT NULL CHECK (status IN
                            ('running', 'succeeded', 'failed', 'killed')),
    trace_id              text NOT NULL DEFAULT '',
    resumed_from_burst_id text REFERENCES bursts (burst_id),
    subscription_id       text REFERENCES subscriptions (subscription_id),
    session_id            text,
    session_key           text,                -- archive key of this burst's snapshot
    session_checksum      text,                -- sha256; the ledger is the checksum authority
    pinned                boolean NOT NULL DEFAULT false,  -- golden-replay fixture; GC never collects it
    started_at            timestamptz NOT NULL DEFAULT now(),
    finished_at           timestamptz,
    exit_code             integer,
    model                 text,
    cost_usd              numeric,
    input_tokens          bigint,
    output_tokens         bigint,
    num_turns             integer,
    CHECK ((status = 'running') = (finished_at IS NULL))
);

-- WALL: sessions are single-writer — at most one running burst per task, as a
-- constraint, not a scheduler promise. Any claiming bug becomes a constraint
-- violation, never a doubled burst.
CREATE UNIQUE INDEX bursts_one_running_per_task ON bursts (task_id)
    WHERE status = 'running';

-- violations — wall blocks and tripwire firings; the experiment's primary
-- dataset. Append-only by grant.
CREATE TABLE violations (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id  text NOT NULL,
    burst_id text NOT NULL,
    tool     text NOT NULL,
    reason   text NOT NULL,
    detail   text NOT NULL DEFAULT '',
    at       timestamptz NOT NULL DEFAULT now()
);

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

-- events — the outbox (below). id is the consumer's replay cursor.
CREATE TABLE events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- memory_entries — a datum, not an instruction (TO-BE §9): auto-class content
-- under the provenance gate, scope-filtered at prompt assembly.
CREATE TABLE memory_entries (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope             text NOT NULL CHECK (scope IN ('product', 'platform', 'role')),
    repository        text REFERENCES repositories (repository),
    role              text REFERENCES roles (role),
    claim             text NOT NULL,
    evidence          text NOT NULL,
    proposed_by_burst text REFERENCES bursts (burst_id),  -- provenance
    status            text NOT NULL CHECK (status IN ('proposed', 'active', 'retired')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CHECK ((scope = 'product') = (repository IS NOT NULL)),
    CHECK ((scope = 'role') = (role IS NOT NULL))
);

-- org_state — exactly one row. STOP moves from the filesystem into the ledger
-- (TO-BE §5): non-null stop_reason means no ticks, no spawns, from any replica.
CREATE TABLE org_state (
    id          boolean PRIMARY KEY DEFAULT true CHECK (id),
    stop_reason text,
    stopped_by  text,
    stopped_at  timestamptz
);
INSERT INTO org_state DEFAULT VALUES;
```

## Walls in the schema

| Wall | Mechanism |
|---|---|
| One running burst per task | `bursts_one_running_per_task` partial unique index |
| A burst finalizes exactly once | CAS: `UPDATE bursts SET … WHERE burst_id = $1 AND status = 'running'` — rowcount 0 means already finalized or killed; plus the `running ⟺ finished_at IS NULL` CHECK |
| One task per issue | `UNIQUE (repository, issue_number)`; the derived `task_id` makes collisions unrepresentable |
| `violations` / `audit` / `events` are append-only | grants: `platform_app` gets `SELECT, INSERT` only — append-only is a grant, not a promise |
| Pipeline role rows are frozen | `roles_pipeline_guard`, the schema's only trigger: rejects updates to wall-bearing fields (`kind`, `identity`, `sink`, `tools`) of `kind = 'pipeline'` rows; CHECKs pin the `identity`/`sink` enumerations for every row |
| No secret values at rest | nothing to enforce — no column exists for a value; refs only |

SQLite parity: partial unique indexes and CHECKs work identically. Grants and
`SKIP LOCKED` don't exist there — the single-node variant runs one process, so
claims are uncontended CAS updates with the same semantics, and append-only is
code discipline. The walls that guard concurrency are real exactly where
concurrency is real.

## The events outbox

Events are how the platform tells the adopter's systems what happened
(`capacity_exhausted`, `subscription_expiring`, `task_blocked`, violation
alerts …) without the platform owning a message bus — capabilities, not
instruments (`TO-BE.md` §12).

- **Written atomically** with the mutation they announce, via the CTE shape
  above. An event without its mutation, or a mutation without its event,
  cannot be observed.
- **Consumed by cursor**: `SELECT * FROM events WHERE id > $cursor ORDER BY id`
  — at-least-once, ordered, replayable; each consumer keeps its own cursor.
  No `LISTEN/NOTIFY` dependency: like webhooks (§5), a push channel may *wake*
  a consumer early, but the poll is the truth.
- **Retention** is a `platform_admin` concern (the runtime role can't delete):
  prune by age once consumers confirm cursors past the horizon.

## The session archive — a tree of immutable snapshots

One immutable snapshot per **burst**, not one mutable blob per task
(`TO-BE.md` §6 DR). The ledger owns the tree — `resumed_from_burst_id` is the
lineage, `session_key`/`session_checksum` name the content — and the archive is
deliberately dumb storage.

- **Key scheme**: `sessions/{task_id}/{burst_id}/session.tar.gz`, where
  `task_id` is the derived `owner/repo#n` (legal in S3 keys and on POSIX;
  presigned URLs percent-encode the `#`).
- **Write-once**: `put` to an existing key is an error. Snapshots never mutate;
  retry re-materializes the same parent node, branch points
  `resumed_from_burst_id` at an older node — tree operations are ledger writes,
  never archive writes.
- **Checksummed**: `put` returns the sha256, recorded in the burst's finalize
  statement; `get` verifies content against the ledger's value. The ledger is
  the authority — a sidecar file is a backend convenience, not a contract.
- The **credential-exclusion rule is unconditional** (`EXCLUDE_FROM_ARCHIVE`
  survives verbatim), and reviewer/tester volumes are never seeded and never
  archived, as in the MVP.
- **GC**: snapshots of `done` tasks age out after a retention window, except
  nodes with `pinned = true` (golden-replay fixtures, M4 scenarios) and any
  node on a path to a pinned one.

The protocol goes key-based to match (A5):

```python
class SessionArchive(Protocol):
    def put(self, key: str, session_dir: Path) -> str:
        """Archive session_dir under key; returns sha256. Write-once."""
    def get(self, key: str, dest_dir: Path, expected_sha256: str) -> None:
        """Materialize key into dest_dir; checksum mismatch raises."""
```

Per-object presigned GET/PUT (the three-container pod, T1) extends this
protocol without changing it for `DockerRunner`, which keeps calling `put`/`get`
in-process.

## From the MVP

What Stage A moves, and where:

| MVP shape | Target shape | Issue |
|---|---|---|
| `tasks.task_id` = bare issue number | derived `owner/repo#n` + `repository` FK + `issue_number` | A6 |
| `tasks.transcript_key/-_checksum` (one blob per task) | `bursts.session_key/-_checksum` (one snapshot per burst) | A5, backfilled at A6 |
| ISO-`TEXT` timestamps everywhere | `timestamptz` columns, strings at the edge | A3 |
| `_ensure_column` ad-hoc migration | numbered SQL + `schema_migrations` + `migrate` verb — for Postgres; the single-node SQLite variant keeps self-bootstrapping until A6 retires it as an origin | A2 |
| no claim verbs (single process) | lease + CAS statements in the `Ledger` protocol | A3 (the finalize CAS), E1 (`claimed_by`/`claimed_until` + the claim verbs), E2 (used in anger) |
| STOP flag on the filesystem | `org_state` row | E3 |

The `Ledger` protocol keeps its nine MVP methods; new verbs (claim, CAS
transition, finalize) arrive additively at A3 so `SqliteLedger` callers never
change. Gate A7: the MVP scenario runs byte-for-byte on Postgres + SeaweedFS with
`LEDGER_DSN`/`ARCHIVE_URL` set, and identically with them unset.

## Deliberately out

- **No ORM, no interactive transactions** — by construction: the store layer
  exposes the four statement shapes and nothing that could hold a transaction
  open.
- **No mirrored counters** — usage, quotas, contribution are views over
  `bursts` (§12).
- **No message bus, no LISTEN/NOTIFY dependency** — the outbox table is the
  contract; adopters bridge it to their own instruments.
- **No secret values, no token custody** — refs only, resolution at spawn.
- **No tiers or modes** — one schema; the single-node variant is the same
  design minus contention, selected by presence toggles, not a mode flag.
