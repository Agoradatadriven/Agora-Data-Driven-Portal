-- 12_activity_weekly.sql -> view activity_weekly
--
-- WEEKLY companion to activity_monthly (same quiz-lead-scoped LEADS / SALES / CLICK_RATE), for the
-- trend chart's Week view. Weeks start Monday. Kept to the last 52 weeks so the chart stays legible.
--
-- Same "complete periods only" rule as the monthly view: emit a week only if it has email data
-- loaded AND it is a fully-elapsed week (the in-progress current week is partial). NOTE: weekly
-- click counts are small, so the weekly click rate is noisier than the monthly one -- read it for
-- shape, not precision.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.activity_weekly` AS
WITH lead AS (
  SELECT email, submitted_at FROM `agora-data-driven.client_tcs.stg_quiz`
),
leads AS (
  SELECT DATE_TRUNC(DATE(submitted_at), WEEK(MONDAY)) AS w, COUNT(*) AS n
  FROM lead GROUP BY 1
),
sales AS (
  SELECT DATE_TRUNC(DATE(o.order_date), WEEK(MONDAY)) AS w, COUNT(*) AS n
  FROM `agora-data-driven.client_tcs.stg_orders` o
  JOIN lead le ON le.email = o.email AND o.order_date >= le.submitted_at
  GROUP BY 1
),
email AS (
  SELECT DATE_TRUNC(DATE(e.event_at), WEEK(MONDAY)) AS w,
         COUNT(*) AS sends, COUNTIF(e.is_click) AS clicks
  FROM `agora-data-driven.client_tcs.stg_email_events` e
  JOIN lead le ON le.email = e.email AND e.event_at >= le.submitted_at
  GROUP BY 1
),
spine AS (
  SELECT w FROM leads UNION DISTINCT SELECT w FROM sales UNION DISTINCT SELECT w FROM email
)
SELECT
  sp.w                             AS week,
  COALESCE(l.n, 0)                 AS leads,
  COALESCE(s.n, 0)                 AS sales,
  e.sends                          AS sends,
  e.clicks                         AS clicks,
  SAFE_DIVIDE(e.clicks, e.sends)   AS click_rate
FROM spine sp
LEFT JOIN leads l ON l.w = sp.w
LEFT JOIN sales s ON s.w = sp.w
LEFT JOIN email e ON e.w = sp.w
WHERE e.sends IS NOT NULL                                                       -- complete weeks only
  AND sp.w < DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))                           -- drop in-progress week
  AND sp.w >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY)), INTERVAL 52 WEEK)
ORDER BY week;
