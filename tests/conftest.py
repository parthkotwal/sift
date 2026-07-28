"""Shared fixtures.

`write_als_state` gives tests the ALS slice groups (D27) without fitting ALS: the
read path cares about the *shape* of that state — an entity id, a boundary
timestamp, and a vector — not about how the vector was produced. Tests that assert
feature values therefore write vectors they chose, so `ui_als_score` is
hand-computable like every other feature, and the boundary semantics (a query
before the first slice gets NULL) become directly testable.

Vectors here are short on purpose. Nothing in the definition hardcodes the
production width; `array_inner_product` works at any matching length, so a 3-D
fixture keeps the arithmetic checkable by eye.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import duckdb

from sift.config import sql_path
from sift.store.materialize import state_path


def write_als_state(
    store_dir: Path,
    users: Mapping[str, Sequence[float]],
    items: Mapping[str, Sequence[float]],
    *,
    boundary: str = "2017-01-01 00:00:00",
) -> None:
    """Write user/item ALS slice state into a store directory.

    All entities share one boundary unless a test needs more; pass a different
    `boundary` and call twice to build a multi-slice timeline.
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    width = len(next(iter(users.values()))) if users else 3
    con = duckdb.connect()
    try:
        for group, rows, id_column in (
            ("user_als", users, "user_id"),
            ("item_als", items, "business_id"),
        ):
            path = state_path(group, store_dir)
            existing = (
                f"SELECT * FROM read_parquet({sql_path(path)}) UNION ALL "
                if path.exists()
                else ""
            )
            values = ", ".join(
                f"('{key}', TIMESTAMP '{boundary}', "
                f"{list(vector)}::FLOAT[{width}])"
                for key, vector in rows.items()
            )
            if not values:
                continue
            con.execute(
                f"CREATE OR REPLACE TABLE staged AS {existing} "
                f"SELECT * FROM (VALUES {values}) AS t({id_column}, ts, value)"
            )
            con.execute(
                f"COPY (SELECT * FROM staged ORDER BY {id_column}, ts) "
                f"TO {sql_path(path)} (FORMAT PARQUET)"
            )
    finally:
        con.close()
