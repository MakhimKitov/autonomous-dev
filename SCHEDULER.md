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
| `claimed` | ours; a burst is queued (`next_burst_kind`: `work` or `review`) |
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

| From | Event (observed) | Side effects | To |
|---|---|---|---|
| — | issue `human`+`ready`, unclaimed | task row; labels `ready`→`in-progress` | `claimed` (work) |
| `claimed` | slot free, cooldown passed, ceilings ok | spawn burst (resume when a session exists) | `working` |
| `working` | burst ok ∧ linked PR open | record `pr_number`; review round → re-request Copilot + owner | `awaiting_review` |
| `working` | burst failed ∨ no PR, attempts < max | attempts+1; failure cooldown | `claimed` |
| `working` | burst failed, attempts = max | comment on issue; clear `in-progress` | `blocked` |
| `working` | kill switch / crash | container killed → burst `killed`; attempts+1; roll back | pre-burst state |
| `awaiting_review` | unseen owner `changes_requested` **or Copilot findings**, rounds < max | rounds+1; queue review prompt | `claimed` (review) |
| `awaiting_review` | unseen owner `changes_requested`, rounds = max | comment; clear `in-progress` | `blocked` |
| `awaiting_review` | unseen Copilot findings, rounds = max | soft cap: advance cursor; comment | `awaiting_review` |
| `awaiting_review` | PR merged | clear `in-progress` | `done` |
| `awaiting_review` | PR closed unmerged | comment; clear `in-progress` | `blocked` |
| any active | issue carries `blocked` (agent rule 9) | stop scheduling the task | `blocked` |
| `blocked` | human re-adds `ready` | attempts/rounds reset | `claimed` |

Notes:
- **"Burst ok but no PR" is a failed attempt.** The work burst's contract is a PR; anything
  less retries (with resume — the session remembers how far it got) until the rail trips.
- A **review round** triggers on reviews above the `last_review_id` cursor (GitHub
  review ids are immutable and monotonic): the owner's `changes_requested`, or a
  **Copilot review carrying inline findings** — Copilot never requests changes
  (`COMMENTED` by design), so the autonomous loop keys on its findings. Overview-only
  Copilot reviews trigger nothing; the cursor advances only after the round's burst
  succeeds, so failed rounds re-surface the same reviews.
- **Re-requests by trigger:** owner round → Copilot + owner; Copilot round that pushed →
  Copilot only (no human ping); reply-only round → nobody (same diff would get the same
  review — a loop).
- **Caps by trigger:** the shared rounds rail *blocks* the task on owner rounds; Copilot
  rounds cap **softly** — cursor advances, a comment flags the leftovers, and the task
  stays at the human merge gate. The Copilot cycle involves no human at all.
- Ledger `blocked` is the platform's own stop; the `blocked` *label* stays the agent's
  (rule 9). The control plane's visible act is an issue comment explaining why.

## The tick

```
tick():
    if stop_requested(): shutdown_clean()     # STOP flag or SIGTERM
    reconcile_running()                       # ledger "running" vs docker ps → killed + rollback
    observe_github()                          # ready issues → claims; reviews/merges → events
    advance()                                 # apply the transition table
    if slot_free():                           # serial: one burst globally (trust first)
        task = pick_next()                    # oldest eligible `claimed`
        if task: execute_burst(task)          # the proven M0/M1 choreography, extracted from cli
```

`control-plane run` loops the tick (`--interval`, default 60 s); `--once` runs a single
tick — the unit CI and humans debug with.

## Prompts

The contract (product `CLAUDE.md`, baked as system prompt) owns *behavior*; the prompt
carries only the *task*:

- **work** — issue number/title/body + the provenance framing (`human`: trusted spec;
  `auto`: symptom data, reproduce-first — phase 0 feeds only `human`). The evidence block
  is flagged untrusted regardless of provenance.
- **review** — the unseen review(s) verbatim + their inline comments + "address on the
  open PR branch". The session resumes; the agent already has the full task context.

## Rails

Defaults, `.env`-overridable:

| Rail | Default |
|---|---|
| work attempts / task | 3 |
| review rounds / task | 3 |
| daily burst ceiling (global) | 20 |
| failure cooldown / task | 5 min |
| poll interval | 60 s |
| per-burst | `max_budget_usd` 2.0 (advisory under subscription) · `max_turns` 100 |

## Kill switch & crash recovery

- **`control-plane stop`** — writes the `STOP` flag under the data dir and kills the live
  burst container (`docker kill burst-<id>`, name from the ledger). The runner returns,
  the burst is marked `killed`, the task rolls back to its pre-burst state, the loop exits
  before the next spawn. SIGTERM does the same in-process. Removing `STOP` and re-running
  resumes cleanly — every decision re-derives from GitHub + ledger.
- **Startup reconciliation** — any ledger burst still `running` with no matching container
  (scheduler died mid-burst) → `killed`, task rolled back, attempts+1 (a crash loop must
  eventually trip the rail, not spin forever).

## GitHub surface

Additive `github_api.py` beside `github_auth.py` (stdlib HTTP, same style): list issues by
label · edit labels · comment · find PR by head branch (`dev/<n>-`) · PR state (merged,
head sha) · list reviews · request reviewers (Copilot + owner — the M1 gap, verified 201).
The installation token is minted lazily and cached to just-under its 1 h expiry; each
burst still gets its own fresh token.

## Ledger additions

Additive columns on `tasks` (phase-0 migration style): `status` (values above),
`pr_number`, `attempts`, `review_rounds`, `next_burst_kind`, `last_review_id`,
`last_burst_at`, plus a `bursts_today` derivation for the ceiling (count query, no column).

## The clone

Bursts mount a **platform-owned clone** (`data/clones/<owner>__<name>/`), never the
operator's working copy — a burst mutates its checkout (branch, `--local` git config,
bytecode churn), and that state must stay platform state (issue #4; learned the hard
way when a burst left the operator's clone on the PR branch with the bot's identity).
The scheduler re-syncs it to origin's default branch before every burst: fetch →
reset → clean (ignored files survive, so the product `.venv` stays warm). Always
HTTPS; for private repos the token travels as a per-invocation header, never written
into config that gets mounted into the box.

## Module layout

```
control_plane/
  scheduler.py    # tick, state machine, rails
  github_api.py   # the REST surface above
  prompts.py      # work/review prompt construction (provenance framing)
  bursts.py       # execute_burst() — extracted from cli._run_burst, shared by CLI + scheduler
  clone.py        # the platform-owned clone (above)
  cli.py          # + `run [--once] [--interval]`, `stop`
```

## Deliberately out

Webhooks (wake-up optimization once off the laptop — see above) · tester role ·
concurrent bursts · Postgres/SeaweedFS backends · Phoenix compose service · monitor/`auto`
issues (the provenance gate ships anyway; phase 0 just never feeds it).
