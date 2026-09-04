-- 0014 — the role verbs open (D5, #31).
--
-- 0002 seeded the four pipeline rows and left `roles` read-only for the runtime,
-- reserving the write grant for "the stage that ships the verbs". This is it:
-- `role set` edits the open fields of a row — preamble, model, budget, turns,
-- mount — as one CTE (the change, its audit row, its outbox event), so
-- platform_app needs UPDATE on roles.
--
-- UPDATE only, deliberately:
--   * no INSERT — a custom role is a new row, and custom-role *execution* waits
--     for T4's trigger machinery; until a row can run, a verb to create one
--     would be a promise the engine can't keep. The grant grows with T4.
--   * no DELETE — a row `bursts.role` points at is history; retention is
--     platform_admin's, like everywhere else.
-- The wall-bearing fields stay frozen underneath this grant: roles_pipeline_guard
-- (0002) fires on UPDATE regardless of who holds it, so the grant widens what the
-- runtime may *touch*, never what a pipeline row may *become*.

GRANT UPDATE ON roles TO platform_app;
