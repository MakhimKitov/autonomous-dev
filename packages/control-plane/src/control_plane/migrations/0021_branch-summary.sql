-- 0021 — a node of the session tree can be abandoned (H5, #107).
--
-- TO-BE §6's "retry ≠ branch": an environment flake re-runs the same node, but
-- an attempt that ran and did not work is a tail the next burst must not carry.
-- The scheduler marks that burst abandoned, and the leaf query stops returning
-- it — so the next burst grows from the last good node (its parent), forked by
-- the SDK rather than cut by hand. The snapshot itself is never touched: an
-- abandoned node stays in the archive, immutable, as the record of what was tried.
--
-- One column is both the mark and its content. `branch_summary` is NULL on a
-- live node; on an abandoned one it is the short summary of the attempt that the
-- next branch is shown, as data. A mark with no summary would be a branch that
-- forgets why it exists, so the two cannot be set apart — and an empty string is
-- refused for the same reason.
--
-- Nullable with no default, so the ADD is catalog-only on this live table, and
-- the CHECK is satisfied by every existing row (all NULL) — NOT VALID buys
-- nothing when no row can fail it, so it is not used.

ALTER TABLE bursts ADD COLUMN branch_summary text
    CONSTRAINT bursts_branch_summary_check CHECK (branch_summary <> '');

-- The leaf is the newest *live* archived dev burst. DROP + CREATE for the reason
-- 0011 gives: two overlapping partials leave the planner a wrong choice.
-- squawk-ignore require-concurrent-index-deletion
DROP INDEX bursts_session_leaf;

-- squawk-ignore require-concurrent-index-creation
CREATE INDEX bursts_session_leaf ON bursts (task_id, started_at)
    WHERE session_key IS NOT NULL AND role = 'dev' AND branch_summary IS NULL;
