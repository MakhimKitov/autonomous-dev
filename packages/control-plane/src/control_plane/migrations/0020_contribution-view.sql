-- 0020 — what each member's subscription actually paid for (F4, #41).
--
-- The pool's side of TO-BE §4's last mechanic: "your subscription ran N bursts /
-- M tokens this week" as a `reporting.*` view, on the same terms as B3's three
-- (0010) — the ledger is the metrics store, a view can only say what a row says,
-- and a role granted SELECT here reads aggregates without reading `bursts`.
--
-- One row per subscription per week it did work in, plus one row per
-- subscription that has done none (the LEFT JOIN keeps a member who just opted
-- in, with `week` NULL and zeros: a contribution that has not been spent yet is
-- a fact worth showing, and an absent row would read as "not contributing").
--
-- Tokens are the unit — consumption under a subscription is measured in tokens,
-- not USD (§4) — and `cost_usd` is deliberately absent: under subscription
-- billing the SDK's figure is an estimate of what the work *would* have cost on
-- the API, which is exactly the number nobody is paying. `bursts.cost_usd` keeps
-- it for the budget rail; a contribution report would only make it look like a bill.
--
-- The window the cap is declared against is the pick's (`USAGE_WINDOW_H`, 5h),
-- which is not a reporting period; `spent_this_week` here is the human horizon,
-- and `headroom` is deliberately not computed — it belongs to an instant, and a
-- view that shows it for a week-old row would be showing a number that was true
-- once.

CREATE VIEW reporting.contribution AS
SELECT s.subscription_id,
       s.member,
       s.status,
       s.cap_tokens,
       s.expires_at,
       s.backoff_until,
       date_trunc('week', b.started_at)                                   AS week,
       count(b.burst_id)                                                  AS bursts,
       count(b.burst_id) FILTER (WHERE b.status = 'succeeded')            AS bursts_succeeded,
       count(b.burst_id) FILTER (WHERE b.status IN ('failed', 'killed'))  AS bursts_that_did_not_finish_clean,
       coalesce(sum(coalesce(b.input_tokens, 0) + coalesce(b.output_tokens, 0)), 0) AS tokens,
       count(DISTINCT t.repository)                                       AS products,
       max(b.started_at)                                                  AS last_burst_at
  FROM subscriptions s
  LEFT JOIN bursts b ON b.subscription_id = s.subscription_id
  LEFT JOIN tasks t ON t.task_id = b.task_id
 GROUP BY s.subscription_id, s.member, s.status, s.cap_tokens, s.expires_at,
          s.backoff_until, date_trunc('week', b.started_at);

COMMENT ON VIEW reporting.contribution IS
    'TO-BE §4: what each contributed subscription paid for, by week — bursts, '
    'tokens and products, with the row''s own declaration beside them. One row '
    'with a NULL week for a subscription that has not been spent yet.';

GRANT SELECT ON reporting.contribution TO platform_app;
