-- 03_kpi.sql -> view kpi_overview
--
-- NN_ prefix controls apply order: this 03_ rollup reads the 02_ model
-- daily_performance, so it MUST be applied after it (create_views.py applies
-- files in filename order).
--
-- Single-row grand totals over the last 30 days (relative to the most recent
-- day present in the data, not wall-clock today, so a lagging feed still shows
-- a full window). days_covered is how many distinct days actually had data in
-- that window.
CREATE OR REPLACE VIEW `agora-data-driven.client_template.kpi_overview` AS
WITH recent AS (
  SELECT *
  FROM `agora-data-driven.client_template.daily_performance`
  WHERE metric_date >= DATE_SUB(
    (SELECT MAX(metric_date) FROM `agora-data-driven.client_template.daily_performance`),
    INTERVAL 29 DAY
  )
)
SELECT
  SUM(sessions)                              AS sessions,
  SUM(users)                                 AS users,
  SUM(conversions)                           AS conversions,
  SUM(spend)                                 AS spend,
  SUM(revenue)                               AS revenue,
  SAFE_DIVIDE(SUM(revenue), NULLIF(SUM(spend), 0)) AS roas,
  COUNT(DISTINCT metric_date)                AS days_covered
FROM recent;
