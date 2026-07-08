-- 10_conversion_trend.sql -> view conversion_trend
--
-- THE HEADLINE DIAGNOSTIC: lead -> customer conversion rate by the MONTH the lead took the quiz,
-- shown next to that cohort's email engagement. Answers "why has conversion dropped, and what did
-- it look like over time?".
--
-- Recency control: a lead who took the quiz last month has had little time to buy, so raw
-- conversion for recent cohorts looks artificially low. `conversion_rate_90d` counts only purchases
-- within 90 days of the quiz, which IS comparable across cohorts; `mature` flags cohorts old enough
-- (>= 90 days) to judge. The dashboard plots the 90-day rate for mature cohorts so the drop is real,
-- not a recency artifact -- and overlays open_rate to show whether falling conversion tracks falling
-- engagement.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.conversion_trend` AS
SELECT
  cohort_month,
  COUNT(*)                                                               AS leads,
  COUNTIF(is_converted)                                                  AS converted,
  SAFE_DIVIDE(COUNTIF(is_converted), COUNT(*))                           AS conversion_rate,
  COUNTIF(is_converted AND days_to_convert <= 90)                        AS converted_90d,
  SAFE_DIVIDE(COUNTIF(is_converted AND days_to_convert <= 90), COUNT(*)) AS conversion_rate_90d,
  SAFE_DIVIDE(SUM(opens),  NULLIF(SUM(emails_sent), 0))                  AS open_rate,
  SAFE_DIVIDE(SUM(clicks), NULLIF(SUM(emails_sent), 0))                  AS click_rate,
  AVG(emails_sent)                                                       AS avg_emails_sent,
  DATE_DIFF(CURRENT_DATE(), cohort_month, DAY) >= 90                     AS mature
FROM `agora-data-driven.client_tcs.quiz_leads`
GROUP BY cohort_month
ORDER BY cohort_month;
