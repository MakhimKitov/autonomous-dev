-- 0005 — validating 0004's lineage foreign key (A5, #16).
--
-- Its own migration for the same reason 0003 was: the runner wraps each file in
-- one transaction, so keeping the ADD and the VALIDATE together would hold the
-- add's strict lock for the length of the scan, which is precisely what the
-- two-step avoids. See 0002's header for why nothing but a test catches a
-- forgotten second step.

ALTER TABLE bursts VALIDATE CONSTRAINT bursts_resumed_from_fk;
