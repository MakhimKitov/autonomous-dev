# Scheduler — M2, the control plane drives the loop

Turns the manual `control-plane burst` choreography into an unattended loop. The M2 exit
bar (`PHASE-0.md`): *drop a `ready` issue, walk away, a PR appears; killing mid-run is
clean.* Everything the loop runs — volume prepare, span, container, harvest — already
exists and is proven; M2 adds the **decision layer** around it.

## The reconciler model

The scheduler is a **reconciler**: each tick it observes GitHub, derives events, advances
a per-task state machine, and runs at most one burst. It holds no state the world can't
re-derive — GitHub owns task/delivery facts, the ledger owns run state (claims, attempts,
sessions), and a crashed scheduler restarts from both.

**Transport: polling now, webhooks later.** Webhook delivery is at-least-once and
best-effort — miss a delivery (endpoint down, redelivery window passed) and a
webhook-driven scheduler silently desyncs. So the reconciler must be able to derive
everything from a poll anyway; that makes webhooks a *latency optimization*, never the
source of truth ("poll for truth, push for speed"). Phase 0 runs on a local machine where
a webhook endpoint means a public HTTPS surface (tunnel/relay) next to the App private
key — cost without benefit at our scale: the loop is serial and a burst takes minutes,
so 60 s of poll lag is noise, and a tick's handful of API calls (~360/h) sits far under
the 5000/h App limit. When the control plane moves off the laptop, a webhook receiver
(issues, pull_request, pull_request_review, push) simply **wakes the reconciler early**;
the tick logic doesn't change. *Built at E5 — "Webhooks as wake-up" below.*

## Task state machine

Ledger-authoritative (`tasks.status`); labels stay reflective (`GITHUB-INTERFACE.md`).

| State | Meaning |
|---|---|
| `claimed` | ours; a burst is queued (`next_burst_kind`: `work`, `review`, or `assess` — a reviewer-role burst judging the open PR, REVIEWERS.md) |
| `working` | the burst container is running |
| `awaiting_review` | PR open; the ball is with Copilot / the owner |
| `blocked` | needs a human — rail tripped, agent rule 9, or PR closed unmerged |
| `done` | PR merged (issue auto-closed) — or the issue closed with no PR needed |

```
issue `human`+`ready` ──claim──► claimed ──spawn──► working ──burst ok, PR open──► awaiting_review
        (poll)                    ▲   ▲               │                            │ │ │
                                  │   │               │                            │ │ │
                 failed / no PR,  │   │               │                            │ │ │
                 attempts < max ──┘   │  changes requested (round < max) ──────────┘ │ │
                                      └──────────────────────────────────────────────┘ │
                                                                            merged ────┘
   blocked ◄── attempts/rounds = max · agent set `blocked` · PR closed unmerged        ▼
      └────────── human re-adds `ready` (attempts reset) ──────► claimed              done
                                                                                       ▲
                                    issue closed, no PR needed (already fixed) ────────┘
```

Every row of this table is written as a **compare-and-set** (E1): the ledger
moves only if the task still reads the (status, holder) the replica saw, and the
side effects run only if it moved — a replica that loses the race writes nothing
and posts nothing. `claimed → working` additionally names the replica's own
**lease**, taken by the claim statement that picked the task (below).

| From | Event (observed) | Side effects | To |
|---|---|---|---|
| — | issue `human`+`ready`, unclaimed | task row (insert-if-absent); labels `ready`→`in-progress` | `claimed` (work) |
| `claimed` | leased by the claim statement: cooldown passed, ceiling ok | the pool is asked who pays (F2); spawn burst (resume when a session exists); heartbeat renews the lease | `working` |
| `claimed` | leased, but no subscription is eligible | release the lease; `capacity_exhausted` event — **no attempt spent** | `claimed` |
| `working` | burst ok ∧ linked PR open | record `pr_number`; review round → re-request Copilot + owner | `awaiting_review` |
| `working` | burst ok ∧ no PR ∧ **issue closed** | clear `in-progress` | `done` |
| `working` | the session snapshot is gone or its bytes disagree | attempts+1; the parking comment names the key | `claimed` |
| `working` | the burst's token was rate limited | bench that subscription (`backoff_until`, CAS); release the lease — **no attempt spent** | `claimed` |
| `working` | burst failed ∨ no PR, attempts < max | attempts+1; failure cooldown | `claimed` |
| `working` | burst failed, attempts = max | comment on issue; clear `in-progress` | `blocked` |
| `working` | kill switch / crash | container killed → burst `killed`; attempts+1; roll back (E3: killed via the heartbeat and the runner — STOP row, lost lease or shutdown; a crash is `killed` by the reconciler) | pre-burst state |
| `awaiting_review` | unseen owner `changes_requested` **or machine findings**, rounds < max | rounds+1; queue review prompt | `claimed` (review) |
| `awaiting_review` | unseen owner `changes_requested`, rounds = max | comment; clear `in-progress` | `blocked` |
| `awaiting_review` | unseen machine findings, rounds = max | soft cap: advance cursor; comment | `awaiting_review` |
| `awaiting_review` | PR merged | clear `in-progress` | `done` |
| `awaiting_review` | PR closed unmerged | comment; clear `in-progress` | `blocked` |
| any active | issue carries `blocked` (agent rule 9) | stop scheduling the task | `blocked` |
| `claimed` | issue closed (a burst finished it, or a human) | clear `in-progress` | `done` |
| `blocked` | human re-adds `ready` | attempts/rounds reset | `claimed` |

Notes:
- **"Burst ok but no PR" is a failed attempt — unless the issue is closed.** The work
  burst's contract is a PR; anything less retries (with resume — the session remembers how
  far it got) until the rail trips. A *closed* issue is the exception: some work needs no
  PR — the symptom was already fixed, the report is a duplicate — and the burst says so by
  closing the issue with its evidence, which the product's contract allows. That is a
  finished task. The observation pass reads the same fact for a `claimed` task, so a human
  closing an issue mid-flight ends it too; an `awaiting_review` task is ended by its PR,
  never by its issue. (#93: without this, the gate rehearsal spent three bursts on
  discord-bot#16 re-reading a closed issue, then parked it as failed.)
  Resume survives *any* death, including the budget rail killing the stream mid-run: the
  worker captures the session id from the init message and writes its result file in a
  `finally` (the task-17 lesson), so attempts × per-burst budget behaves as an installment
  plan — each retry continues where the last died, never a restart from zero.
- **Whose turn it is, is a weight and not a clock** (H1). The fleet orders its
  products by *bursts served per unit of weight* over a day — the numerator an
  aggregate over `bursts`, the denominator the row's `usage_share` — so a product
  at 2.0 sees twice the bursts of one at 1.0 without anyone maintaining a
  counter. Equal weights make it least-served-first, and ties fall through to C4's
  order (least *recently* served, then the slug), so two products with the same
  history still alternate instead of one name always winning.
- **The pool is asked once for the fleet, before any product's turn** (H1). It is
  the platform's, not a product's, so an empty one is empty for everybody: the
  fleet asks, and when nothing is eligible it says so once and ends the tick with
  no task claimed, no lease moved and no turn spent. The single-product loop has
  no fleet, so it asks for itself — where an exhausted pool still costs one task
  its lease and nothing else.
- **The pool is asked before anything is spent.** Admission has a second question
  since F2: *who pays for this burst?* `pick_capacity` reads the contributed
  subscriptions and one rolling-window aggregate over `bursts`, and hands back the
  eligible one with the most headroom (active ∧ unexpired ∧ not backing off ∧
  headroom > 0), its token resolved from the member's ref at that moment. A ref
  that no longer resolves is the member's own revocation — the platform cannot
  override it — so that subscription is logged and skipped, never fallen back
  from. Nothing eligible is **not a failed attempt**: nothing was tried, the task
  keeps its attempts and its `claimed` status, the lease goes back, and the
  adopter hears `capacity_exhausted`. An empty pool is the machine's own
  credential, which is the MVP's shape and the single-owner laptop's. Two
  replicas can pick the same subscription in the same instant and both spend it:
  a burst's token count is not known until it ends, so headroom always reads
  *finished* work and the cap is a budget rail, not a wall — the wall is the
  provider's 429, which F3 turns into `backoff_until`.
- **An unusable session snapshot is a failed attempt, not a dead replica** (#87).
  The archive no longer holds the node the ledger named — pruned, offline, or
  bytes that disagree with the recorded checksum — and that used to leave an
  uncaught `ArchiveError` on the way out of `execute_burst`: the replica died,
  its peer reconciled the task, resolved the same leaf and died too. It is an
  attempt now, and the rail parks the task with the key named in the comment, so
  a human decides whether to restore the object or re-`ready` the issue. A
  mismatch and an absence are the same rail on purpose: they differ in what a
  reader should do, not in what the scheduler can do, and the message says which
  one happened. Neither is a `violations` row — that table is a burst crossing a
  wall, and this is the platform's own storage failing.
- **A spent token is not a failed attempt** (F3). A burst whose result reads as a
  rate limit benches the subscription that paid for it — `backoff_until`, moved
  forward only, from the provider's own reset stamp when it sent one — and the
  task keeps its attempts, gives back its lease and is admitted again on the next
  tick, on another member's token, resuming the session the limited burst had
  already written. The session belongs to the task, not to the token.
- A **review round** triggers on reviews above the `last_review_id` cursor (GitHub
  review ids are immutable and monotonic): the owner's `changes_requested`, or a
  registered machine reviewer's **`changes_requested` or inline findings** — the
  reviewer App requests changes exactly when it has findings (a clean pass approves,
  REVIEWERS.md) but its 422 body-only fallback keeps only the state, while Copilot
  never requests changes (`COMMENTED` by design) and only ever signals via findings.
  Approvals and overview-only reviews trigger nothing; the cursor advances only after
  the round's burst succeeds, so failed rounds re-surface the same reviews.
- **Re-requests by trigger:** owner round → active providers + owner; reviewer round that
  pushed → active providers only (no human ping); reply-only round → nobody (same diff
  would get the same review — a loop). Which machine reviewers exist at all is the
  **review policy** (`REVIEWERS.md`): a toggle + provider selector, declared in `.env`,
  verified at startup. **Copilot requests ride the owner's user PAT**
  (`GITHUB_COPILOT_REVIEW_TOKEN`): GitHub grants a Copilot review request only to a user
  with a Copilot seat and silently drops it from an App/bot token with a 201 (falsified
  on PR #6). `request_reviewers` checks the response echo and the scheduler logs any
  reviewer that was dropped.
- **Caps by trigger:** the shared rounds rail *blocks* the task on owner rounds; machine
  rounds cap **softly** — cursor advances, a comment flags the leftovers, and the task
  stays at the human merge gate. The machine cycle involves no human at all.
- **Spawn providers** (REVIEWERS.md): when the `claude` provider is active, PR open and
  every pushed round queue an `assess` burst — reviewer role, fresh session, no GitHub
  credential; the control plane posts its findings as the reviewer App, which then
  trigger a dev round like any other. Assess failures degrade ("no machine review this
  round", commented) — they never block the task.
- Ledger `blocked` is the platform's own stop; the `blocked` *label* stays the agent's
  (rule 9). The control plane's visible act is an issue comment explaining why.

## The tick

```
tick():
    if standing_down(): return                # the org's STOP row, or this process's shutdown flag (E3) — a no-op tick
    reconcile_running()                       # orphaned "running"/"working" (no live lease) → killed + rollback
    observe_github()                          # ready issues → claims; reviews/merges → events
    advance()                                 # apply the transition table (every write a CAS)
    if standing_down(): return                # read again: a `stop` may have landed during the GitHub round trips
    task = pick_next()                        # admission: ceiling + claim, one ledger call — the task comes back leased
    if task: execute_burst(task)              # the proven M0/M1 choreography; the runner heartbeats the lease
```

`control-plane run` loops the tick (`--interval`, default 60 s) until the process is
told to shut down; `--once` runs a single tick — the unit CI and humans debug with. In
the fleet (below) the reconcile and observe lines are one product's **observation
turn**, taken through a lease on its registry row (E2).

## Leases and compare-and-set (E1)

The scheduler holds no lock the ledger can see; it holds **leases**, which are
rows (`STORES.md`, "The discipline"). Three mechanics, all single statements:

- **Claim.** `pick_next` is one ledger call, `claim_next`: the oldest `claimed`
  task of the product whose lease nobody holds and whose failure cooldown has
  passed, stamped `claimed_by = this replica, claimed_until = now + lease` by the
  `UPDATE … FOR UPDATE SKIP LOCKED … RETURNING` that picks it. The daily
  ceiling is checked in the same call, behind Postgres's one advisory lock (the
  admission fence, TO-BE §5) — the count is *admitted* work, bursts today plus
  live leases with no burst row yet, so two replicas cannot both see "one under".
  A peer holding the fence means "idle this tick", never "wait".
- **Move.** Every transition is `Scheduler._move`: write the named columns only if
  the row still reads the (status, holder) this replica read; rowcount decides.
  The transitions that end a burst release the lease in the same statement. A
  lost race is logged and otherwise ignored — the ledger moved first, and the
  comment or label belongs to whoever moved it.
- **Keep.** While the container runs the runner asks the burst's heartbeat every
  `HEARTBEAT_POLL_S` (5 s) on the clock — output or none, so a silent hung call
  (finding #6) does not look dead — and every third of the lease it renews
  `claimed_until`, guarded by `claimed_by`. A renewal that finds another holder
  means a peer absorbed the task; the runner kills the container. A ledger error
  is not a lost lease: the burst keeps going and the next beat retries — for two
  thirds of the lease (E2). Past that the holder can no longer vouch for its own
  lease and fences the burst itself, a third of the span before any peer may
  absorb the task: the container is dead before its successor can lawfully spawn.

The replica's name is `host:pid` (`default_replica`), so a lapsed lease says
where to look. `SCHEDULER_LEASE_S` (default 90) is the longest a dead replica's
work stays owned. Nothing here is a mode: one replica on a laptop runs the same
statements as N on a cluster, and with one the fence is never contended.

**Two replicas over one ledger (E2)** is the shape all of this exists for, and
`tests/test_replicas.py` runs it in CI: two real control-plane processes on one
Postgres (GitHub and docker in fakes, everything from the loop down real), one
SIGKILLed mid-burst. The survivor finishes every task, the dead replica's included
— after its lease lapsed, never on sight — and the ledger shows one `succeeded`
burst per task, one `killed` row (the orphan, reconciled by the survivor), and the
crash as one failed attempt on its task. Two things fan out with the second
replica:

- **The observation turn.** The fleet observes a product through a lease on its
  registry row (`repositories.claimed_by` / `claimed_until`, the same statement as
  the task claim: `claim_observation`), one poll interval long. Whoever's claim
  lands reconciles and observes; everyone else skips the product this tick. N
  replicas therefore poll GitHub once per interval, not N times, and a dead
  observer is waited out for at most one interval. It is a rate, not a wall — the
  observe is idempotent by construction (every state change it makes is a CAS),
  and the single-product loop (`run --github`, which may serve an unregistered
  product) observes every tick without it.
- **The product's concurrency.** The registry row's `concurrency` (default 1) is
  counted at admission, fleet-wide: `claim_next` refuses when the product already
  has that many tasks in flight — a live lease or a running burst, whichever
  stage a spawn is at. With the default, a second replica is a hot standby that
  takes over the moment the lane is free or the lease lapses; a row set wider
  runs that many bursts of the product at once, one per replica.

**The kill switch is a row (E3)**, and the same rig proves it: `stop` issued from
a third process kills a burst one replica holds, and `resume` picks the work back
up with nothing restarted (`tests/test_replicas.py`, second test). The MVP's STOP
was a file under the data dir plus a `docker kill` from the same shell — one host
by construction. Now:

- **`org_state`** is one row for the whole ledger (`stop_reason`, `stopped_by`,
  `stopped_at`; STORES.md). `control-plane stop --reason …` sets it by
  compare-and-set, with its audit row (`org.stop`, target `org`) and outbox
  event (`org_stopped`) in the same statement; `control-plane resume` clears it
  the same way. A second `stop` is refused and names the stop in force — which
  is also how you ask whether the org is stopped. No file, no `docker kill`: the
  verb writes the row and reports what is live and who holds it.
- **Every loop reads it** at the top of every tick and again before every spawn
  (`standing_down`) — the fleet and the single-product loop alike — so a stop
  lands on every replica, on every host, within a tick. The loop keeps ticking
  while stopped, reading the row and doing nothing, so `resume` needs no
  restart anywhere; `run` no longer refuses to start under a STOP, because a
  supervisor restarting replicas must not crash-loop them.
- **Every live burst's heartbeat reads it**, on every beat — every
  `HEARTBEAT_POLL_S`, not every third of the lease — and a stop makes the beat
  answer False, on which **the runner holding the burst kills its container**,
  on whatever host that is. That is the path from a verb on one machine to a
  container on another: the row, the heartbeat, the runner. A burst the
  heartbeat fenced is recorded `killed` (the exit code alone cannot tell a kill
  from a crash — both leave 137 — so `execute_burst` remembers the answer), and
  its task rolls back to `claimed` with one more attempt, the reason reading
  "was killed", not "failed".
- **SIGTERM/SIGINT are the same fence, locally.** `_serve` sets the engine's
  `halt` flag — the process's own, shared by every scheduler a fleet builds —
  the heartbeat fences the live burst on it, the tick completes, the loop exits.
  Nothing is written to the ledger: a restarted replica is not a stopped org,
  and the next `run` starts clean. (The MVP's SIGTERM left a STOP file behind
  that blocked the next start until removed.)

**The runner is a protocol (E4).** Where a burst runs is the third seam beside
the ledger and the archive (TO-BE §6): `Runner` in `runner.py`, two methods, and
`DockerRunner` behind it with its behavior untouched.

- **`run(spec, log_path, heartbeat) -> int`** — start the burst, tee its output,
  ask the heartbeat on the clock, fence on the first False, return the burst's
  own exit code. **`alive(burst_id) -> bool`** — the reconciler's question: is
  this burst still running where this runner can see? Under docker that is
  `docker ps` by the name every container carries; the scheduler no longer
  spells `docker` anywhere — it holds one runner per process (`open_runner`,
  the third of the `open_ledger`/`open_archive` kind — one presence toggle,
  `BURST_NAMESPACE`, and `JobsRunner` instead of `DockerRunner`, G3) and hands it to
  `execute_burst` and to `reconcile_running` alike.
- **`burst_env(spec)`** is the box's contract, factored out of the docker
  command: the environment every runner hands the container verbatim — the
  one Anthropic credential (subscription first, none on a `check`), the
  agent's OTel, the tester's resolved sandbox refs. A runner packages it and
  adds nothing, so the second runner cannot re-derive those rules differently.
- **`tests/test_runner_contract.py`** is the contract, one suite parameterized
  over runners the way the ledger's and the archive's are — the done-when, and
  what G3 reuses: exit code, tee, heartbeat-on-the-clock, fence, `alive`, and
  the box getting `burst_env` unchanged. Each runner brings a *rig* (the runner,
  a way to run a shell script as a burst, a way to read back the packaged env);
  the docker rig is in-process — a `DockerRunner` whose three docker lines
  (`docker run`/`kill`/`ps`) are stood in for by a shell, with the pump, the
  tee, the clock and the fence real — so the suite needs no daemon.
  `test_runner.py` keeps what is docker's alone: the command line, the mounts,
  the seccomp profile, kill-by-name.

**Webhooks as wake-up, polls as truth (E5).** The transport paragraph at the top is
now built, exactly as it said: a delivery makes the next poll happen *now*, and
decides nothing else. Three parts, one wall between them (TO-BE §5):

- **The receiver** (`packages/webhook-receiver`, its own image, the platform's only
  public surface) verifies `X-Hub-Signature-256` against the App's webhook secret
  (`WEBHOOK_APPS="<app id>=<secret ref>,…"` — refs, resolved once at start), drops a
  delivery id it has seen (an in-memory LRU; a restart forgets, and the price is one
  duplicate poll), and POSTs a *wake* — `repository`, `kind` (the event name),
  `delivery` — to `WAKE_URL`. Forged → 401 and the id is *not* remembered (a forger
  cannot poison the dedup); replayed → 200; not a wake (`ping`, `installation`…) →
  200; forwarded → 202 whether or not the control plane took it. It never imports
  the control plane and carries no other secret: CI checks the image cannot import
  `control_plane`, `psycopg`, `boto3`, and has no git and no docker.
- **The wake endpoint** (`WAKE_BIND`, internal, unauthenticated by design — the
  worst a caller can cause is one poll of real GitHub state): `POST /wake` is one
  ledger statement, `wake_repository` — the product's **observation lease is
  cleared** (the lease is a rate, not a correctness wall, E2, so clearing a live one
  is exactly one extra poll) and a `product_woken` event with the delivery id is
  written; the replicas' `wakes` channel is notified. A product that is not active
  is 404, nothing written. `GET /healthz` for whatever sits in front.
- **Every replica's loop sleeps on a `wake` flag**, not a timer: its own endpoint
  sets it, and `subscribe_wakes` — the Postgres ledger's LISTEN, a thread with its
  own connection, reconnecting — sets it when *any* replica's endpoint took a
  delivery. So a webhook that reached one replica has every replica looking within
  a second, and the observation lease still makes exactly one of them poll. A lost
  notification costs latency (the interval), never truth.
- **A wake settles before anyone looks (E6).** GitHub delivers the webhook for a
  write before its own read side shows the write: the gate rehearsal saw `?labels=
  ready` list an issue 0.75 s after the label landed, and the poll the wake fired
  at 0.6 s missed it — then held the observation lease for the whole interval, so
  the wake had made the claim *later* (65 s). So `wake_repository` does not clear
  the lease but **reserves** it — `claimed_by = NULL, claimed_until = now() +
  WAKE_SETTLE_S` (2 s): nobody's, and nobody may take it until the window passes —
  and the woken loops sleep the same `WAKE_SETTLE_S` before their tick. A wake that
  lands inside a pending window joins it rather than pushing it, so the platform's
  own label edits (three deliveries in a second) are one look, not a chase; a wake
  onto a *held* lease takes it and settles anew. Measured lag with margin, not a
  knob. Rehearsed: ready → claim in 6.1 s.

`tests/test_wakes.py` runs the done-when as processes on Postgres: two replicas (one
with an endpoint, one hearing wakes only through the channel) and the real receiver;
a signed delivery is one poll after the settle and within seconds, and one
`product_woken` event; the same delivery replayed and two forgeries (wrong secret,
unknown App) are zero polls and zero events, each named in the receiver's log by
delivery id; a second signed delivery is exactly one more poll.
`product_woken.created_at` → the task row's `created_at` is the ready-to-claim
latency E6 measured (`DEPLOYMENT.md`, the record).

## The fleet (C4)

`run` without `--github` serves **every active registered product** through the same
serial loop. Each tick the fleet re-reads the registry — `register`, `pause`/`resume`
and `product set` take effect on the next tick, no restart — and builds each product's
scheduler from its row: policies via `from_product` (C3), the row's `daily_bursts` as
its ceiling and, E2, its `concurrency`. Every product's observation turn is taken every
tick (the turn is a lease, one per interval fleet-wide — above); then at most **one
burst** runs — the **least recently served** product (`repositories.last_burst_at`,
never-served first, slug breaks ties) with a claimable task under its own quotas. One
burst per tick keeps each replica's loop serial; the product's concurrency, counted at
admission, is what bounds the fleet.

Isolation is structural: a product's scheduler reads only its own tasks and bursts
(the ledger queries are repository-scoped), and one product's trouble — an
unhonorable declaration, an unreachable repo, a failed observe — quarantines that
product for the tick, never the fleet. With one active product the fleet degenerates
to exactly the single-product loop. `--github owner/name` still pins one repo:
registered → its row's policies (C3); unregistered → the env-governed dev mode.

## Prompts

The contract (product `CLAUDE.md`, baked as system prompt) owns *behavior*; the prompt
carries only the *task*:

- **work** — issue number/title/body + the provenance framing (`human`: trusted spec;
  `auto`: symptom data, reproduce-first — fed by the tester's relayed symptom issues,
  TESTER.md; the human `ready` gate authorizes both provenances). The evidence block
  is flagged untrusted regardless of provenance.
- **review** — the unseen review(s) verbatim + their inline comments + "address on the
  open PR branch". The session resumes; the agent already has the full task context.

## Rails

Defaults, `.env`-overridable:

| Rail | Default |
|---|---|
| work attempts / task | 3 |
| review rounds / task | 3 |
| daily burst ceiling | 20 — per product: a registered row's `daily_bursts` (C4) |
| failure cooldown / task | 5 min |
| poll interval | 60 s |
| lease / claimed task | 90 s (`SCHEDULER_LEASE_S`) — renewed every third while the burst runs; unrenewable for two thirds = the holder fences itself; lapsed = a peer may reconcile |
| observation lease / product | one poll interval (E2) — the fleet polls a product once per interval, whoever's turn it is |
| concurrency / product | 1 — the registered row's `concurrency` (E2), counted at admission fleet-wide; no env knob, `--github` is one lane by construction |
| per-burst | the **role row's** `max_budget_usd` / `max_turns` (D5; seeded 2.0 / 100, `role set <role> --budget --turns`). The budget is a **hard cap** on estimated cost — the SDK kills the burst mid-run; falsified "advisory" on task 17 |

## Kill switch & crash recovery

- **`control-plane stop --reason …`** — sets the org's STOP row (E3, above). Every
  replica stands down at its next tick; every live burst is fenced by its own heartbeat
  and killed by its own runner within a heartbeat, wherever it runs; the burst is
  marked `killed`, the task rolls back to `claimed` with one more attempt. The loops
  keep ticking, idle. **`control-plane resume`** clears the row and they pick up as they
  are — every decision re-derives from GitHub + ledger. SIGTERM/SIGINT fence the live
  burst the same way and exit the process, writing nothing. A container whose replica
  is already dead is out of everyone's reach and ends on its own rails (budget, turns).
- **Reconciliation, every tick** (in the fleet: every observation turn, E2) — a `running`
  burst or a `working` task that **no live
  lease covers** (E1: `claimed_until` lapsed or never set) is a replica that died
  mid-burst → burst `killed`, task rolled back, attempts+1 (a crash loop must eventually
  trip the rail, not spin forever). A live lease — whoever holds it — is proof of life
  from any host and is never touched, so a peer's burst elsewhere is not judged by this
  host's runner; a burst this replica's runner still sees running (`Runner.alive`, E4
  — `docker ps` under docker) is proof of life too, whatever the lease says (the
  heartbeat failed, the work did not). A restarted single replica therefore absorbs
  its own crash within one lease rather than instantly.

## GitHub surface

Additive `github_api.py` beside `github_auth.py` (stdlib HTTP, same style): list issues by
label · edit labels · comment · find PR by head branch (`dev/<n>-`) · PR state (merged,
head sha) · list reviews · request reviewers (owner on the App token; Copilot on the
owner's `GITHUB_COPILOT_REVIEW_TOKEN` PAT, echo-verified — a bot-token Copilot request
gets a 201 and is silently dropped). The installation token is minted lazily and cached
to just-under its 1 h expiry; each burst still gets its own fresh token.

## Ledger additions

Additive columns on `tasks` (phase-0 migration style): `status` (values above),
`pr_number`, `attempts`, `review_rounds`, `next_burst_kind`, `last_review_id`,
`last_burst_at`, plus a `bursts_today` derivation for the ceiling (count query, no column).
E1 added the lease pair `claimed_by` / `claimed_until` (migration 0015) and the statements
that read it: `create_task`, `transition_task`, `claim_next`, `renew_lease`, and the
`orphaned` filter on `tasks_by_status` / `running_bursts`. `upsert_task` — the whole-row
write — is no longer something the scheduler calls. E2 added `claim_observation` on the
registry row's lease pair (columns since 0002) and the `concurrency` count to `claim_next`
— no new columns. E3 added the `org_state` row (migration 0016) and its three verbs:
`org_stop` (the read every tick, spawn and heartbeat makes), `stop_org`, `resume_org`.

## The clone

Bursts mount a **platform-owned clone** (`data/clones/<owner>__<name>/`), never the
operator's working copy — a burst mutates its checkout (branch, `--local` git config,
bytecode churn), and that state must stay platform state (issue #4; learned the hard
way when a burst left the operator's clone on the PR branch with the bot's identity).
The scheduler re-syncs it to origin's default branch before every burst: fetch →
reset → clean (ignored files survive, so the product `.venv` stays warm). Assess
bursts sync it to the **PR head instead (detached)** and mount it **read-only**
(REVIEWERS.md). The lifecycle invariant: one clone, serial bursts, **sync before
every mount** — whatever a burst leaves behind is erased before the next one looks.
Always HTTPS; for private repos the token travels as a per-invocation header, never
written into config that gets mounted into the box.

## Module layout

```
control_plane/
  scheduler.py    # tick, state machine, rails; leases + CAS (E1); observation turn + concurrency (E2); STOP row + shutdown flag (E3)
  github_api.py   # the REST surface above
  prompts.py      # work/review prompt construction (provenance framing)
  bursts.py       # execute_burst() — extracted from cli._run_burst, shared by CLI + scheduler
  runner.py       # the Runner protocol (E4): run + alive; burst_env, the box's contract; DockerRunner behind it
  clone.py        # the platform-owned clone (above)
  wake.py         # the internal wake endpoint + attach_wakes (E5): a webhook's wake → one ledger write → every loop's flag
  cli.py          # + `run [--once] [--interval]`, `stop --reason`, `resume`; the loop sleeps on the wake flag
packages/webhook-receiver/   # the public HMAC filter (E5): verify · dedup · forward; never imports control-plane
packages/secret-refs/        # SecretResolver (D4), the leaf both share
```

## Deliberately out

Concurrent bursts · Postgres/SeaweedFS backends · Phoenix compose service · the monitor
(the `auto` pipeline runs already — the tester feeds it, TESTER.md; a monitor adds
production telemetry as a second feeder).
