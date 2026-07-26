"""business.json -> dim_business (silver), via DuckDB.

The entity dimension the v1 ranker features need (ARCHITECTURE.md → Ranker
features): categories, lat/long, price tier. Scoped to the frozen metro (D17).
Single Parquet file — 14.5K rows, no time axis, so nothing to partition by.

Idempotent: overwrite-in-place, deterministic transform.

## What is deliberately NOT here (the spine, dimension edition)

`stars` and `review_count` are dump-time accumulating counters: a business's 2022
review_count includes the very review a 2016 training row predicts. They are
blocklisted as features (DATA.md), and the strongest way to enforce a blocklist is
to never materialize the column — so this job drops them on the floor. You cannot
leak what you did not ingest.

`is_open` IS ingested, because rerank needs it as a serving-time filter, and is
blocklisted as a *feature* (D13) — the one column here that discipline rather than
absence has to protect. `test_dim_business.py` asserts both halves.

The three columns that do become features are quasi-static identity attributes, and
each needs its one-line leakage argument (AGENTS.md):

  latitude/longitude  a business's location is fixed when it opens; it cannot vary
                      as a function of the review being predicted.
  categories          what kind of business it is, set at listing time; likewise
                      independent of any individual later review.
  price_tier          the weakest of the three — a venue can change price band over
                      years — accepted as quasi-static and recorded in D21.

All three are dump-time (Jan 2022) values applied to historical rows. That is not
*causal* leakage the way review_count is, but it is future state, and the
future-invariance test does not cover it: that test mutates events, and these are
not events. Chokepoint 4 (the blocklist schema test) is what guards this table.

Run: python -m sift.offline.dim_business
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from sift.config import BUSINESS_JSON, METRO_CITY, METRO_STATE, sql_path
from sift.offline.ingest import SILVER_DIR

DIM_BUSINESS = SILVER_DIR / "dim_business.parquet"

# Read `attributes` as JSON so one nested key can be plucked without declaring the
# whole ~40-key schema; `categories` is a comma-joined string (DATA.md).
_COLS = (
    "{'business_id':'VARCHAR','name':'VARCHAR','city':'VARCHAR','state':'VARCHAR',"
    "'latitude':'DOUBLE','longitude':'DOUBLE','is_open':'BIGINT',"
    "'categories':'VARCHAR','attributes':'JSON'}"
)

# Price lives at attributes.RestaurantsPriceRange2 and arrives three ways: a
# quoted digit ('2'), JSON null, or the *string* 'None' (2 rows in the metro).
# TRY_CAST collapses the last two to NULL instead of raising; the BETWEEN then
# drops anything outside the documented 1-4 band.
_PRICE = (
    "CASE WHEN TRY_CAST(json_extract_string(attributes, '$.RestaurantsPriceRange2') "
    "AS SMALLINT) BETWEEN 1 AND 4 "
    "THEN TRY_CAST(json_extract_string(attributes, '$.RestaurantsPriceRange2') "
    "AS SMALLINT) END"
)

# Comma-split, trim each token, drop empties; a NULL categories string becomes [].
_CATEGORIES = (
    "COALESCE(list_filter(list_transform(string_split(categories, ','), "
    "x -> trim(x)), x -> x <> ''), [])"
)


def build_dim_business(
    *,
    business_json: Path = BUSINESS_JSON,
    out_file: Path = DIM_BUSINESS,
    metro_city: str = METRO_CITY,
    metro_state: str = METRO_STATE,
) -> int:
    """Build the metro's business dimension. Returns row count."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
                SELECT
                    business_id,
                    name,
                    latitude,
                    longitude,
                    {_CATEGORIES}          AS categories,
                    {_PRICE}               AS price_tier,
                    CAST(is_open AS BOOLEAN) AS is_open
                FROM read_json({sql_path(business_json)}, format='newline_delimited',
                               columns={_COLS})
                WHERE trim(city) = ? AND trim(state) = ?
                ORDER BY business_id
            ) TO {sql_path(out_file)} (FORMAT PARQUET)
            """,
            [metro_city, metro_state],
        )
        row = con.execute(
            f"SELECT count(*) FROM read_parquet({sql_path(out_file)})"
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        con.close()


def main() -> None:
    n = build_dim_business()
    glob = sql_path(DIM_BUSINESS)
    con = duckdb.connect()
    try:
        print(f"dim_business rows: {n:,}  -> {DIM_BUSINESS}")
        stats = con.execute(
            f"""
            SELECT
                avg(CASE WHEN latitude IS NOT NULL THEN 1.0 ELSE 0 END),
                avg(CASE WHEN len(categories) > 0 THEN 1.0 ELSE 0 END),
                avg(CASE WHEN price_tier IS NOT NULL THEN 1.0 ELSE 0 END),
                avg(CASE WHEN is_open THEN 1.0 ELSE 0 END),
                avg(len(categories))
            FROM read_parquet({glob})
            """
        ).fetchone()
        assert stats is not None
        print("\ncoverage (non-null fraction):")
        print(f"  lat/long      {stats[0]:.3f}")
        print(f"  categories    {stats[1]:.3f}   (mean {stats[4]:.1f} per business)")
        print(f"  price_tier    {stats[2]:.3f}")
        print(f"  is_open=true  {stats[3]:.3f}   (serving filter only — D13)")
        print("\nprice tier distribution:")
        for tier, cnt in con.execute(
            f"SELECT price_tier, count(*) FROM read_parquet({glob}) "
            "GROUP BY price_tier ORDER BY price_tier NULLS LAST"
        ).fetchall():
            label = "unknown" if tier is None else "$" * int(tier)
            print(f"  {label:<8} {cnt:>7,}")
        print("\nmost common categories:")
        for cat, cnt in con.execute(
            "SELECT c, count(*) FROM "
            f"(SELECT unnest(categories) AS c FROM read_parquet({glob})) "
            "GROUP BY c ORDER BY 2 DESC, 1 LIMIT 10"
        ).fetchall():
            print(f"  {cat:<28} {cnt:>6,}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
