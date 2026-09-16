-- 0018 — a burst names the subscription that paid for it (F2, #39).
--
-- The last column `schema.sql`'s shape note was holding back for the stage that
-- needs it. F1 opened the pool; this is what makes it accountable: the pick at
-- admission stamps `bursts.subscription_id`, and from there usage per member is
-- a rolling-window aggregate over this table — the ledger *is* the metrics
-- store (TO-BE §12), so the pool keeps no counter of its own to drift.
--
-- NULL is not "unknown", it is the honest value for a burst that ran on the
-- machine's own credential: every burst before this migration, and every burst
-- in a deployment with an empty pool. That is why the column is nullable and
-- why nothing backfills it — inventing an attribution for the MVP's history
-- would be a worse answer than the true one.
--
-- `bursts` is a live table, so the foreign key lands NOT VALID here and is
-- validated in 0019 (the 0002 → 0003 two-step). Existing rows are all NULL and
-- would pass either way; the two-step is kept because the scan is what blocks
-- writers, and this table is the one with volume.

ALTER TABLE bursts ADD COLUMN subscription_id text;

ALTER TABLE bursts
    ADD CONSTRAINT bursts_subscription_fk FOREIGN KEY (subscription_id)
        REFERENCES subscriptions (subscription_id) NOT VALID;

-- The pick reads usage per subscription over a window, which is this index's
-- one query: the rows of a subscription since a timestamp. Partial, because
-- every burst that ran on the machine's own credential is NULL here and none of
-- them is ever an answer.
-- squawk-ignore require-concurrent-index-creation
CREATE INDEX bursts_subscription_started_idx ON bursts (subscription_id, started_at)
    WHERE subscription_id IS NOT NULL;
