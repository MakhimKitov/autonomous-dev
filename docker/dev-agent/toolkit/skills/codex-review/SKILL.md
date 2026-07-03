---
name: codex-review
description: Pre-PR self-review with Codex. Use ALWAYS before opening a pull request — after tests are green and your changes are committed on the branch. Also use to re-check the diff after addressing human review feedback, before pushing.
---

# Pre-PR Codex self-review

Codex is your independent reviewer — a different model reading your diff. The review
is **read-only by design**: Codex must never modify anything, and it gets no
credentials. Do not improvise the invocation; use exactly the recipe below.

## Invocation

```bash
codex exec review --base main -c 'sandbox_mode="read-only"' 2>&1
```

- `codex` here is the platform shim: it scrubs your credentials (`GH_TOKEN`,
  `GITHUB_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`) unconditionally and
  **refuses sandbox-weakening flags** (`--dangerously-bypass-approvals-and-sandbox`,
  `danger-full-access`, `--yolo`) — don't pass them; the attempt is refused and logged.
- If the read-only run fails, that is a finding to report, not a reason to escalate
  permissions.
- Expect a startup `shell_snapshot` ERROR line in the output — it is cosmetic; the
  review still runs. The verdict is the final `codex` block.

## Handling findings

- Address **genuine correctness findings** (bugs, missed edge cases, broken tests).
  Style opinions and scope expansions are yours to decline — the issue's scope
  boundaries outrank the reviewer.
- At most **two rounds** (review → fix → re-review). Then stop, regardless.
- Quote the final verdict **verbatim** in the PR body's Evidence section, including
  which invocation produced it.
- If Codex is unavailable (auth, network), say so explicitly in the PR body instead
  of silently skipping — never fabricate a review verdict.
