# -*- coding: utf-8 -*-
"""Stage 1+2 for the Agora (internal) Upwork-demand dashboard.

Raw Telegram export (result.json, the Zenfl Upwork Bot chat) is UNPROCESSED and
never visualized directly. This script is the process step:

    raw result.json  --stream-->  parse job posts  -->  dedupe by job URL
        -->  classify into demand tags  -->  dash/data/jobs.sqlite (+ FTS)
        -->  dash/data/aggregates.json (pre-baked charts for first paint)

The dashboard web service reads ONLY the outputs, mirroring the repo's
three-stage contract (sql -> job -> dash) until this ingestion gets the full
treatment (BigQuery views + export job).

Usage:
    python process_upwork.py [path\to\result.json]
Outputs land in  ../dash/data/  (gitignored).
"""

import json
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

try:
    import ijson
except ImportError:  # pragma: no cover
    sys.exit("pip install ijson first (streaming parse; the export is ~1 GB)")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RAW = os.path.join(HERE, "..", "raw_files", "result.json")
OUT_DIR = os.path.join(HERE, "..", "dash", "data")

BOT_NAME = "Zenfl Upwork Bot"
DESC_CAP = 6000  # chars; keeps pathological posts from bloating the DB

# ---------------------------------------------------------------------------
# Demand-tag taxonomy. Each tag is a hand-written regex run over
# "title \n skills \n description" lowercased. "Paid Media" is the umbrella
# for every paid channel plus generic media-buying language.
# ---------------------------------------------------------------------------

TAG_PATTERNS = {
    "Google Ads": r"google\s*ads?\b|google\s*adwords|\badwords\b|performance\s*max|\bpmax\b|google\s*shopping|youtube\s*ads?\b|google\s*display|search\s*ads\b|google\s*ad\s*grants",
    "Facebook Ads": r"facebook\s*ads?\b|meta\s*ads?\b|\bfb\s*ads?\b|facebook\s*advertis|instagram\s*ads?\b|facebook\s*pixel|meta\s*pixel|facebook\s*campaign|meta\s*advertis|advantage\+",
    "TikTok Ads": r"tiktok\s*ads?\b|tik\s*tok\s*ads?\b|tiktok\s*advertis|tiktok\s*campaign|spark\s*ads\b",
    "LinkedIn Ads": r"linkedin\s*ads?\b|linkedin\s*advertis|linkedin\s*campaign",
    "Paid Media (any)": r"paid\s*(media|ads?|social|search|traffic|advertis)|media\s*buy(er|ing)?\b|\bppc\b|performance\s*marketing|programmatic|\bdsp\b|ad\s*campaigns?\b|retargeting|remarketing|\broas\b|google\s*ads?\b|adwords|facebook\s*ads?\b|meta\s*ads?\b|instagram\s*ads?\b|tiktok\s*ads?\b|linkedin\s*ads?\b|youtube\s*ads?\b|snapchat\s*ads?\b|pinterest\s*ads?\b",
    "AI / Machine Learning": r"machine\s*learning|artificial\s*intelligence|\bai\b|\bml\b|chat\s*gpt|chatgpt|\bgpt-?\d|\bllms?\b|openai|anthropic|\bclaude\b|gemini|deep\s*learning|neural\s*network|computer\s*vision|\bnlp\b|natural\s*language|generative|ai\s*agents?|langchain|\brag\b|prompt\s*engineer|stable\s*diffusion|midjourney|hugging\s*face|fine-?tun(e|ing)|chatbots?\b|copilot",
    "Automation": r"automat(e|ion|ing|ed)|zapier|make\.com|\bintegromat\b|\bn8n\b|gohighlevel|go\s*high\s*level|\bghl\b|apps?\s*script|power\s*automate|webhooks?\b|api\s*integration|\brpa\b|workflow\s*(automation|builder)|airtable",
    "Social Media (organic)": r"social\s*media|community\s*manage|content\s*calendar|instagram|tiktok|threads\b|social\s*content|reels\b",
    "SEO": r"\bseo\b|search\s*engine\s*optimi|backlinks?\b|keyword\s*research|ahrefs|semrush|link\s*building|\bserps?\b|on-?page|off-?page|google\s*search\s*console|technical\s*seo",
    "Email Marketing": r"email\s*marketing|klaviyo|mailchimp|activecampaign|convertkit|email\s*campaign|email\s*automation|cold\s*email|email\s*flows?|newsletters?\b|email\s*sequence|\bdrip\b",
    "Data & Analytics": r"data\s*analy|data\s*scien|data\s*engineer|power\s*bi|tableau|looker|bigquery|\bsql\b|\betl\b|data\s*visuali|dashboards?\b|google\s*analytics|\bga4\b|data\s*studio|data\s*pipeline|\bexcel\b|google\s*sheets|spreadsheets?\b|business\s*intelligence",
    "Web / Landing Pages": r"wordpress|shopify|webflow|\bwix\b|landing\s*pages?\b|web\s*design|website\s*design|web\s*development|squarespace|elementor|clickfunnels|funnels?\b|unbounce|framer\b",
    "Video / Creative": r"video\s*edit|graphic\s*design|\bcanva\b|photoshop|premiere|after\s*effects|motion\s*graphics|\bugc\b|thumbnails?\b|short-?form|video\s*ads?\b|creatives?\b|illustrator\b|figma",
    "Copywriting / Content": r"copywrit|content\s*writ|blog\s*writ|ghostwrit|article\s*writ|script\s*writ|content\s*creat|copy\s*editing|proofread",
    "CRM / Sales Ops": r"hubspot|salesforce|\bcrm\b|pipedrive|\bzoho\b|lead\s*gen(eration)?\b|cold\s*call|appointment\s*sett|sales\s*funnel|outreach\b",
    "VA / Admin": r"virtual\s*assistant|data\s*entry|admin(istrative)?\s*(support|assistant|task)|executive\s*assistant|\bva\b",
    "Legal / Paralegal": r"paralegal|legal\s*(assistant|research|writing|admin|support|draft)|litigation|contract\s*(review|draft)|law\s*firm",
}

TAG_RE = {tag: re.compile(pat, re.IGNORECASE) for tag, pat in TAG_PATTERNS.items()}
TAG_NAMES = list(TAG_PATTERNS.keys())

# --- job-post field parsing -------------------------------------------------

MONEY = r"\$([\d,]+(?:\.\d+)?)"
RE_HOURLY_RANGE = re.compile(MONEY + r"\s*-\s*" + MONEY + r"\s*per\s*hour", re.IGNORECASE)
RE_HOURLY_ONE = re.compile(MONEY + r"\s*per\s*hour", re.IGNORECASE)
RE_FIXED = re.compile(r"^\s*" + MONEY + r"\s*$")
RE_RATING = re.compile(r"⭐️?\s*([\d.]+)")
RE_REVIEWS = re.compile(r"([\d,]+)\s+reviews?", re.IGNORECASE)
RE_NJOBS = re.compile(r"([\d,]+)\s+jobs?", re.IGNORECASE)
RE_HIRE = re.compile(r"Hire\s*rate\s*(\d+)%", re.IGNORECASE)
RE_AVG = re.compile(r"Average\s*rate\s*" + MONEY, re.IGNORECASE)
RE_SPENT = re.compile(r"Spent\s*" + MONEY + r"([KkMm]?)", re.IGNORECASE)


def _num(s):
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse_client_line(line):
    """'⭐️ 5 • 4 reviews • United Kingdom • 4 jobs • Hire rate 80% • Spent $780.00 • ✅ Payment verified'
    ... or just 'United Kingdom'. Segments vary; classify each one."""
    out = {"rating": None, "reviews": None, "country": None, "client_jobs": None,
           "hire_rate": None, "avg_rate": None, "spent": None, "verified": 0}
    for seg in (s.strip() for s in line.split("•")):
        if not seg:
            continue
        if "⭐" in seg:
            m = RE_RATING.search(seg)
            out["rating"] = _num(m.group(1)) if m else None
        elif RE_REVIEWS.search(seg):
            out["reviews"] = int(_num(RE_REVIEWS.search(seg).group(1)) or 0)
        elif RE_HIRE.search(seg):
            out["hire_rate"] = int(RE_HIRE.search(seg).group(1))
        elif RE_AVG.search(seg):
            out["avg_rate"] = _num(RE_AVG.search(seg).group(1))
        elif RE_SPENT.search(seg):
            m = RE_SPENT.search(seg)
            v = _num(m.group(1))
            if v is not None and m.group(2):
                v *= 1000.0 if m.group(2) in "Kk" else 1000000.0
            out["spent"] = v
        elif "payment verified" in seg.lower():
            out["verified"] = 1
        elif RE_NJOBS.search(seg) and "review" not in seg.lower():
            out["client_jobs"] = int(_num(RE_NJOBS.search(seg).group(1)) or 0)
        elif out["country"] is None and not any(ch.isdigit() for ch in seg):
            out["country"] = seg
    return out


def _parse_meta_segments(text, job):
    """Bullet segments that can appear in the header plain (no-rate layout) OR
    in the plain right after the bold rate: budget type, weekly hours,
    experience level, contract-to-hire."""
    for seg in (s.strip() for s in text.split("•")):
        low = seg.lower()
        if not seg:
            continue
        if low == "hourly":
            job["budget_type"] = job["budget_type"] or "Hourly"
        elif low == "fixed budget":
            job["budget_type"] = job["budget_type"] or "Fixed Budget"
        elif low.endswith("level"):
            job["level"] = seg[:-len("level")].strip() or job["level"]
        elif "contract to hire" in low:
            job["contract_to_hire"] = 1


def parse_job(msg):
    """One bot message (text_entities list) -> flat job dict, or None if it
    isn't a job post. Layout (see raw samples): title entities (bold, possibly
    split by links), a header plain '\\n\\nCategory • Hourly|Fixed Budget •
    [• hours][• X level][• project][• Contract to hire]', optional bold rate
    followed by the remaining meta segments, bold 'Skills' + plain list, bold
    'About Client' + plain line, bold 'Description' + blockquote,
    italic '📢 feed', italic '⏱️ ago'."""
    ents = msg.get("text_entities")
    if not isinstance(ents, list) or len(ents) < 4:
        return None

    job = {"title": None, "category": None, "budget_type": None,
           "rate_min": None, "rate_max": None, "fixed_budget": None,
           "level": None, "contract_to_hire": 0, "skills": [],
           "description": "", "feed": None, "url": None}
    client = {}
    saw_skills_header = False

    # the header is the first plain holding a bullet list after a blank line;
    # everything before it (bold + inline links) is the title
    hidx = None
    for i, ent in enumerate(ents):
        if ent.get("type") == "plain" and "\n\n" in ent.get("text", "") and "•" in ent.get("text", ""):
            hidx = i
            break
    if not hidx:  # None or 0: not a job post
        return None
    job["title"] = "".join(e.get("text", "") for e in ents[:hidx]).strip()
    header_segs = [s.strip() for s in ents[hidx]["text"].split("•")]
    job["category"] = header_segs[0] or None
    _parse_meta_segments("•".join(header_segs[1:]), job)

    for i in range(hidx + 1, len(ents)):
        ent = ents[i]
        etype, etext = ent.get("type"), ent.get("text", "")
        nxt = ents[i + 1].get("text", "") if i + 1 < len(ents) else ""
        if etype == "bold":
            low = etext.strip().lower()
            if low == "skills":
                saw_skills_header = True
                job["skills"] = [s.strip() for s in nxt.split("•") if s.strip()]
            elif low == "about client":
                client = parse_client_line(nxt)
            elif low == "description":
                pass  # description arrives as the blockquote entity
            elif "$" in etext and job["rate_min"] is None and job["fixed_budget"] is None:
                m = RE_HOURLY_RANGE.search(etext)
                if m:
                    job["rate_min"], job["rate_max"] = _num(m.group(1)), _num(m.group(2))
                else:
                    m = RE_HOURLY_ONE.search(etext)
                    if m:
                        job["rate_min"] = job["rate_max"] = _num(m.group(1))
                    else:
                        m = RE_FIXED.match(etext.strip())
                        if m:
                            job["fixed_budget"] = _num(m.group(1))
                _parse_meta_segments(nxt, job)
        elif etype == "blockquote" and not job["description"]:
            job["description"] = etext[:DESC_CAP]
        elif etype == "italic":
            if "📢" in etext:
                job["feed"] = etext.replace("📢", "").strip()

    if not (job["title"] and saw_skills_header):
        return None

    for row in msg.get("inline_bot_buttons") or []:
        for btn in row:
            if btn.get("type") == "url" and "upwork.com" in (btn.get("data") or ""):
                job["url"] = btn["data"]
                break
        if job["url"]:
            break
    if not job["url"]:
        return None

    job.update(client)
    job["date"] = msg.get("date", "")[:19]
    return job


def tags_for(job):
    hay = "{}\n{}\n{}".format(job["title"] or "", " • ".join(job["skills"]), job["description"])
    return [tag for tag, rx in TAG_RE.items() if rx.search(hay)]


# --- main pipeline -----------------------------------------------------------

SCHEMA = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    date TEXT NOT NULL,             -- ISO 'YYYY-MM-DDTHH:MM:SS' (first sighting)
    week TEXT NOT NULL,             -- ISO Monday 'YYYY-MM-DD'
    month TEXT NOT NULL,            -- 'YYYY-MM'
    title TEXT, category TEXT, budget_type TEXT,
    rate_min REAL, rate_max REAL, fixed_budget REAL,
    level TEXT, contract_to_hire INTEGER DEFAULT 0,
    skills TEXT,                    -- ' • ' joined, original casing
    description TEXT,
    feed TEXT,                      -- 📢 feed name(s), ' | ' joined
    country TEXT, rating REAL, reviews INTEGER, client_jobs INTEGER,
    hire_rate INTEGER, avg_rate REAL, spent REAL, verified INTEGER DEFAULT 0,
    tags TEXT,                      -- '|' joined demand tags
    dup_count INTEGER DEFAULT 1,    -- times this URL appeared in the export
    weight REAL NOT NULL DEFAULT 1  -- coverage-adjusted contribution (see calibrate)
);
CREATE TABLE job_skills (job_id INTEGER NOT NULL, skill TEXT NOT NULL);
CREATE TABLE job_tags (job_id INTEGER NOT NULL, tag TEXT NOT NULL);
"""

POST_INDEX = """
CREATE INDEX ix_jobs_date ON jobs(date);
CREATE INDEX ix_jobs_week ON jobs(week);
CREATE INDEX ix_skills_skill ON job_skills(skill);
CREATE INDEX ix_skills_job ON job_skills(job_id);
CREATE INDEX ix_tags_tag ON job_tags(tag);
CREATE INDEX ix_tags_job ON job_tags(job_id);
CREATE VIRTUAL TABLE jobs_fts USING fts5(
    title, skills, description, content='jobs', content_rowid='id'
);
INSERT INTO jobs_fts(rowid, title, skills, description)
    SELECT id, title, skills, description FROM jobs;
"""


def monday_of(iso_date):
    d = datetime.strptime(iso_date[:10], "%Y-%m-%d")
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def run(raw_path):
    os.makedirs(OUT_DIR, exist_ok=True)
    db_path = os.path.join(OUT_DIR, "jobs.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)

    seen = {}          # url -> rowid (for dup counting / feed merging)
    feeds_by_row = defaultdict(set)
    dup_counts = Counter()   # rowid -> extra sightings (applied after all inserts)
    n_msgs = n_jobs = n_dups = 0
    next_id = 1
    t0 = time.time()
    batch = []

    def flush():
        if batch:
            db.executemany(
                "INSERT INTO jobs (id,url,date,week,month,title,category,budget_type,"
                "rate_min,rate_max,fixed_budget,level,contract_to_hire,skills,description,"
                "feed,country,rating,reviews,client_jobs,hire_rate,avg_rate,spent,verified,tags)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            del batch[:]

    with open(raw_path, "rb") as f:
        for msg in ijson.items(f, "messages.item"):
            n_msgs += 1
            if n_msgs % 100000 == 0:
                flush()
                db.commit()
                print("  ... %dk messages, %d unique jobs, %.0fs" %
                      (n_msgs // 1000, n_jobs, time.time() - t0), flush=True)
            if msg.get("from") != BOT_NAME or msg.get("type") != "message":
                continue
            job = parse_job(msg)
            if job is None:
                continue
            url = job["url"]
            if url in seen:
                n_dups += 1
                rid = seen[url]
                dup_counts[rid] += 1
                if job["feed"]:
                    feeds_by_row[rid].add(job["feed"])
                continue
            rid = next_id
            next_id += 1
            seen[url] = rid
            n_jobs += 1
            if job["feed"]:
                feeds_by_row[rid].add(job["feed"])
            tags = tags_for(job)
            batch.append((
                rid, url, job["date"], monday_of(job["date"]), job["date"][:7],
                job["title"], job["category"], job["budget_type"],
                job["rate_min"], job["rate_max"], job["fixed_budget"],
                job["level"], job["contract_to_hire"],
                " • ".join(job["skills"]), job["description"],
                job["feed"] or "", job.get("country"), job.get("rating"),
                job.get("reviews"), job.get("client_jobs"), job.get("hire_rate"),
                job.get("avg_rate"), job.get("spent"), job.get("verified", 0),
                "|".join(tags)))
            db.executemany("INSERT INTO job_skills VALUES (?,?)",
                           [(rid, s) for s in dict.fromkeys(job["skills"])])
            db.executemany("INSERT INTO job_tags VALUES (?,?)", [(rid, t) for t in tags])

    flush()
    db.executemany("UPDATE jobs SET dup_count = dup_count + ? WHERE id=?",
                   [(n, rid) for rid, n in dup_counts.items()])
    # merge multi-feed names collected from duplicates
    for rid, feeds in feeds_by_row.items():
        if len(feeds) > 1:
            db.execute("UPDATE jobs SET feed=? WHERE id=?", (" | ".join(sorted(feeds)), rid))
    db.commit()
    print("indexing + FTS ...", flush=True)
    db.executescript(POST_INDEX)
    db.commit()

    calibrate(db)
    write_aggregates(db)
    db.execute("VACUUM")
    db.close()
    print("DONE: %d messages scanned, %d unique jobs (+%d duplicate sightings) in %.0fs"
          % (n_msgs, n_jobs, n_dups, time.time() - t0))
    print("  -> %s  (%.1f MB)" % (db_path, os.path.getsize(db_path) / 1e6))


# Zenfl's pipeline changed capacity at every major outage (verified against the
# chat audit trail — no feed-setting changes by Ian near any boundary):
# the July-2025 rebuild OVER-delivered ~3x, the October-2025 rebuild delivers
# ~half the original baseline. Raw counts are NOT comparable across these
# boundaries; the dashboard defaults to the current era, offers a "% of jobs"
# share mode for cross-era comparison, and bands every outage on the chart.
ERA_FROM = "2025-11-03"
OUTAGES = [  # >=12h offline periods (clustered), from the bot's own messages
    {"from": "2024-11-23", "to": "2025-03-30", "label": "no active feeds · Zenfl offline"},
    {"from": "2025-07-18", "to": "2025-07-26", "label": "Zenfl outage → came back over-delivering"},
    {"from": "2025-10-17", "to": "2025-10-28", "label": "Zenfl outage → coverage reset"},
]
FEED_EVENTS = [  # feed creations (real coverage additions — marked on the chart)
    {"date": "2025-03-30", "label": "SMMA feed created"},
    {"date": "2025-05-24", "label": "Paralegal feed created"},
    {"date": "2025-06-30", "label": "Bookkeeping feed created"},
    {"date": "2025-07-04", "label": "Automation feed created"},
    {"date": "2025-08-08", "label": "Data Science feed created"},
]

# --- coverage calibration (chain-linked, per feed stream) --------------------
# Zenfl's delivery capacity changed at the July and October 2025 outages.
# To make counts comparable, every job gets weight = 1/factor(feed, regime),
# where the factor is the pipeline's delivery level relative to the current
# (post-Oct) pipeline, measured per feed stream in windows ADJACENT to each
# boundary (chain-linking: a demand level can't jump 3x in two weeks, so the
# boundary jump isolates the pipeline change while real trends pass through).
R1_FROM, R2_FROM, R3_FROM = "2025-03-31", "2025-07-28", ERA_FROM
_WINDOWS = {                      # [from, to) week windows around boundaries
    "r1_late": ("2025-06-02", "2025-07-14"),
    "r2_early": ("2025-07-28", "2025-09-08"),
    "r2_late": ("2025-08-25", "2025-10-13"),
    "r3_early": ("2025-11-03", "2025-12-15"),
}
_MIN_LEVEL, _MIN_WEEKS = 5.0, 3   # below this a stream falls back to the global factor
_CAP_LO, _CAP_HI = 1.0 / 3, 6.0


def _window_level(counts, first_week, lo, hi):
    """Mean weekly count of one stream in [lo, hi), zero-filled from the
    stream's own first week (feed births mid-window handled)."""
    weeks = [w for w in counts["_all_weeks"] if lo <= w < hi and w >= first_week]
    if len(weeks) < _MIN_WEEKS:
        return None
    total = sum(counts.get(w, 0) for w in weeks)
    return total / float(len(weeks))


def calibrate(db):
    """Compute jobs.weight (coverage-adjusted count contribution)."""
    all_weeks = [r[0] for r in db.execute(
        "SELECT DISTINCT week FROM jobs ORDER BY week")]
    feeds = [r[0] for r in db.execute("SELECT DISTINCT feed FROM jobs")]

    def stream_counts(feed):
        c = {w: n for w, n in db.execute(
            "SELECT week, COUNT(*) FROM jobs WHERE feed=? GROUP BY week", (feed,))}
        c["_all_weeks"] = all_weeks
        return c

    def levels_of(counts, first_week):
        return {k: _window_level(counts, first_week, lo, hi)
                for k, (lo, hi) in _WINDOWS.items()}

    # global fallback factors from the all-jobs series
    g_counts = {w: n for w, n in db.execute(
        "SELECT week, COUNT(*) FROM jobs GROUP BY week")}
    g_counts["_all_weeks"] = all_weeks
    gl = levels_of(g_counts, "0000")
    g_f2 = (gl["r2_late"] / gl["r3_early"]) if gl["r2_late"] and gl["r3_early"] else 1.0
    g_jump = (gl["r2_early"] / gl["r1_late"]) if gl["r2_early"] and gl["r1_late"] else 1.0
    g_f1 = g_f2 / g_jump if g_jump else 1.0

    def clamp(f):
        return max(_CAP_LO, min(_CAP_HI, f))

    updates = []  # (weight, feed, week_lo, week_hi)
    print("calibration factors (feed: f1 pre-July, f2 boom; 1.0 = current coverage):")
    for feed in feeds:
        counts = stream_counts(feed)
        first = db.execute("SELECT MIN(week) FROM jobs WHERE feed=?", (feed,)).fetchone()[0]
        lv = levels_of(counts, first)
        ok = lambda a, b: (lv[a] or 0) >= _MIN_LEVEL and (lv[b] or 0) >= _MIN_LEVEL
        f2 = clamp(lv["r2_late"] / lv["r3_early"]) if ok("r2_late", "r3_early") else clamp(g_f2)
        jump = (lv["r2_early"] / lv["r1_late"]) if ok("r2_early", "r1_late") else g_jump
        f1 = clamp(f2 / jump) if jump else clamp(g_f1)
        print("  %-55r f1=%.2f f2=%.2f" % (feed[:52], f1, f2))
        updates.append((1.0 / f1, feed, R1_FROM, R2_FROM))
        updates.append((1.0 / f2, feed, R2_FROM, R3_FROM))
    db.execute("UPDATE jobs SET weight = 1.0")
    db.executemany(
        "UPDATE jobs SET weight=? WHERE feed=? AND week >= ? AND week < ?", updates)
    db.commit()
    print("  global fallbacks: f1=%.2f f2=%.2f" % (g_f1, g_f2))


def _agg_block(db, since):
    """One pre-baked stats payload; since=None -> all time, else week >= since."""
    jw = " AND j.week >= ?" if since else ""
    p = (since,) if since else ()
    q = lambda sql: db.execute(sql, p).fetchall()

    total, dmin, dmax = q("SELECT COUNT(*), MIN(j.date), MAX(j.date) FROM jobs j WHERE 1=1" + jw)[0]
    weekly = q("SELECT j.week, COUNT(*), ROUND(SUM(j.weight)) FROM jobs j WHERE 1=1%s"
               " GROUP BY j.week ORDER BY j.week" % jw)
    daily = q("SELECT substr(j.date,1,10) d, COUNT(*), ROUND(SUM(j.weight)) FROM jobs j WHERE 1=1%s"
              " GROUP BY d ORDER BY d" % jw)
    tag_totals = q("SELECT t.tag, COUNT(*) n FROM job_tags t JOIN jobs j ON j.id=t.job_id"
                   " WHERE 1=1%s GROUP BY t.tag ORDER BY n DESC" % jw)
    top_skills = q("SELECT s.skill, COUNT(*) n FROM job_skills s JOIN jobs j ON j.id=s.job_id"
                   " WHERE 1=1%s GROUP BY s.skill ORDER BY n DESC LIMIT 40" % jw)
    categories = q("SELECT j.category, COUNT(*) n FROM jobs j WHERE j.category IS NOT NULL%s"
                   " GROUP BY j.category ORDER BY n DESC LIMIT 20" % jw)
    countries = q("SELECT j.country, COUNT(*) n FROM jobs j WHERE j.country IS NOT NULL%s"
                  " GROUP BY j.country ORDER BY n DESC LIMIT 20" % jw)

    hourly = [r[0] for r in q("SELECT j.rate_min FROM jobs j WHERE j.rate_min IS NOT NULL" + jw)]
    med_rate = round(statistics.median(hourly), 2) if hourly else None
    fixed = [r[0] for r in q("SELECT j.fixed_budget FROM jobs j WHERE j.fixed_budget IS NOT NULL" + jw)]
    med_fixed = round(statistics.median(fixed), 2) if fixed else None

    # momentum: last 4 FULL weeks vs the slice's historical 4-week average,
    # on coverage-adjusted counts so pipeline eras compare fairly
    # (final week is usually partial — excluded, matching the KPI delta)
    momentum = []
    wk = [w for w, _, _ in weekly]
    if len(wk) >= 10:
        recent0, hist0 = wk[-5], wk[0]
        hist_weeks = len(wk) - 5
        for tag, r_n, h_n in db.execute(
                "SELECT t.tag,"
                " SUM(CASE WHEN j.week >= ? AND j.week < ? THEN j.weight ELSE 0 END),"
                " SUM(CASE WHEN j.week >= ? AND j.week < ? THEN j.weight ELSE 0 END)"
                " FROM job_tags t JOIN jobs j ON j.id=t.job_id GROUP BY t.tag",
                (recent0, wk[-1], hist0, recent0)):
            if (r_n or 0) + (h_n or 0) >= 10 and hist_weeks:
                hist4 = 4.0 * (h_n or 0) / hist_weeks
                pct = round(100.0 * (r_n - hist4) / hist4, 1) if hist4 else None
                momentum.append({"tag": tag, "recent": round(r_n),
                                 "hist": round(hist4), "pct": pct})

    return {
        "total_jobs": total, "date_min": dmin, "date_max": dmax,
        "median_hourly_min": med_rate, "median_fixed": med_fixed,
        "momentum": momentum,
        "weekly": [{"week": w, "n": n, "a": a} for w, n, a in weekly],
        "daily": [{"d": d, "n": n, "a": a} for d, n, a in daily],
        "tags": [{"tag": t, "n": n} for t, n in tag_totals],
        "top_skills": [{"skill": s, "n": n} for s, n in top_skills],
        "categories": [{"category": c, "n": n} for c, n in categories],
        "countries": [{"country": c, "n": n} for c, n in countries],
    }


def write_aggregates(db):
    """Pre-baked payloads for the dashboard's zero-scan first paint:
    all-time at the root (back-compat) + the comparable post-outage era."""
    agg = _agg_block(db, None)

    weekly_by_tag = defaultdict(dict)
    for tag, week, n in db.execute(
            "SELECT t.tag, j.week, COUNT(*) FROM job_tags t JOIN jobs j ON j.id=t.job_id"
            " GROUP BY t.tag, j.week"):
        weekly_by_tag[tag][week] = n
    feeds = db.execute(
        "SELECT feed, COUNT(*) n FROM jobs WHERE feed != '' GROUP BY feed ORDER BY n DESC LIMIT 30").fetchall()

    agg.update({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weekly_by_tag": {t: sorted(d.items()) for t, d in weekly_by_tag.items()},
        "feeds": [{"feed": f, "n": n} for f, n in feeds],
        "tag_names": TAG_NAMES,
        "era_from": ERA_FROM,
        "outages": OUTAGES,
        "events": FEED_EVENTS,
        "era": _agg_block(db, ERA_FROM),
        "mar31": _agg_block(db, "2025-03-31"),
    })
    out = os.path.join(OUT_DIR, "aggregates.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(agg, fh, ensure_ascii=False)
    print("  -> %s  (%.1f KB)" % (out, os.path.getsize(out) / 1e3))


if __name__ == "__main__":
    raw = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RAW
    print("processing", raw, flush=True)
    run(raw)
