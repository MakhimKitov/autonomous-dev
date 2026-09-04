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
the tick logic doesn't change.

## Task state machine

Ledger-authoritative (`tasks.status`); labels stay reflective (`GITHUB-INTERFACE.md`).

| State | Meaning |
|---|---|
| `claimed` | ours; a burst is queued (`next_burst_kind`: `work`, `review`, or `assess` — a reviewer-role burst judging the open PR, REVIEWERS.md) |
| `working` | the burst container is running |
| `awaiting_review` | PR open; the ball is with Copilot / the owner |
| `blocked` | needs a human — rail tripped, agent rule 9, or PR closed unmerged |
| `done` | PR merged; issue auto-closed |

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
```

Every row of this table is written as a **compare-and-set** (E1): the ledger
moves only if the task still reads the (status, holder) the replica saw, and the
side effects run only if it moved — a replica that loses the race writes nothing
and posts nothing. `claimed → working` additionally names the replica's own
**lease**, taken by the claim statement that picked the task (below).

| From | Event (observed) | Side effects | To |
|---|---|---|---|
| — | issue `human`+`ready`, unclaimed | task row (insert-if-absent); labels `ready`→`in-progress` | `claimed` (work) |
| `claimed` | leased by the claim statement: cooldown passed, ceiling ok | spawn burst (resume when a session exists); heartbeat renews the lease | `working` |
| `working` | burst ok ∧ linked PR open | record `pr_number`; review round → re-request Copilot + owner | `awaiting_review` |
| `working` | burst failed ∨ no PR, attempts < max | attempts+1; failure cooldown | `claimed` |
| `working` | burst failed, attempts = max | comment on issue; clear `in-progress` | `blocked` |
| `working` | kill switch / crash | container killed → burst `killed`; attempts+1; roll back | pre-burst state |
| `awaiting_review` | unseen owner `changes_requested` **or machine findings**, rounds < max | rounds+1; queue review prompt | `claimed` (review) |
| `awaiting_review` | unseen owner `changes_requested`, rounds = max | comment; clear `in-progress` | `blocked` |
| `awaiting_review` | unseen machine findings, rounds = max | soft cap: advance cursor; comment | `awaiting_review` |
| `awaiting_review` | PR merged | clear `in-progress` | `done` |
| `awaiting_review` | PR closed unmerged | comment; clear `in-progress` | `blocked` |
| any active | issue carries `blocked` (agent rule 9) | stop scheduling the task | `blocked` |
| `blocked` | human re-adds `ready` | attempts/rounds reset | `claimed` |

Notes:
- **"Burst ok but no PR" is a failed attempt.** The work burst's contract is a PR; anything
  less retries (with resume — the session remembers how far it got) until the rail trips.
  Resume survives *any* death, including the budget rail killing the stream mid-run: the
  worker captures the session id from the init message and writes its result file in a
  `finally` (the task-17 lesson), so attempts × per-burst budget behaves as an installment
  plan — each retry continues where the last died, never a restart from zero.
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
    if stop_requested(): shutdown_clean()     # STOP flag or SIGTERM
    reconcile_running()                       # orphaned "running"/"working" (no live lease) → killed + rollback
    observe_github()                          # ready issues → claims; reviews/merges → events
    advance()                                 # apply the transition table (every write a CAS)
    task = pick_next()                        # admission: ceiling + claim, one ledger call — the task comes back leased
    if task: execute_burst(task)              # the proven M0/M1 choreography; the runner heartbeats the lease
```

`control-plane run` loops the tick (`--interval`, default 60 s); `--once` runs a single
tick — the unit CI and humans debug with.

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
  is not a lost lease: the burst keeps going and the next beat retries.

The replica's name is `host:pid` (`default_replica`), so a lapsed lease says
where to look. `SCHEDULER_LEASE_S` (default 90) is the longest a dead replica's
work stays owned. Nothing here is a mode: one replica on a laptop runs the same
statements as N on a cluster, and with one the fence is never contended.

## The fleet (C4)

`run` without `--github` serves **every active registered product** through the same
serial loop. Each tick the fleet re-reads the registry — `register`, `pause`/`resume`
and `product set` take effect on the next tick, no restart — and builds each product's
scheduler from its row: policies via `from_product` (C3), the row's `daily_bursts` as
its ceiling. Every product is observed every tick; then at most **one burst** runs —
the **least recently served** product (`repositories.last_burst_at`, never-served
first, slug breaks ties) with a claimable task under its own quota. One burst per tick
keeps the loop serial; per-product concurrency arrives with real runners (TO-BE §6).

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
| lease / claimed task | 90 s (`SCHEDULER_LEASE_S`) — renewed every third while the burst runs; lapsed = a peer may reconcile |
| per-burst | the **role row's** `max_budget_usd` / `max_turns` (D5; seeded 2.0 / 100, `role set <role> --budget --turns`). The budget is a **hard cap** on estimated cost — the SDK kills the burst mid-run; falsified "advisory" on task 17 |

## Kill switch & crash recovery

- **`control-plane stop`** — writes the `STOP` flag under the data dir and kills the live
  burst container (`docker kill burst-<id>`, name from the ledger). The runner returns,
  the burst is marked `killed`, the task rolls back to its pre-burst state, the loop exits
  before the next spawn. SIGTERM does the same in-process. Removing `STOP` and re-running
  resumes cleanly — every decision re-derives from GitHub + ledger.
- **Reconciliation, every tick** — a `running` burst or a `working` task that **no live
  lease covers** (E1: `claimed_until` lapsed or never set) is a replica that died
  mid-burst → burst `killed`, task rolled back, attempts+1 (a crash loop must eventually
  trip the rail, not spin forever). A live lease — whoever holds it — is proof of life
  from any host and is never touched, so a peer's burst elsewhere is not judged by this
  host's `docker ps`; a container visible *here* is proof of life too, whatever the
  lease says (the heartbeat failed, the work did not). A restarted single replica
  therefore absorbs its own crash within one lease rather than instantly.

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
write — is no longer something the scheduler calls.

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
  scheduler.py    # tick, state machine, rails; leases + CAS (E1)
  github_api.py   # the REST surface above
  prompts.py      # work/review prompt construction (provenance framing)
  bursts.py       # execute_burst() — extracted from cli._run_burst, shared by CLI + scheduler
  clone.py        # the platform-owned clone (above)
  cli.py          # + `run [--once] [--interval]`, `stop`
```

## Deliberately out

Webhooks (wake-up optimization once off the laptop — see above) ·
concurrent bursts · Postgres/SeaweedFS backends · Phoenix compose service · the monitor
(the `auto` pipeline runs already — the tester feeds it, TESTER.md; a monitor adds
production telemetry as a second feeder).
