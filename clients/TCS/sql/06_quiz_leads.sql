-- 06_quiz_leads.sql -> view quiz_leads
--
-- The FACT view: one row per quiz lead, joining the quiz answers (stg_quiz) to conversion
-- (quiz_conversion) and post-quiz engagement (quiz_engagement). This is the client_tcs
-- equivalent of the old DASHBOARD_quiz_nurture_analysis, and the drill-down the dashboard
-- lists. Non-converters keep is_converted=FALSE with zeroed revenue/orders.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.quiz_leads` AS
SELECT
  q.email, q.submitted_at, q.submitted_date, q.cohort_year, q.cohort_month, q.cohort_quarter,
  q.first_name, q.business_age, q.services, q.description, q.`current`, q.website, q.pain_points,
  q.ein, q.llc, q.bank_account, q.operating_agreement, q.trademark, q.refund_policy, q.terms,
  COALESCE(c.is_converted, FALSE)                           AS is_converted,
  c.first_order_date,
  DATE_DIFF(DATE(c.first_order_date), q.submitted_date, DAY) AS days_to_convert,
  c.first_order_name, c.first_order_products, c.first_order_discount_code,
  COALESCE(c.revenue_post_quiz, 0.0)                        AS revenue_post_quiz,
  COALESCE(c.order_count_post_quiz, 0)                      AS order_count_post_quiz,
  COALESCE(e.emails_sent, 0)                               AS emails_sent,
  COALESCE(e.opens, 0)                                     AS opens,
  COALESCE(e.clicks, 0)                                    AS clicks,
  e.open_rate, e.click_rate, e.last_open_at, e.last_click_at
FROM `agora-data-driven.client_tcs.stg_quiz`            AS q
LEFT JOIN `agora-data-driven.client_tcs.quiz_conversion` AS c ON c.email = q.email
LEFT JOIN `agora-data-driven.client_tcs.quiz_engagement` AS e ON e.email = q.email;
