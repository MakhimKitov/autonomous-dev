-- 0012 — the product registry opens (C1, #23).
--
-- A4 built `repositories` and left it deliberately empty: nothing before
-- `product register` could legitimately create a row, so `tasks.repository`
-- stayed a plain text column. This file is the other half — now that the verb
-- exists, a task must name a registered product.
--
-- Three steps, in dependency order:
--
--   1. Every repository the ledger already holds tasks for is materialized as a
--      registered product, each with the audit row and outbox event any
--      registration writes. The values are the platform's *code* defaults —
--      a migration is plain SQL and reads no .env — so the audit reason says so
--      and names `product set` as where the real policies get declared. Nothing
--      reads these columns before C3, so behavior is unchanged; what the seed
--      buys is that step 3 can validate on a live database.
--   2. `tasks.repository` gains its foreign key, NOT VALID here and validated in
--      0013 — the 0002 → 0003 two-step, for the same locking reasons.
--   3. `platform_app` gains INSERT and UPDATE on `repositories` — the grant 0002
--      reserved for "the stage that ships the verbs", which is this one. DELETE
--      stays withheld: unregistering is not a verb, retention is platform_admin's.

INSERT INTO repositories (repository, team, state, review_enabled, review_provider,
                          e2e_enabled, test_env_refs, concurrency, daily_bursts, usage_share)
SELECT DISTINCT repository, split_part(repository, '/', 1), 'active',
       false, '', false, '{}'::jsonb, 1, 20, 1.0
FROM tasks;

INSERT INTO audit (actor, action, target, reason)
SELECT 'migration:0012', 'product.register', repository,
       'materialized from existing tasks; policy and quota values are platform code '
       'defaults — declare the real ones with `product set`'
FROM (SELECT DISTINCT repository FROM tasks) AS existing;

INSERT INTO events (kind, payload)
SELECT 'product_registered',
       jsonb_build_object('audit_id', id, 'repository', target, 'actor', actor)
FROM audit
WHERE actor = 'migration:0012' AND action = 'product.register';

ALTER TABLE tasks
    ADD CONSTRAINT tasks_repository_fk FOREIGN KEY (repository)
        REFERENCES repositories (repository) NOT VALID;

GRANT INSERT, UPDATE ON repositories TO platform_app;
