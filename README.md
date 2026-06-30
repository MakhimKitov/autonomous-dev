# autonomous-dev

The **platform** in an autonomous-maintenance experiment: a control plane plus a
dev-agent image that maintains a *product* repo (the sibling `discord-bot`) by working
like an engineer — branch, code, test, open a PR — through GitHub. Built on
[`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python).

Design docs live here: **`ARCHITECTURE.md`** (the full design), **`GITHUB-INTERFACE.md`**
(the GitHub protocol), **`PHASE-0.md`** (the first build slice).

## Quick start

```bash
uv sync
cp .env.example .env   # add ANTHROPIC_API_KEY, or run `claude login`

# Current PoC: run one task against the product repo (defaults to read-only 'plan' mode):
uv run autonomous-dev "Summarize the repo layout" --repo ../discord-bot

uv run pytest          # tests
```

## Status

**PoC scaffold** — proves `claude-agent-sdk` can drive the Claude Code CLI (config → agent
loop → streamed result + cost). To be reworked into the control-plane / dev-agent split
described in `ARCHITECTURE.md`. Requires the `claude` CLI on PATH.
