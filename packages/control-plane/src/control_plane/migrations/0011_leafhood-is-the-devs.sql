-- B5 (#67): leafhood is the dev's — narrow the session-leaf index to role = 'dev'.
--
-- `latest_archived_burst` was role-blind: newest burst with a session_key wins.
-- Harmless while only dev bursts ever archived, latent the moment anything else
-- does — and B5 starts archiving tester volumes as evidence. Without this filter,
-- one archived tester burst and the next dev round would materialize its volume
-- from the *tester's* session: exactly the mixing `archive_session=False` was
-- standing in for. The query gains `AND role = 'dev'` in both backends; this
-- index follows it, staying partial on exactly the predicate the query uses.
--
-- DROP + CREATE rather than a second index: the old predicate has no remaining
-- reader, and two overlapping partials would leave the planner a wrong choice.
--
-- Blocking drop for the same reason the build below is blocking: CONCURRENTLY
-- cannot run inside the runner's transaction. The lock is momentary — dropping
-- takes no scan — and the runner's LOCK_TIMEOUT caps the wait for it.
-- squawk-ignore require-concurrent-index-deletion
DROP INDEX bursts_session_leaf;

-- Blocking build, like every index here: CONCURRENTLY cannot run inside the
-- runner's transaction, and a failed concurrent build leaves an INVALID index
-- that enforces nothing while looking present. See 0002 for the longer version.
-- squawk-ignore require-concurrent-index-creation
CREATE INDEX bursts_session_leaf ON bursts (task_id, started_at)
    WHERE session_key IS NOT NULL AND role = 'dev';
