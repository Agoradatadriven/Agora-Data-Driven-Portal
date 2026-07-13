# client_agora — Agora's own internal dashboard

Tab 1: **Upwork job-demand analytics** over the Zenfl Upwork Bot Telegram feed
archive. Built for spotting what services are in demand (Paid Media, AI/ML,
Automation, …) and for interns to browse real jobs (skills, description, link).

## Pipeline (raw is never visualized directly)

```
raw_files/result.json          Telegram Desktop export of the bot chat (~1 GB, gitignored-worthy)
   │  processing/process_upwork.py       (streams with ijson; ~11 min for 160k messages)
   ▼
dash/data/jobs.sqlite          one row per unique job URL + job_skills/job_tags + FTS5 index
dash/data/aggregates.json      pre-baked unfiltered payload (weekly series, momentum, top lists)
   │  dash/deploy_dash_agora.ps1
   ▼
gs://agora-data-driven-agora-dash/upwork/    (private; service downloads at startup)
Cloud Run service `agora-dash`               (asia-southeast1, SA agora-dash-web@)
```

- **Processor** parses each bot message's `text_entities` (title, category,
  budget/rate, level, skills, client stats, description, feed name, job URL),
  dedupes by URL, and classifies every job into demand tags
  (`TAG_PATTERNS` in `process_upwork.py` — edit there to add a tag, then re-run).
- **Dashboard** (`dash/main.py` + `dash/dashboard.html`) is a small Flask API
  (`/api/aggregates`, `/api/stats`, `/api/jobs`) + one self-contained HTML page:
  weekly demand chart with tag comparison, top skills/categories/countries,
  momentum (last 4 full weeks vs prior 4), and the filterable jobs table.
  Filters live in the URL, so a pre-filtered view can be linked/iframed.
- The service sets `Content-Security-Policy: frame-ancestors *` — it is meant
  to be **iframed anywhere** and carries no auth (aggregated public job posts).

## Known data artifact — the Oct 2025 cliff (investigated 2026-07-13)

Weekly volume fell ~5,800 → ~1,500 between 2025-10-13 and 2025-11-03. This is
**not market demand and not a feed-setting change** (the chat audit trail shows
no feed edits anywhere near the drop — the last change was creating the "Data
Science" feed on 2025-08-08, which caused the *rise*). Zenfl itself was down
2025-10-17 → 10-28 (it apologized and extended subscriptions 7 days) and came
back with a rebuilt pipeline: it stopped forwarding jobs without a posted rate
(~27% of volume → 0%) and delivers ~4× fewer matching jobs overall (hourly hit
hardest; low-volume niche feeds like Paralegal were unaffected; the "USA"
country-label variant also vanished — backend change). Rate/country/category
mixes are otherwise unchanged, so it is a coverage cut, not a filter.

The dashboard handles this three ways (all shipped 2026-07-13):

1. **Chain-linked coverage calibration** (`calibrate()` in the processor):
   every feed stream gets a delivery factor per pipeline era, measured in
   6-week windows adjacent to each outage boundary (a demand level cannot
   jump 3× in two weeks — the boundary jump isolates the pipeline change).
   Each job carries `weight = 1/factor`; summing weights instead of counting
   rows gives the **Comparable** series the chart shows by default (default
   range: since 2025-03-31). KPI baselines and Momentum use adjusted numbers.
   Raw counts stay one click away ("Raw count").
2. **"% of jobs" share mode** per tag — fully assumption-free comparability.
3. **Every outage banded + feed creations marked** on the chart, so any
   remaining step has a visible explanation.

## Refresh with a new Telegram export

1. Replace `raw_files/result.json` (Telegram Desktop → export chat → JSON).
2. `python processing/process_upwork.py`
3. `dash/deploy_dash_agora.ps1 -DataOnly` then restart the service (the
   command it prints), or run the full script to also rebuild the image.

Deploys run as `info@agoradatadriven.com` (the script sets
`CLOUDSDK_CORE_ACCOUNT`). Full pipeline: `dash/deploy_dash_agora.ps1`.

## Next (the real data-engineering treatment)

Automate the Telegram pull (Telethon/telegram-export on a schedule) and move
the processed store into BigQuery per the repo's three-stage contract
(`sql/` views → export job → dash), replacing the laptop-run processor.
