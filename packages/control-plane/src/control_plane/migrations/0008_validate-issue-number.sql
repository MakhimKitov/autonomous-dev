-- 0008 — and the promise becomes NOT NULL (A6, #17).
--
-- This is the migration that is *meant* to fail on a database still holding
-- MVP-era rows, and the failure is the whole design: a task without an issue
-- number has no derived id, so there is nothing for the one-task-per-issue wall to
-- be unique about. SQL cannot repair those rows — deriving an id needs the
-- repository slug, and the column it replaced held a filesystem path — so
-- `control-plane backfill` is the only thing that can, and the state after this
-- file fails is a prefix: 0007 applied, this one not.
--
-- The order of the three statements is the point:
--
--   1. VALIDATE CONSTRAINT scans the table under a lock that readers and writers
--      both pass through. A bad row stops the migration here, by name.
--   2. SET NOT NULL then finds a validated proof that no NULLs exist and skips its
--      own scan (Postgres 12+). It takes ACCESS EXCLUSIVE, but only for as long as
--      a catalog update — the scan already happened, under the weak lock.
--   3. The CHECK is dropped, because it now says nothing the column doesn't.
--      Keeping it would mean carrying a second constraint in `schema.sql` forever
--      in exchange for nothing.
--
-- Sharing one transaction is safe in this direction and only this direction: the
-- scan runs first, under the weak lock, and the two strong locks that follow are
-- brief. Reversed, it would be 0007's mistake.

ALTER TABLE tasks VALIDATE CONSTRAINT tasks_issue_number_not_null;
ALTER TABLE tasks ALTER COLUMN issue_number SET NOT NULL;
ALTER TABLE tasks DROP CONSTRAINT tasks_issue_number_not_null;
