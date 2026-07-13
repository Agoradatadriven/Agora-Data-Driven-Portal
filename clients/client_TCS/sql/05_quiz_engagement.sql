-- 05_quiz_engagement.sql -> view quiz_engagement
--
-- Per lead (email), summarize the email engagement that happened AT/AFTER their quiz
-- submission (the "nurture" window). Counts sends/opens/clicks and the derived rates -- the
-- per-lead equivalent of the old nurture_open_count / nurture_click_count.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.quiz_engagement` AS
SELECT
  q.email,
  COUNT(*)                                   AS emails_sent,
  COUNTIF(e.is_open)                         AS opens,
  COUNTIF(e.is_click)                        AS clicks,
  SAFE_DIVIDE(COUNTIF(e.is_open),  COUNT(*)) AS open_rate,
  SAFE_DIVIDE(COUNTIF(e.is_click), COUNT(*)) AS click_rate,
  MAX(IF(e.is_open,  e.event_at, NULL))      AS last_open_at,
  MAX(IF(e.is_click, e.event_at, NULL))      AS last_click_at
FROM `agora-data-driven.client_tcs.stg_quiz`         AS q
JOIN `agora-data-driven.client_tcs.stg_email_events` AS e
  ON e.email = q.email
 AND e.event_at >= q.submitted_at
GROUP BY q.email;
