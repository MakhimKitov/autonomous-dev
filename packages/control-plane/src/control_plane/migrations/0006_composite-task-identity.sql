-- 0006 — the task id becomes derived (A6, #17).
--
-- The MVP keyed a task by the bare issue number and kept the platform's clone
-- path in `repo`. Both are wrong for more than one product: two repositories both
-- have an issue #7, and a filesystem path is a fact about one machine. The target
-- (TO-BE §2, STORES.md) derives the id — '{owner}/{repo}#{issue_number}' — and
-- keeps the slug beside it, so a row and a GitHub URL name the same thing and two
-- replicas compute the same id without coordinating.
--
-- `repo` is *renamed* rather than dropped and re-added, so the column keeps its
-- statistics and the migration rewrites nothing. Its meaning changes with the
-- name: what used to hold '/Users/x/clones/repo' now holds 'owner/repo'. Any row
-- that predates this migration therefore carries a value that is no longer true,
-- which is what the next paragraph is about.
--
-- THIS MIGRATION DOES NOT TOUCH DATA, and 0007 will refuse to finish if data
-- needs it to. `issue_number` lands nullable here; 0007 makes it NOT NULL and
-- fails while any row lacks one. On an empty database — CI, a fresh deployment —
-- both apply straight through. On a database carrying MVP-era rows, the failure is
-- the point: those rows cannot be repaired by SQL, because deriving an id needs
-- the repository slug and the old column holds a path. `control-plane backfill`
-- is what repairs them (and drops the manual-era ones), and the state after 0007
-- fails is a prefix — 0006 applied, nothing else — exactly as designed.
--
-- `transcript_key` / `transcript_checksum` go here. A5 moved the snapshot onto the
-- burst that produced it and stopped writing these; the values left behind are
-- copies. The origin they were copied from is the single-node SQLite ledger, which
-- is where `backfill` reads them, so dropping them here loses nothing — and it is
-- what retires that ledger as an origin (STORES.md, "From the MVP").

-- A rename breaks any client still selecting the old name, which is why squawk
-- flags it and why this is spent now rather than later: nothing is deployed yet
-- (E6 is where the laptop retires), so the cost is one restart of one process. A
-- rename after that would have to expand and contract instead — add the new
-- column, dual-write, backfill, cut readers over, drop the old one — four
-- migrations and a release in between. The alternative here, dropping and
-- re-adding, would rewrite the table and throw away its statistics for nothing.
-- squawk-ignore renaming-column
ALTER TABLE tasks RENAME COLUMN repo TO repository;

-- Nullable on purpose: see the header. NOT NULL arrives in 0007, which is
-- allowed to fail.
ALTER TABLE tasks ADD COLUMN issue_number integer;

-- WALL: one task per issue. A unique index rather than a UNIQUE constraint — the
-- constraint form takes ACCESS EXCLUSIVE for the length of its build, and this one
-- serves lookups by (repository, issue_number) too.
--
-- Rows with a NULL issue_number do not collide here (NULLs are distinct in a
-- unique index), which is exactly why this can land before 0007's NOT NULL.
--
-- Blocking build: CONCURRENTLY cannot run inside the runner's transaction, and a
-- failed concurrent build leaves an INVALID index that enforces nothing while
-- looking present. See 0002 for the longer version.
-- squawk-ignore require-concurrent-index-creation
CREATE UNIQUE INDEX tasks_one_per_issue ON tasks (repository, issue_number);

-- Dropping a column breaks a client that still selects it. A5 stopped writing
-- these and nothing has read them since, so the only such client is code from
-- before A5 — and see the note on the rename above for why that is affordable
-- exactly now. The data is not lost by dropping it here: these were copies of
-- pointers whose origin is the single-node SQLite ledger, which is where
-- `backfill` reads them from.
-- One statement each: the suppression applies to a statement, and a two-clause
-- ALTER gets one finding per clause, so only the first would be covered.
-- squawk-ignore ban-drop-column
ALTER TABLE tasks DROP COLUMN transcript_key;
-- squawk-ignore ban-drop-column
ALTER TABLE tasks DROP COLUMN transcript_checksum;
