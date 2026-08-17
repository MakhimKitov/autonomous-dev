-- 0004 — the session tree (A5, #16).
--
-- The MVP kept one mutable blob per task: `tasks.transcript_key` pointed at
-- `sessions/{task_id}/session.tar.gz`, and every dev burst overwrote it. The
-- target is one immutable snapshot per *burst*, with the ledger owning the
-- lineage (TO-BE §6 DR, STORES.md "The session archive"):
--
--   * `session_key` / `session_checksum` name what this burst left behind. The
--     ledger is the checksum authority — `get` verifies the bytes against this
--     column, not against anything the archive says about itself.
--   * `resumed_from_burst_id` names the node this burst's volume came from,
--     recorded at spawn. Self-referencing, so walking a task's history is a
--     recursive CTE rather than a join through the task row.
--
-- All three are nullable, and that is the shape rather than laziness: a burst
-- that archives nothing (reviewer, tester — never seeded, never archived) has no
-- snapshot, and the first burst of a task has no parent. Nullable columns also
-- mean this migration rewrites no rows and takes no strict lock for longer than
-- the catalog update.
--
-- `tasks.transcript_key` / `transcript_checksum` stay for now and stop being
-- written by this stage. A6 (#17) backfills the existing blobs into the new shape
-- and drops the columns; until then they hold whatever the last MVP-era burst put
-- there, which is exactly the input that backfill needs.
--
-- Deliberately not here: `bursts.pinned`. It exists to keep GC off golden-replay
-- fixtures, and lands with the harness that pins them (G4, #45).

ALTER TABLE bursts
    ADD COLUMN resumed_from_burst_id text,
    ADD COLUMN session_key           text,
    ADD COLUMN session_checksum      text;

-- The lineage foreign key: a parent that isn't a burst is not a parent. NOT VALID
-- because `bursts` is live; 0005 validates it (there is nothing to scan yet, but
-- the two-step is the rule and the rule is what survives the table getting big).
ALTER TABLE bursts
    ADD CONSTRAINT bursts_resumed_from_fk
        FOREIGN KEY (resumed_from_burst_id) REFERENCES bursts (burst_id) NOT VALID;

-- latest_archived_burst(): the leaf a task's next burst resumes from. Partial on
-- the same predicate the query uses, so only archived bursts are indexed —
-- reviewer and tester rows never enter it.
--
-- Blocking build, like every index here: CONCURRENTLY cannot run inside the
-- runner's transaction, and a failed concurrent build leaves an INVALID index
-- that enforces nothing while looking present. See 0002 for the longer version.
-- squawk-ignore require-concurrent-index-creation
CREATE INDEX bursts_session_leaf ON bursts (task_id, started_at) WHERE session_key IS NOT NULL;
