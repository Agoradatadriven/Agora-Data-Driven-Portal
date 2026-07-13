# -*- coding: utf-8 -*-
"""Agora internal dashboard — tab 1: Upwork job-demand analytics.

Serves the processed outputs of processing/process_upwork.py ONLY (never the
raw Telegram export): data/jobs.sqlite (+FTS) and data/aggregates.json.

Data resolution order:
  1. local DATA_DIR (default ./data — dev, or baked into the image)
  2. gs://$DATA_BUCKET/$DATA_PREFIX  -> downloaded once to /tmp at startup

Endpoints:
  GET /                 the dashboard (static, self-contained HTML)
  GET /api/aggregates   pre-baked unfiltered payload (first paint)
  GET /api/stats        filtered aggregates (weekly series, skills, momentum)
  GET /api/jobs         filtered + paginated table rows
  GET /healthz

This service is meant to be iframed anywhere: it sets
Content-Security-Policy: frame-ancestors * and no X-Frame-Options.
"""

import json
import os
import re
import sqlite3
import statistics
import threading
from flask import Flask, g, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
DATA_BUCKET = os.environ.get("DATA_BUCKET", "")
DATA_PREFIX = os.environ.get("DATA_PREFIX", "upwork")

app = Flask(__name__)

_dl_lock = threading.Lock()
_resolved_dir = None


def data_dir():
    """Local data dir, downloading from GCS once if not present locally."""
    global _resolved_dir
    if _resolved_dir:
        return _resolved_dir
    with _dl_lock:
        if _resolved_dir:
            return _resolved_dir
        if os.path.exists(os.path.join(DATA_DIR, "jobs.sqlite")):
            _resolved_dir = DATA_DIR
            return _resolved_dir
        if not DATA_BUCKET:
            raise RuntimeError("no local data and DATA_BUCKET unset")
        from google.cloud import storage  # lazy: not needed for local dev

        tmp = "/tmp/upwork_data"
        os.makedirs(tmp, exist_ok=True)
        bucket = storage.Client().bucket(DATA_BUCKET)
        for name in ("jobs.sqlite", "aggregates.json"):
            dest = os.path.join(tmp, name)
            if not os.path.exists(dest):
                # unique temp + atomic replace: concurrent workers can never
                # truncate a file another worker is already serving from
                part = "%s.part.%d" % (dest, os.getpid())
                bucket.blob("%s/%s" % (DATA_PREFIX, name)).download_to_filename(part)
                os.replace(part, dest)
        _resolved_dir = tmp
        return _resolved_dir


def db():
    if "db" not in g:
        conn = sqlite3.connect(os.path.join(data_dir(), "jobs.sqlite"))
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "PRAGMA query_only=1; PRAGMA mmap_size=1073741824; PRAGMA cache_size=-64000;")
        g.db = conn
    return g.db


@app.teardown_appcontext
def _close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.after_request
def _frame_friendly(resp):
    # embeddable anywhere, on purpose
    resp.headers["Content-Security-Policy"] = "frame-ancestors *"
    resp.headers.pop("X-Frame-Options", None)
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# --- filter plumbing ---------------------------------------------------------

def _fts_query(raw):
    """User text -> safe FTS5 MATCH string: quoted prefix terms, implicit AND."""
    terms = [t for t in re.split(r"\s+", raw.strip()) if t][:8]
    return " ".join('"%s"*' % t.replace('"', '""') for t in terms)


def build_where(args):
    """Shared by /api/stats and /api/jobs. Returns (sql_fragment, params)."""
    where, params = ["1=1"], []
    q = args.get("q", "").strip()
    if q:
        where.append("j.id IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)")
        params.append(_fts_query(q))
    tags = [t for t in args.get("tags", "").split(",") if t.strip()]
    if tags:
        where.append(
            "j.id IN (SELECT job_id FROM job_tags WHERE tag IN (%s))"
            % ",".join("?" * len(tags)))
        params.extend(tags)
    for col, key in (("category", "category"), ("country", "country"),
                     ("budget_type", "budget"), ("level", "level")):
        v = args.get(key, "").strip()
        if v:
            where.append("j.%s = ?" % col)
            params.append(v)
    if args.get("verified") == "1":
        where.append("j.verified = 1")
    ms = args.get("min_spent", "").strip()
    if ms:
        try:
            v = float(ms)
        except ValueError:
            v = None
        if v is not None:
            where.append("j.spent >= ?")
            params.append(v)
    d_from, d_to = args.get("from", "").strip(), args.get("to", "").strip()
    if d_from:
        where.append("j.date >= ?")
        params.append(d_from)
    if d_to:
        where.append("j.date <= ?")
        params.append(d_to + "T99")  # inclusive end-of-day
    return " AND ".join(where), params


# --- endpoints ---------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(HERE, "dashboard.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/aggregates")
def aggregates():
    with open(os.path.join(data_dir(), "aggregates.json"), encoding="utf-8") as fh:
        return app.response_class(fh.read(), mimetype="application/json")


_stats_cache = {}  # normalized filter querystring -> response json (data is immutable per deploy)


@app.get("/api/stats")
def stats():
    cache_key = "&".join(sorted(
        "%s=%s" % (k, v) for k, v in request.args.items() if k not in ("_",)))
    hit = _stats_cache.get(cache_key)
    if hit is not None:
        return app.response_class(hit, mimetype="application/json")

    where, params = build_where(request.args)
    conn = db()
    tags = [t for t in request.args.get("tags", "").split(",") if t.strip()]

    total = conn.execute("SELECT COUNT(*) n FROM jobs j WHERE " + where, params).fetchone()["n"]
    weekly = [dict(r) for r in conn.execute(
        "SELECT j.week, COUNT(*) n FROM jobs j WHERE %s GROUP BY j.week ORDER BY j.week"
        % where, params)]
    daily = [dict(r) for r in conn.execute(
        "SELECT substr(j.date,1,10) d, COUNT(*) n FROM jobs j WHERE %s GROUP BY d ORDER BY d"
        % where, params)]

    # per-tag comparison series + the tag-free context slice (the "all jobs"
    # line and the share denominator — same filters MINUS the tag condition)
    weekly_by_tag, daily_by_tag = {}, {}
    weekly_ctx, daily_ctx = None, None
    if tags:
        args_no_tags = {k: v for k, v in request.args.items() if k != "tags"}
        where_ctx, params_ctx = build_where(args_no_tags)
        weekly_ctx = [dict(r) for r in conn.execute(
            "SELECT j.week, COUNT(*) n FROM jobs j WHERE %s GROUP BY j.week ORDER BY j.week"
            % where_ctx, params_ctx)]
        daily_ctx = [dict(r) for r in conn.execute(
            "SELECT substr(j.date,1,10) d, COUNT(*) n FROM jobs j WHERE %s GROUP BY d ORDER BY d"
            % where_ctx, params_ctx)]
        rows = conn.execute(
            "SELECT t.tag, j.week, COUNT(*) n FROM job_tags t JOIN jobs j ON j.id=t.job_id"
            " WHERE t.tag IN (%s) AND %s GROUP BY t.tag, j.week"
            % (",".join("?" * len(tags)), where), tags + params)
        for r in rows:
            weekly_by_tag.setdefault(r["tag"], []).append([r["week"], r["n"]])
        rows = conn.execute(
            "SELECT t.tag, substr(j.date,1,10) d, COUNT(*) n"
            " FROM job_tags t JOIN jobs j ON j.id=t.job_id"
            " WHERE t.tag IN (%s) AND %s GROUP BY t.tag, d"
            % (",".join("?" * len(tags)), where), tags + params)
        for r in rows:
            daily_by_tag.setdefault(r["tag"], []).append([r["d"], r["n"]])

    top_skills = [dict(r) for r in conn.execute(
        "SELECT s.skill, COUNT(*) n FROM job_skills s JOIN jobs j ON j.id=s.job_id"
        " WHERE %s GROUP BY s.skill ORDER BY n DESC LIMIT 15" % where, params)]
    categories = [dict(r) for r in conn.execute(
        "SELECT j.category, COUNT(*) n FROM jobs j WHERE %s AND j.category IS NOT NULL"
        " GROUP BY j.category ORDER BY n DESC LIMIT 10" % where, params)]
    countries = [dict(r) for r in conn.execute(
        "SELECT j.country, COUNT(*) n FROM jobs j WHERE %s AND j.country IS NOT NULL"
        " GROUP BY j.country ORDER BY n DESC LIMIT 10" % where, params)]

    rates = [r["rate_min"] for r in conn.execute(
        "SELECT j.rate_min FROM jobs j WHERE %s AND j.rate_min IS NOT NULL" % where, params)]
    fixed = [r["fixed_budget"] for r in conn.execute(
        "SELECT j.fixed_budget FROM jobs j WHERE %s AND j.fixed_budget IS NOT NULL" % where, params)]

    # momentum: last 4 FULL weeks vs the 4 before, within this slice (the
    # final observed week is usually partial — excluded, like the KPI delta)
    momentum = []
    wk = [r["week"] for r in weekly]
    if len(wk) >= 9:
        recent0, prior0, cut = wk[-5], wk[-9], wk[-1]
        rows = conn.execute(
            "SELECT t.tag,"
            " SUM(CASE WHEN j.week >= ? AND j.week < ? THEN 1 ELSE 0 END) recent,"
            " SUM(CASE WHEN j.week >= ? AND j.week < ? THEN 1 ELSE 0 END) prior"
            " FROM job_tags t JOIN jobs j ON j.id=t.job_id WHERE %s"
            " GROUP BY t.tag HAVING recent + prior >= 10 ORDER BY recent DESC"
            % where, [recent0, cut, prior0, recent0] + params)
        for r in rows:
            pct = None
            if r["prior"]:
                pct = round(100.0 * (r["recent"] - r["prior"]) / r["prior"], 1)
            momentum.append({"tag": r["tag"], "recent": r["recent"],
                             "prior": r["prior"], "pct": pct})

    payload = json.dumps({
        "total": total,
        "weekly": weekly,
        "daily": daily,
        "weekly_ctx": weekly_ctx,
        "daily_ctx": daily_ctx,
        "weekly_by_tag": weekly_by_tag,
        "daily_by_tag": daily_by_tag,
        "top_skills": top_skills,
        "categories": categories,
        "countries": countries,
        "median_hourly": round(statistics.median(rates), 2) if rates else None,
        "median_fixed": round(statistics.median(fixed), 2) if fixed else None,
        "momentum": momentum,
    })
    if len(_stats_cache) < 256:
        _stats_cache[cache_key] = payload
    return app.response_class(payload, mimetype="application/json")


SORTS = {
    "date": "j.date DESC", "date_asc": "j.date ASC",
    "rate": "j.rate_max DESC NULLS LAST", "spent": "j.spent DESC NULLS LAST",
    "dups": "j.dup_count DESC",
}

JOB_COLS = ("id,date,title,url,category,budget_type,rate_min,rate_max,fixed_budget,"
            "level,contract_to_hire,skills,description,feed,country,rating,reviews,"
            "client_jobs,hire_rate,avg_rate,spent,verified,tags")


@app.get("/api/jobs")
def jobs():
    where, params = build_where(request.args)
    conn = db()
    try:
        page = max(1, int(request.args.get("page", 1)))
        per = min(100, max(10, int(request.args.get("per", 25))))
    except ValueError:
        page, per = 1, 25
    order = SORTS.get(request.args.get("sort", "date"), SORTS["date"])
    total = conn.execute("SELECT COUNT(*) n FROM jobs j WHERE " + where, params).fetchone()["n"]
    rows = conn.execute(
        "SELECT %s FROM jobs j WHERE %s ORDER BY %s LIMIT ? OFFSET ?"
        % (",".join("j." + c for c in JOB_COLS.split(",")), where, order),
        params + [per, (page - 1) * per])
    return jsonify({"total": total, "page": page, "per": per,
                    "rows": [dict(r) for r in rows]})


# with gunicorn --preload this runs ONCE in the master before workers fork,
# so a cold start does a single data download and every worker is born ready
if DATA_BUCKET:
    try:
        data_dir()
    except Exception as exc:  # data not uploaded yet: first request retries
        print("data preload failed (will retry per-request):", exc)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8082)), debug=False)
