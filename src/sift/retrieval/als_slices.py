"""Point-in-time ALS: one model per time slice, materialised as store state (D27).

## Why slices exist

The headline ALS artifact is fit on every interaction before the frozen split, so
its score for a pair *it was fit on* is not a prediction — it is a report that the
pair was observed. Measured on the real dump: observed pairs mean 0.4555 against
0.0047 for unobserved, and an observed pair outranks an unobserved one 98.7% of the
time. Handed to the ranker as a feature on pre-T training rows, where every positive
is an observed pair and every sampled negative is not, that is the label wearing a
disguise. The ranker would learn "high ALS score = positive", and at serving the
user's highest-scoring items are the businesses they have *already reviewed* rather
than the ones they will visit next.

So the score a training row sees must come from a model that never saw that row.
This module fits one ALS per yearly boundary `y` over interactions strictly before
`y`, and stores each model's factors as ordinary entity state timestamped `y`. The
store's existing right-exclusive ASOF join then does the slice selection for free: a
query at `t` matches the greatest boundary `< t`, so its score always comes from a
model trained only on data that predates it. Snapshotting an aggregate at partition
boundaries and reading it as-of is the same pattern the state groups already use —
the vectors are just a heavier payload.

## The costs, stated

**Staleness.** A row is scored by a model up to one year old, and serving (as-of T)
is scored by the 2018 slice while *retrieval itself* uses the full pre-T model. That
asymmetry is deliberate: one uniform right-exclusive rule for every state group beats
a special case that would buy a fresher score. Staleness is safe; leakage is not.

**Coverage.** Boundaries start at 2010 because 88.4% of pre-T reviews fall after it
and an ALS fit on the metro's first few thousand interactions is noise. Rows before
the first boundary get NULL, which is the honest answer — no model existed yet.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
from numpy.typing import NDArray

from sift.config import sql_path
from sift.offline.dim_business import DIM_BUSINESS
from sift.offline.ingest import EVENTS_DIR
from sift.retrieval.als import FACTORS, fit_factors
from sift.retrieval.interactions import build_interactions, load_interactions
from sift.store.materialize import HISTORICAL_DIR, state_path

# One boundary per year. A model at boundary y saw only events strictly before y.
SLICE_BOUNDARIES: tuple[date, ...] = tuple(date(y, 1, 1) for y in range(2010, 2019))

# The slices are store state, so they live in the store's layout and are named by
# the same `state_path` convention every other group uses.
ALS_STATE_DIR = HISTORICAL_DIR
USER_ALS_STATE = state_path("user_als", ALS_STATE_DIR)
ITEM_ALS_STATE = state_path("item_als", ALS_STATE_DIR)


def _slice_rows(
    con: duckdb.DuckDBPyConnection,
    ids: tuple[str, ...],
    factors: NDArray[np.float32],
    boundary: date,
    table: str,
    id_column: str,
) -> None:
    """Append one slice's factors to `table` as (id, ts, value) state rows.

    POSITIONAL JOIN, not `row_number()`: row *i* of `factors` belongs to `ids[i]`,
    and an unordered window over a parallel scan does not promise that (I22).

    `factors` is transposed before registering because DuckDB scans a 2-D ndarray as
    one column per *first-axis* entry: an (N, 64) array arrives as 64 rows of N
    columns, so selecting `column0..column63` silently yields 64 populated rows and
    NULL-filled vectors for everything after them. `als.write_factor_parquet` already
    documents this; the shape check below is the assertion that was missing there.
    """
    if factors.shape != (len(ids), FACTORS):
        raise ValueError(f"factor shape {factors.shape} does not match {len(ids)} ids")
    con.register(f"{table}_ids", np.asarray(ids))
    con.register(f"{table}_values", np.ascontiguousarray(factors.T))
    vector = ", ".join(f"f.column{i}" for i in range(FACTORS))
    con.execute(
        f"INSERT INTO {table} SELECT CAST(i.column0 AS VARCHAR) AS {id_column}, "
        f"TIMESTAMP '{boundary}' AS ts, CAST([{vector}] AS FLOAT[{FACTORS}]) AS value "
        f"FROM {table}_ids i POSITIONAL JOIN {table}_values f"
    )
    con.unregister(f"{table}_ids")
    con.unregister(f"{table}_values")


def build_slices(
    *,
    events_dir: Path = EVENTS_DIR,
    dim_file: Path = DIM_BUSINESS,
    boundaries: tuple[date, ...] = SLICE_BOUNDARIES,
    out_dir: Path = ALS_STATE_DIR,
    show_progress: bool = False,
) -> dict[str, int]:
    """Fit one ALS per boundary and write the two ALS state groups."""
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    work = Path(tempfile.mkdtemp(prefix="als_slices_"))
    try:
        con.execute(
            f"CREATE TABLE user_als(user_id VARCHAR, ts TIMESTAMP, "
            f"value FLOAT[{FACTORS}])"
        )
        con.execute(
            f"CREATE TABLE item_als(business_id VARCHAR, ts TIMESTAMP, "
            f"value FLOAT[{FACTORS}])"
        )
        for boundary in boundaries:
            interactions_file = work / f"interactions_{boundary.year}.parquet"
            pairs = build_interactions(
                events_dir=events_dir,
                dim_file=dim_file,
                out_file=interactions_file,
                cutoff=boundary,
            )
            if pairs == 0:
                continue
            data = load_interactions(
                interactions_file=interactions_file, dim_file=dim_file
            )
            user_factors, item_factors = fit_factors(data, show_progress=show_progress)
            _slice_rows(con, data.user_ids, user_factors, boundary, "user_als", "user_id")
            _slice_rows(con, data.item_ids, item_factors, boundary, "item_als", "business_id")

        counts: dict[str, int] = {}
        for table, out_file, key in (
            ("user_als", state_path("user_als", out_dir), "user_id"),
            ("item_als", state_path("item_als", out_dir), "business_id"),
        ):
            out_file.unlink(missing_ok=True)
            con.execute(
                f"COPY (SELECT * FROM {table} ORDER BY {key}, ts) "
                f"TO {sql_path(out_file)} (FORMAT PARQUET)"
            )
            row = con.execute(
                f"SELECT count(*), "
                "sum(CASE WHEN len(list_filter(value, x -> x IS NULL)) > 0 THEN 1 ELSE 0 END) "
                f"FROM read_parquet({sql_path(out_file)})"
            ).fetchone()
            assert row is not None
            # `list_contains(value, NULL)` cannot express this: it returns NULL rather
            # than true, so the obvious check passes vacuously (ISSUES.md I8's class,
            # and how the transposition below went unnoticed the first time).
            if row[1]:
                raise ValueError(
                    f"{table}: {row[1]:,} of {row[0]:,} vectors contain NULL elements"
                )
            counts[table] = int(row[0])
        return counts
    finally:
        con.close()
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    counts = build_slices(show_progress=False)
    print(f"ALS slice state -> {ALS_STATE_DIR}\n")
    for table, n in counts.items():
        print(f"  {table:<10} {n:>10,} state rows")
    con = duckdb.connect()
    try:
        print("\nslices (a model at ts saw only events strictly before it):")
        for ts, users in con.execute(
            f"SELECT ts, count(*) FROM read_parquet({sql_path(USER_ALS_STATE)}) "
            "GROUP BY ts ORDER BY ts"
        ).fetchall():
            print(f"  {str(ts)[:10]}   {users:>9,} users")
    finally:
        con.close()


if __name__ == "__main__":
    main()
