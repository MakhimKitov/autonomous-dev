-- 0007 — the promise that every task names its issue (A6, #17).
--
-- One statement, and the first half of the no-scan route to NOT NULL. A CHECK
-- added `NOT VALID` is enforced for new and modified rows from this moment, while
-- taking only a brief lock: no table is scanned, so nothing waits on this file.
-- 0008 validates it and turns it into a real NOT NULL.
--
-- Why not both here: adding the constraint takes ACCESS EXCLUSIVE, and a lock is
-- held until its transaction ends. Validating in the same transaction would run
-- the scan while that lock is still held — blocking every read on `tasks` for the
-- length of it, which is exactly what the two-step exists to prevent. squawk names
-- this (`constraint-missing-not-valid`) and was right when this file tried it.

ALTER TABLE tasks
    ADD CONSTRAINT tasks_issue_number_not_null CHECK (issue_number IS NOT NULL) NOT VALID;
