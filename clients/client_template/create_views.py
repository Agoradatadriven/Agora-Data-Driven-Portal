"""Apply this client's BigQuery views in dependency order.

Every ``sql/*.sql`` file in this client's ``sql/`` directory is a single
``CREATE OR REPLACE VIEW`` statement. They are applied in *filename order*:
the ``NN_`` numeric prefix encodes the dependency chain so that staging views
exist before the models/rollups that read them.

    01_stg_source.sql  -> view stg_source        (typed/filtered raw rows)
    02_model.sql       -> view daily_performance  (reads stg_source)
    03_kpi.sql         -> view kpi_overview       (reads daily_performance)

Because a downstream view cannot be created before the view it selects from,
the ``NN_`` prefix is the ONLY thing guaranteeing correct apply order. Never
rename a file in a way that breaks the numeric sort, and never reorder by hand.

Files are read as UTF-8 so any non-ASCII characters in SQL string filters
(e.g. a campaign name with an accented character) survive unchanged.

Views are applied through this script via the BigQuery client library against
project ``agora-data-driven`` in location ``asia-southeast1`` — never edited by
hand in the BigQuery console (the console copy would drift from the repo).
"""

import pathlib

from google.cloud import bigquery

PROJECT = "agora-data-driven"
LOCATION = "asia-southeast1"

# Directory holding this client's view definitions, resolved relative to this
# file so the script works regardless of the caller's current directory.
SQL_DIR = pathlib.Path(__file__).resolve().parent / "sql"


def main():
    bq = bigquery.Client(project=PROJECT, location=LOCATION)

    # sorted() over the filenames applies the NN_ ordering rule: stg_* (01_)
    # is created before the models (02_) that read it, and the KPI rollup
    # (03_) last. This is a plain lexicographic sort, which is why the prefix
    # is always zero-padded two digits.
    sql_files = sorted(SQL_DIR.glob("*.sql"))
    if not sql_files:
        raise SystemExit(f"[ERROR] no .sql files found in {SQL_DIR}")

    for sql_path in sql_files:
        # UTF-8 so non-ASCII in SQL filters survives round-tripping.
        ddl = sql_path.read_text(encoding="utf-8")
        bq.query(ddl, location=LOCATION).result()
        print(f"[OK] applied {sql_path.name}")


if __name__ == "__main__":
    main()
