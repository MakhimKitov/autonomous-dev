-- 0015 — the task lease columns (E1, #32).
--
-- STORES.md's "Claim — a lease is data": work is claimed by writing who holds it
-- and until when, in the same UPDATE that picks the row (FOR UPDATE SKIP LOCKED,
-- RETURNING *). `repositories` has carried the pair since 0002 for the
-- observation lease E2 fans out; `tasks` gets it now, with the stage where
-- contention becomes real. Both nullable and defaultless: no table rewrite, no
-- lock beyond the catalog's, and NULL is the honest value for every task that
-- exists today — nobody holds it.
--
-- The lease is operational state, not an operator mutation: no audit row, no
-- event, `updated_at` untouched, exactly as `last_burst_at` is treated. What
-- reads it: `claim_next` (lapsed or absent → claimable), `renew_lease` (the
-- heartbeat, guarded by claimed_by), and the reconciler's `orphaned` filters
-- (a running burst or a working task nobody's lease covers is a replica that
-- died mid-burst). platform_app already holds UPDATE on tasks (0002).

ALTER TABLE tasks ADD COLUMN claimed_by text;
ALTER TABLE tasks ADD COLUMN claimed_until timestamptz;
