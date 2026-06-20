-- 02_model.sql -> view daily_performance
--
-- NN_ prefix controls apply order: this 02_ model reads the 01_ staging view
-- stg_source, so it MUST be applied after it (create_views.py applies files in
-- filename order).
--
-- Per-day rollup: one row per metric_date with the day's totals and ROAS.
CREATE OR REPLACE VIEW `agora-data-driven.client_template.daily_performance` AS
SELECT
  metric_date,
  SUM(sessions)                              AS sessions,
  SUM(users)                                 AS users,
  SUM(conversions)                           AS conversions,
  SUM(spend)                                 AS spend,
  SUM(revenue)                               AS revenue,
  SAFE_DIVIDE(SUM(revenue), NULLIF(SUM(spend), 0)) AS roas
FROM `agora-data-driven.client_template.stg_source`
GROUP BY metric_date
ORDER BY metric_date;
