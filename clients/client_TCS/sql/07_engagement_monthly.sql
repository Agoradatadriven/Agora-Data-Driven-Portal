-- 07_engagement_monthly.sql -> view engagement_monthly
--
-- THE DIAGNOSTIC TIME SERIES. For every email sent to a quiz lead AT/AFTER their quiz, bucket
-- by calendar month and measure volume + open/click rates -- so the dashboard can show whether
-- quiz leads are opening/clicking LESS over time (and specifically this year). Split by whether
-- the lead ever converted, to see if engagement is the lever separating buyers from non-buyers.
-- Because stg_quiz is one row per email, each event counts once (no repeat-submission inflation).
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.engagement_monthly` AS
WITH lead_events AS (
  SELECT
    DATE_TRUNC(DATE(e.event_at), MONTH) AS month,
    q.email,
    e.is_open, e.is_click,
    COALESCE(c.is_converted, FALSE)     AS is_converted
  FROM `agora-data-driven.client_tcs.stg_quiz`         AS q
  JOIN `agora-data-driven.client_tcs.stg_email_events` AS e
    ON e.email = q.email AND e.event_at >= q.submitted_at
  LEFT JOIN `agora-data-driven.client_tcs.quiz_conversion` AS c ON c.email = q.email
)
SELECT
  month,
  COUNT(*)                                   AS emails_sent,
  COUNTIF(is_open)                           AS opens,
  COUNTIF(is_click)                          AS clicks,
  SAFE_DIVIDE(COUNTIF(is_open),  COUNT(*))   AS open_rate,
  SAFE_DIVIDE(COUNTIF(is_click), COUNT(*))   AS click_rate,
  COUNT(DISTINCT email)                      AS active_leads,
  SAFE_DIVIDE(COUNTIF(is_open AND is_converted),     NULLIF(COUNTIF(is_converted), 0))     AS converted_open_rate,
  SAFE_DIVIDE(COUNTIF(is_open AND NOT is_converted), NULLIF(COUNTIF(NOT is_converted), 0)) AS nonconverted_open_rate
FROM lead_events
GROUP BY month
ORDER BY month;
