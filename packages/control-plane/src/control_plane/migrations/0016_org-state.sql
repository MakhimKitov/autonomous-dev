-- 0016 — the org STOP row (E3, #34).
--
-- The MVP's kill switch was a file: `control-plane stop` touched `STOP` under the
-- data dir, the loop on that machine saw it and exited, and a `docker kill` from
-- the same shell took the live burst down. Every word of that is one-host: a
-- second replica on another machine never sees the file, and the shell that
-- writes it cannot reach a container elsewhere. TO-BE §5 moves the flag into the
-- ledger — one org-scoped row, read at the top of every tick and before every
-- spawn, and by the heartbeat of every live burst — so a `stop` issued anywhere
-- lands everywhere within a tick, and on live work within a heartbeat, killed by
-- the runner that holds it. The console later writes the same row.
--
-- Exactly one row, by construction: the key is a boolean that must be true, so a
-- second INSERT is a duplicate key and a false one fails the CHECK. The row is
-- seeded here, in its running state, and is never inserted or deleted again —
-- platform_app gets SELECT and UPDATE and nothing else, so the runtime can flip
-- it and cannot remove it. The three stop columns are all-or-nothing: a stop
-- carries who and when or it is not a stop, and a half-written one is a CHECK
-- violation, not a state.
--
-- `stop_reason IS NOT NULL` is the whole signal. The verbs (`stop_org`,
-- `resume_org`) are compare-and-set on it — stopping a stopped org or resuming a
-- running one changes nothing and leaves no audit row — and each lands with its
-- audit row and outbox event in one statement, like every registry verb since
-- C1. Nothing here is a lease: the row has no expiry. An operator's STOP holds
-- until an operator lifts it.

CREATE TABLE org_state (
    id          boolean PRIMARY KEY DEFAULT true CONSTRAINT org_state_singleton_check CHECK (id),
    stop_reason text,
    stopped_by  text,
    stopped_at  timestamptz,
    CONSTRAINT org_state_stop_shape_check
        CHECK ((stop_reason IS NULL) = (stopped_by IS NULL) AND (stop_reason IS NULL) = (stopped_at IS NULL))
);

INSERT INTO org_state DEFAULT VALUES;

GRANT SELECT, UPDATE ON org_state TO platform_app;
