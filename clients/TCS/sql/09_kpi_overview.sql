-- 09_kpi_overview.sql -> view kpi_overview
--
-- Single-row headline KPIs for the dashboard's top strip. Funnel metrics come from quiz_leads;
-- the this-year-vs-prior OPEN/CLICK rates come from engagement_monthly (the diagnostic answer:
-- is engagement down this year?). Applied last (09_) because it reads quiz_leads AND
-- engagement_monthly. "This year" is the current calendar year (UTC).
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.kpi_overview` AS
SELECT
  (SELECT COUNT(*)                                         FROM `agora-data-driven.client_tcs.quiz_leads`) AS leads,
  (SELECT COUNTIF(is_converted)                            FROM `agora-data-driven.client_tcs.quiz_leads`) AS converted,
  (SELECT SAFE_DIVIDE(COUNTIF(is_converted), COUNT(*))     FROM `agora-data-driven.client_tcs.quiz_leads`) AS conversion_rate,
  (SELECT SUM(revenue_post_quiz)                           FROM `agora-data-driven.client_tcs.quiz_leads`) AS revenue,
  (SELECT AVG(IF(is_converted, days_to_convert, NULL))     FROM `agora-data-driven.client_tcs.quiz_leads`) AS avg_days_to_convert,
  (SELECT AVG(opens)                                       FROM `agora-data-driven.client_tcs.quiz_leads`) AS avg_opens,
  (SELECT AVG(clicks)                                      FROM `agora-data-driven.client_tcs.quiz_leads`) AS avg_clicks,
  (SELECT COUNTIF(cohort_year = EXTRACT(YEAR FROM CURRENT_DATE()))
     FROM `agora-data-driven.client_tcs.quiz_leads`) AS leads_this_year,
  (SELECT COUNTIF(cohort_year = EXTRACT(YEAR FROM CURRENT_DATE()) AND is_converted)
     FROM `agora-data-driven.client_tcs.quiz_leads`) AS converted_this_year,
  (SELECT SAFE_DIVIDE(SUM(opens), NULLIF(SUM(emails_sent), 0))
     FROM `agora-data-driven.client_tcs.engagement_monthly`
     WHERE EXTRACT(YEAR FROM month) = EXTRACT(YEAR FROM CURRENT_DATE())) AS open_rate_this_year,
  (SELECT SAFE_DIVIDE(SUM(opens), NULLIF(SUM(emails_sent), 0))
     FROM `agora-data-driven.client_tcs.engagement_monthly`
     WHERE EXTRACT(YEAR FROM month) < EXTRACT(YEAR FROM CURRENT_DATE())) AS open_rate_prior,
  (SELECT SAFE_DIVIDE(SUM(clicks), NULLIF(SUM(emails_sent), 0))
     FROM `agora-data-driven.client_tcs.engagement_monthly`
     WHERE EXTRACT(YEAR FROM month) = EXTRACT(YEAR FROM CURRENT_DATE())) AS click_rate_this_year,
  (SELECT SAFE_DIVIDE(SUM(clicks), NULLIF(SUM(emails_sent), 0))
     FROM `agora-data-driven.client_tcs.engagement_monthly`
     WHERE EXTRACT(YEAR FROM month) < EXTRACT(YEAR FROM CURRENT_DATE())) AS click_rate_prior;
