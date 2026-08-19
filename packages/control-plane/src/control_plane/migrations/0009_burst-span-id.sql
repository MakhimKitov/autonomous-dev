-- 0009 — the burst span's own id (B1, #19).
--
-- ARCHITECTURE.md §9 promises that "each burst links to its predecessor via span
-- links" and, in the same paragraph, that the ledger stores each burst's
-- `trace_id`. Those two sentences don't meet: a W3C link target is a *pair*,
-- `(trace_id, span_id)`, because a link names a span and not a trace. With the
-- trace id alone the only link we could emit would carry a zero span id, which a
-- viewer resolves to nothing — a parent that looks broken rather than unrecorded.
--
-- So the ledger records both halves of the burst span it opened. The successor
-- needs no extra query: the row it links to is the leaf it already materialized
-- its volume from (`resumed_from_burst_id`).
--
-- `DEFAULT ''` and not null, matching `trace_id`: both are "the id, or the empty
-- string when tracing produced no span" — a state, not a missing value, and one
-- fewer nullable text column for callers to branch on. The default is constant, so
-- Postgres records it in the catalog and rewrites no rows (11+); the table stays
-- readable and writable throughout.

ALTER TABLE bursts ADD COLUMN span_id text NOT NULL DEFAULT '';

-- No index. Nothing looks a burst up by span id — the lineage query is
-- `resumed_from_burst_id`, and this column is only ever read off a row that was
-- already found.
