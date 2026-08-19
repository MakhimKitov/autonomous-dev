-- 0010 — the reporting views (B3, #21).
--
-- TO-BE §12: "the ledger *is* the metrics store" — the ARCHITECTURE §9 metric
-- families ship as documented read-only SQL views, and any tool with a Postgres
-- driver renders them. No emitted-metrics pipeline, because a second copy of a
-- ledger fact drifts from it (labels reflect, the ledger locks).
--
-- Three things follow from that, and they shape everything below:
--
--   * A view can only say what a row says. Where §9 names a metric the ledger has
--     no source for, the view leaves it out and the comment says where it would
--     have to come from. A column computed from nothing is worse than an absent
--     one: it reads as an answer.
--   * The views are the *only* thing that needs to know how a metric is defined.
--     "Success rate" is a choice about denominators; making it here, once, in SQL
--     a reader can see, is the point of shipping views instead of a dashboard.
--   * They are read by something outside the platform, so they are a capability
--     boundary as well as a query: a view runs with its owner's privileges, so a
--     dashboard role granted SELECT on `reporting` reads aggregates without any
--     access to `tasks`, `bursts` or `violations` (the grants at the bottom, and
--     tests/test_reporting_views.py provokes it).

CREATE SCHEMA reporting;

COMMENT ON SCHEMA reporting IS
    'Read-only views over the ledger: the ARCHITECTURE §9 metric families (B3).';


-- ------------------------------------------------------------------ task facts
--
-- One row per task, with everything the family views aggregate. It exists so the
-- three of them are one SELECT each rather than three copies of the same joins,
-- and so a question the families do not ask ("which task cost the most?") is
-- still one SELECT away. Postgres inlines it into the callers.
--
-- Counts are 0 and not NULL: a task with no bursts has no bursts, which is a
-- number. Money and tokens stay NULL when nothing recorded them — 0.00 would
-- claim a burst ran for free.

CREATE VIEW reporting.task_facts AS
SELECT t.task_id,
       t.repository,
       t.issue_number,
       t.status,
       t.pr_number,
       t.attempts,
       t.review_rounds,
       t.created_at,
       t.updated_at,
       coalesce(b.bursts, 0)                    AS bursts,
       coalesce(b.work_bursts, 0)               AS work_bursts,
       coalesce(b.review_bursts, 0)             AS review_bursts,
       coalesce(b.test_bursts, 0)               AS test_bursts,
       coalesce(b.bursts_that_did_not_finish_clean, 0) AS bursts_that_did_not_finish_clean,
       b.cost_usd,
       b.input_tokens,
       b.output_tokens,
       b.turns,
       b.first_burst_at,
       b.last_burst_at,
       coalesce(v.violations, 0)                AS violations,
       coalesce(v.bursts_with_violations, 0)    AS bursts_with_violations
  FROM tasks t
  LEFT JOIN (
        SELECT task_id,
               count(*)                                        AS bursts,
               count(*) FILTER (WHERE kind = 'work')            AS work_bursts,
               count(*) FILTER (WHERE kind = 'review')          AS review_bursts,
               count(*) FILTER (WHERE kind = 'test')            AS test_bursts,
               -- failed *or* killed: the budget rail killing a burst is not the
               -- same event as a burst erroring, and neither one finished clean.
               count(*) FILTER (WHERE status IN ('failed', 'killed')) AS bursts_that_did_not_finish_clean,
               sum(cost_usd)                                   AS cost_usd,
               sum(input_tokens)                               AS input_tokens,
               sum(output_tokens)                              AS output_tokens,
               sum(num_turns)                                  AS turns,
               min(started_at)                                 AS first_burst_at,
               max(coalesce(finished_at, started_at))          AS last_burst_at
          FROM bursts
         GROUP BY task_id
       ) b USING (task_id)
  LEFT JOIN (
        SELECT task_id,
               count(*)                   AS violations,
               count(DISTINCT burst_id)   AS bursts_with_violations
          FROM violations
         GROUP BY task_id
       ) v USING (task_id);

COMMENT ON VIEW reporting.task_facts IS
    'One row per task: burst counts by kind, cost, tokens, violations. The base '
    'the three family views aggregate (B3).';


-- ----------------------------------------------------------------- reliability
--
-- §9: task success rate · blocked/needs-review rate · rework rate · cost/task.
--
-- The denominators are the judgment call, so they are explicit. Success and
-- blocked rates are over *settled* tasks — done or blocked — because a task the
-- scheduler is still working is not a failure yet, and dividing by it makes every
-- rate a function of how busy the queue happens to be. `needs_review_rate` is
-- over all tasks on purpose: "the human has the ball" is a fact about now, and its
-- share of the whole board is what an operator is asking about.
--
-- `rework_rate` reads `attempts > 1 OR review_rounds > 0`: a second attempt or any
-- review round means the first pass was not enough, which is what rework means
-- here. Both are counters the scheduler's rails already maintain.

CREATE VIEW reporting.reliability AS
SELECT repository,
       count(*)                                             AS tasks,
       count(*) FILTER (WHERE status = 'done')               AS done,
       count(*) FILTER (WHERE status = 'blocked')            AS blocked,
       count(*) FILTER (WHERE status = 'awaiting_review')    AS awaiting_review,
       count(*) FILTER (WHERE status IN ('claimed', 'working')) AS in_flight,
       count(*) FILTER (WHERE status IN ('done', 'blocked')) AS settled,
       count(*) FILTER (WHERE status = 'done')::numeric
           / nullif(count(*) FILTER (WHERE status IN ('done', 'blocked')), 0)  AS success_rate,
       count(*) FILTER (WHERE status = 'blocked')::numeric
           / nullif(count(*) FILTER (WHERE status IN ('done', 'blocked')), 0)  AS blocked_rate,
       count(*) FILTER (WHERE status = 'awaiting_review')::numeric
           / nullif(count(*), 0)                             AS needs_review_rate,
       count(*) FILTER (WHERE attempts > 1 OR review_rounds > 0)::numeric
           / nullif(count(*), 0)                             AS rework_rate,
       sum(cost_usd)                                         AS cost_usd,
       avg(cost_usd)                                         AS cost_per_task,
       avg(cost_usd) FILTER (WHERE status = 'done')          AS cost_per_done_task,
       sum(bursts)                                           AS bursts,
       avg(bursts)                                           AS bursts_per_task
  FROM reporting.task_facts
 GROUP BY repository;

COMMENT ON VIEW reporting.reliability IS
    'ARCHITECTURE §9 reliability, per repository. Success and blocked rates are '
    'over settled tasks (done + blocked); needs-review and rework are over all.';


-- ------------------------------------------------------------------ containment
--
-- §9: wall-hit count · attempted-violation count · successful-breach count
-- (must be 0). The first two are the same number today and the view says so by
-- having one: every row in `violations` is an attempt the PreToolUse hook denied,
-- so hits and attempts coincide. They stop coinciding the moment a tripwire is
-- observe-only, and that is when this view grows a column.
--
-- The third is deliberately absent. A successful breach is by definition what the
-- walls did not catch, so no row records one and a `0` here would be a claim the
-- data cannot support — it would read as "none happened" when it means "none was
-- recorded". Breach rate is measured, not queried: G6/#47 runs the M4 red-team
-- suite against a real deployment, and #10's acknowledged-defects channel is where
-- a human classifies one after the fact.
--
-- Grain is (repository, tool, reason) because the useful question is *what* the
-- walls stopped. Totals roll up with a sum; the denominator for "per burst" is
-- `reporting.reliability.bursts`. `reason` is the wall's own sentence, grouped
-- verbatim rather than classified: any categorization here would be a second
-- vocabulary to keep in step with `walls.py`, and #69 is already about to change
-- how those sentences are produced.

CREATE VIEW reporting.containment AS
SELECT t.repository,
       v.tool,
       v.reason,
       count(*)                    AS hits,
       count(DISTINCT v.burst_id)  AS bursts,
       count(DISTINCT v.task_id)   AS tasks,
       min(v.at)                   AS first_seen,
       max(v.at)                   AS last_seen
  FROM violations v
  JOIN tasks t USING (task_id)
 GROUP BY t.repository, v.tool, v.reason;

COMMENT ON VIEW reporting.containment IS
    'ARCHITECTURE §9 containment: every wall hit by repository, tool and reason. '
    'Hits and attempts coincide (each row was denied); breaches are measured by '
    'the M4 suite, not recorded here.';


-- ------------------------------------------------------------------- capability
--
-- §9: automated-review pass-rate · human-review pass-rate · post-merge defect
-- rate · task difficulty handled. This is the family the ledger answers least,
-- and the view says which parts it is answering with a proxy rather than pretending.
--
--   * `merged_first_pass` / `first_pass_rate` — tasks that reached `done` with no
--     review round. A *proxy* for automated-review pass-rate, not the metric: a
--     review verdict is a GitHub review event, and the ledger keeps the round
--     count and the cursor (`review_rounds`, `last_review_id`), never the verdict.
--     Recording per-provider verdicts is what would close it.
--   * human-review pass-rate — no source. A human's approval is a GitHub event on
--     a PR the platform opened; nothing writes it here.
--   * post-merge defect rate — no source. It needs a link from a later issue back
--     to the task whose merge caused it; #10's acknowledged-defects channel is the
--     nearest thing filed.
--   * task difficulty handled — no source. `turns`, `cost_usd` and `attempts` in
--     `task_facts` are the honest proxies, and they are inputs to a judgment
--     rather than the judgment.
--
-- Test bursts are counted, and named so nobody reads a burst's exit code as a
-- test verdict: `test_bursts_that_did_not_run` is a burst that failed or was
-- killed — the scheduler's "machine test could not run" path. What the report
-- *said* becomes checkable when B6 (#68) makes its citations resolve.

CREATE VIEW reporting.capability AS
SELECT repository,
       count(*) FILTER (WHERE status = 'done')                            AS merged,
       count(*) FILTER (WHERE status = 'done' AND review_rounds = 0)      AS merged_first_pass,
       count(*) FILTER (WHERE status = 'done' AND review_rounds = 0)::numeric
           / nullif(count(*) FILTER (WHERE status = 'done'), 0)           AS first_pass_rate,
       avg(review_rounds) FILTER (WHERE status = 'done')                  AS review_rounds_per_merge,
       avg(attempts) FILTER (WHERE status = 'done')                       AS attempts_per_merge,
       sum(review_bursts)                                                 AS review_bursts,
       sum(test_bursts)                                                   AS test_bursts,
       sum(bursts_that_did_not_finish_clean)                              AS bursts_that_did_not_finish_clean,
       avg(turns) FILTER (WHERE status = 'done')                          AS turns_per_merge,
       max(turns)                                                         AS most_turns_on_one_task
  FROM reporting.task_facts
 GROUP BY repository;

COMMENT ON VIEW reporting.capability IS
    'ARCHITECTURE §9 capability, as far as the ledger reaches: first-pass merges '
    '(a proxy for automated-review pass-rate), rounds and attempts per merge, '
    'burst counts. Review verdicts and post-merge defects have no source yet.';


-- ----------------------------------------------------------------------- grants
--
-- `platform_app` gets USAGE and SELECT so the runtime can read its own numbers —
-- it already may read the base tables, so this grants nothing new.
--
-- The interesting grant is the one *not* here. A dashboard is not the runtime, and
-- it should not connect as the runtime: views execute with their owner's
-- privileges (`security_invoker` is off by default), so a role with USAGE on this
-- schema and SELECT on these views reads every aggregate and cannot read a single
-- row of `tasks`, `bursts` or `violations`. That role is the operator's to create,
-- because how it authenticates is theirs; STORES.md carries the two statements.
GRANT USAGE ON SCHEMA reporting TO platform_app;
GRANT SELECT ON reporting.task_facts, reporting.reliability,
                reporting.containment, reporting.capability TO platform_app;
