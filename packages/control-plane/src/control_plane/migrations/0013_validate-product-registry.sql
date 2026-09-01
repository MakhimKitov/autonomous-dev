-- 0013 — validate the registry wall (the second half of 0012's two-step).
--
-- `VALIDATE CONSTRAINT` scans without blocking writers and belongs in its own
-- transaction (0002 → 0003, same reasoning). 0012's seed is what makes this pass
-- on a live database: every repository the tasks table names became a registered
-- product there. A forgotten validate would look synced to Atlas and correct to
-- squawk — test_no_constraint_is_left_unvalidated is what stands behind it.

ALTER TABLE tasks VALIDATE CONSTRAINT tasks_repository_fk;
