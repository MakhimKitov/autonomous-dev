-- 0017 — the subscription pool opens (F1, #38).
--
-- A4 built `subscriptions` and left it deliberately empty, the way it left
-- `repositories` empty until C1: nothing before `subscription register` could
-- legitimately create a row, and platform_app held SELECT alone. This file is
-- the other half — the verbs ship, so the table gets the wall that makes the
-- pool's central claim structural, and the runtime gets the grants to write it.
--
-- THE WALL. `secret_ref` holds a *ref*: `env:NAME`, `file:PATH`, `helper:NAME`
-- (TO-BE §4 — the platform never holds a token value). That is enforced three
-- deep, and this is the deepest: `subscriptions.check_ref` refuses a value at
-- the verb, both backends call it before their statement, and the CHECK here
-- refuses one that reached the table by any other path — a console later, a
-- psql session, a bug. What no column can hold, no dump, log line or archive
-- can leak; the wall is the absence of the value, not a redaction pass.
--
-- `cap_tokens > 0` is the second, smaller one: a cap is capacity per rolling
-- window, and zero registers a contribution no pick could ever use (F2).
--
-- Both constraints land validated rather than the usual `NOT VALID` two-step
-- (0002 → 0003). The table has no rows and could not have any: no verb wrote to
-- it before this migration and the runtime could not INSERT. Validating an
-- empty table takes no meaningful lock, and a row someone put there by hand
-- that fails this check is a row that must not survive the pool's opening.
--
-- The grants mirror the registry's (0012): INSERT and UPDATE for platform_app —
-- register, revoke, `set`, and the rail's `backoff_until` CAS at F3. DELETE
-- stays withheld: an ended subscription is not deleted, because the bursts it
-- paid for still name it.

ALTER TABLE subscriptions
    -- squawk-ignore constraint-missing-not-valid
    ADD CONSTRAINT subscriptions_secret_ref_check CHECK (secret_ref ~ '^(env|file|helper):.');

ALTER TABLE subscriptions
    -- squawk-ignore constraint-missing-not-valid
    ADD CONSTRAINT subscriptions_cap_tokens_check CHECK (cap_tokens > 0);

GRANT INSERT, UPDATE ON subscriptions TO platform_app;
