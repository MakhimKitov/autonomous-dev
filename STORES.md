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

The first one in the code is `record_violation` (A4): the violation and its
`violation_recorded` event, plus a `pg_notify` on the wake channel, in one
statement. Note the shape — the main statement *consumes* the CTE. Postgres does
not order an unreferenced data-modifying CTE against the statement around it, so
two sub-statements touching the same unique index can race each other; anything
that finalizes one row and inserts its successor has to carry the data
dependency, as the example above does.

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
(`ARCHITECTURE.md` §12, "Schema authoring"). Read the block below as the *target*
and `schema.sql` as what exists: Stage A closed most of the gap (org tables and
walls at A4, per-burst snapshots at A5, derived task identity at A6), and what
remains arrives with the stage that needs it — `tasks.repository`'s foreign key
into `repositories` with the registry that can create one (C1), the claim columns
at E1/E2, `org_state` at E3, `bursts.subscription_id` with the pool at F,
`bursts.pinned` with the harness that pins fixtures at G4. `schema.sql`'s header
carries that list, and it is the shorter document to keep honest.

Four things here are outside what a schema diff can see, all measured rather than
assumed: the `roles_pipeline_guard` trigger, every grant, the seeded role rows,
and — the sharp one — whether a constraint is `NOT VALID`. Atlas calls a database
whose validation step never ran *synced*, so a wall enforced for new rows only
looks exactly like one that holds for all of them. Provocation tests cover the
first three; `pg_constraint.convalidated` covers the fourth.

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
-- and joins the GitHub world for free (A6). The foreign key below is the one
-- part not yet built: nothing before C1 can create a `repositories` row, so
-- requiring one would make every task uninsertable in the meantime.
CREATE TABLE tasks (
    task_id         text PRIMARY KEY,          -- '{owner}/{repo}#{issue_number}'
    repository      text NOT NULL REFERENCES repositories (repository),  -- the FK: C1
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
    span_id               text NOT NULL DEFAULT '',  -- the burst span in that trace; a link needs the pair (B1)
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
| Pipeline role rows are frozen | `roles_pipeline_guard`, the schema's only trigger: rejects updates to the wall-bearing fields (`kind`, `identity`, `sink`, `tools`) of `kind = 'pipeline'` rows, plus renames of `role` itself and deletion — a row that can be dropped and re-inserted is not frozen. CHECKs pin the `identity`/`sink` enumerations for every row, and `platform_app` holds no write grant on `roles` at all, so the runtime cannot open a trust slot |
| No secret values at rest | nothing to enforce — no column exists for a value; refs only |

Every one of these is provoked in `tests/test_schema_walls.py` and required to
raise — the two that a schema diff cannot see (the trigger, the grants) have
nothing else standing behind them.

SQLite carries none of them, deliberately. It *can* express partial unique
indexes and CHECKs, but it cannot add a constraint to a table that already
exists, so a wall placed in its bootstrap DDL would hold in ledgers created after
the change and be silently missing from every older file — worse than absent,
because nothing can tell you which one you're holding. Grants and `SKIP LOCKED`
don't exist there at all. The single-node variant runs one process, so claims are
uncontended CAS updates with the same semantics, append-only is code discipline,
and A6 retires that ledger as an origin. The walls that guard concurrency are
real exactly where concurrency is real.

## Reporting — the metrics are queries, not a pipeline

`TO-BE.md` §12: the ledger *is* the metrics store. The `ARCHITECTURE.md` §9 metric
families ship as read-only views in a `reporting` schema (B3, migration `0010`), and
anything with a Postgres driver renders them. Nothing emits a metric that mirrors a
ledger fact — a second copy drifts, and the labels would be the copy.

| View | Grain | Answers |
|---|---|---|
| `reporting.task_facts` | one task | burst counts by kind, non-clean finishes, cost, tokens, turns, violations, first/last burst. The base the other three aggregate |
| `reporting.reliability` | repository | tasks by status · success / blocked / needs-review / rework rates · cost per task · bursts per task |
| `reporting.containment` | repository × tool × reason | every wall hit, with the bursts and tasks it touched and when it was first and last seen |
| `reporting.capability` | repository | merges · first-pass merges · review rounds and attempts per merge · review/test burst counts · turns |

Three definitions worth knowing before reading a number:

- **Success and blocked rates are over *settled* tasks** — `done + blocked`. A task
  the scheduler is still working is not a failure yet, and putting it in the
  denominator makes every rate move with queue depth. `needs_review_rate` is over
  all tasks on purpose: "a human has the ball" is a fact about now.
- **Wall hits and attempted violations are one number**, because every row in
  `violations` is an attempt the deny hook refused. They separate the day a
  tripwire becomes observe-only, and that is when the view grows a column.
- **A successful breach has no row, so the view has no column.** It is by
  definition what the walls did not catch; a `0` there would read as "none
  happened" while meaning "none was recorded". Breach rate is *measured* — G6
  (#47) runs the M4 suite against a real deployment — and #10's
  acknowledged-defects channel is where a human classifies one after the fact.

And what §9 asks for that the ledger cannot answer yet, named rather than faked:
a review **verdict** (the ledger keeps `review_rounds` and the review cursor; the
verdict is a GitHub review event), the human-review pass-rate (same reason),
post-merge defect rate (needs a link from a later issue back to the merge that
caused it), and task difficulty (`turns`, `cost_usd`, `attempts` are proxies, and
inputs to a judgment rather than the judgment). `reporting.capability` reports
first-pass merges instead and is named for what it counts.

**A dashboard is not the runtime.** Views execute with their owner's privileges
(`security_invoker` is off by default), so a read-only role sees every aggregate and
no row of `tasks`, `bursts` or `violations`. Two statements, and how it
authenticates is the operator's business:

```sql
GRANT USAGE ON SCHEMA reporting TO <dashboard_role>;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO <dashboard_role>;
```

`tests/test_reporting_views.py` provokes that boundary rather than assuming it, and
it carries the rest of the verification too: the views are `schema.sql`'s newest
blind spot, and the loudest one — Atlas's community version refuses a file
containing `CREATE VIEW` outright ("views are available to logged-in users only"),
so the drift check cannot see these definitions at all. Seeded rows in, known
numbers out is what stands behind them.

Reference dashboards are examples, not components: `examples/dashboards/` (B4) is a
portable `.sql` file plus one importable dashboard that embeds the same statements,
run by `tests/test_dashboards.py` as a role holding nothing but the two grants above —
so the example cannot rot and cannot quietly come to need a base table.
`docker/telemetry/dashboard-role.sql` is those grants as a script, and
`OBSERVABILITY.md` is the operator's read. The single-node ledger has no reporting
layer and is not getting one: a laptop reads its own numbers with a query.

## The events outbox

Events are how the platform tells the adopter's systems what happened
(`capacity_exhausted`, `subscription_expiring`, `task_blocked`, violation
alerts …) without the platform owning a message bus — capabilities, not
instruments (`TO-BE.md` §12).

- **Written atomically** with the mutation they announce, via the CTE shape
  above. An event without its mutation, or a mutation without its event,
  cannot be observed. `violation_recorded` is the first kind (A4) — the alert
  signal `TO-BE.md` §12 documents, and the exact form of the condition
  (`OBSERVABILITY.md`); `capacity_exhausted`, `subscription_expiring` and
  `task_blocked` arrive with the stages that raise them.
- **Consumed by cursor**: `SELECT * FROM events WHERE id > $cursor ORDER BY id`
  — at-least-once, ordered, replayable; each consumer keeps its own cursor, and
  the `Ledger` protocol's `events(after_id)` is that read. No `LISTEN/NOTIFY`
  dependency: like webhooks (§5), a push channel may *wake* a consumer early, but
  the poll is the truth. The channel is `events`, notified from inside the same
  statement — transactional, so it is delivered at commit or not at all, and
  dropped entirely if nobody happens to be listening.
- **Retention** is a `platform_admin` concern (the runtime role can't delete):
  prune by age once consumers confirm cursors past the horizon.

## The session archive — a tree of immutable snapshots

One immutable snapshot per **burst**, not one mutable blob per task
(`TO-BE.md` §6 DR). The ledger owns the tree — `resumed_from_burst_id` is the
lineage, `session_key`/`session_checksum` name the content — and the archive is
deliberately dumb storage.

- **Key scheme**: `sessions/{task_id}/{burst_id}/session.tar.gz`, where
  `task_id` is the derived `owner/repo#n` — so a key holds both a `/` and a `#`,
  which is legal in S3 and on POSIX and comes back correct through a presigned
  URL, where the `#` arrives as `%23` (measured on the rig's gateway at A6, not
  assumed).
- **Write-once**: `put` to an existing key is an error. Snapshots never mutate;
  retry re-materializes the same parent node, branch points
  `resumed_from_burst_id` at an older node — tree operations are ledger writes,
  never archive writes.
- **Checksummed**: `put` returns the sha256, recorded in the burst's finalize
  statement; `get` verifies content against the ledger's value. The ledger is
  the authority — the copy the archive keeps beside the object (S3 metadata, a
  sidecar file locally) is for reading, never for deciding. It has one job:
  when the control plane never held the bytes, as with presigned PUT, it is how
  the checksum gets home (`recorded_checksum`).
- The **credential-exclusion rule is unconditional** (`EXCLUDE_FROM_ARCHIVE`
  survives verbatim). Reviewer volumes are never seeded and never archived —
  nothing resumes a reviewer, and it reads without executing, so there is
  nothing to prove. **Tester volumes are archived** (B5), under the same key
  shape — `sessions/{task_id}/{burst_id}/session.tar.gz`, the row's `role`
  telling the kinds apart — but as *evidence*, not as nodes: the leaf query is
  role-filtered, so a tester's snapshot is reachable only by its key and can
  never be what a dev burst resumes.
- **GC**: snapshots of `done` tasks age out after a retention window, except
  nodes with `pinned = true` (golden-replay fixtures, M4 scenarios) and any
  node on a path to a pinned one.

The protocol is key-based, and `ARCHIVE_URL` chooses the backend the same way
`LEDGER_DSN` chooses the ledger's —
`s3://[key:secret@]host[:port]/bucket[/prefix][?tls=false]`, where `tls=false`
is this store's `sslmode=disable`:

```python
class SessionArchive(Protocol):
    def put(self, key: str, session_dir: Path) -> str:
        """Archive session_dir under key; returns sha256. Write-once."""
    def get(self, key: str, dest_dir: Path, expected_sha256: str) -> None:
        """Materialize key into dest_dir; checksum mismatch raises."""
    def recorded_checksum(self, key: str) -> str | None:
        """What the archive itself stored beside the object. Not the authority."""
```

Per-object presigned GET/PUT (the three-container pod, T1) is a second protocol
over the same backend — `PresigningArchive`, implemented by the object store and
by nothing else, because `DockerRunner` keeps putting and getting in-process and
should not have to fake a capability it never uses.

**Walking the tree** is one query, not a join through the task row:
`latest_archived_burst(task_id)` returns the leaf — the newest **dev** burst with
a `session_key` — and that is both what the next volume is materialized from and
what its `resumed_from_burst_id` points at. `bursts_session_leaf`, a partial index
on exactly that predicate (`session_key IS NOT NULL AND role = 'dev'`, 0011),
serves it. Three consequences worth stating: a burst that crashes before harvest
never becomes a leaf, so the next one resumes the last node that actually produced
something; an archived *tester* burst never becomes one either — its snapshot is
evidence, and the role filter is what lets B5 archive it without a new column; and
a `parent` of `None` is the whole mechanism behind an amnesiac burst, so there is
no way to ask for a volume that resumes nothing yet still holds the session.

## From the MVP

What Stage A moves, and where:

| MVP shape | Target shape | Issue |
|---|---|---|
| `tasks.task_id` = bare issue number | derived `owner/repo#n` + `issue_number` + the one-task-per-issue index (`repository`'s FK waits for C1) | A6 ✔ |
| `tasks.repo` = the platform's clone path | `repository` = `owner/name`; the clone path is a runtime argument and lives in no row | A6 ✔ |
| `tasks.transcript_key/-_checksum` (one blob per task) | `bursts.session_key/-_checksum` (one snapshot per burst) | A5 stopped writing them ✔, A6 carried the blobs over and dropped the columns ✔ |
| task-keyed archive `put`/`get` | key-based, one object per burst, checksum passed in | A5 ✔ |
| ISO-`TEXT` timestamps everywhere | `timestamptz` columns, strings at the edge | A3 ✔ |
| `_ensure_column` ad-hoc migration | numbered SQL + `schema_migrations` + `migrate` verb. The single-node ledger self-bootstraps a *fresh* file and no longer repairs an old one: A6 retires it as an origin, so it refuses a pre-A6 file and names `control-plane backfill`. What survived is `_ADDITIVE_COLUMNS`: a column with a default, added to a file whose ids are already current, is DDL and not guesswork — the SQLite half of a numbered migration, listed one line per migration | A2, A6 ✔ |
| the experiment's history, single-node | `control-plane backfill --repository owner/name`: rows re-keyed, blobs re-keyed as their last dev burst's snapshot, manual-era rows dropped, source left read-only | A6 ✔ |
| no claim verbs (single process) | lease + CAS statements in the `Ledger` protocol | A3 (the finalize CAS), E1 (`claimed_by`/`claimed_until` + the claim verbs), E2 (used in anger) |
| STOP flag on the filesystem | `org_state` row | E3 |

The `Ledger` protocol keeps its nine MVP methods; new verbs (claim, CAS
transition, finalize) arrive additively at A3 so `SqliteLedger` callers never
change.

## Parity, and what it can mean

Gate A7 asks that the MVP scenario run the same on Postgres + SeaweedFS as on
SQLite + a directory. It cannot mean equal bytes — ids are random, timestamps
advance, and a model's cost is its own business — so every field lands in one of
three buckets, and `scripts/ledger-parity.py` + `scripts/archive-parity.py` are
where the buckets are declared:

- **strict** — shape, status, and lineage. Compared after *interning*: a burst id
  becomes `B1, B2 …` in start order and `resumed_from_burst_id` becomes its
  target's symbol, which turns "resumed from the burst before it" into a
  comparable value instead of an opaque hex string. `session_key` gets the same
  treatment, so the key scheme is compared and not merely its presence.
- **excused** — what cannot repeat: `burst_id`, `trace_id`, `span_id`,
  `session_id`, `session_checksum` (a fresh tarball has its own sha256), every
  timestamp. Interned or shape-checked rather than compared.
- **variance** — `cost_usd`, `input_tokens`, `output_tokens`. Printed, never
  compared: a difference there is the model's, not the store's. `num_turns`,
  `exit_code` and `status` are deliberately strict.

Three asymmetries are by design, and a parity run should not report them:
`platform_app`'s grants and the walls that reject a write exist only in Postgres
(the single-node file carries none — "Walls in the schema"); `events.created_at` comes from
the database clock there and the app clock here, both meaning "when the row was
written"; and the payload is `jsonb`, so key order is normalized on the way in
while SQLite keeps insertion order — callers see a parsed dict either way.

A clean parity report is also narrower than it looks: it compares the rows two runs
*left behind*. Walls that reject a write leave no rows, and those are
`tests/test_schema_walls.py`'s job, not a diff's.

What the gate found is what the buckets were built to find — divergence in a
*read*, not in a write. `bursts_started_since` compared ISO **strings** on SQLite
and **instants** on Postgres, so the daily-burst rail counted 0 bursts where
Postgres counted 2 (`+06:00`) and 2 where Postgres counted 0 (`-08:00`); the
SQLite path now respells the caller's stamp through `utc_iso` first.
`tasks_by_status` and `running_bursts` returned rows in an order no query
promised — the planner's, not the statement's — and now break their ties on
`task_id` and `(started_at, burst_id)`. All three are held by
`tests/test_ledger_contract.py`, which is where a claim about two backends
belongs.

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
