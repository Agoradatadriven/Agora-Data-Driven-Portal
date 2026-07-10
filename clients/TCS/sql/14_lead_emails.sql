-- 14_lead_emails.sql -> view lead_emails
--
-- One row per (quiz lead, email received at/after their quiz): the send date, subject, and whether
-- they OPENED and/or CLICKED it. Powers the per-lead drill-down (click a lead -> see all their
-- emails). ~16k rows total, so the export embeds them all keyed by lead email. NOTE: open here is
-- the raw Klaviyo open (Apple Mail Privacy inflates opens in aggregate, but per-email "did it
-- register an open" is still useful to eyeball at the individual level).
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.lead_emails` AS
WITH lead AS (
  SELECT email, submitted_at FROM `agora-data-driven.client_tcs.stg_quiz`
)
SELECT
  e.email,
  e.event_at                                            AS sent_at,
  COALESCE(NULLIF(TRIM(e.subject), ""), "(no subject)") AS subject,
  e.is_open,
  e.is_click
FROM `agora-data-driven.client_tcs.stg_email_events` e
JOIN lead le ON le.email = e.email AND e.event_at >= le.submitted_at
ORDER BY e.email, e.event_at DESC;
