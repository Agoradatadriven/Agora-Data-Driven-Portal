-- 03_stg_email_events.sql -> view stg_email_events
--
-- Typed per-recipient email events from the direct-API mirror raw_windsor.tcs_klaviyo_events
-- (one row per SEND, flagged is_open / is_click). This is the grain the diagnostic needs to
-- ask "are THESE quiz leads opening/clicking less?". event_at is the send timestamp -- the
-- reference point for "post-quiz" engagement downstream.
--
-- DE-DUPE: the loader appends incrementally (newest-first backfill + forward catch-up), so a
-- send could in principle be pulled twice across runs. event_id is the Klaviyo event id, so
-- ROW_NUMBER over it keeps exactly one row per send (a re-run of any window is harmless).
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.stg_email_events` AS
SELECT
  email, message_id, subject, campaign, flow, event_at, is_open, is_click
FROM (
  SELECT
    LOWER(TRIM(email))        AS email,
    message_id, subject, campaign, flow,
    sent_at                   AS event_at,
    COALESCE(is_open,  FALSE) AS is_open,
    COALESCE(is_click, FALSE) AS is_click,
    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY sent_at) AS _rn
  FROM `agora-data-driven.raw_windsor.tcs_klaviyo_events`
  WHERE email IS NOT NULL AND sent_at IS NOT NULL
)
WHERE _rn = 1;
