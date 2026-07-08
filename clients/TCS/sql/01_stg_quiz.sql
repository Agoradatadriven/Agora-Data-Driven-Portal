-- 01_stg_quiz.sql -> view stg_quiz
--
-- NN_ prefix controls apply order: create_views.py applies files in filename order,
-- so this 01_ staging view is created BEFORE the models that read it.
--
-- Grain: ONE ROW PER LEAD (email), taken at their FIRST quiz submission. De-duping to
-- the first submission avoids double-counting people who took the quiz more than once
-- (the old notebook keyed per (email, submitted_at), which double-counted post-quiz
-- engagement for repeat submitters). Source: the direct-API mirror raw_windsor.tcs_quiz
-- (NOT Windsor -- the quiz sheet has no Windsor connector; see the ingest loader).
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.stg_quiz` AS
WITH ranked AS (
  SELECT
    LOWER(TRIM(email)) AS email,
    submitted_at,
    first_name, business_age, services, description, `current`, website, pain_points,
    ein, llc, bank_account, operating_agreement, trademark, refund_policy, terms,
    ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(email)) ORDER BY submitted_at) AS rn
  FROM `agora-data-driven.raw_windsor.tcs_quiz`
  WHERE email IS NOT NULL AND TRIM(email) != '' AND submitted_at IS NOT NULL
)
SELECT
  email,
  submitted_at,
  DATE(submitted_at)                    AS submitted_date,
  EXTRACT(YEAR FROM submitted_at)       AS cohort_year,
  DATE_TRUNC(DATE(submitted_at), MONTH) AS cohort_month,
  FORMAT('%d-Q%d', EXTRACT(YEAR FROM submitted_at), EXTRACT(QUARTER FROM submitted_at)) AS cohort_quarter,
  first_name, business_age, services, description, `current`, website, pain_points,
  ein, llc, bank_account, operating_agreement, trademark, refund_policy, terms
FROM ranked
WHERE rn = 1;
