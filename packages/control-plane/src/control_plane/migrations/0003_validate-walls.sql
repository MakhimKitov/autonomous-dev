-- 0003 — the second half of 0002's two-step constraints (A4, #15).
--
-- 0002 added four constraints to live tables as NOT VALID: enforced from that
-- moment for every new or modified row, but with the existing rows unexamined.
-- This file examines them. `VALIDATE CONSTRAINT` scans the table under a lock
-- that readers and writers both pass through, which is the whole reason the two
-- steps are separate — the combined `ADD CONSTRAINT` would hold ACCESS EXCLUSIVE
-- for the length of that scan.
--
-- It is a separate migration, not two statements in one file, because the runner
-- wraps each file in a single transaction: keeping the add and the validate
-- together would hold the strict lock from the add until the scan finished, which
-- is exactly what the split avoids.
--
-- This file is allowed to fail, and that is a feature. A row that predates the
-- constraint and violates it stops the migration here, with the constraint still
-- in force for new writes — a database that says "these six rows are wrong"
-- rather than one that quietly keeps them. The fix is to repair the rows and run
-- `migrate` again.

ALTER TABLE tasks  VALIDATE CONSTRAINT tasks_status_check;
ALTER TABLE bursts VALIDATE CONSTRAINT bursts_status_check;
ALTER TABLE bursts VALIDATE CONSTRAINT bursts_running_finished_check;
ALTER TABLE bursts VALIDATE CONSTRAINT bursts_task_fk;
ALTER TABLE bursts VALIDATE CONSTRAINT bursts_role_fk;
