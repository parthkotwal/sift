"""Raw JSON -> canonical event table (silver), via DuckDB.

One normalized shape for every event source (DECISIONS.md D2):

    (user_id, business_id, event_type, ts, stars)

Downstream feature code queries this table and never touches Yelp's file
formats. The table is populated from reviews now; tips and check-ins fold in
later as pure ingest additions (same schema, new event_type) — the whole point
of a canonical table. Scoped to the frozen metro (D17); Parquet partitioned by
event year so point-in-time queries ("events before cutoff") skip whole years.

Idempotent: the output dir is rebuilt from scratch each run, and the transform
is deterministic, so re-running on the same dump yields the same table.

Run: python -m sift.offline.ingest
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb

from sift.config import (
    BUSINESS_JSON,
    DATA_DIR,
    METRO_CITY,
    METRO_STATE,
    REVIEW_JSON,
    sql_path,
)

SILVER_DIR = DATA_DIR / "silver"
EVENTS_DIR = SILVER_DIR / "events"

# Columns read from each raw file — naming them keeps DuckDB from scanning the
# heavy `text` field it doesn't need.
_BUSINESS_COLS = "{'business_id':'VARCHAR','city':'VARCHAR','state':'VARCHAR'}"
_REVIEW_COLS = (
    "{'user_id':'VARCHAR','business_id':'VARCHAR','stars':'DOUBLE','date':'VARCHAR'}"
)


def build_events(
    *,
    business_json: Path = BUSINESS_JSON,
    review_json: Path = REVIEW_JSON,
    out_dir: Path = EVENTS_DIR,
    metro_city: str = METRO_CITY,
    metro_state: str = METRO_STATE,
) -> int:
    """Build the canonical review-event table for one metro. Returns row count."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)

    # Paths are trusted internal/config values, so they are interpolated into the
    # SQL directly. (DuckDB mis-binds positional `?` across a COPY ... TO target;
    # only the city/state literals below stay bound parameters.)
    biz = sql_path(business_json)
    rev = sql_path(review_json)
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            CREATE TEMP TABLE metro_biz AS
            SELECT business_id
            FROM read_json({biz}, format='newline_delimited', columns={_BUSINESS_COLS})
            WHERE trim(city) = ? AND trim(state) = ?
            """,
            [metro_city, metro_state],
        )
        con.execute(
            f"""
            COPY (
                SELECT
                    r.user_id                              AS user_id,
                    r.business_id                          AS business_id,
                    'review'                               AS event_type,
                    CAST(r.date AS TIMESTAMP)              AS ts,
                    CAST(r.stars AS SMALLINT)              AS stars,
                    CAST(EXTRACT(year FROM CAST(r.date AS TIMESTAMP)) AS INTEGER) AS year
                FROM read_json({rev}, format='newline_delimited', columns={_REVIEW_COLS}) r
                SEMI JOIN metro_biz m ON r.business_id = m.business_id
            ) TO {sql_path(out_dir)} (FORMAT PARQUET, PARTITION_BY (year))
            """
        )
        row = con.execute(
            f"SELECT count(*) FROM read_parquet({sql_path(out_dir / '**' / '*.parquet')}, "
            "hive_partitioning=true)"
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        con.close()


def events_glob(out_dir: Path = EVENTS_DIR) -> str:
    """Glob pattern to read the whole partitioned table with `year` recovered."""
    return str(out_dir / "**" / "*.parquet")


def main() -> None:
    n = build_events()
    glob = sql_path(EVENTS_DIR / "**" / "*.parquet")
    con = duckdb.connect()
    try:
        print(f"canonical events written: {n:,}  -> {EVENTS_DIR}")
        print("\nby event_type:")
        for etype, cnt in con.execute(
            f"SELECT event_type, count(*) FROM read_parquet({glob}, hive_partitioning=true) "
            "GROUP BY event_type ORDER BY 2 DESC"
        ).fetchall():
            print(f"  {etype:<8} {cnt:>10,}")
        span = con.execute(
            f"SELECT min(ts), max(ts) FROM read_parquet({glob}, hive_partitioning=true)"
        ).fetchone()
        assert span is not None
        print(f"\nts range: {span[0]}  ->  {span[1]}")
        print("\nrows per partition year:")
        for year, cnt in con.execute(
            f"SELECT year, count(*) FROM read_parquet({glob}, hive_partitioning=true) "
            "GROUP BY year ORDER BY year"
        ).fetchall():
            print(f"  {year}  {cnt:>8,}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
