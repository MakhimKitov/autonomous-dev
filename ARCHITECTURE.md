# Autonomous Maintenance Platform — Architecture

Source of truth for the whole experiment. Spans three repos; sits above all of
them. Each repo's own `CLAUDE.md`/`AGENTS.md` governs work *inside* that repo;
this document defines how the pieces fit and what we're testing.

> Status: **MVP built and proven end-to-end** (2026-07). A human-`ready` issue became
> a merged, deployed feature through work bursts, autonomous machine-review rounds,
> and a live E2E test burst — unattended between the two human gates (discord-bot
> task 17 / PR #18 was the proving run). This document remains the *design*; where
> the build deviated, the subsystem docs are authoritative for as-built behavior:
> **`SCHEDULER.md`** (loop, state machine, rails), **`REVIEWERS.md`** (review
> providers), **`TESTER.md`** (E2E test bursts), **`GITHUB-INTERFACE.md`** (protocol).
> Known as-built deltas: stores are SQLite + a filesystem session archive (§6's
> Postgres/SeaweedFS is the scale-up design, not built); OTel/Phoenix tracing is not
> wired yet (structlog + the ledger stand in); the monitor is not built (the tester's
> symptom relay already exercises its `auto`-issue pipeline); pre-PR self-review was
> dropped in favor of reviewer bursts (decision record in REVIEWERS.md).

---

## 1. What this is

Not an app — a **platform** that runs an autonomous *developer* against a *product*
codebase. A developer runs in a container with a clone of the product repo, works
like a normal engineer (branch → code → test → PR), and hands a pull request to a
human for review. The product deploys via its own CI on merge.

The platform is **product-agnostic by design**: the product is a parameter — a repo +
its CI + its runtime. The first instance under maintenance is the sibling
`discord-bot`; it appears in this doc only as the running example, never as a design
assumption.

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
| **Tester** (QA) | same platform repo (**same image**, different contract) | ephemeral, per test-burst | **no GitHub credential** (as built — the report returns via harvest and the control plane posts it as the reviewer App; a dedicated tester App is deferred) · **product-sandbox creds** (`TEST_*` intersection, which the dev never has) |
| **Monitor** | **separate** repo | long-lived | telemetry-read + `issues:write` only |
| **Product** | its own repo (first instance: `discord-bot`) | runs in prod (human-operated) | n/a — the thing maintained |

Control plane + dev agent share one repo because the agent is the image the control
plane builds and runs in lockstep, not an independent service. Revisit if the
developer ever needs to be reused across products. The **tester is a role, not a
codebase**: the dev image booted with a different operating contract and a disjoint
credential set (§6 "Testing", §8).

## 4. Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│ PLATFORM repo                                                         │
│  ┌───────────────┐  spawn (Docker) per burst        ┌──────────────┐  │
│  │ Control plane │ ───────────────────────────────▶│ Burst        │  │
│  │  scheduler    │   track run / budget / killswitch│ container    │  │
│  │  owns issue→  │ ◀─────────────────────────────── │ dev·reviewer │  │
│  │  burst mapping│      result via harvest           │ ·tester roles│  │
│  └───────────────┘                                   └──────────────┘  │
└───────────────────────────┬───────────────────────────────────────────┘
                            │  (all interaction via GitHub API)
   MONITOR repo (phase 1+, ─┤
   not built) logs·metrics  │  files issues (provenance = auto —
                            │  today fed by the tester's symptom relay)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│ GITHUB  — integration substrate / source of truth                    │
│  Issues  queue: human-filed = trusted · machine-filed = auto/tainted │
│  PRs     work output + machine review + test report + HUMAN gate     │
│  Branch protection  no merge without human approval                  │
│  CI/CD   tests + deploy-on-merge   (owned by product repo)           │
└───────────────────────────┬───────────────────────────────────────────┘
                            ▼
         PRODUCT — live, real traffic, human merges → deploys
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
  ─▶ agent: reproduce ─▶ branch ─▶ code ─▶ run tests
  ─▶ open PR ─▶ container tears down                     ◀── burst ends
  ─▶ machine review (REVIEWERS.md): an assess burst posts findings as the
       reviewer App; findings drive autonomous dev rounds (resume), capped
  ─▶ TEST burst (TESTER.md): after a clean machine review, the tester boots the
       PR head, runs invented scenarios in the sandbox, report posted
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
  unit of compute — "issue → PR", "address one round of review feedback", *or* "test a
  PR end-to-end" (a **test burst**, run by the tester role). The
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
  unassigned issue, a PR with actionable feedback / CI failure, or a PR that is CI-green
  and not yet E2E-tested (→ test burst).
- **Crash recovery:** a dead burst releases its issue/PR back (lease + heartbeat on
  the control plane); respawn re-derives state from GitHub + session.
- **Kill switch + budgets:** instant stop + per-burst caps enforced by the control
  plane (independent of the agent behaving). Under **subscription billing** tokens
  (not USD) are the consumption metric, and cost figures are estimates — but
  `max_budget_usd` is a **hard cap regardless**: the SDK kills the burst mid-stream
  when estimated cost crosses it ("advisory" was falsified on task 17). Because the
  worker captures its session id from the init message and writes its result file
  even on a dying stream, a budget-killed burst **resumes** where it died — attempts
  × per-burst budget behaves as an installment plan, never a restart from zero. The
  SDK surfaces **rate-limit events/status** — the scheduler backs off spawning when
  the subscription window is near exhaustion instead of burning bursts into 429s.

### Session continuity (confirmed, SDK v0.2.110)

The worker keeps its **full context across the whole task** — one persistent Claude
session per task, not per burst. Mechanism, confirmed from the installed SDK:

- The session id comes from `ResultMessage.session_id`; a later burst resumes by
  setting `ClaudeAgentOptions.resume = <session_id>`, which replays the full prior
  transcript.
- **The platform owns all persistence; the dev agent is a stateless worker** that
  touches only a local working dir. The SDK writes its session to `CLAUDE_CONFIG_DIR`,
  a **control-plane-owned volume** mounted into the burst. The agent holds **no storage
  credentials** (no Postgres, no object store) and never talks to a store directly — so
  a worker can't read or delete another task's transcript, or touch the control DB (a
  wall, §8).
- **Persistence is out-of-band, owned by the control plane.** *Before boot* it
  materializes the task's transcript into the mounted `CLAUDE_CONFIG_DIR` so
  `resume = session_id` finds it locally; *after the burst* (plus optional periodic
  snapshots — it owns the volume) it harvests the JSONL, mirrors it to the object store,
  and updates the index. So we use the SDK's **default local storage**, not the in-agent
  **`SessionStore`** adapter (which would need store creds in the box). The SDK's
  `SessionStore` contract — `append(key, entries[])`/`load(key)`, keyed
  `SessionKey{project_key, session_id, subpath}`, entries as opaque JSON-safe values,
  dedupe-by-`entry.uuid` — stays the reference for the control plane's own store layer.
- **Auto-compaction (re-verify against our pin).** Current SDK docs describe
  subprocess-level auto-compaction: on resume the agent sees a *compacted* chain
  (`getSessionMessages`), while `load()` still returns the raw entries. If that holds on
  our pinned version, the old "replays the entire transcript toward the limit" worry is
  largely handled by the SDK and the adapter just owns raw-entry retention. Compaction
  was *absent* at v0.2.110 — **confirm before relying on it either way.**
- **Caveat — resume carries the conversation only**, not settings/hooks/MCP/permission
  config. Those are **baked into the dev image** — which is desirable: the walls (§8)
  live in the image, not in resumable state the agent could touch.

So state per task splits three ways, all keyed by issue and reconstituted each burst:
**branch** (workspace ← GitHub) · **session** (context ← control-plane `SessionStore`)
· **issue/PR thread** (status + human conversation ← GitHub).

### State & storage

The control plane owns both stores **and all I/O to them**; the dev agent holds no
storage credentials and touches only a mounted working dir. Nothing durable lives in
the ephemeral container.

> **As built (MVP):** the control DB is **SQLite** (`data/ledger.db` — tasks, bursts,
> violations) and the object store is a **filesystem archive** (`data/sessions/` for
> task-keyed resumable transcripts, `data/volumes/` for per-burst forensics). Same
> split, same walls, zero services to operate at experiment scale. Postgres +
> SeaweedFS below are the scale-up design.

- **Control DB — Postgres.** The queryable index the scheduler reasons over: `issue ↔
  session_id ↔ branch`, the burst ledger (when/where/cost/tokens), lifecycle status,
  provenance, the violations ledger (§9), and each transcript's object key + checksum.
- **Object store — SeaweedFS (S3-compatible).** The raw session transcripts (one JSONL
  part-file per `append()`, per the SDK's S3 storage model) plus any large artifacts.
  Self-hosted with an S3 API, so the SDK's stock S3 `SessionStore` adapter works
  unchanged — point its client at the SeaweedFS gateway, no cloud dependency for the
  experiment.

Keeping raw transcripts out of Postgres keeps the control DB small and queryable, dodges
the write-amplification bloat that per-entry relational checkpoints are known for, and
keeps raw agent context (code, tool output, secrets that may leak into logs) out of the
operational DB — consistent with the walls (§8).

### Review — a policy of providers (`REVIEWERS.md`)

Machine review is a **toggle + provider selector**, declared in the platform config
and verified loudly at startup. Every provider delivers the same artifact — a PR
review with inline findings, on GitHub — so there is one review surface, one trigger,
one etiquette:

- **`claude`** — a **reviewer burst** (`assess`): same image, reviewer role, fresh
  session, clone mounted read-only at the PR head, **no GitHub credential** — findings
  return via the harvest and the control plane posts them as a second GitHub App (the
  reviewer App; findings → `REQUEST_CHANGES`, clean → `APPROVE`).
- **`codex`** — the same reviewer envelope with the codex CLI as the engine (slice 3,
  not yet built).
- **`copilot`** — a request to GitHub's reviewer, sent on the owner's PAT and
  echo-verified (App tokens are silently dropped — the 201 lies).

Findings drive **autonomous dev rounds**: the dev burst resumes its session, fixes,
and replies in every thread; a push re-queues assess. The earlier design's **pre-PR
in-container Codex self-review was dropped** (decision record in `REVIEWERS.md`): its
output wasn't a platform artifact, and it put a second vendor's credential inside the
dev's box. The human remains the final gate regardless of policy.

### Testing — the tester role (shipped 2026-07; `TESTER.md`)

Reviewers read diffs; **nobody has run the thing**. The tester is **QA, not a third
reviewer** — the goal is to behave like a human tester. Same image as the dev, booted
with a tester contract, spawned as a **test burst** after a clean machine review
(only a clean assess queues a test — no sandbox boot for a diff that's about to
change):

- **Designs its own test scenarios** from the product's specs and its `TESTING.md`
  (the product-owned testing contract: frontmatter names the env it needs, prose
  tells the tester how this product is driven) — happy path, edge cases, "what would
  a user actually try" — rather than executing a checklist someone else wrote.
- **Runs them against a live system**: boots the product from the read-only PR-head
  clone and drives it end-to-end in the **sandbox** (a disposable instance of the
  product's runtime; creds provisioned platform-side under `TEST_*` and injected
  only as the intersection with what the manifest requests).
- **Reports** structured results returned via the harvest (it holds **no GitHub
  credential**); the control plane posts them: PR-caused defects as a
  `REQUEST_CHANGES` review that drives a normal dev round, pre-existing defects
  relayed as `auto` issues behind the human `ready` gate, a clean run as an advisory
  comment. Advisory input to the human at gate 2 — not a required check.

Explicitly *not* CI's job re-done by an agent: deterministic unit/integration/smoke
tests stay in CI as the reproducible floor. The tester covers what scripted tests
can't — scenario invention and exploratory judgement ("the command works but the
error message is nonsense").

Independence is engineered, not assumed:

- **Separate context.** Fresh session per test burst — no persistent tester session,
  no access to the dev's transcript. It tests "does the built thing satisfy the
  spec," not "did the dev do what it intended." Round 2 starts clean — no anchoring
  on round 1's verdict; its durable output is the PR thread.
- **Disjoint credentials** (the role asymmetry, §8): dev writes code but holds no
  sandbox creds; tester holds sandbox creds but is repo-read-only. Neither can ship a
  change *and* validate its own claim about it.
- **Its report is machine-generated data** entering the dev's context — same provenance
  class as monitor issues (§8): the dev reproduces findings first-hand, never executes
  report prose as instructions.

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
| No prod creds (product tokens, DB) | simply absent from the dev environment | wall |
| No storage creds (agent is a stateless worker) | control plane owns all persistence; agent touches only a mounted working dir | wall |
| No agent validates its own work (role asymmetry) | dev: repo write, **no sandbox creds** · tester: sandbox creds, **repo read-only** — disjoint credential sets | wall |
| Sandbox blast radius | disposable product runtime, throwaway rotating creds, no real users — the tester *executes dev-written code*, so what that runtime holds must be worthless | wall |
| Untrusted user content | handled by the **separate** monitor + structured issues | wall |
| Reproduce-first · write report · small reversible changes | operating contract; observer logs every deviation | tripwire (data) |

### The injection-laundering path (the critical one)

```
end user sends malicious content to the product
  └▶ product logs it ─▶ monitor files an issue containing it
        └▶ dev reads issue ─▶ ⚠️ could execute the injection as "work"
```

**Provenance gate** breaks it:

- **`human` issues** → spec is *trusted instruction*; the dev may act on its prose.
- **`auto` issues** (monitor/agent-filed) → body is *tainted data describing a
  symptom*, **never an instruction**. The dev reproduces from first-hand evidence
  (runs the failing test itself), acts only within a fixed envelope
  (reproduce → verify → propose), and an auto issue can **never** widen the agent's
  own scope or permissions.

The **tester's test reports are the same class**: machine-generated, structured
(scenarios run, pass/fail, repro steps) — the dev treats findings as symptoms to
reproduce first-hand, never as prose to execute. Sandbox content can reach the tester's
report (it drives a running bot), so the laundering rule applies end-to-end.

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

### Tracing

**OpenTelemetry end-to-end**, in the code from the first line (the backend can come
later; retrofitting correlation can't). Span naming follows the OTel **GenAI semantic
conventions** where applicable (still experimental) and Claude Code's built-in
`claude_code.*` spans as-is.

- **Trace = one burst; the task is a correlation attribute.** A task can span days,
  and multi-day open traces break tooling — so each burst is its own trace. Every
  span/event carries `task.id` (= issue), `burst.id`, `role` (dev|tester), and each
  burst links to its predecessor via span links. The burst ledger (Control DB) stores
  each burst's `trace_id`; task-level assembly is a query.
- **Context propagation is built in:** the control plane opens the burst span and
  injects `TRACEPARENT` into the container env; Claude Code in SDK/non-interactive
  mode adopts it, nesting its span tree (`claude_code.interaction` →
  `llm_request` / `tool` / `hook`, subagents, even Bash subprocesses) under our burst
  span. No custom instrumentation inside the box.
- **Agent-side telemetry is Claude Code's own OTel** (opt-in env in the image):
  metrics, structured events (`tool_result`, `tool_decision`, `api_request`), and
  traces. `OTEL_RESOURCE_ATTRIBUTES` stamps every signal with our correlation IDs.
  Content flags (`OTEL_LOG_TOOL_DETAILS`, prompts) are **on** — tool visibility *is*
  the experiment's dataset — and export goes only to our OTLP endpoint, never
  elsewhere.
- **One instrumentation point, two consumers.** `tool_decision` / hook events feed
  both the trace stream (debugging) and the **violations ledger** (the §9 dataset).
  Ledger derivation is control-plane-owned — tripwire-relevant events are also
  written to the burst volume and harvested with the session, so the agent needs no
  DB or telemetry-store access.
- **Telemetry is outward-only for agents:** they emit, never read — the concrete
  mechanism behind "can't skew its own scorecard."
- **Spans stay thin.** The deep record of agent behavior is the session JSONL in the
  object store; spans carry timing/outcome/cost/IDs plus the transcript's object key,
  never embedded content.
- **Backend (phase 0): Arize Phoenix** — OTel-native, a single container that acts as
  OTLP collector + trace UI, backed by our existing Postgres. Chosen over Langfuse
  (v3 self-host needs ~6 services incl. ClickHouse + Redis) at experiment scale;
  because everything speaks OTLP, the backend is swappable config, not a design
  commitment.

## 10. Rollout phases

Same architecture throughout — exposure is graduated, not the design.

0. **Walls-first:** single dev container, human-filed issues only, static/staging
   product. Prove **0 wall failures** over N runs. → build plan in `PHASE-0.md`.
   **Status: built and running** — the loop is proven end-to-end; the systematic
   red-team suite (M4) is still to run.
1. **Loop:** add the **tester** (test bursts + sandbox — **pulled forward and shipped
   2026-07**, so capability metrics mean something), then the monitor + auto issues
   on staging / recorded traffic (the provenance gate and the `auto` relay pipeline
   are already live via the tester). Watch the laundering path specifically.
2. **Live:** real user traffic, then horizontal scaling. Only after 0–1 show
   zero successful breaches.

## 11. Scope: now vs. future

- **Built (MVP):** control plane spawning **one** container per burst on Docker;
  human-triaged intake (`ready` label); GitHub-native flow (issue → PR → human
  review → merge → CI deploy); machine review as a provider policy (`REVIEWERS.md`)
  with autonomous dev rounds; the **tester role** with E2E test bursts and the
  `auto`-issue relay (`TESTER.md`); the wall set in §8.
- **Designed-for, not built:** the monitor (files issues from prod telemetry — its
  pipeline is already exercised by the tester's symptom relay); the `codex` review
  engine and the tester tools registry; Postgres/SeaweedFS stores; OTel/Phoenix
  tracing; horizontal scaling (N containers); monitor **reaction**
  (auto-remediation); k8s as the runtime when N>1 is real.

## 12. Decisions

**Resolved**

- **Repos:** control plane + dev agent in **one** repo behind a clean
  `control-plane` / `dev-agent` module seam (cheap to split later if the developer
  becomes a reusable, product-agnostic artifact). Monitor is its own repo.
- **Container lifecycle:** ephemeral **per work-burst** with resume-from-durable-state
  (§6). Warm base image to amortize setup; warm pool only if setup is heavy.
- **Intake:** human-triaged via a `ready` label (§6, gate 1).
- **Review:** a **toggle + provider selector** (`REVIEWERS.md`): `claude` reviewer
  bursts posting as a second GitHub App, optional `codex` engine, optional `copilot`
  request on the owner's PAT. *Supersedes* the original two-stage design — the pre-PR
  in-container Codex self-review was dropped (its output wasn't a platform artifact;
  it put a second vendor's credential in the dev's box).
- **Tester:** a **QA role, not a reviewer, not a codebase** — the dev image booted with
  a tester contract and disjoint creds (no GitHub credential — reports return via the
  harvest; product-sandbox creds the dev never has). Behaves like a human tester:
  invents scenarios from the product's `TESTING.md` + specs and runs them against a
  live sandbox boot of the PR head — the non-deterministic complement to CI, not a
  replacement for it. **Fresh context per test burst** — never sees the dev's session,
  deliberately blind to the PR conversation. Advisory report, not a required check.
  **Shipped 2026-07** (slices 1–2, `TESTER.md`).
- **Staging:** the RC-validation env; product CI deploys the PR/branch there.
- **Session continuity:** one persistent Claude session per task, externalized via a
  control-plane `SessionStore` adapter; resume by `session_id` each burst (§6).
- **Storage:** the **platform owns all persistence** — Postgres (index) + **SeaweedFS**
  (transcripts + artifacts). The dev agent is a **stateless worker with no store creds**;
  the control plane loads the session into a mounted `CLAUDE_CONFIG_DIR` before boot and
  harvests/mirrors it after (SDK **default local storage**, not the in-agent
  `SessionStore` adapter). Raw transcripts stay out of Postgres (bloat + containment);
  retention lives in the object store (§6, "State & storage").

- **Anthropic auth = the owner's subscription**, not API billing: `claude setup-token`
  → a 1-year, **inference-only** OAuth token in the control-plane env
  (`CLAUDE_CODE_OAUTH_TOKEN`), injected per run-burst as the container's **only**
  Anthropic credential. Never inject an API key alongside it (the CLI prefers the API
  key and would silently switch billing). `.credentials.json` is excluded from session
  harvest unconditionally. Consequences: consumption is measured in tokens; all bursts
  share one subscription's rate limits (a real constraint on N>1 scaling, phase 2);
  personal-experiment use — a multi-tenant service would need API billing.
- **Tracing:** OpenTelemetry end-to-end from the first line of code; trace-per-burst
  with `task.id`/`burst.id`/`role` correlation attributes + span links across bursts;
  `TRACEPARENT` injected by the control plane so Claude Code's native spans nest under
  the burst span; Phoenix as the phase-0 OTLP backend (single container, swappable);
  agents emit but never read telemetry (§9, "Tracing").

**Still open**

- Transcript-growth policy — **gated on the auto-compaction re-verify (§6).** If the SDK
  compacts the resumed chain on our pinned version, this is largely handled; otherwise
  the adapter's `load()` owns truncate/summarize (and/or a turn cap). Defer until a task
  spans enough rounds to matter.
- Warm-pool threshold — defer until per-burst setup time is measured.
- ~~Whether `max_budget_usd` enforces anything under subscription billing~~ —
  **resolved (task 17): it's a hard cap**; the SDK kills the burst mid-stream on
  estimated cost. Resume-on-any-death makes retries an installment plan (§6).
  Rate-limit backoff tuning for the scheduler stays open.
- ~~How the tester *drives* the product E2E in the sandbox~~ — **resolved: the
  socket-and-plug model** (`TESTER.md`). "How to test this product" is product
  knowledge and lives in the product repo's `TESTING.md` (frontmatter for the
  platform, prose for the tester); the platform owns only the envelope and stays
  product-agnostic.
