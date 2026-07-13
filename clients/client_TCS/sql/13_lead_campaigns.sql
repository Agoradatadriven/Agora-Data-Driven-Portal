-- 13_lead_campaigns.sql -> view lead_campaigns
--
-- Per email SUBJECT LINE sent to quiz leads (at/after their quiz): when it went out, how many
-- leads it reached, and its click rate among them. Powers the "Emails sent to leads" table so the
-- team can see which subject lines actually earn clicks. click_rate = clicked (binary per send) /
-- sends, same definition as everywhere else. Small-volume subjects (< 10 lead sends) are dropped
-- as noise; kept to the last 18 months.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.lead_campaigns` AS
WITH lead AS (
  SELECT email, submitted_at FROM `agora-data-driven.client_tcs.stg_quiz`
),
ev AS (
  SELECT
    COALESCE(NULLIF(TRIM(e.subject), ""), "(no subject)") AS subject,
    e.campaign, e.flow, DATE(e.event_at) AS d, e.is_click
  FROM `agora-data-driven.client_tcs.stg_email_events` e
  JOIN lead le ON le.email = e.email AND e.event_at >= le.submitted_at
  WHERE e.event_at >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL 18 MONTH))
)
SELECT
  subject,
  ANY_VALUE(campaign)                          AS campaign,
  ANY_VALUE(flow)                              AS flow,
  MAX(d)                                       AS last_sent,
  COUNT(*)                                     AS sends,
  COUNTIF(is_click)                            AS clicks,
  SAFE_DIVIDE(COUNTIF(is_click), COUNT(*))     AS click_rate
FROM ev
GROUP BY subject
HAVING sends >= 10
ORDER BY last_sent DESC, sends DESC;
