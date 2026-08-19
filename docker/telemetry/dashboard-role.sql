-- The dashboard reader (B4, #22). Run once, after `control-plane migrate`.
--
--   docker compose -f docker/compose.yaml exec -T postgres \
--     psql -U platform_admin -d platform < docker/telemetry/dashboard-role.sql
--
-- This is not a migration and never will be: migration 0010 grants the reporting
-- views to `platform_app` and stops there, because *how a reader authenticates* is
-- the operator's decision, not the schema's. What the schema does own is the
-- boundary — a view runs with its owner's privileges, so the grants below buy every
-- aggregate in `reporting` and no row of `tasks`, `bursts` or `violations`. The rig
-- connects Grafana as this role so the reference dashboard cannot quietly come to
-- depend on a base table (`tests/test_reporting_views.py` provokes the wall;
-- `tests/test_dashboards.py` holds the dashboard to it).
--
-- The password is a rig password: the cluster is on localhost and its volume is
-- thrown away. In a real deployment this file is three statements you keep and one
-- you replace — put the reader behind whatever the rest of your estate uses.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard') THEN
        CREATE ROLE dashboard LOGIN PASSWORD 'dashboard';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA reporting TO dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO dashboard;

-- So a view added by a later migration is readable without re-running this file.
-- `ALL TABLES` above is a snapshot of now; this is the standing rule. Both are
-- scoped to `reporting`, which holds views and nothing else — a base table put
-- there would be readable, and that is the argument for keeping it views-only.
ALTER DEFAULT PRIVILEGES FOR ROLE platform_admin IN SCHEMA reporting
    GRANT SELECT ON TABLES TO dashboard;
