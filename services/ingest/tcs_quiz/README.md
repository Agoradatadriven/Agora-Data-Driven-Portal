# TCS Business-Quiz loader (direct-API)

Reads the **Business-Quiz Google Sheet** (the live Paperform tab + the Typeform archive
tab), normalizes and stacks them into one frame, and loads it into the shared raw layer
table **`raw_windsor.tcs_quiz`** — one row per quiz submission. Scheduled writer of
`raw_windsor` (not self-gating).

> **Why direct-API, not Windsor:** the Google Sheet is the current source of record for
> the quiz and Windsor has no connector for it. Ported from the "Business Quiz" section of
> `clients/TCS/archive_code/analytics.py`.

## Prerequisite — share the sheet

The loader reads Sheets via ADC. **Share the sheet with the runtime SA**
(`ingest-runner@agora-data-driven.iam.gserviceaccount.com`, Viewer) and enable the Sheets
+ Drive APIs on the project. Sheet id + tab names are overridable via env
`QUIZ_SHEET_ID` / `QUIZ_PAPERFORM_TAB` / `QUIZ_TYPEFORM_TAB`.

## How to run

```powershell
.\.venv\Scripts\python.exe services\ingest\create_dataset.py
.\.venv\Scripts\python.exe services\ingest\tcs_quiz\create_tcs_quiz_table.py
.\.venv\Scripts\python.exe services\ingest\tcs_quiz\tcs_quiz_loader.py
```

In production: Cloud Run job `tcs-quiz-ingest`, deployed + scheduled by
[`tools/deploy_ingest_jobs.ps1`](../../../tools/deploy_ingest_jobs.ps1).

**Raw target:** `raw_windsor.tcs_quiz` (`email, submitted_at, first_name, business_age,
services, description, current, website, pain_points` + setup flags `ein, llc,
bank_account, operating_agreement, trademark, refund_policy, terms`).
