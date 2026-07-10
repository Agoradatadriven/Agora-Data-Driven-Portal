-- 11_activity_monthly.sql -> view activity_monthly
--
-- Month-on-month LEADS, SALES and CLICK RATE for the combined trend chart -- ALL scoped to the
-- quiz-lead cohort so the three series describe the SAME people (earlier this mixed quiz leads
-- with the whole store's orders/clicks, which made the lines incomparable). "Post-quiz" means we
-- only count a lead's activity at/after the month they took the quiz.
--
--   LEADS      = new quiz leads that month (stg_quiz, one row per lead).
--   SALES      = orders placed BY a quiz-lead email at/after their quiz.
--   CLICK_RATE = emails a quiz lead clicked (BINARY: one send counts once no matter how many
--                clicks) / emails sent to quiz leads, that month. This is the engagement-drop
--                signal (open rate is unusable -- Apple Mail Privacy auto-opens inflate it).
--
-- ONLY COMPLETE MONTHS: we emit a month only if it has email data loaded (a gap in the Klaviyo
-- backfill would otherwise show as an empty/misleading point) AND it is a fully-elapsed calendar
-- month (the current in-progress month is partial, so its click rate reads artificially low).
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.activity_monthly` AS
WITH lead AS (            -- one row per quiz lead (email already LOWER(TRIM) in stg_quiz)
  SELECT email, submitted_at FROM `agora-data-driven.client_tcs.stg_quiz`
),
leads AS (
  SELECT DATE_TRUNC(DATE(submitted_at), MONTH) AS m, COUNT(*) AS n
  FROM lead GROUP BY 1
),
sales AS (               -- orders by a quiz-lead email, at/after that lead took the quiz
  SELECT DATE_TRUNC(DATE(o.order_date), MONTH) AS m, COUNT(*) AS n
  FROM `agora-data-driven.client_tcs.stg_orders` o
  JOIN lead le ON le.email = o.email AND o.order_date >= le.submitted_at
  GROUP BY 1
),
email AS (               -- emails sent to a quiz lead at/after their quiz; is_click is binary per send
  SELECT DATE_TRUNC(DATE(e.event_at), MONTH) AS m,
         COUNT(*) AS sends, COUNTIF(e.is_click) AS clicks
  FROM `agora-data-driven.client_tcs.stg_email_events` e
  JOIN lead le ON le.email = e.email AND e.event_at >= le.submitted_at
  GROUP BY 1
),
spine AS (
  SELECT m FROM leads UNION DISTINCT SELECT m FROM sales UNION DISTINCT SELECT m FROM email
)
SELECT
  sp.m                             AS month,
  COALESCE(l.n, 0)                 AS leads,
  COALESCE(s.n, 0)                 AS sales,
  e.sends                          AS sends,
  e.clicks                         AS clicks,
  SAFE_DIVIDE(e.clicks, e.sends)   AS click_rate
FROM spine sp
LEFT JOIN leads l ON l.m = sp.m
LEFT JOIN sales s ON s.m = sp.m
LEFT JOIN email e ON e.m = sp.m
WHERE e.sends IS NOT NULL                                   -- only months WITH complete email data
  AND sp.m < DATE_TRUNC(CURRENT_DATE(), MONTH)              -- drop the in-progress (partial) current month
  AND sp.m >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 24 MONTH), MONTH)
ORDER BY month;
