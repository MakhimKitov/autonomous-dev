-- Reference panels over the reporting views (B4, #22). Examples, not components:
-- TO-BE §12 ships the views and no dashboard, so this file is what a dashboard
-- would ask, written so any tool with a Postgres driver can ask it — psql, a
-- notebook, a spreadsheet's SQL import, or the Grafana dashboard beside it, which
-- embeds these same statements verbatim (`tests/test_dashboards.py` holds the two
-- copies to each other and runs every one of them).
--
-- Two rules keep them portable, and both are worth keeping if you edit:
--
--   * No tool macros. Grafana's `$__timeFilter(...)` and friends would make these
--     queries run in exactly one place. Windows are plain `now() - interval`, which
--     every driver understands and a human can paste.
--   * Nothing but `reporting.*`. The dashboard reader is a role with USAGE on this
--     schema and SELECT on its views (docker/telemetry/dashboard-role.sql) — it
--     cannot read `tasks`, `bursts` or `violations`, so a panel that reaches for a
--     base table fails at the boundary rather than widening it.
--
-- Every statement here is a whole panel, in the order the dashboard shows them.


-- 1. Waiting on a human. The first question an operator has, and the only panel
-- here at task grain: `awaiting_review` means the platform did its part and stopped,
-- `blocked` means it gave up. Both need a person, so both are one list.
SELECT task_id,
       repository,
       issue_number,
       pr_number,
       status,
       attempts,
       review_rounds,
       updated_at
  FROM reporting.task_facts
 WHERE status IN ('awaiting_review', 'blocked')
 ORDER BY updated_at;


-- 2. Reliability, per repository (ARCHITECTURE §9). Percentages rather than the
-- views' fractions, because a panel is read at a glance -- the view keeps the
-- fraction so a query can do arithmetic with it. Success and blocked are shares of
-- *settled* tasks, which is why they can both be blank while work is in flight.
SELECT repository,
       tasks,
       done,
       blocked,
       awaiting_review,
       in_flight,
       round(success_rate * 100, 1)  AS success_pct,
       round(blocked_rate * 100, 1)  AS blocked_pct,
       round(rework_rate * 100, 1)   AS rework_pct,
       round(cost_per_task, 2)       AS cost_per_task,
       round(bursts_per_task, 1)     AS bursts_per_task
  FROM reporting.reliability
 ORDER BY repository;


-- 3. Capability, per repository (ARCHITECTURE §9), as far as the ledger reaches.
-- `first_pass_pct` is merges that took no review round: a proxy for the
-- automated-review pass-rate and named for what it counts, because the ledger keeps
-- the round count and never the verdict. The view's comment says what would close
-- that, and which two metrics of this family have no source at all.
SELECT repository,
       merged,
       merged_first_pass,
       round(first_pass_rate * 100, 1)     AS first_pass_pct,
       round(review_rounds_per_merge, 2)   AS review_rounds_per_merge,
       round(attempts_per_merge, 2)        AS attempts_per_merge,
       round(turns_per_merge)              AS turns_per_merge,
       most_turns_on_one_task,
       review_bursts,
       test_bursts,
       bursts_that_did_not_finish_clean
  FROM reporting.capability
 ORDER BY repository;


-- 4. What the walls stopped (ARCHITECTURE §9 containment). Grain is the useful
-- question -- which tool, refused for which reason -- and `reason` is the wall's own
-- sentence, so this panel grows a row the first time a wall says something new.
-- There is no breach column here or in the view: a breach is what the walls missed,
-- so nothing records one, and a zero would read as an answer.
SELECT repository,
       tool,
       reason,
       hits,
       bursts,
       tasks,
       last_seen
  FROM reporting.containment
 ORDER BY last_seen DESC;


-- 5. The alert condition (TO-BE §12: any new violations row is a page). Rows here
-- means page. The window has to be longer than your poll interval or a hit lands
-- between two polls and is never seen -- five minutes for a one-minute poll. This
-- is the pull form, and it pages at group grain: a second identical hit inside the
-- window keeps one alert firing rather than raising a new one. OBSERVABILITY.md,
-- "The alert condition", has the two forms that are exact and what they cost.
SELECT repository,
       tool,
       reason,
       hits,
       last_seen
  FROM reporting.containment
 WHERE last_seen > now() - interval '5 minutes'
 ORDER BY last_seen DESC;


-- 6. Spend, by day and repository. A time series, so the shape is (time, series
-- name, value) -- the format every dashboard tool recognizes without a hint.
-- `last_burst_at` and not the task's `updated_at`: the money was spent when the
-- bursts ran, and a task can be touched long after.
SELECT date_trunc('day', last_burst_at) AS time,
       repository                       AS metric,
       sum(cost_usd)                    AS value
  FROM reporting.task_facts
 WHERE last_burst_at IS NOT NULL
 GROUP BY 1, 2
 ORDER BY 1;


-- 7. The most expensive tasks. Not a §9 metric -- it is the reason `task_facts`
-- exists as a view of its own: the three families answer the questions the
-- architecture named, and a question it did not name should still be one SELECT.
-- Also the honest read on "task difficulty handled", which has no metric: turns,
-- cost and attempts are inputs to a judgment, not the judgment.
SELECT task_id,
       status,
       bursts,
       turns,
       round(cost_usd, 2) AS cost_usd,
       violations
  FROM reporting.task_facts
 ORDER BY cost_usd DESC NULLS LAST
 LIMIT 10;
