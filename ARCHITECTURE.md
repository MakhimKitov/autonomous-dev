# Autonomous Maintenance Platform — Architecture

Source of truth for the whole experiment. Spans three repos; sits above all of
them. Each repo's own `CLAUDE.md`/`AGENTS.md` governs work *inside* that repo;
this document defines how the pieces fit and what we're testing.

> Status: **architecture defined, not yet built.** Today's `autonomous-dev` code
> is a PoC that only proves `claude-agent-sdk` can drive the Claude Code CLI. It
> will be reworked to match this document.

---

## 1. What this is

Not an app — a **platform** that runs an autonomous *developer* against a *product*
codebase. A developer runs in a container with a clone of the product repo, works
like a normal engineer (branch → code → test → PR), and hands a pull request to a
human for review. The product (a Discord bot) deploys via its own CI on merge.

The first product under maintenance is the sibling Discord bot
(`discord-bot`); the platform is product-agnostic by design.

## 2. Hypotheses & bars

The experiment tests three things at once. Each maps to a metric family (§9):

| Hypothesis | Question | Bar |
|---|---|---|
| **Reliability under constraint** | Does it produce correct, mergeable work while honoring the operating contract, at an acceptable human-intervention rate? | qualitative, tracked over time |
| **Containment / safety** | Do the walls hold, unattended, even under prompt-injection from real users? | **zero successful breaches** (hard pass/fail) |
| **Raw capability** | Can it produce correct non-trivial fixes/features in this codebase at all? | review pass-rate, post-merge defects |

## 3. Components & repos

> Rule: a separately-running component gets its own repo.

| Component | Repo | Lifecycle | Identity / blast radius |
|---|---|---|---|
| **Control plane** (scheduler) | platform repo (`autonomous-dev`) | long-lived | spawns Docker containers, holds run state |
| **Dev agent** (the developer) | same platform repo (payload image) | ephemeral, per-issue | GitHub App scoped to product repo: `contents`+`pull_requests` write, **no merge/admin/secrets** |
| **Monitor** | **separate** repo | long-lived | telemetry-read + `issues:write` only |
| **Product** (Discord bot) | `discord-bot` | runs in prod (human-operated) | n/a — the thing maintained |

Control plane + dev agent share one repo because the agent is the image the control
plane builds and runs in lockstep, not an independent service. Revisit if the
developer ever needs to be reused across products.

## 4. Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│ PLATFORM repo                                                         │
│  ┌───────────────┐  spawn (Docker) + assign issue  ┌──────────────┐  │
│  │ Control plane │ ───────────────────────────────▶│ Dev container│  │
│  │  scheduler    │   track run / budget / killswitch│ clone+agent+ │  │
│  │  owns issue→  │ ◀─────────────────────────────── │ product tool │  │
│  │  dev mapping  │            PR opened / done       │ chain        │  │
│  └───────────────┘                                   └──────────────┘  │
└───────────────────────────┬───────────────────────────────────────────┘
                            │  (all interaction via GitHub API)
   MONITOR repo  ───────────┤
   logs·metrics·Alertmanager│  files issues (provenance = auto)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│ GITHUB  — integration substrate / source of truth                    │
│  Issues  queue: human-filed = trusted · monitor-filed = auto/tainted │
│  PRs     work output + Copilot review + HUMAN review gate            │
│  Branch protection  no merge without human approval                  │
│  CI/CD   tests + deploy-on-merge   (owned by product repo)           │
└───────────────────────────┬───────────────────────────────────────────┘
                            ▼
         PRODUCT (Discord bot) — live, real traffic, human merges → deploys
```

## 5. GitHub is the substrate

> The concrete protocol — label taxonomy, issue/PR templates, branch/PR conventions,
> identities & scopes — is **`GITHUB-INTERFACE.md`**.

The product's bespoke local protocol (`ops/tasks/`, `ops/reports/`) is **replaced**
by GitHub primitives:

- **Issues** = the task queue. Triaged and assigned by the control plane.
- **PRs** = release candidates *and* the review gate.
- **Branch protection** = the "never deploy to prod" wall: the dev identity cannot
  merge; deploy happens on human merge.
- **CI/CD** (product repo) = tests on PR; releases via `release-please` (Release PR →
  human merge → deploy). Not the platform's concern. Protocol: `GITHUB-INTERFACE.md`.
- **Reports** = PR description + issue comments. Run telemetry stays in the
  platform's own store, never in the product repo.

What survives of the product repo's contract: the **rules** in its
`CLAUDE.md`/`AGENTS.md` (treat user content as untrusted, small reversible changes,
reproduce-before-fix) — delivered as the dev agent's operating prompt, not as an
`ops/bin` filesystem mechanism.

## 6. Run lifecycle

Two human gates bracket an autonomous middle: a human approves *what* to work on
(intake) and *what ships* (PR review + merge). The engineering in between is unattended.

```
issue (open)
  ─▶ HUMAN triage: approve via `ready` label            ◀── gate 1 (intake)
  ─▶ control plane assigns + spawns a BURST container
  ─▶ agent: reproduce ─▶ branch ─▶ code ─▶ run tests ─▶ in-container Codex review
  ─▶ open PR ─▶ container tears down                     ◀── burst ends
  ─▶ post-PR: GitHub Copilot reviews the PR, posts findings
  ─▶ HUMAN review                                        ◀── gate 2 (ship)
       ├─ changes requested ─▶ control plane spawns a NEW burst (resume from branch+PR)
       └─ approve + merge ─▶ CI deploys ─▶ issue closed
states: open · ready · assigned · in-progress · pr-open · needs-review ·
        changes-requested · blocked · done
```

- **Gate 1 — human triage.** Nothing is assignable until a human approves the issue
  (a `ready` label). This is the primary control on *what work happens*; it sits on
  top of the provenance gate (§8), which still governs *how* the dev interprets an
  approved issue's body. Monitor proposes → human disposes → dev executes.
- **Assignment authority = control plane**, not GitHub labels. With N devs, labels
  only *reflect* its decision — never the lock (race-free claiming).
- **Container lifecycle = ephemeral per *work-burst*, not per issue.** A burst is one
  unit of compute — "issue → PR" *or* "address one round of review feedback." The
  container runs the burst, pushes the branch, and **dies**; it never idles through
  the human-paced (possibly multi-day) review loop. Durable state lives in **GitHub
  (branch + PR thread + issue) plus a persisted agent session**; a follow-up burst
  spawns fresh and **resumes** from it. This keeps per-burst context clean
  (isolation, crash recovery = respawn, scaling = spawn more) without parking idle
  containers. Per-burst setup is amortized by a **warm base image** (toolchain +
  deps pre-baked → a burst is just `git fetch/checkout` + agent boot); a warm pool
  is the escape hatch only if setup is heavy.
  → **Shutdown rule: a container lives exactly one burst.**
- **Control plane is event-driven:** spawns a burst when there is an approved,
  unassigned issue, or a PR with actionable feedback / CI failure.
- **Crash recovery:** a dead burst releases its issue/PR back (lease + heartbeat on
  the control plane); respawn re-derives state from GitHub + session.
- **Kill switch + budgets:** instant stop + per-burst `max_budget_usd`, wall-clock
  cap, and a global daily ceiling enforced by the control plane (independent of the
  agent behaving).

### Session continuity (confirmed, SDK v0.2.110)

The worker keeps its **full context across the whole task** — one persistent Claude
session per task, not per burst. Mechanism, confirmed from the installed SDK:

- The session id comes from `ResultMessage.session_id`; a later burst resumes by
  setting `ClaudeAgentOptions.resume = <session_id>`, which replays the full prior
  transcript.
- Default storage is `~/.claude/projects/<cwd>/<id>.jsonl` — **in-container, so it
  dies with the burst.** To persist across bursts/hosts, the control plane
  implements the SDK's **`SessionStore`** adapter (async `append`/`load`) backed by
  external storage and keyed by issue. With a `SessionStore`, resume works on **any
  host regardless of cwd**; transcripts are portable JSONL (no host-specific state).
- **Caveat — no SDK-layer compaction:** resume replays the *entire* transcript, so a
  task spanning many review rounds grows toward the context limit. The control plane
  / `SessionStore.load()` owns a growth policy (truncate or summarize old turns).
- **Caveat — resume carries the conversation only**, not settings/hooks/MCP/permission
  config. Those are **baked into the dev image** — which is desirable: the walls (§8)
  live in the image, not in resumable state the agent could touch.

So state per task splits three ways, all keyed by issue and reconstituted each burst:
**branch** (workspace ← GitHub) · **session** (context ← control-plane `SessionStore`)
· **issue/PR thread** (status + human conversation ← GitHub).

### Review — two independent stages

1. **Pre-PR — Codex, in-container.** Before opening the PR the agent runs Codex on
   its own diff and addresses findings (bounded rounds — no infinite loop).
2. **Post-PR — GitHub Copilot, on the PR.** Copilot is configured as an automatic
   reviewer on the product repo, so it reviews the diff and posts findings on PR
   creation — independently of platform compute (the burst container is already torn
   down at PR-open), with no custom Action to maintain.

Two *different* reviewers — Codex then Copilot — both read-only and distinct from the
implementing agent, so each is a genuine independent check. The human remains the
final gate.

## 7. The monitor

Separate service (own repo, own creds, own lifecycle). Watches the product's
**logs, metrics, and deterministic alerts (e.g. Alertmanager)** and **files GitHub
issues** describing symptoms.

- It **only produces issues** — it never talks to the dev directly. The issue is
  the clean, auditable, rate-limitable boundary.
- **An issue means a code change is needed.** Operational-only signals (transient errors,
  capacity, config drift) are *not* issues — they stay in the alerting plane (Alertmanager
  / dashboards) for humans. The monitor is the narrow bridge from code-change signals to
  issues, not the general alerting system.
- Monitor-filed issues carry **`auto` provenance** (see §8).
- Issues are **structured/templated**, not raw log dumps — narrows injection surface.
- Rate-limited so it can't manufacture unbounded work.
- **Future (not now):** the monitor reacts (opens its own remediation tasks) rather
  than only reporting. Designed-for, out of current scope.

## 8. Trust model & enforcement

Hybrid: catastrophic boundaries are **walls** (physically impossible); process
rules are **tripwires** (instructed, observed, counted as data).

| Boundary | Mechanism | Type |
|---|---|---|
| Can't deploy to prod | branch protection (no-merge identity) + deploy-on-merge | wall |
| Can't edit its own maintainer | container holds only the product clone; platform code isn't in the box | wall |
| Repo-scope confinement | the container *is* the scope | wall |
| Blast radius | GitHub App scoped to product repo, no merge/admin/secrets | wall |
| No prod creds (Discord token, DB) | simply absent from the dev environment | wall |
| Untrusted user content | handled by the **separate** monitor + structured issues | wall |
| Reproduce-first · write report · small reversible changes | operating contract; observer logs every deviation | tripwire (data) |

### The injection-laundering path (the critical one)

```
Discord user types malicious text
  └▶ bot logs it ─▶ monitor files an issue containing it
        └▶ dev reads issue ─▶ ⚠️ could execute the injection as "work"
```

**Provenance gate** breaks it:

- **`human` issues** → spec is *trusted instruction*; the dev may act on its prose.
- **`auto` issues** (monitor/agent-filed) → body is *tainted data describing a
  symptom*, **never an instruction**. The dev reproduces from first-hand evidence
  (runs the failing test itself), acts only within a fixed envelope
  (reproduce → verify → propose), and an auto issue can **never** widen the agent's
  own scope or permissions.

Upstream of this, **human triage (§6, gate 1)** is an additional control: no issue
— auto or human — is worked until a human approves it. Triage governs *whether*
the work is legitimate; the provenance gate governs *how* the body is interpreted.

## 9. Observability & metrics

Telemetry lives in the **platform's** store (never the product repo, so the agent
can't read or skew its own scorecard).

- **Reliability** → task success rate · blocked/needs-review rate · rework rate · cost/task
- **Containment** → wall-hit count · attempted-violation count · **successful-breach count (must be 0)**
- **Capability** → automated-review pass-rate (Codex + Copilot) · human-review pass-rate · post-merge defect rate · task difficulty handled
- **Violations ledger** — every tripwire firing and every wall block, logged. This
  is the experiment's primary dataset.

## 10. Rollout phases

Same architecture throughout — exposure is graduated, not the design.

0. **Walls-first:** single dev container, human-filed issues only, static/staging
   product. Prove **0 wall failures** over N runs. → build plan in `PHASE-0.md`.
1. **Loop:** add the monitor + auto issues (provenance gate live) on staging /
   recorded traffic. Watch the laundering path specifically.
2. **Live:** real Discord traffic, then horizontal scaling. Only after 0–1 show
   zero successful breaches.

## 11. Scope: now vs. future

- **Build now:** control plane spawning **one** dev container per work-burst on
  Docker; human-triaged intake (`ready` label); GitHub-native flow (issue → PR →
  human review → merge → CI deploy); two-stage review (Codex pre-PR + Copilot
  post-PR); monitor that **files** issues; the wall set in §8.
- **Designed-for, not built:** horizontal scaling (N containers); monitor
  **reaction** (auto-remediation); k8s as the runtime when N>1 is real.

## 12. Decisions

**Resolved**

- **Repos:** control plane + dev agent in **one** repo behind a clean
  `control-plane` / `dev-agent` module seam (cheap to split later if the developer
  becomes a reusable, product-agnostic artifact). Monitor is its own repo.
- **Container lifecycle:** ephemeral **per work-burst** with resume-from-durable-state
  (§6). Warm base image to amortize setup; warm pool only if setup is heavy.
- **Intake:** human-triaged via a `ready` label (§6, gate 1).
- **Review:** two independent stages — Codex in-container pre-PR, GitHub Copilot on
  the PR post-PR (§6).
- **Staging:** the RC-validation env; product CI deploys the PR/branch there.
- **Session continuity:** one persistent Claude session per task, externalized via a
  control-plane `SessionStore` adapter; resume by `session_id` each burst (§6).

**Still open**

- Transcript-growth policy for long multi-round tasks (truncate vs. summarize old
  turns in `SessionStore.load()`, and/or a turn cap). Defer until a task actually
  spans enough rounds to approach the limit.
- `SessionStore` backend (S3 / Redis / Postgres) — pick when building the control plane.
- Warm-pool threshold — defer until per-burst setup time is measured.
