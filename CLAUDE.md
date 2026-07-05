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
