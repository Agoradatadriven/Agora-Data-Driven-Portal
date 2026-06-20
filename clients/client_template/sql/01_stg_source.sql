-- 01_stg_source.sql -> view stg_source
--
-- NN_ prefix controls apply order: this 01_ staging view is created BEFORE the
-- 02_/03_ models that read it (create_views.py applies files in filename order,
-- and a view cannot be created before the view it selects from).
--
-- Source: the shared Windsor mirror `agora-data-driven.raw_windsor.metrics_daily`.
-- This is Windsor's blended daily export. For a real client the operator points
-- this view at the actual Windsor connector table(s) for that client -- e.g. a
-- UNION of `raw_windsor.ga4` + `raw_windsor.google_ads` -- but the template
-- reads the single blended mirror so the contract is concrete out of the box.
--
-- Selects only the typed/filtered columns the downstream models need.
CREATE OR REPLACE VIEW `agora-data-driven.client_template.stg_source` AS
SELECT
  CAST(metric_date AS DATE)      AS metric_date,
  CAST(channel     AS STRING)    AS channel,
  CAST(sessions    AS INT64)     AS sessions,
  CAST(users       AS INT64)     AS users,
  CAST(conversions AS INT64)     AS conversions,
  CAST(spend       AS FLOAT64)   AS spend,
  CAST(revenue     AS FLOAT64)   AS revenue
FROM `agora-data-driven.raw_windsor.metrics_daily`
WHERE metric_date IS NOT NULL;
