"""Riverdance RV Resort export job (Cloud Run job) — Stage 2, LIVE Windsor pull.

Unlike the BigQuery-fed template job, Riverdance's data is a Meta (Facebook) ad account whose
rich fields (reach, link clicks, pixel-purchase bookings, purchase value, and the creative
images/copy) are NOT in the shared raw_windsor mirror. So this job pulls **straight from the
Windsor.ai connector API** on each scheduled run and assembles the private `riverdance.json`
consumed by the gated dash service — the "live and accurate" path.

Shape written to the bucket (matched BY NAME to dash/dashboard.html's DATA.*):
  { client, location, dates[], rows[] (per ad-per-day), creatives[] (with inlined image),
    campaign{}, source{}, logo, agora_logo }

Secrets/config:
  WINDSOR_API_KEY  — Windsor connector key (Secret Manager `riverdance-windsor-key`, mounted as env).
  GCS_BUCKET / DATA_OBJECT — output location (deploy script sets them; defaults derive from CLIENT).
  RIVERDANCE_LOCAL_OUT — if set, write the JSON to this local path instead of GCS (off-cloud test).

Never logs or persists the API key. The key lives only in Secret Manager + the job env.
"""
import base64
import json
import os
import re
import urllib.request

# --- Per-client derivation (change only CLIENT) ------------------------------
PROJECT = "agora-data-driven"
CLIENT = "riverdance"
BUCKET = os.environ.get("GCS_BUCKET", "agora-data-driven-%s-dash" % CLIENT)
DATA_OBJECT = os.environ.get("DATA_OBJECT", "%s.json" % CLIENT)

# --- Windsor connector config ------------------------------------------------
WINDSOR_URL = "https://connectors.windsor.ai/all"
WINDSOR_ACCOUNT = os.environ.get("WINDSOR_ACCOUNT", "facebook__921953393594856")
DATE_PRESET = os.environ.get("WINDSOR_DATE_PRESET", "last_180d")
# Demographic/geographic breakdowns are SEPARATE pulls (Meta won't return them alongside the ad/day
# rows, and rejects the 'omni' revenue field with a breakdown) — so these carry reach/engagement
# metrics only (spend/impressions/clicks/link_clicks), aggregated over the window. Conversions (8
# total) are far too sparse to split by demographic meaningfully.
AGE_GENDER_FIELDS = ["age", "gender", "spend", "impressions", "clicks", "link_clicks"]
REGION_FIELDS = ["region", "spend", "impressions", "clicks", "link_clicks"]
FIELDS = ",".join([
    "account_name", "ad_name", "adcontent", "adset_name", "campaign", "clicks", "cpp",
    "datasource", "date", "frequency", "impressions", "link_clicks", "reach", "source",
    "spend", "unique_actions_link_click", "action_values_omni_purchase",
    "actions_offsite_conversion_fb_pixel_purchase",
    "thumbnail_url", "image_url", "creative_id", "ad_id", "title", "body",
])
WINDSOR_SECRET = "riverdance-windsor-key"   # Secret Manager id (fallback when env not set)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _num(x):
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _api_key():
    """Windsor key from env (mounted secret) or, as a fallback, Secret Manager directly."""
    k = os.environ.get("WINDSOR_API_KEY", "").strip()
    if k:
        return k
    from google.cloud import secretmanager
    sm = secretmanager.SecretManagerServiceClient()
    name = "projects/%s/secrets/%s/versions/latest" % (PROJECT, WINDSOR_SECRET)
    return sm.access_secret_version(name={"name": name}).payload.data.decode("utf-8").strip()


def _fetch(api_key, fields, preset=None):
    """One Windsor `all` pull. `fields` may be a comma-string or list. Returns the data rows."""
    from urllib.parse import urlencode
    if isinstance(fields, (list, tuple)):
        fields = ",".join(fields)
    q = urlencode({"api_key": api_key, "date_preset": preset or DATE_PRESET,
                   "fields": fields, "select_accounts": WINDSOR_ACCOUNT})
    req = urllib.request.Request(WINDSOR_URL + "?" + q,
                                 headers={"User-Agent": "agora-riverdance/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return (payload["data"] if isinstance(payload, dict) and "data" in payload else payload) or []


def _fetch_windsor(api_key):
    return _fetch(api_key, FIELDS)


def _demo_row(r, dims):
    out = {"spend": round(_num(r.get("spend")), 2), "imps": int(_num(r.get("impressions"))),
           "clicks": int(_num(r.get("clicks"))), "lclk": int(_num(r.get("link_clicks")))}
    for d in dims:
        out[d] = r.get(d) or "Unknown"
    return out


def _fetch_demographics(api_key):
    """Age×gender + region breakdowns (aggregated over the window). Best-effort: a failed breakdown
    pull yields [] rather than failing the whole export."""
    demo = {"age_gender": [], "region": []}
    try:
        demo["age_gender"] = [_demo_row(r, ["age", "gender"]) for r in _fetch(api_key, AGE_GENDER_FIELDS)]
    except Exception as e:  # noqa: BLE001
        print("  age/gender breakdown skip: %s" % str(e)[:120])
    try:
        demo["region"] = [_demo_row(r, ["region"]) for r in _fetch(api_key, REGION_FIELDS)]
    except Exception as e:  # noqa: BLE001
        print("  region breakdown skip: %s" % str(e)[:120])
    return demo


def _fetch_image(url):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "image/jpeg")
        if data and ctype.startswith("image/") and len(data) < 1_500_000:
            return "data:%s;base64,%s" % (ctype, base64.b64encode(data).decode())
    except Exception as e:  # noqa: BLE001 — a dead CDN link must never fail the whole build
        print("  image fetch skip: %s" % str(e)[:80])
    return None


def _logos():
    """Read the bundled brand marks and return (riverdance_datauri, agora_datauri)."""
    logo = agora = None
    try:
        svg = open(os.path.join(_HERE, "assets", "riverdance.svg"), encoding="utf-8").read()
        m = re.search(r'href="(data:image/[^"]+)"', svg)
        if m:
            logo = m.group(1)
    except Exception as e:  # noqa: BLE001
        print("  riverdance logo skip: %s" % e)
    try:
        b = open(os.path.join(_HERE, "assets", "agora.png"), "rb").read()
        agora = "data:image/png;base64," + base64.b64encode(b).decode()
    except Exception as e:  # noqa: BLE001
        print("  agora logo skip: %s" % e)
    return logo, agora


def build(rows_in):
    rows = []
    for r in rows_in:
        rows.append({
            "date": r.get("date"), "ad": r.get("ad_name"),
            "spend": round(_num(r.get("spend")), 4),
            "imps": int(_num(r.get("impressions"))), "clicks": int(_num(r.get("clicks"))),
            "lclk": int(_num(r.get("link_clicks"))), "reach": int(_num(r.get("reach"))),
            "pur": _num(r.get("actions_offsite_conversion_fb_pixel_purchase")),
            "rev": round(_num(r.get("action_values_omni_purchase")), 2),
        })
    dates = sorted({r["date"] for r in rows if r["date"]})

    meta, order = {}, []
    for r in rows_in:
        name = r.get("ad_name")
        if name not in meta:
            order.append(name)
            typ = (name or "").split("_", 1)[0]
            lbl = (name or "").split("_", 1)[1] if "_" in (name or "") else name
            meta[name] = {"ad": name, "type": typ, "label": lbl,
                          "creative_id": r.get("creative_id"),
                          "title": r.get("title") or "", "body": r.get("body") or "",
                          "_img": r.get("image_url") or "", "_thumb": r.get("thumbnail_url") or ""}
    creatives = []
    for name in order:
        c = meta[name]
        img = _fetch_image(c["_img"]) or _fetch_image(c["_thumb"])
        c.pop("_img", None)
        c.pop("_thumb", None)
        c["img"] = img
        creatives.append(c)

    logo, agora = _logos()
    campaigns = sorted({r.get("campaign") for r in rows_in if r.get("campaign")})
    adsets = sorted({r.get("adset_name") for r in rows_in if r.get("adset_name")})
    account = rows_in[0].get("account_name") if rows_in else "Riverdance Ad Account"

    return {
        "client": "Riverdance RV Resort", "location": "Gypsum, CO",
        "dates": dates, "rows": rows, "creatives": creatives,
        "campaign": {"name": campaigns[0] if campaigns else "",
                     "adset": adsets[0] if adsets else "", "objective": "Conversions"},
        "source": {"platform": "Meta (Facebook)", "account": account,
                   "connector": "Windsor.ai", "date_preset": DATE_PRESET},
        "logo": logo, "agora_logo": agora,
    }


def main():
    key = _api_key()
    rows_in = _fetch_windsor(key)
    data = build(rows_in)
    data["demographics"] = _fetch_demographics(key)
    body = json.dumps(data, separators=(",", ":"))

    local_out = os.environ.get("RIVERDANCE_LOCAL_OUT")
    if local_out:
        with open(local_out, "w", encoding="utf-8") as fh:
            fh.write(body)
        print("[%s] wrote %s (%d rows, %d creatives, %d days, %d KB) — LOCAL"
              % (CLIENT, local_out, len(data["rows"]), len(data["creatives"]),
                 len(data["dates"]), len(body) // 1024))
        return

    from google.cloud import storage
    blob = storage.Client(project=PROJECT).bucket(BUCKET).blob(DATA_OBJECT)
    blob.cache_control = "no-store"
    blob.upload_from_string(body, content_type="application/json")
    print("[%s] uploaded gs://%s/%s (%d rows, %d creatives, %d days, %d KB)"
          % (CLIENT, BUCKET, DATA_OBJECT, len(data["rows"]), len(data["creatives"]),
             len(data["dates"]), len(body) // 1024))


if __name__ == "__main__":
    main()
