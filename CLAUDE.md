# autonomous-dev — the platform

This repo is the **platform** in the autonomous-maintenance experiment: the **control
plane** (scheduler) plus the **dev-agent** image it runs. The dev agent maintains a
*product* repo — any codebase, passed as a parameter (the experiment's first instance
is the sibling `discord-bot`) — by working like an engineer — branch, code, test, open
a PR — through GitHub.

## Engine

[`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python) — Claude
Code as an embeddable Python library (agent loop, tool suite, layered permission model,
sandbox mode, sessions/resume, `max_budget_usd`). We build on these primitives rather
than reinvent them. Requires the `claude` CLI on PATH.

## Setup

```bash
uv sync                      # create the venv, install deps
cp .env.example .env         # add CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`)
```

## Layout

Two workspace packages behind the §12 seam — `packages/control-plane` (scheduler,
owns all persistence + telemetry) and `packages/dev-agent` (the burst worker; ships
alone in the image and **must never import control-plane**). The env-var contract
lives in `dev_agent.env`; control-plane imports it, never the reverse.

## Run

```bash
# Build the dev-agent image
docker build -f docker/dev-agent/Dockerfile -t autonomous-dev/dev-agent:latest .

# Boot smoke (no network, no key): container check in a clone
uv run control-plane burst --issue 1 --repo ../discord-bot --mode check --data-dir data

# One real burst (needs CLAUDE_CODE_OAUTH_TOKEN in the control-plane env)
uv run control-plane burst --issue 1 --repo ../discord-bot \
  --prompt "Summarize the repo layout" --budget 1.0

# The M2 scheduler (SCHEDULER.md): ready issue → PR → review rounds, unattended
uv run control-plane run --repo ../discord-bot     # loop (--once for a single tick)
uv run control-plane stop                          # kill switch: STOP flag + docker kill
```

## Stores

SQLite + a local directory by default; the service backends are presence toggles,
not modes (`STORES.md`). The dev rig supplies both:

```bash
docker compose -f docker/compose.yaml up -d --wait   # Postgres + SeaweedFS
export LEDGER_DSN='postgresql://platform_admin:platform@localhost:5432/platform?sslmode=disable'
export ARCHIVE_URL='s3://platform:platform-dev-secret@localhost:8333/platform-sessions?tls=false'
uv run control-plane migrate                         # numbered SQL; idempotent
uv run control-plane migrate --check                 # exit 1 if anything is pending
```

Two toggles, two stores, and they are independent — a deployment can buy one
capability before the other. `tls=false` is the archive's `sslmode=disable`; the
bucket is created on first write if it isn't there.

`sslmode=disable` is in the DSN so the same string works for psycopg and for Atlas,
which refuses to guess. `open_ledger` is the one place a backend is chosen; nothing
else may name one.

`migrate` is the only command a fresh database needs: it creates the `platform_app`
role if the cluster has none (passwordless — the schema owns what the role may do,
the operator owns how it logs in) and grants it what the runtime needs. Those
grants are walls: `violations`, `audit` and `events` are `SELECT, INSERT` only, and
nothing anywhere gets `DELETE`. Two more walls live outside the schema's reach —
the `roles_pipeline_guard` trigger and the grants themselves — so
`tests/test_schema_walls.py` provokes every wall and requires an error; that file
is the only thing standing behind those two.

### Changing the schema

`schema.sql` states what the schema should be; the numbered migrations are the path
that gets a database there. Never write ad-hoc DDL in the store layer, and never
edit an applied migration.

```bash
# 1. edit schema.sql — the desired state
# 2. ask Atlas what would have to change (read-only; ~30s, it spins its own dev db)
atlas schema diff --env local --from "$LEDGER_DSN" --to file://schema.sql
# 3. save that SQL as the next migrations/NNNN_slug.sql, then apply it
uv run control-plane migrate
# 4. re-run step 2 — "Schemas are synced" is what CI checks
# 5. and check what the DDL *does*, not just what it declares:
uv run squawk packages/control-plane/src/control_plane/migrations/*.sql
```

`squawk` catches DDL that is correct and still stalls a live replica. Anything
added to a live table needs the two-step `NOT VALID` (one migration) +
`VALIDATE CONSTRAINT` (the next), and a `UNIQUE` wall wants `CREATE UNIQUE INDEX`
rather than `ADD CONSTRAINT`; squawk says so if you forget. Rules live in
`.squawk.toml`, each exclusion carrying the claim that justifies it; the two
timeout rules are satisfied by the runner (`LOCK_TIMEOUT` / `STATEMENT_TIMEOUT` in
`migrate.py`), so no migration has to remember them. A single statement can be
excused with `-- squawk-ignore <rule>` on the line above it, which is the right
place for it — the reason sits next to the statement and the rule stays live for
the next one.

Atlas is a development instrument, never a runtime dependency: it diffs and it
lints, `control-plane migrate` applies. It cannot see triggers, grants, seeded
rows, views, or whether a constraint is still `NOT VALID` — so it will call a
database with a forgotten `VALIDATE CONSTRAINT` synced, and it *refuses outright* a
`schema.sql` containing a `CREATE VIEW` ("views are available to logged-in users
only"), which is why B3's reporting views live in their migration with
`tests/test_reporting_views.py` behind them. Walls and definitions of that kind are
tested by provoking them, not by diffing (`ARCHITECTURE.md` §12).

**A column lands in three places, sometimes four.** The numbered migration, then
`schema.sql` (the declared state Atlas diffs), then the SQLite `_SCHEMA` in
`store.py` — and if a file created before the column has to survive it,
`_ADDITIVE_COLUMNS` beside it, with a test that opens a pre-column file. The first
three are self-enforcing: both `record_burst` and `upsert_task` build their column
lists from the dataclass, so a field with no SQLite column fails the first insert
and CI's `test` job (which runs single-node) goes red. The fourth is not — CI only
ever creates fresh files, so it is the one to remember.

### The product registry

The tenancy unit is a registered product (TO-BE §2); the verbs are C1's:

```bash
uv run control-plane product register owner/name --team bots   # defaults materialized from .env
uv run control-plane product list
uv run control-plane product show owner/name                   # the row + its audit trail
uv run control-plane product pause owner/name --reason "..."   # the reason is required
uv run control-plane product resume owner/name
uv run control-plane product set owner/name --e2e on --test-env DISCORD_TOKEN=env:TEST_DISCORD_TOKEN
uv run control-plane product check owner/name --lint-issue    # re-run preflight; file the gaps (C2)
```

Three rules carry the design. **Defaults are materialized, not inherited**:
`register` fills every policy/quota column from the platform env (validated
through `ReviewPolicy.declared` — an incoherent declaration never becomes a row),
and a changed platform default never retro-propagates — changing a live product is
`product set`, audited. **Every mutation is one write**: the row change, its
`audit` row (actor, action, target, reason) and its outbox event commit together —
a CTE on Postgres, a transaction on SQLite. **A task names a registered product**:
`tasks.repository` is a foreign key (the registry wall), so on the service ledger
`run` and `backfill` need the product registered first — migration 0012 seeded
rows for repositories that already had tasks, with placeholder policies the audit
row tells you to `product set`. Test-env values never enter the ledger: `--test-env`
takes `NAME=env:REF` / `NAME=file:PATH` references, resolved at D4.

**Registration preflights (C2)**: `register` — and `set`, when it touches a policy
column — runs `preflight.run_preflight` before the write. The *gate* refuses a
declaration the platform cannot execute: the org App unable to read the repo, a
review provider without its credentials on this machine (the old scheduler-startup
error, relocated), `e2e on` without the claude provider or without a parseable
`TESTING.md` whose declared env every ref covers *and resolves* (env var set /
file present — values are never read). Everything else is *socket lint* —
`CLAUDE.md`, issue forms, PR template, commitlint config — warned and recorded in
the registration's audit row, never a refusal. `product check` re-runs the same
preflight against the live repo (exit 1 = drift: the row is no longer executable);
`--lint-issue` on `register`/`check` files the gaps as a `human` issue in the
product repo. Quota/team changes and pause/resume never probe GitHub.

**Row-sourced policy (C3)**: at scheduler startup, a registered product's review
and e2e policies come from its row (`ReviewPolicy.from_product` /
`E2EPolicy.from_product`) — the row is the entire truth about the declaration, and
a machine-level `REVIEW=off` never silently degrades a registered product. The env
keeps answering only what stays a machine question: credentials (the honor-check
fires from the row exactly as it did from env — registration preflight and startup
share the constructor) and the `REVIEWER_MODEL`/`TESTER_MODEL` overrides (the row
has no model column on purpose). `from_env` remains dev mode: an unregistered repo
on the SQLite ledger runs env-governed until C4 makes the scheduler
registry-driven.

**Registry-driven scheduler (C4)**: `run` without `--github` serves every active
registered product through one serial loop (`Fleet`). The registry is re-read
every tick (register/pause/set need no restart); each product's scheduler is
built from its row, with the row's `daily_bursts` as its ceiling. At most one
burst per tick — the least recently served product first
(`repositories.last_burst_at`, stamped via `touch_repository_burst`: operational
state, no audit row). A product's scheduler reads only its own tasks and bursts
(repository-scoped ledger queries), and one product's trouble quarantines that
product for the tick, never the fleet. `--github owner/name` still pins one repo
— dev mode when unregistered. SCHEDULER.md "The fleet" has the full contract.

**Role-prefixed App credentials (D1)**: one App per pipeline role, addressed by
env prefix over the same three names — the dev App reads `GITHUB_APP_ID` /
`GITHUB_APP_PRIVATE_KEY_PATH` bare, the others behind `REVIEWER_`, `TESTER_`,
`MONITOR_` (`ROLE_PREFIXES` in `github_auth.py`; `MONITOR_` is reserved until
T4). App names are the adopter's choice — the prefix scheme is the contract.
`control-plane github-check` preflights every configured role: a declared App
that can't mint fails the check (exit 1), an unconfigured role is reported and
skipped. The tester still posts under the reviewer App until D3.

### Task identity

A task id is derived, never typed: `{owner}/{repo}#{issue_number}` (`task_id_for`).
Both places that mint one — the scheduler's intake and the `burst` verb — build it
from the repository slug, so a hand-run burst joins the task the scheduler would
have created rather than making a second one. `UNIQUE (repository, issue_number)`
makes a duplicate unrepresentable, and `tasks.repository` holds the slug: the
platform's clone path is a fact about one machine and lives in no row.

MVP-era data — bare-number ids, a clone path in `repo` — moves with one verb:

```bash
uv run control-plane backfill --repository owner/name --data-dir data --dry-run
```

It reads the single-node pair read-only and writes to whatever the toggles name:
rows re-keyed, each task's one blob re-keyed as its last dev burst's snapshot,
manual-era rows (`t1` and friends — ids that were never issues) dropped and
counted. It refuses a second run rather than colliding on every derived id.
Two consequences worth knowing: `migrate` **fails on purpose** at `0008` while any
task lacks an `issue_number` (the repair is the verb, and the state after the
failure is a prefix), and `SqliteLedger` refuses to open a pre-A6 file for the same
reason instead of half-repairing it.

### The session tree

One immutable snapshot per burst, keyed `sessions/{task_id}/{burst_id}/session.tar.gz`,
and the ledger owns the lineage: `resumed_from_burst_id` names the parent a volume
was materialized from, `session_key`/`session_checksum` name what the burst left.
`latest_archived_burst(task_id)` is the leaf — the newest **dev** snapshot: a
tester's volume is archived too (B5), but as evidence the role filter keeps out of
the tree — and `prepare_volume(..., parent=None)`
is how an amnesiac burst is expressed — there is deliberately no way to ask for a
volume that resumes nothing but still holds the session. Snapshots are write-once:
a retry is a new burst, so a new key, and branching is a ledger write.

With `LEDGER_DSN` and/or `ARCHIVE_URL` set, `uv run pytest` additionally runs that
store's contract suite (`test_ledger_contract.py`, `test_archive_contract.py`)
against the service backend as well as the single-node one. Everything else keeps
its own scratch ledger and archive regardless — see `tests/conftest.py` for why
that has to be forced.

### Parity, at a gate

The contract suites hold the backends to one answer per method; a `gate` issue asks
it of a whole scenario. Run the same commands twice — toggles set, then unset, into
two data dirs — and compare with the instruments A7 built:

```bash
uv run python scripts/ledger-parity.py  data-single/ledger.sqlite3 "$LEDGER_DSN"
uv run python scripts/archive-parity.py data-single "$LEDGER_DSN" "$ARCHIVE_URL"
```

Both exit non-zero on a difference. What they can and can't say — the three
buckets, and the asymmetries that are by design — is `STORES.md`, "Parity, and
what it can mean". They are by-hand instruments (a live rig, a real burst), so
`tests/test_gate_instruments.py` is what keeps a rename from silently breaking
them between gates.

## Telemetry

Two OTLP endpoints, because two processes in two network namespaces. Both are
presence toggles; unset means spans exist only to mint correlation ids, and the
container is told nothing about telemetry at all.

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318          # the control plane's own
export DEV_AGENT_OTLP_ENDPOINT=http://host.docker.internal:4318   # where a *burst* pushes
```

The second string is the one that gets it wrong: `localhost` inside the box is the
box, and the CLI drops export failures silently — a burst with an unreachable
collector looks exactly like one with none. Point both at the same collector by two
different names, and if nothing arrives, `CLAUDE_CODE_OTEL_DIAG_STDERR=1` in the
container env makes the CLI say why.

What a burst emits is decided in `control_plane.telemetry.agent_telemetry_env`, with
the reason for each choice next to it: traces and log events yes, metrics no (the
ledger is the metrics store — TO-BE §12), tool arguments yes, tool and API bodies no.
`CONTROL_PLANE_TRACE_CONSOLE=1` prints the control plane's own spans instead of
pushing them; it is never passed into a container, where stdout is the SDK's message
channel.

The rig that receives all this is `docker/compose.telemetry.yaml` (B4) — collector,
VictoriaLogs, Phoenix, Grafana, no volumes, four commands to boot. **`OBSERVABILITY.md`
is the read**: the correlation join, the three UIs, what to check when nothing
arrives, the alert condition in the three forms an adopter can consume, and the
reference dashboards in `examples/dashboards/`. The traps it records because they cost
real time: a correlation id used as a VictoriaLogs stream field kills the process, a
collector config mounted at the other distribution's path is silently ignored while
everything reports healthy, and Phoenix's cost is its own estimate (~0.64× measured)
because it prices tokens rather than reading the CLI's figure — the burst row stays
the authority. The rig's Grafana connects as a role that can read the views and
nothing else, so a panel reaching for a base table fails rather than widening the
boundary.

## Test

```bash
uv run pytest                # structural tests, no network
```

## Conventions

- Python 3.12, `uv` for env/deps, type hints throughout.
- `async` core with a thin sync wrapper for the CLI.
- Keep the seam to the SDK in one place; layer guardrails as composable hooks.
- Logs are structlog, slog-shaped (event phrase + `key=value`), configured once per
  CLI entry via `dev_agent.log`: colorized console on a TTY, logfmt everywhere else.
  `print()` is reserved for payload — command output, agent prose, container streams.
