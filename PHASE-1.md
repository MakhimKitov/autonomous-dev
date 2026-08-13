# PHASE-1 — building the TO-BE

The implementation of `TO-BE.md`, decomposed into **PR-sized steps tracked as
GitHub issues** — the platform's own philosophy applied to itself: issues are the
task queue, this file is only the map. Companion to `PHASE-0.md` (the MVP build).

**The board:** milestones `Stage A`–`Stage G` + `Later horizons`, label `phase-1`.
Each issue carries its scope, a one-sentence *done when*, and `Blocked by:`
references; `org-ask` marks steps needing anything outside the repo; `gate` marks
the checkpoints below.

| Stage | Issues | What ships | Org asks |
|---|---|---|---|
| **A — state block** | #52, #12–#18 | CI workflow · STORES.md · migrations · PostgresLedger · schema walls · session-tree archive · composite ids · backfill | — |
| **B — observability** | #19–#22 | trace lineage · agent OTel · reporting views · testing rig | — |
| **C — product registry** | #23–#26 | registry verbs · preflight (gate + lint) · row-sourced policy · registry-driven scheduler | second test repo (§15) |
| **D — identity + roles** | #27–#31 | role prefixes · token downscoping · tester provenance · SecretResolver · roles as rows | Apps created + installed |
| **E — deployment** | #32–#37 | leases + CAS · replica safety · STOP row · Runner protocol · webhook receiver · server | server + HTTPS route |
| **F — subscription pool** | #38–#41 | ref-only verbs · headroom pick · backoff/retry · lifecycle + accounting | teammates' tokens |
| **G — runners + isolation** | #42–#47 | 3-phase choreography · presigned URLs · JobsRunner · golden-replay harness · bake-off · egress + M4 | cluster · bake-off owner (§15) |
| **H–K — later horizons** | #48–#51 | coarse placeholders for T1-remainder / T2 / T3 / T4 — decomposed when reached | — |

## The three gates

Regressions have nowhere to hide at:

- **A7 (#18)** — the MVP runs byte-for-byte on Postgres + SeaweedFS.
- **C4 (#26)** — the registry-driven scheduler runs two products serially; with one
  product registered, behavior is byte-for-byte the MVP's.
- **E6 (#37)** — the laptop retires: two stateless replicas + the public receiver
  on a server, `ready`→claim latency measured in seconds, compose-file parity kept.

(G6/#47 is the fourth, security-shaped gate: M4 as a measured breach rate.)

## Dependency spine

A → {B, C} → D → E → F → G; H–K after their wave prerequisites. The critical path
to "laptop retired" is **A → C → D → E**; B lands any time after A; F needs only
A + D plus willing teammates. Everything before D1 (#27) needs nobody's
permission — stages A–C are deliberately laptop-only.

## Conventions

- One issue = one PR; a step that can't say *done when* in a sentence is two steps.
- Per-PR verification is CI (#52) — green means merge. Humans test only at the
  `gate` issues; each carries its rehearsal checklist.
- `TO-BE.md`'s DRs are the spec; issues carry ordering and acceptance, never
  redefine design.
- The discipline holds in every PR: transactional-but-not-interactive ledger
  writes, capabilities-not-instruments seams, walls in schema/code.
