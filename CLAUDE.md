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
uv run control-plane burst --task-id t1 --repo ../discord-bot --mode check --data-dir data

# One real burst (needs CLAUDE_CODE_OAUTH_TOKEN in the control-plane env)
uv run control-plane burst --task-id t1 --repo ../discord-bot \
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
uv run control-plane migrate                         # numbered SQL; idempotent
uv run control-plane migrate --check                 # exit 1 if anything is pending
```

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

With `LEDGER_DSN` set, `uv run pytest` additionally runs the ledger contract
(`test_ledger_contract.py`) against Postgres as well as SQLite, in a throwaway
database. Everything else keeps its isolated SQLite ledger regardless — see
`tests/conftest.py` for why.

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
