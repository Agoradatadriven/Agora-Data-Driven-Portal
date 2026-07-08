-- 08_cohort_performance.sql -> view cohort_performance
--
-- One row per quiz-cohort YEAR (the year the lead took the quiz): how many leads, how many
-- converted, and their engagement. Answers "are recent cohorts both converting worse AND
-- engaging less?" -- the cohort lens on the same diagnostic.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.cohort_performance` AS
SELECT
  CAST(cohort_year AS STRING)                          AS cohort,
  COUNT(*)                                             AS leads,
  COUNTIF(is_converted)                                AS converted,
  SAFE_DIVIDE(COUNTIF(is_converted), COUNT(*))         AS conversion_rate,
  AVG(opens)                                           AS avg_opens,
  AVG(clicks)                                          AS avg_clicks,
  SAFE_DIVIDE(SUM(opens),  NULLIF(SUM(emails_sent), 0)) AS open_rate,
  SAFE_DIVIDE(SUM(clicks), NULLIF(SUM(emails_sent), 0)) AS click_rate,
  SUM(revenue_post_quiz)                               AS revenue
FROM `agora-data-driven.client_tcs.quiz_leads`
GROUP BY cohort_year
ORDER BY cohort_year;
