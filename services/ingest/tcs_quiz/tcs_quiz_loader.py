"""TCS Business-Quiz loader (DIRECT-API, not Windsor).

Raw target : raw_windsor.tcs_quiz  (the shared raw layer; project agora-data-driven,
             dataset raw_windsor, location asia-southeast1).
Source     : the Business-Quiz Google Sheet (Typeform archive + live Paperform tab).
Cadence    : daily scheduled pull (see tools/deploy_ingest_jobs.ps1).

The quiz is the ENTRY POINT of the funnel the dashboard diagnoses: one row per quiz
submission (email + submitted_at + answers). Ported from the "Business Quiz" section of
clients/TCS/archive_code/analytics.py -- it stacks the two form tabs into one normalized
frame. Direct-API (Google Sheets), not Windsor -- the sheet is the current source of
record and Windsor has no connector for it.

Auth:
  * Google Sheets/Drive read via ADC (google.auth.default with read-only scopes). The
    sheet MUST be shared with the runtime SA (ingest-runner@agora-data-driven.iam...).
  * BigQuery via ADC.
"""

import os
from typing import Any, Dict, List, Optional

import gspread
import pandas as pd
from google.auth import default as google_auth_default
from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
LOCATION = "asia-southeast1"
TABLE = "tcs_quiz"

SHEET_ID = os.environ.get("QUIZ_SHEET_ID", "1wgjZm_SjgilEAUP8lplLKpkJTPMsbPkH7ypuB5NAn2I")
PAPERFORM_TAB = os.environ.get("QUIZ_PAPERFORM_TAB", "[Automatic] Business Quiz")
TYPEFORM_TAB = os.environ.get("QUIZ_TYPEFORM_TAB", "[Archive] Business Quiz Typeform")

SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Setup features encoded from Paperform's multi-value "Set up" column, and the Yes/No
# columns present as-is in the Typeform tab. (Ported verbatim from analytics.py.)
SETUP_FEATURES = ["EIN", "LLC or Corporation", "Business Bank Account",
                  "Operating Agreement", "Trademark Filings", "Refund Policy",
                  "Terms and Conditions"]

# Typeform's verbose headers -> the shared names Paperform already uses (so concat aligns).
TYPEFORM_RENAME = {
    "First things first: what's your *name*?": "first name?",
    "{{field:06b3d088-f567-4d90-b0ff-4a7b6ccfac73}}, how long have you been running your business? ": "Business Age",
    "Please provide a brief description of the products and/or services you are planning to offer!": "Description",
    "How do you currently handle the *client contracts* required for your business?": "Current",
    "What is the website link or social media handle for your business?..": "Website",
    "Got it! Enter your *email address* and we will send you your recommendations within ~1 business day": "Email",
    "Are there specific concerns you have for your business?": "Pain Points",
}

# Final canonical (snake_case) column -> the merged-frame source column.
CANONICAL = {
    "email": "Email",
    "submitted_at": "Submitted At",
    "first_name": "first name?",
    "business_age": "Business Age",
    "services": "Services",
    "description": "Description",
    "current": "Current",
    "website": "Website",
    "pain_points": "Pain Points",
    "ein": "EIN",
    "llc": "LLC or Corporation",
    "bank_account": "Business Bank Account",
    "operating_agreement": "Operating Agreement",
    "trademark": "Trademark Filings",
    "refund_policy": "Refund Policy",
    "terms": "Terms and Conditions",
}
FLAG_COLS = ["ein", "llc", "bank_account", "operating_agreement",
             "trademark", "refund_policy", "terms"]


def _open_sheet():
    creds, _ = google_auth_default(scopes=SHEETS_SCOPES)
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def _tab_df(sheet, title: str) -> pd.DataFrame:
    ws = sheet.worksheet(title)
    # get_all_values (not get_all_records): the sheets have duplicate / blank header cells,
    # which get_all_records rejects. We read raw rows and build the frame ourselves, de-duping
    # and back-filling blank headers so pandas is happy while preserving the real column names
    # the CANONICAL / TYPEFORM_RENAME maps key off.
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return pd.DataFrame()
    header, seen, cols = values[0], {}, []
    for i, h in enumerate(header):
        h = (str(h).strip()) or ("col_%d" % i)
        if h in seen:
            seen[h] += 1
            h = "%s__%d" % (h, seen[h])
        else:
            seen[h] = 0
        cols.append(h)
    df = pd.DataFrame(values[1:], columns=cols)
    # blank strings -> NA so all-empty rows drop cleanly
    df = df.replace("", pd.NA).dropna(how="all").reset_index(drop=True)
    return df.loc[:, [c for c in df.columns if c and "Unnamed" not in str(c)]]


def _prep_paperform(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    # One-hot the multi-value "Set up" column into the setup features (contains-match).
    if "Set up" in df.columns:
        for feat in SETUP_FEATURES:
            df[feat] = df["Set up"].astype(str).str.contains(feat, na=False).astype(int)
        df = df.drop(columns=["Set up"])
    return df


def _prep_typeform(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.rename(columns=TYPEFORM_RENAME)
    # Yes / No-Unsure -> 1 / 0 for the setup flags.
    for col in SETUP_FEATURES:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: 1 if str(x).strip() == "Yes" else 0)
    return df


def build_frame() -> pd.DataFrame:
    sheet = _open_sheet()
    paperform = _prep_paperform(_tab_df(sheet, PAPERFORM_TAB))
    typeform = _prep_typeform(_tab_df(sheet, TYPEFORM_TAB))
    merged = pd.concat([paperform, typeform], ignore_index=True)

    # Project onto the canonical schema (missing source columns -> NA).
    out = pd.DataFrame()
    for canon, src in CANONICAL.items():
        out[canon] = merged[src] if src in merged.columns else pd.NA

    out["email"] = out["email"].astype(str).str.lower().str.strip()
    out["submitted_at"] = pd.to_datetime(out["submitted_at"], errors="coerce", utc=True)
    out = out[(out["email"].notna()) & (out["email"] != "nan") & (out["email"] != "")]
    out = out.dropna(subset=["submitted_at"])
    return out


def to_rows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        row: Dict[str, Any] = {"email": r["email"],
                               "submitted_at": r["submitted_at"].isoformat()}
        for col in CANONICAL:
            if col in ("email", "submitted_at"):
                continue
            val = r[col]
            if pd.isna(val):
                row[col] = None
            elif col in FLAG_COLS:
                row[col] = int(val)
            else:
                row[col] = str(val)
        rows.append(row)
    return rows


def load_rows(bq: bigquery.Client, rows: List[Dict[str, Any]]) -> None:
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    bq.load_table_from_json(rows, table_id, job_config=job_config).result()
    print(f"[OK] loaded {len(rows)} rows into {table_id}")


def main() -> None:
    df = build_frame()
    print(f"[tcs_quiz] normalized {len(df)} quiz submissions.")
    rows = to_rows(df)
    if not rows:
        print("[tcs_quiz] no quiz rows; leaving table unchanged.")
        return
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    load_rows(bq, rows)


if __name__ == "__main__":
    main()
