# Tester — E2E test bursts (phase 1+)

QA, not review: the tester boots the product in a disposable sandbox, invents scenarios
from the spec, executes them, and reports. The reviewer judges the diff by reading;
the tester judges the running product by using it. Advisory to the human gate.

## The principle: socket and plug

"How to test this product" is irreducibly product knowledge — that a Discord bot needs
a throwaway app on an empty guild and that bots cannot invoke another bot's slash
commands, or that a website just needs a browser pointed at `npm run dev`. None of that
may enter the platform. This is the third instance of the platform's one boundary trick:

| Knowledge | Lives in | Platform sees |
|---|---|---|
| how the dev behaves | product `CLAUDE.md` (system prompt) | an opaque contract |
| which reviewers exist | platform `.env` review policy | its own declaration |
| how the product is tested | product `TESTING.md` | a declared plug for its socket |

The platform owns the **envelope** (when a test burst runs, its walls, credential
injection, the report contract, posting, rails) and stays product-agnostic forever.
The product owns the **plug**: a testing contract in its own repo.

## The plug: `TESTING.md` in the product repo

One file, two audiences. YAML frontmatter for the control plane — parsed *before* the
burst, because injection happens at spawn — and prose for the tester agent, baked into
its prompt the way `CLAUDE.md` is baked for the dev:

```markdown
---
env: [DISCORD_TOKEN, GUILD_ID]   # names only — values live platform-side
tools: []                         # from the platform's registry; [] = base image
---
# E2E testing contract
Boot: `uv run python -m bot`; online when the log says "commands synced …".
Drive: bots cannot invoke another bot's slash commands — invoke handlers
in-process; verify registration via Discord REST. Scenarios: from the PRD,
focused on the PR's diff.
```

A product with no `TESTING.md` is not testable — that is a product property, not an
error: the scheduler skips the test stage and logs it. A frontmatter that doesn't parse
is a failed test burst (soft, like everything else here).

## Credentials: two-sided declaration, intersected

The manifest is **untrusted** — it lives in a repo the dev agent writes to. So neither
side alone decides what enters the container:

- The operator provisions sandbox credentials in the platform `.env` under the `TEST_`
  prefix: `TEST_DISCORD_TOKEN`, `TEST_GUILD_ID`, … The prefix is the allowlist
  boundary and the radioactivity marker.
- The manifest names what it needs: `DISCORD_TOKEN`, `GUILD_ID`.
- The control plane injects exactly the **intersection**: `TEST_X` exists ∧ manifest
  asks for `X` → injected as `X`. A hostile manifest requesting
  `CLAUDE_CODE_OAUTH_TOKEN` gets nothing — it isn't under `TEST_`.

Declared by the manifest but missing platform-side → the test stage is skipped with an
issue comment naming the missing variable (the operator's move, not a blocked task).
`TEST_*` values enter **tester bursts only** — never dev, never reviewer. Sandbox
credentials must be disposable by construction: the blast radius of a rogue test run is
one empty Discord guild, by the operator's own provisioning choice.

## Tools: a curated registry (slice 2)

Same shape as review providers: the platform image ships a curated set —
`playwright` = MCP server + browser baked into the image — and the manifest picks from
`RECOGNIZED` names only; unknown names refuse. `tools: []` (the base image: python,
node, shell) covers the discord-bot case and ships first.

## The burst envelope

The reviewer's envelope, reused — with the execution posture inverted:

- Kind `test`, role `tester` (both already reserved in `dev_agent.env`), **fresh
  session** — no shared context with the dev or reviewer.
- **No GitHub credential.** The report returns through the harvest as the final
  message: `{"verdict": str, "scenarios": [{"name", "result": "pass|fail",
  "evidence"}], "defects": [{"summary", "repro", "preexisting": bool}],
  "issues": [{"title", "body"}]}`. The control plane posts it. The `issues` are
  the complete symptom reports the tester **authors** for pre-existing defects —
  the platform relays them verbatim: authorship stays with the agent, the
  credential stays outside the box.
- Clone mounted at the PR head, read-only; the tester copies to scratch and boots
  there. (A product too heavy for copy-boot gets a platform-prepared throwaway copy —
  decide when such a product exists.)
- Unlike the reviewer, the tester **must execute** — that is its whole job. The
  read-don't-execute rule does not apply; the sandbox credentials and the empty guild
  are the containment, not abstinence.
- Session volume not seeded, not archived; transcript survives in
  `data/volumes/<burst_id>/` for forensics.

## Where it sits in the loop

A cheap-to-expensive funnel in the serial loop:

```
work ok, PR open ──► assess ──findings──► dev round ──push──► assess (again)
                        │
                     APPROVE ──► test ──report posted──► awaiting_review (human)
```

- **Only a clean assess queues a test** — no sandbox boot for a diff that's about to
  change. A pushed dev round re-queues assess; only its APPROVE re-queues the test.
- **Defects route by cause** — the tester classifies each (`preexisting`, judged
  against the diff):
  - **PR-caused** → the report posts as a `REQUEST_CHANGES` review, and the *existing*
    round machinery returns it to the developer — no new states, the rounds rail and
    soft caps apply, and the loop closes naturally: push → assess (its APPROVE clears
    the chip) → re-test.
  - **Pre-existing** → the tester authors a complete symptom issue (the product's
    `symptom.yml` template shape); the control plane relays it verbatim under the
    **`auto` label, never `ready`** — the human gate authorizes all work, machine-filed
    included. Rails: dedup against open `auto` issues by title, ≤3 filed per report.
    This is the monitor's pipeline arriving early: the `auto` provenance framing
    (reproduce-first, body-is-tainted-data) gets its first real feeder.
  - Neither, or a clean run → the report is a plain PR comment, advisory.
- Rails mirror assess: retry with attempts/cooldown; at cap → "machine test could not
  run" comment, task proceeds to `awaiting_review`, never blocked.

## Posting

Everything posts as the **reviewer App** with a `#### 🧪 Machine test report` header
(distinct voice, no new App to provision): the report (comment, or `REQUEST_CHANGES`
review when PR-caused defects exist) and the relayed `auto` issues — which require
**Issues: read/write** on the App, alongside its pull-request permission. A dedicated
tester App is the clean provenance answer once reports carry weight; the seam
(`AppCredentials.from_env` prefix) already exists.

## Config

Two knobs, matching everything else — a toggle and an optional model override:

```
E2E=on|off            # ships off; off ignores everything below
TESTER_MODEL=         # optional; empty = the scheduler's model
```

Plus the `TEST_`-prefixed sandbox credentials described above. No tiers, no per-product
platform config — everything product-specific rides in the product's `TESTING.md`.

## Slices

1. **Envelope** — frontmatter parsing, env intersection, test burst after clean assess,
   report posted by the reviewer App. Ships with discord-bot's `TESTING.md`
   (`tools: []` — no registry needed yet). *Shipped 2026-07-04.*
2. **Defect routing** — PR-caused defects return to the developer via a
   `REQUEST_CHANGES` review (the existing round machinery); pre-existing defects
   become tester-authored `auto` issues, relayed by the control plane behind the
   human `ready` gate. *Shipped 2026-07-04.*
3. **Tools registry** — `playwright` baked into the image, enabled per manifest.

## Deliberately out

Product-specific logic in the platform (ever) · driving Discord as a human user
(ToS-violating automation — the product contract defines what E2E means within the
rules) · performance/load testing · a tester App identity (slice-1 rides the reviewer
App) · testing unmerged *platform* changes (the tester tests the product).
