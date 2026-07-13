# -*- coding: utf-8 -*-
"""Score Upwork jobs 0-100 for Agora fit with Gemini 2.5 Flash-Lite on Vertex AI.

Reads jobs from dash/data/jobs.sqlite, writes scores to dash/data/job_scores.sqlite
(SEPARATE file keyed by job URL -- process_upwork.py rebuilds jobs.sqlite from scratch,
so scores must live outside it; URLs are the stable key across rebuilds).

The system prompt is agora_job_fit_brief.md (edit that file to change judging) plus the
scoring rubric below. One job per request, JSON-schema output, thinking off. Resumable:
already-scored URLs are skipped, so re-running after a crash or a new export only does
the missing ones.

Auth mirrors intel_ai.py: VERTEX_ACCESS_TOKEN env if set, else `gcloud auth
print-access-token` (refreshed automatically on 401).

Usage:
  python score_jobs.py --smoke          # 3 jobs, prints full results
  python score_jobs.py --limit 500      # pilot (seeded random sample)
  python score_jobs.py --all            # every unscored job
  python score_jobs.py --report         # distribution + samples from what's scored
"""

import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DB = os.path.join(HERE, "..", "dash", "data", "jobs.sqlite")
SCORES_DB = os.path.join(HERE, "..", "dash", "data", "job_scores.sqlite")
BRIEF_PATH = os.path.join(HERE, "agora_job_fit_brief.md")

MODEL = "gemini-2.5-flash-lite"
PROJECT = os.environ.get("VERTEX_PROJECT") or "agora-data-driven"
LOCATION = os.environ.get("VERTEX_LOCATION", "global")
PRICE_IN, PRICE_OUT = 0.10, 0.40  # USD per 1M tokens, sync tier (batch would be half)

RUBRIC = """
You score Upwork job posts for fit with Agora Data Driven (the agency described above).
Score 0-100. Judge the WORK BEING ASKED FOR against Agora's services. Remember the core
rule: breadth is an advantage -- never penalize a narrow/single-discipline ask.

Score bands:
- 90-100: The ask is squarely one or more of Agora's services (paid media/media buying,
  ad creative/UGC/copywriting, email/lifecycle/CRM, SEO/organic, funnels/lead gen,
  analytics/dashboards/tracking, data engineering/BigQuery/pipelines, automation/AI
  systems, market research, web/landing-page design) with no red flags. Ongoing or
  retainer-shaped work sits at the top of the band.
- 75-89: Core-service ask with minor friction: tiny one-off task, vague scope, a
  mandated no-code-only stack (Zapier/Make/n8n), or "individual freelancer preferred"
  wording.
- 50-74: Partial overlap: Agora could deliver it, but the core of the job is adjacent
  to (not inside) its services -- e.g. a software build with a marketing component, VA
  work with some marketing tasks, a Shopify store build focused on catalog setup.
- 25-49: Mostly outside services OR a significant red flag applies.
- 0-24: No meaningful overlap (e.g. pure mobile/game development, hardware, blockchain
  protocol work, translation, legal/medical/accounting practice work).

Red flags (these, not narrowness, are what lower a score):
- Requires on-site presence, a specific country of residence, or local field work
  (Agora is remote/offshore, Philippines-based).
- It is employment/staffing (W2, full-time employee placement) rather than
  client/agency work.
- Requires professional licensure Agora doesn't hold (law, medicine, accounting).
- Explicit "no agencies" requirements.
- The work itself is outside every Agora service.
Budget signals: a clearly unworkable budget for the scope (e.g. $3/hr for senior data
engineering) is minor friction, not a disqualifier. Do not chase client history stats;
they are context only.

Return ONLY JSON: {"score": <integer 0-100>, "reason": "<1-2 specific sentences naming
which Agora service(s) the job maps to, or why it doesn't fit>"}
"""

_token_lock = threading.Lock()
_token = {"value": "", "ts": 0.0}


def get_token(force=False):
    with _token_lock:
        if not force and _token["value"] and time.time() - _token["ts"] < 2700:
            return _token["value"]
        env = os.environ.get("VERTEX_ACCESS_TOKEN", "").strip()
        if env and not force:
            _token.update(value=env, ts=time.time())
            return env
        out = subprocess.run("gcloud auth print-access-token", shell=True,
                             capture_output=True, text=True, timeout=60)
        tok = (out.stdout or "").strip()
        if not tok:
            raise RuntimeError("no GCP token: set VERTEX_ACCESS_TOKEN or run gcloud auth login "
                               "(stderr: %s)" % (out.stderr or "").strip()[:200])
        _token.update(value=tok, ts=time.time())
        return tok


def vertex_url():
    host = ("aiplatform.googleapis.com" if LOCATION == "global"
            else "%s-aiplatform.googleapis.com" % LOCATION)
    return ("https://%s/v1/projects/%s/locations/%s/publishers/google/models/%s:generateContent"
            % (host, PROJECT, LOCATION, MODEL))


def job_text(row):
    (url, date, title, category, budget_type, rate_min, rate_max, fixed_budget,
     level, skills, description, country, rating, reviews, client_jobs,
     hire_rate, avg_rate, spent, verified) = row
    if budget_type == "hourly":
        lo = "$%g" % rate_min if rate_min else "?"
        hi = "$%g" % rate_max if rate_max else "?"
        budget = "Hourly %s-%s" % (lo, hi)
    elif fixed_budget:
        budget = "Fixed $%g" % fixed_budget
    else:
        budget = "Not stated"
    client_bits = []
    if country:
        client_bits.append(country)
    if rating:
        client_bits.append("rating %.1f (%s reviews)" % (rating, reviews or 0))
    if hire_rate:
        client_bits.append("%d%% hire rate" % hire_rate)
    if spent:
        client_bits.append("$%s spent" % ("{:,.0f}".format(spent)))
    if verified:
        client_bits.append("payment verified")
    return "\n".join([
        "TITLE: %s" % (title or ""),
        "CATEGORY: %s" % (category or ""),
        "BUDGET: %s | LEVEL: %s" % (budget, level or "unspecified"),
        "SKILLS: %s" % (skills or ""),
        "CLIENT: %s" % (", ".join(client_bits) or "no info"),
        "POSTED: %s" % (date or "")[:10],
        "DESCRIPTION:",
        (description or "").strip(),
    ])


def score_one(system_prompt, text, session):
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": {
                "type": "OBJECT",
                "properties": {
                    "score": {"type": "INTEGER"},
                    "reason": {"type": "STRING"},
                },
                "required": ["score", "reason"],
            },
            "maxOutputTokens": 256,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    last_err = ""
    for attempt in range(5):
        try:
            r = session.post(vertex_url(), json=payload, timeout=90,
                             headers={"Authorization": "Bearer " + get_token(),
                                      "Content-Type": "application/json"})
        except Exception as exc:
            last_err = type(exc).__name__
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 401:
            get_token(force=True)
            continue
        if r.status_code in (429, 500, 503, 529):
            last_err = "HTTP %s" % r.status_code
            time.sleep(min(2 ** attempt * 2, 30))
            continue
        if r.status_code >= 400:
            raise RuntimeError("Vertex %s: %s" % (r.status_code, r.text[:300]))
        data = r.json()
        u = data.get("usageMetadata") or {}
        tin = int(u.get("promptTokenCount") or 0)
        tout = int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            raw = "".join(p.get("text", "") for p in parts).strip()
            obj = json.loads(raw)
            score = max(0, min(100, int(obj["score"])))
            reason = str(obj.get("reason", "")).strip()
            return score, reason, tin, tout
        except Exception as exc:
            last_err = "parse: %s" % type(exc).__name__
            time.sleep(1)
    raise RuntimeError("gave up after retries (%s)" % last_err)


def open_scores_db():
    db = sqlite3.connect(SCORES_DB)
    db.execute("""CREATE TABLE IF NOT EXISTS scores (
        url TEXT PRIMARY KEY, score INTEGER NOT NULL, reason TEXT,
        model TEXT, scored_at TEXT, in_tokens INTEGER, out_tokens INTEGER)""")
    return db


JOB_COLS = ("url,date,title,category,budget_type,rate_min,rate_max,fixed_budget,"
            "level,skills,description,country,rating,reviews,client_jobs,"
            "hire_rate,avg_rate,spent,verified")


def pick_jobs(limit, seed=42):
    jobs = sqlite3.connect(JOBS_DB)
    done = {u for (u,) in open_scores_db().execute("SELECT url FROM scores")}
    urls = [u for (u,) in jobs.execute("SELECT url FROM jobs") if u not in done]
    if limit and len(urls) > limit:
        rng = random.Random(seed)
        urls = rng.sample(urls, limit)
    rows = []
    for i in range(0, len(urls), 900):
        chunk = urls[i:i + 900]
        q = "SELECT %s FROM jobs WHERE url IN (%s)" % (JOB_COLS, ",".join("?" * len(chunk)))
        rows.extend(jobs.execute(q, chunk).fetchall())
    jobs.close()
    return rows


def run(rows, workers):
    system_prompt = open(BRIEF_PATH, encoding="utf-8").read() + "\n\n" + RUBRIC
    db = open_scores_db()
    db_lock = threading.Lock()
    tally = {"n": 0, "in": 0, "out": 0, "err": 0}
    t0 = time.time()
    session_local = threading.local()

    def sess():
        if not hasattr(session_local, "s"):
            session_local.s = requests.Session()
        return session_local.s

    def work(row):
        return row[0], score_one(system_prompt, job_text(row), sess())

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, row) for row in rows]
        for fut in as_completed(futures):
            try:
                url, (score, reason, tin, tout) = fut.result()
            except Exception as exc:
                tally["err"] += 1
                print("  ERROR: %s" % str(exc)[:200], flush=True)
                continue
            with db_lock:
                db.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?)",
                           (url, score, reason, MODEL,
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            tin, tout))
                tally["n"] += 1
                tally["in"] += tin
                tally["out"] += tout
                if tally["n"] % 50 == 0:
                    db.commit()
                    print("  %d/%d scored, %.0fs elapsed" %
                          (tally["n"], len(rows), time.time() - t0), flush=True)
    db.commit()
    cost = tally["in"] / 1e6 * PRICE_IN + tally["out"] / 1e6 * PRICE_OUT
    print("\nDone: %d scored, %d failed, %.0fs" % (tally["n"], tally["err"], time.time() - t0))
    print("Tokens: %s in / %s out -> $%.4f (sync tier)" %
          ("{:,}".format(tally["in"]), "{:,}".format(tally["out"]), cost))


def report():
    db = open_scores_db()
    n, = db.execute("SELECT COUNT(*) FROM scores").fetchone()
    if not n:
        print("no scores yet")
        return
    print("scored: %d   avg: %.1f" % (n, db.execute("SELECT AVG(score) FROM scores").fetchone()[0]))
    print("\nDistribution:")
    for lo in range(0, 100, 10):
        hi = lo + 9 if lo < 90 else 100
        c, = db.execute("SELECT COUNT(*) FROM scores WHERE score BETWEEN ? AND ?", (lo, hi)).fetchone()
        print("  %3d-%3d  %5d  %s" % (lo, hi, c, "#" * max(1, round(c * 60 / n)) if c else ""))
    jobs = sqlite3.connect(JOBS_DB)
    titles = {u: t for (u, t) in jobs.execute("SELECT url,title FROM jobs")}

    def show(order, label):
        print("\n%s:" % label)
        for url, score, reason in db.execute(
                "SELECT url,score,reason FROM scores ORDER BY score %s LIMIT 6" % order):
            t = (titles.get(url) or "?")[:70]
            print(("  [%3d] %s\n        %s" % (score, t, (reason or "")[:160]))
                  .encode("ascii", "replace").decode())
    show("DESC", "Highest")
    show("ASC", "Lowest")
    mid = db.execute("SELECT url,score,reason FROM scores WHERE score BETWEEN 45 AND 75 "
                     "ORDER BY RANDOM() LIMIT 6").fetchall()
    print("\nMid-band sample (45-75):")
    for url, score, reason in mid:
        t = (titles.get(url) or "?")[:70]
        print(("  [%3d] %s\n        %s" % (score, t, (reason or "")[:160]))
              .encode("ascii", "replace").decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="score 3 jobs, print everything")
    ap.add_argument("--limit", type=int, default=0, help="seeded random sample of N unscored jobs")
    ap.add_argument("--all", action="store_true", help="every unscored job")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        report()
        return
    if args.smoke:
        rows = pick_jobs(3, seed=7)
        system_prompt = open(BRIEF_PATH, encoding="utf-8").read() + "\n\n" + RUBRIC
        s = requests.Session()
        for row in rows:
            text = job_text(row)
            print("=" * 70)
            print(text[:400].encode("ascii", "replace").decode())
            score, reason, tin, tout = score_one(system_prompt, text, s)
            print("--> SCORE %d | %s | %d in / %d out tokens"
                  % (score, reason.encode("ascii", "replace").decode(), tin, tout))
        return
    limit = 0 if args.all else (args.limit or 500)
    rows = pick_jobs(limit)
    if not rows:
        print("nothing to score")
        return
    print("Scoring %d jobs with %s (%d workers)..." % (len(rows), MODEL, args.workers))
    run(rows, args.workers)
    report()


if __name__ == "__main__":
    main()
