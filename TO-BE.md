# Autonomous Maintenance Platform — TO-BE architecture

Target architecture for the platform as an **internal Fermata product**: an org-wide
service that maintains **dozens of repos** on behalf of their owning teams. This
document is the design target; `ARCHITECTURE.md` + the subsystem docs describe the
proven MVP it grows from. Nothing here is speculative capability — every mechanism
generalizes something the MVP already proved end-to-end (discord-bot task 17: issue →
work bursts → autonomous review rounds → live E2E test → human merge).

> Status: **draft for team review** (2026-07). Decisions marked *(DR)* are proposed
> decision records; open questions are collected in §15.

---

## 1. What changes and what doesn't

**The MVP proved the shape.** These invariants carry over untouched:

- **GitHub is the only work substrate.** Issues = queue, PRs = delivery + review,
  branch protection = the ship gate. The platform never grows its own task tracker,
  and the console (§8) never becomes a GitHub replacement.
- **Two human gates bracket an autonomous middle** — `ready` (intake) and merge
  (ship). Teams keep owning both, in GitHub, exactly as today.
- **Subscription-powered, not API-billed** *(DR)*. The whole experiment runs on
  Claude subscriptions and the TO-BE keeps that: every team member has one, and the
  platform's compute is the team's **pooled subscription capacity** (§4). The open
  design axis is the *mechanism* (token pool vs member-hosted runners), never the
  billing model.
- **Bursts are the unit of compute**: ephemeral container + durable session, one
  container per burst, resume-on-any-death (installment-plan budgeting).
- **Provenance gate + laundering defense**: `human` = trusted spec, `auto` = tainted
  symptom data; evidence blocks untrusted always.
- **Socket and plug**: product knowledge lives in the product repo (`CLAUDE.md`,
  `TESTING.md`); the platform owns only envelopes and stays product-agnostic. This is
  what makes "dozens of repos" a registration problem, not an integration project.
- **Walls before tripwires**; agents hold no storage credentials; labels reflect,
  the ledger locks.
- **Config taste**: minimal orthogonal controls — toggles and selectors, no tiers,
  no modes, no per-product platform code.

**The MVP also measured its own ceilings.** The TO-BE exists to remove exactly these:

| MVP ceiling | Consequence at org scale | TO-BE answer |
|---|---|---|
| Serial loop, one burst globally | a day of wall-clock per task, mostly queueing | N runners, per-task serialization (§6) |
| Laptop process | no availability story, no webhooks | control plane on k8s (§5) |
| SQLite + filesystem | single-operator stores | Postgres + S3 object store (§5) |
| One owner's subscription | one person's rate limit = org throughput | the team subscription pool (§4) |
| Ledger surgery as operator UX | SQL against prod state (finding #9) | operator API + live console with audit (§8) |
| Memory dies with the task | every burst relearns the product's gotchas | platform memory (§9) |
| Review is sampling (finding #7) | ~1.6 of ~7 defects found per round; QA loop ≈ 85 % of task cost | parallel lens-scoped review + verify round (§10) |
| Nobody watches prod (finding #11) | integration breakage waits for a human to notice | the monitor, per-product (§11) |

## 2. Product model — the tenancy unit

Single-tenant platform (one Fermata org), **multi-product**. A *product* is a
registered entity in the control plane:

```
product:
  repo            github.com/fermata/<name>
  team            owning team (gate authority, Slack channel)
  contracts       CLAUDE.md · TESTING.md — live in the repo, never here
  review policy   toggle + provider selector (per product, as today)
  e2e             on|off + TEST_* sandbox credentials (per product)
  quotas          concurrency (default 1) · daily bursts · usage share
  state           active | paused
```

Registration is an operator action (console/CLI): install the org Apps on the repo,
declare quotas, provision sandbox creds. **No platform code changes per product** —
if a product needs platform code, the socket-and-plug boundary was violated and the
design review should catch it.

Teams own their products' gates and PRs through GitHub, exactly like the MVP's owner.
The platform team owns the service: quotas, capacity, incidents, the fleet view.

## 3. Components

| Component | Runs as | Identity | New vs MVP |
|---|---|---|---|
| **Control plane** | k8s Deployment (leader-elected) | org GitHub Apps (mints per-repo tokens) | deployed service; subscription-pool scheduler |
| **Webhook receiver** | k8s Deployment behind the ingress — **the platform's only public surface** | App webhook secrets (HMAC verify only) — no other credential | **new** (§5) |
| **Burst runners** | k8s **Jobs** in a microVM runtime (§6 DR: Kata / e2b / gVisor) — or, as a *deployment variant*, the same runner on a member's machine (§4, mechanism B) | dev App token (dev role only) · none (reviewer/tester) | hardware-virtualized isolation; same walls |
| **Console** | web GUI + the existing CLI | Fermata SSO; roles: owner / operator / viewer | **new**; live fleet state + streaming burst logs |
| **Monitor** | k8s Deployment, per-product watchers | monitor App (`issues:write` only) | **new** (pipeline already proven by the tester relay) |
| **Ledger** | Postgres (managed) | — | replaces SQLite behind the existing `Ledger` protocol |
| **Session archive** | S3-compatible object store | — | replaces the filesystem behind the existing `SessionArchive` protocol |
| **Platform memory** | Postgres + object store (§9) | written only via harvest; read only via prompt assembly | **new — core subsystem** |
| **Observability** | OTel collector + backend (OTLP-swappable) | agents emit, never read | wired for real (was designed-only) |

## 4. Identities & the subscription pool *(DR)*

- **GitHub: four org-level Apps, one per role** — `fermata-ai-dev`,
  `fermata-ai-reviewer`, `fermata-ai-tester`, `fermata-ai-monitor` — installed
  org-wide, **tokens minted per-burst and scoped to the single target repo**. This
  replaces per-product App pairs (2 Apps × N products → 4 Apps total) and finally
  gives the tester its own provenance (deferred in the MVP, where it rode the
  reviewer App). Scopes as in `GITHUB-INTERFACE.md`; the dev App still cannot merge;
  no App satisfies the CODEOWNERS gate.

- **Anthropic: the team subscription pool.** Every team member has a Claude
  subscription; the platform runs on that pooled capacity. Two mechanisms, not
  mutually exclusive:

  **A — registered token pool (the backbone).** A member opts in with
  `claude setup-token` (a 1-year, **inference-only** OAuth token) and registers it
  under their name with a **usage cap** ("the platform may use up to X of my
  window"). Tokens live in the secret manager; each burst is stamped with whose
  subscription powered it. The scheduler is **rate-limit-aware per token**: the SDK
  surfaces rate-limit status/events, the pool tracks each token's window, picks the
  least-loaded eligible token, and backs off a token near exhaustion instead of
  burning bursts into 429s. Revocation and cap changes are one console action;
  contribution is always visible to the contributor ("your subscription ran N
  bursts / M tokens this week").

  **B — member-hosted runners (the extension).** Not a separate component — the
  **same burst runner, deployed on a member's own machine** (a deployment-diagram
  concern, not a container): the daemon picks up bursts assigned to *their*
  subscription and executes them locally (Docker); the OAuth token **never leaves
  their device**. Strongest personal-credential story and free compute, at the cost
  of availability variance (laptops sleep) and heterogeneous environments. The
  dispatch protocol treats a member-hosted runner exactly like a k8s runner that
  only accepts its owner's bursts; harvest ships results to the platform stores
  over a per-burst scoped channel.

  Consequences, either mechanism: **consumption is measured in tokens, not USD** —
  accounting (§8) reports usage per subscription, per task, per product;
  `max_budget_usd` keeps its proven role as the runaway hard-stop (the SDK kills on
  estimated cost regardless of billing model — falsified into shape on task 17);
  org throughput = the sum of contributed windows, so capacity planning is a
  people-question ("who opts in") rather than a procurement one.

- **Humans: Fermata SSO** on the console. Three roles, deliberately few: **owner**
  (their products: gates visibility, force-test, re-ready), **operator** (fleet:
  quotas, pause, overrides, capacity), **viewer**. Every mutating console/CLI action
  lands in an **audit log** (the ledger-surgery lesson: the override path must be
  first-class and attributable, finding #9).

- **Secrets**: platform credentials, registered subscription tokens, and
  per-product `TEST_*` sets live in the org's secret manager (Vault/External
  Secrets); injected at spawn, never in images, never in the archive (the MVP's
  unconditional credential-exclusion rule survives).

## 5. Control plane as a service

The **reconciler model survives unchanged** — observe GitHub, derive events, advance
per-task state machines, run bursts. What changes is where and how much at once.

- **Deployment**: k8s, leader-elected single active reconciler (the state machine
  stays simple); horizontal scale comes from runners, not schedulers.
- **Stores**: Postgres implements the existing `Ledger` protocol (tasks, bursts,
  violations — plus products, quotas, subscriptions, memory index, audit); the
  object store implements `SessionArchive` (task-keyed transcript blobs,
  checksummed). The MVP's protocols were designed for exactly this swap; callers
  don't change.
- **Webhooks as wake-up, polls as truth** — the MVP's standing decision activates
  now that a receiver is deployable. Mechanics *(DR)*:
  - **Delivery rides the org Apps.** A GitHub App carries its own webhook: every
    repo an App is installed on delivers `issues`, `pull_request`,
    `pull_request_review`, and `push` events to the App's webhook URL
    automatically. Registering product #30 configures **zero** webhooks — the App
    installation *is* the subscription.
  - **A dedicated receiver, deliberately dumb.** The platform's **only public
    surface** is a small stateless deployment behind the ingress that does exactly
    three things: verify the HMAC signature against the App's webhook secret,
    dedup by delivery id, and enqueue a *wake event* (`repo`, `kind`) for the
    reconciler. It holds no other credential, writes nothing but wake events, and
    shares no process with the scheduler — the MVP skipped webhooks precisely
    because a public endpoint next to the App private key was a bad trade on a
    laptop; the receiver is how that objection is retired.
  - **Forgery is harmless by construction.** Because polls remain the only source
    of truth, the worst a forged/replayed webhook can cause is one extra poll of
    real GitHub state — never a state transition. The wake path needs availability,
    not trust; missed deliveries cost latency, never correctness.
- **Scheduling** *(DR)*: per-task serialization is a hard invariant (one running
  burst per task — sessions are single-writer). Across tasks: **weighted fair share
  across products** with per-product concurrency caps (default 1) under a global
  runner ceiling — now intersected with **token availability** from the
  subscription pool (a burst needs both a runner slot and an eligible token). No
  priority knobs until fair-share demonstrably fails (config-taste: add controls
  when a real scheduling failure demands them).
- **Rails, now three-level**: per-burst (hard budget cap, max_turns, wall-clock —
  plus a default **per-tool-call timeout** inside the burst; an 18-minute hung Bash
  call burned a round in the MVP, finding #6) · per-task (attempts, review rounds —
  the installment plan) · per-product (daily bursts, usage share) · per-subscription
  (the member's cap) · org ceiling. All `.env`-shaped defaults, overridable per
  product at registration.
- **Kill switches**: burst, task, product (pause), and org STOP — all through the
  operator API with audit, all converging on the same mechanics the MVP proved
  (mark killed, roll back task state, re-derive on resume).

## 6. Burst execution

- **k8s Jobs** replace local Docker: one Job per burst, `activeDeadlineSeconds` =
  the wall-clock rail, no retries at the k8s layer (retries are the scheduler's
  decision, informed by the result file).
- **Isolation runtime** *(DR — bake-off in wave T1)*: bursts execute arbitrary
  code an agent wrote seconds earlier; container-grade isolation is the floor, not
  the target. Candidates:
  - **Kata Containers** — microVMs as a k8s `RuntimeClass`: OCI-compatible, so the
    Jobs design, warm images, and volume choreography carry over unchanged. The
    conservative default.
  - **e2b/infra** — Firecracker-based sandbox infrastructure purpose-built for AI
    agents; attractive ergonomics (snapshot/restore could make warm boots instant),
    but its own orchestration next to k8s.
  - **gVisor** — syscall-interception; lighter than microVMs, weaker isolation
    story. The fallback if microVM overhead hurts.
  Bake-off criteria: boot latency on warm images · bind-mount support for the
  session volume choreography · egress policy enforcement · snapshot/restore ·
  ops burden on our cluster. Decision wanted before T1 exit; the Jobs abstraction
  isolates the choice.
- **Session choreography unchanged**: the control plane materializes the task's
  transcript from the archive into the Job's volume before boot, harvests
  result + violations + session (+ memory proposals, §9) after; reviewer/tester
  volumes are never seeded and never archived. `burst-result.json` written in
  `finally` + session id captured from the init message — resume survives any
  death, including budget kills.
- **Warm images per product family**: a base agent image + product toolchain layers
  (uv/python, node, …), built by the platform's CI, pinned per product. A burst is
  `git fetch/checkout` + agent boot, as designed in the MVP.
- **Network walls**: no k8s API access, NetworkPolicy egress allowlist — GitHub,
  the Anthropic API, and (tester bursts only) the product's declared sandbox
  endpoints. The MVP's walls carry over; the network wall gets real enforcement
  instead of trust-by-construction.
- **The clone** becomes a per-runner cache keyed by repo, synced before every mount —
  same invariant ("whatever a burst leaves behind is erased before the next look"),
  now safe under concurrency because a task's bursts are serialized and caches are
  per-runner.

## 7. Task lifecycle at scale

Unchanged from `SCHEDULER.md` — the state machine, the funnel
(work → PR → assess → dev rounds → clean assess → test → human gate), soft/hard
round caps, defect routing (PR-caused → `REQUEST_CHANGES`; pre-existing → relayed
`auto` issues behind the `ready` gate). The TO-BE adds only *where it runs* (Jobs)
and *how many at once* (fair share × token availability). Multi-repo means the
funnel runs concurrently across products, never concurrently within a task.

## 8. Human surfaces — the console *(GUI, live)*

GitHub stays the surface for **work** (issues, PRs, reviews, gates). The console is
the surface for **operations** — a web GUI whose defining property is **liveness**:
it shows the platform *now*, not a report about it.

- **Live fleet view**: products × task states streaming as they change (SSE/WS over
  the ledger's event feed); what's queued, working, awaiting whom; per-product
  health (rails tripped, assess failures, test skips); pool status (which
  subscriptions are contributing, window headroom per token).
- **Live burst view**: a running burst's structured log stream in real time (the
  structlog events are already shaped for this), current tool call, elapsed
  time per step, cost ticking against the cap. (This page is the productization of
  every "check artifacts / why is it slow" investigation from the MVP cycle —
  including catching the next 18-minute hung tool call *while it hangs*.)
- **Task timeline**: bursts with durations, per-step timing, cost, session lineage,
  links to PR/issue; the full history that today lives in tarballs.
- **Usage accounting**: tokens per burst → task → product → subscription → week;
  contribution view per member; the ~85 % QA-iteration cost structure becomes a
  visible, optimizable number per product.
- **Violations & containment feed**: the tripwire dataset as a first-class live
  page — the experiment's primary dataset becomes the platform's security
  telemetry.
- **Memory browser** (§9): per-product memory entries with provenance — inspect,
  edit, retire; owner-gated for their product.
- **Operator actions, audited**: stage override (force test / re-assess — finding
  #9 as porcelain), reset rails, re-`ready`, pause product, kill burst/task, org
  STOP, pool management (register/cap/revoke tokens). Every action = who/when/why
  in the audit log.
- **Notifications: Slack** per owning team — the ball-is-with-you nudges (PR
  awaiting owner review, task blocked, budget threshold, monitor filed an issue).
  Deep links into GitHub/console; never actionable from Slack (gates live in
  GitHub, full stop).
- The **CLI keeps parity** for operators; the console adds visibility, not a second
  control path with different semantics.

## 9. Platform memory *(new core subsystem)*

A long-running platform must not relearn its products from zero every burst. The
MVP already has three memory layers in embryo — the TO-BE makes them explicit:

| Tier | Scope | Lives in | MVP ancestor |
|---|---|---|---|
| **Session** | one task | session archive | proven resume — untouched |
| **Product memory** | cross-task, per product | platform memory store | living docs + the acknowledged-defects idea (#10) |
| **Platform memory** | cross-product, operational | platform memory store | the human-curated retrospective (findings #6–#11) |

- **An entry is a datum, not an instruction**: `{claim, evidence, scope, provenance
  (task/burst/date), status}`. Memory is auto-class content under the provenance
  gate — injected as *data the agent may verify*, never as contract. The contract
  channel for durable product knowledge remains the repo's living docs
  (human-reviewable, versioned); memory holds what doesn't belong in a contract:
  operational patterns, failure history, environment quirks ("YouTube bot-walls
  this sandbox's egress IP — use a direct audio URL for the voice path and report
  the wall"), review heuristics ("watch-URL-with-list-param was a real defect
  here").
- **Write path — through the harvest, like everything else.** The burst result
  contract gains a `memories` field: the agent *proposes* entries; the control
  plane stores them with provenance. Nothing writes to memory from inside the box,
  and no proposal is auto-trusted into high-weight recall.
- **Read path — through prompt assembly.** At spawn, the control plane injects a
  size-capped, scope-filtered selection (product memory for that product; role
  memory for that role) into the burst prompt, explicitly framed as recalled data.
  Dev bursts get product gotchas; **reviewer lenses get the product's defect
  history** (memory is what turns finding #7's parallel lenses from generic into
  product-tuned); the tester gets environment knowledge that today has to be
  re-discovered per run.
- **Curation — memory rots.** Entries age; recall-verify applies (an entry naming
  a file/flag is checked against the repo head before high-weight injection where
  cheap); a periodic **gardener pass** consolidates duplicates and retires
  contradicted entries; the console's memory browser gives humans direct
  edit/retire. Provenance makes every entry traceable to the burst that proposed
  it.
- **Containment**: the memory store is platform-owned (agents hold no store
  creds); scope-filtering means product A's bursts never see product B's memory;
  injected memory is auto-class data, so the laundering defense covers a poisoned
  memory entry exactly as it covers a poisoned issue body.

## 10. Review & QA at scale *(roadmap wave 3)*

- **Parallel lens-scoped review** (finding #7): on each assess trigger, fan out
  reviewer bursts by lens — correctness, security, tests/evidence,
  spec-compliance — dedup findings, post as **one** review by the reviewer App.
  Serial rounds proved to be sampling (~1.6 of ~7 present defects per pass); lenses
  attack coverage, and the round machinery (cursor, caps, threads) is unchanged —
  it just receives better rounds. A **verify round** after fixes stays irreducible
  (fix-induced defects were real in the MVP). Lenses read product memory (§9).
- **Tester tools registry** (TESTER.md slice 3): `playwright` and friends baked in
  the image, enabled per product manifest. The plug declares needs; the platform
  ships capabilities.
- **Acknowledged-defects channel** (finding #10) folds into product memory:
  human-labeled known-defect issues become memory entries, so known pre-existing
  failures stop re-headlining every report — sourced only from human-labeled
  issues, never PR text (no laundering).
- **Feasibility discipline** (the yt-dlp lesson) is already contractual — spec-time
  feasibility section, dev live round-trip rule, tester defect-grade headline —
  and ships with each product's contracts, not the platform.

## 11. The monitor *(roadmap wave 4)*

As designed in `ARCHITECTURE.md` §7, now with a proven receiving pipeline (the
tester's symptom relay exercised `auto` end-to-end): per-product watchers over the
org's existing telemetry (logs/metrics/Alertmanager), filing structured `auto`
issues behind the `ready` gate, rate-limited, dedup-capped. Operational-only signals
stay in the alerting plane. First detector class: **external-integration breakage**
(the arms-race feed — yt-dlp-style dependencies break on the vendor's schedule, and
finding #11 showed that's recurring maintenance demand, i.e. the platform's business
case). Monitor findings that repeat become product memory (§9).

## 12. Observability

OTel end-to-end as designed: trace-per-burst, `task.id`/`burst.id`/`role`
attributes, span links across a task's bursts, `TRACEPARENT` into the Job so the
agent's native spans nest. Backend: whatever the org runs (OTLP keeps it
swappable); Phoenix remains the dev-scale default. structlog logfmt feeds both the
org's log aggregation and the console's live streams. The §9-of-ARCHITECTURE metric
families (reliability / containment / capability) become standing dashboards;
violations alert.

## 13. Containment at org scale

Everything in `ARCHITECTURE.md` §8 survives. New surface, new walls:

| Boundary | Mechanism | Type |
|---|---|---|
| Burst escapes its box | microVM isolation (§6 DR) — hardware virtualization, not just namespaces | wall |
| The public webhook endpoint | receiver verifies HMAC + dedups, holds no creds, emits wake events only — a forged webhook can at worst trigger one poll of real state | wall |
| Burst can't touch the cluster | no service account, no k8s API, NetworkPolicy egress allowlist | wall |
| Burst can't reach another product | per-repo-scoped App token minted per burst; sandbox creds injected per product; memory scope-filtered | wall |
| A member's subscription is protected | per-token usage cap + one-click revoke + full attribution; token B never leaves its owner's machine (mechanism B) | wall |
| Memory poisoning | write only via harvest with provenance; injected as auto-class data; gardener + human browser | tripwire (data) |
| Operator actions are attributable | audit log on every mutating API call | tripwire (data) |
| Console can't bypass gates | console holds no merge identity; gates only in GitHub | wall |
| Secrets never persist | secret-manager injection at spawn; archive exclusion unconditional | wall |
| Platform CI red-team | the M4 suite runs as a scheduled job against a canary product | tripwire (data) |

## 14. Roadmap — AS-IS → TO-BE

Waves, not phases (rollout phases 0–2 in `ARCHITECTURE.md` stay the *exposure*
story; waves are the *build* story). Each wave ships alone and is useful alone.

- **T0 — the service** *(ops hardening)*: Postgres + object store behind the
  existing protocols; control plane containerized on k8s; the webhook receiver
  (App-delivered events, HMAC-verified, wake-only); org Apps (4 roles, per-repo
  token scoping); secrets via the org manager; **subscription pool v1** (token
  registry, per-token caps, rate-limit-aware pick, attribution).
  Exit: the laptop is retired; two products registered; MVP behavior byte-for-byte
  on pooled tokens.
- **T1 — concurrency + isolation**: k8s Jobs runners; **isolation runtime bake-off
  decided** (Kata / e2b / gVisor) and adopted; per-task serialization + weighted
  fair share × token availability + quotas; per-tool-call timeout; clone caches;
  **memory store + write path** (harvest-side, entries accumulate with provenance).
  Exit: 5+ products active, ≥5 concurrent bursts, zero cross-task interference,
  bursts running under the chosen runtime.
- **T2 — human surfaces**: the live console (fleet, live burst logs, task
  timelines, usage accounting, violations, memory browser, pool management),
  operator actions with audit, Slack notifications, CLI parity; **memory read
  path** for tester + reviewer prompts. Exit: an operator who didn't build the
  platform runs it for a week without SQL; a hung tool call is spotted live.
- **T3 — review & QA depth**: parallel lens review + verify round (memory-tuned);
  tester tools registry; acknowledged-defects → memory; WIP-branch push rule
  (finding #8); member-hosted runners (mechanism B) if the pool wants the
  capacity. Exit: measured defect yield per round beats the MVP's ~1.6 on the same
  benchmark class.
- **T4 — the monitor**: integration-breakage detectors on 2–3 products, `auto`
  issues flowing behind the gate, repeat findings feeding memory. Exit: first
  monitor-originated fix merged by a human gate, no human having noticed the
  breakage first.

## 15. Open questions (for team review)

- **Subscription pooling posture**: mechanism A moves a member's inference-only
  token into the org secret manager — acceptable to everyone, or do some members
  want mechanism B (token never leaves their machine) from day one? Consent UX
  matters more than the tech here.
- **Token lifecycle ops**: `setup-token` credentials live 1 year — renewal
  reminders, expiry handling mid-task, revocation drills.
- **Isolation bake-off owner**: who runs the Kata vs e2b vs gVisor evaluation, and
  on which cluster.
- **Object store**: SeaweedFS self-hosted vs the org's existing S3-compatible
  storage — pure ops preference, the protocol doesn't care.
- **Observability backend**: org standard (Grafana stack?) vs Phoenix — OTLP either
  way.
- **Transcript + memory retention**: transcripts contain code and tool output;
  retention and access policy is a security/compliance call (proposal: transcripts
  hot 90 days then delete; memory entries and the violations ledger retained —
  they're the platform's long-term value).
- **Console stack**: whatever the org's internal-tools default is; the console is
  deliberately thin (read models + event stream over the ledger, one operator API).
- **Which repos first**: two low-stakes-but-real products for T0; candidates are a
  team decision.
