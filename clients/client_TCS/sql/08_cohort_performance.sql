-- 08_cohort_performance.sql -> view cohort_performance
--
-- One row per quiz-cohort YEAR (the year the lead took the quiz): how many leads, how many
-- converted, and their OPEN engagement. Answers "are recent cohorts both converting worse AND
-- engaging less?" -- the cohort lens on the same diagnostic.
--
-- COMPLETE-DATA WINDOW: the raw Klaviyo mirror only has events from 2024-08-01 onward, so a lead
-- has full post-quiz email history only if they submitted on/after that date. We therefore scope
-- the whole view to submitted_date >= '2024-08-01'. NOTE: even inside this window the mirror has
-- month-level gaps (Jan-Jul 2025, Nov 2025, Apr 2026 are missing), so the open-rate columns are
-- directional, not exact -- surfaced with a data-gap caveat on the dashboard.
--
-- Engagement is measured with OPEN rate (not the old send-weighted click rate, which was too
-- sparse and gap-corrupted to compare across cohorts):
--   pct_leads_opened      -- share of leads who opened at least one post-quiz email (cumulative;
--                            confounded by email volume -- shown but not the trend to trust)
--   first_email_open_rate -- open rate on each lead's FIRST post-quiz email (position-matched,
--                            the cleanest apples-to-apples engagement comparison across cohorts)
--   first5_open_rate      -- open rate across each lead's first 5 post-quiz emails
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.cohort_performance` AS
WITH leads AS (
  SELECT cohort_year, email, is_converted, opens, revenue_post_quiz
  FROM `agora-data-driven.client_tcs.quiz_leads`
  WHERE submitted_date >= '2024-08-01'
),
ranked AS (
  -- Rank each lead's post-quiz email sends by time so we can measure the first / first-5 emails.
  SELECT q.cohort_year, q.email, e.is_open,
    ROW_NUMBER() OVER (PARTITION BY q.email ORDER BY e.event_at) AS rn
  FROM `agora-data-driven.client_tcs.stg_quiz`         AS q
  JOIN `agora-data-driven.client_tcs.stg_email_events` AS e
    ON e.email = q.email AND e.event_at >= q.submitted_at
  WHERE q.submitted_date >= '2024-08-01'
),
open_stats AS (
  SELECT cohort_year,
    SAFE_DIVIDE(COUNTIF(is_open AND rn = 1),  COUNTIF(rn = 1))  AS first_email_open_rate,
    SAFE_DIVIDE(COUNTIF(is_open AND rn <= 5), COUNTIF(rn <= 5)) AS first5_open_rate
  FROM ranked
  GROUP BY cohort_year
)
SELECT
  CAST(l.cohort_year AS STRING)                        AS cohort,
  COUNT(*)                                             AS leads,
  COUNTIF(l.is_converted)                              AS converted,
  SAFE_DIVIDE(COUNTIF(l.is_converted), COUNT(*))       AS conversion_rate,
  SAFE_DIVIDE(COUNTIF(l.opens > 0), COUNT(*))          AS pct_leads_opened,
  ANY_VALUE(os.first_email_open_rate)                  AS first_email_open_rate,
  ANY_VALUE(os.first5_open_rate)                       AS first5_open_rate,
  SUM(l.revenue_post_quiz)                             AS revenue
FROM leads l
LEFT JOIN open_stats os ON os.cohort_year = l.cohort_year
GROUP BY l.cohort_year
ORDER BY l.cohort_year;
