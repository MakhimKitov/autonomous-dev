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
rows, or whether a constraint is still `NOT VALID` — so it will call a database
with a forgotten `VALIDATE CONSTRAINT` synced. Walls of that kind are tested by
provoking them, not by diffing (`ARCHITECTURE.md` §12).

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
`latest_archived_burst(task_id)` is the leaf, and `prepare_volume(..., parent=None)`
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
channel. The dev-only rig that receives all this — collector, logs, trace UI — is
B4 (#22); until then any OTLP collector works.

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
