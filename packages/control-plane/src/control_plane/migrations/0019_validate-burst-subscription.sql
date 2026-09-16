-- 0019 — validate the pool's attribution wall (the second half of 0018's two-step).
--
-- `VALIDATE CONSTRAINT` scans without blocking writers and belongs in its own
-- transaction (0002 → 0003, 0012 → 0013, same reasoning). Every existing row
-- has a NULL `subscription_id` and passes; what this buys is that the constraint
-- holds for the rows already there as well as the ones to come — and that Atlas,
-- which cannot see `pg_constraint.convalidated`, is not left calling a database
-- synced while the wall is only half up. test_no_constraint_is_left_unvalidated
-- is what stands behind that.

ALTER TABLE bursts VALIDATE CONSTRAINT bursts_subscription_fk;
