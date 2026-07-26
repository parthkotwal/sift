"""Historical materialisation: persist the entity state groups to Parquet (D23).

This is the offline half of "one definition, two materialisations". It runs the
state SQL from `sift.features.state` — the same text `sift.features.pit` inlines —
and writes one Parquet file per group. The online half (current values → Redis)
comes next and reads the last row per entity from these same tables.

## Why this stores whole history, not just pre-T

The store is a store: it holds every state row, back to the first event and forward
to the last. It is *not* filtered to the frozen split. Right-exclusivity is applied
by the reader (`q.ts > state.ts`), never by the writer, which is what lets one
materialisation serve a training row in 2013, an eval query as-of T, and a serving
query at any future "now" without rebuilding.

That means the artifact contains post-T state rows, and those rows encode holdout
events. They are unreachable through the read path — a query as-of T selects only
rows strictly before T — but the safety property lives in the ASOF join, so it is
the join that is tested (`test_pit_features.py`, `test_store_read.py`), not this job.

## Idempotency

Every group has a total sort key (`state.SORT_KEYS`), the transform is
deterministic, and the output is overwritten rather than appended, so re-running on
the same dump yields byte-identical Parquet — verified on the real dump for all
three groups.

That is *provable* rather than merely observed, because every stored column is an
integer: lat/long are accumulated as fixed-point BIGINTs (`state.GEO_SCALE`), and
integer addition is associative, so a parallel aggregation cannot change the result
whatever order it sums in. The earlier float encoding made this group the one
artifact that would not reproduce (ISSUES.md I18).

Run: python -m sift.store.materialize
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from sift.config import DATA_DIR, sql_path
from sift.features.state import GEO_SCALE, TIMELINE_GROUPS, state_query
from sift.offline.dim_business import DIM_BUSINESS
from sift.offline.ingest import EVENTS_DIR

STORE_DIR = DATA_DIR / "store"
HISTORICAL_DIR = STORE_DIR / "historical"


def state_path(group: str, out_dir: Path = HISTORICAL_DIR) -> Path:
    """Where one materialised state group lives."""
    return out_dir / f"{group}_state.parquet"


def _attach_sources(
    con: duckdb.DuckDBPyConnection, events_dir: Path, dim_file: Path
) -> None:
    """Expose the raw sources under the relation names the state SQL expects."""
    events = sql_path(events_dir / "**" / "*.parquet")
    con.execute(
        f"CREATE VIEW events AS SELECT * FROM read_parquet({events}, hive_partitioning=true)"
    )
    con.execute(
        f"CREATE VIEW dim_business AS SELECT * FROM read_parquet({sql_path(dim_file)})"
    )


def materialize_historical(
    *,
    events_dir: Path = EVENTS_DIR,
    dim_file: Path = DIM_BUSINESS,
    out_dir: Path = HISTORICAL_DIR,
    groups: tuple[str, ...] = TIMELINE_GROUPS,
) -> dict[str, int]:
    """Write each state group to Parquet. Returns {group: row_count}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    con = duckdb.connect()
    try:
        _attach_sources(con, events_dir, dim_file)
        for group in groups:
            out = state_path(group, out_dir)
            out.unlink(missing_ok=True)
            con.execute(
                f"COPY ({state_query(group, ordered=True)}) "
                f"TO {sql_path(out)} (FORMAT PARQUET)"
            )
            row = con.execute(
                f"SELECT count(*) FROM read_parquet({sql_path(out)})"
            ).fetchone()
            assert row is not None
            counts[group] = int(row[0])
    finally:
        con.close()
    return counts


def main() -> None:
    counts = materialize_historical()
    con = duckdb.connect()
    try:
        print(f"materialised to {HISTORICAL_DIR}\n")
        for group, n in counts.items():
            path = state_path(group)
            size_mb = path.stat().st_size / 1e6
            print(f"  {group:<16} {n:>10,} state rows   {size_mb:>7.1f} MB")

        # Every stage must be printable (AGENTS.md). One user's timeline is the
        # clearest possible illustration of what "state as-of every change" means.
        user_state = sql_path(state_path("user"))
        sample = con.execute(
            f"""
            SELECT user_id FROM read_parquet({user_state})
            GROUP BY user_id HAVING count(*) BETWEEN 4 AND 6
            ORDER BY user_id LIMIT 1
            """
        ).fetchone()
        if sample is not None:
            print("\nexample user timeline (id withheld — Yelp ToS):")
            rows = con.execute(
                f"""
                SELECT ts, cum_count, round(cum_sum / cum_count, 2) AS mean_stars,
                       round(cum_lat_e7 / ({GEO_SCALE}.0 * NULLIF(cum_geo_n, 0)), 4)
                           AS centroid_lat
                FROM read_parquet({user_state}) WHERE user_id = ? ORDER BY ts
                """,
                [sample[0]],
            ).fetchall()
            print(f"  {'ts':<21} {'cum_count':>9} {'mean_stars':>11} {'centroid_lat':>13}")
            for ts, cum_count, mean_stars, lat in rows:
                print(f"  {str(ts):<21} {cum_count:>9} {mean_stars:>11} {lat:>13}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
