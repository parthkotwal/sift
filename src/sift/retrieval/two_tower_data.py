"""Point-in-time training examples and static item inputs for the two-tower.

Only user-side signals vary with the query timestamp. Item-side inputs are the
quasi-static identity attributes accepted in D21, so an in-batch item cannot carry
future aggregate state into an earlier row in the same batch.

Run: ``python -m sift.retrieval.two_tower_data``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
from numpy.typing import NDArray

from sift.config import DERIVED_DIR, SPLIT_T, sql_path
from sift.offline.dim_business import DIM_BUSINESS
from sift.offline.ingest import EVENTS_DIR
from sift.store.materialize import HISTORICAL_DIR
from sift.store.read import asof_feature_query, attach_store

TWO_TOWER_DIR = DERIVED_DIR / "two_tower"
TRAINING_EXAMPLES = TWO_TOWER_DIR / "training_examples.parquet"
USER_FEATURES = (
    "u_reviews_to_date",
    "u_mean_stars_to_date",
    "u_days_since_last",
)


@dataclass(frozen=True)
class TrainingExamples:
    user_indices: NDArray[np.int64]
    item_indices: NDArray[np.int64]
    user_values: NDArray[np.float32]


@dataclass(frozen=True)
class ItemInputs:
    dense: NDArray[np.float32]
    categories: NDArray[np.float32]
    category_names: tuple[str, ...]


def build_training_examples(
    *,
    events_dir: Path = EVENTS_DIR,
    dim_file: Path = DIM_BUSINESS,
    store_dir: Path = HISTORICAL_DIR,
    out_file: Path = TRAINING_EXAMPLES,
) -> int:
    """Write one pre-T review event plus its right-exclusive user features."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        events = sql_path(events_dir / "**" / "*.parquet")
        con.execute(
            f"""
            CREATE TABLE queries AS
            SELECT row_number() OVER (ORDER BY e.user_id, e.ts, e.business_id) AS query_id,
                   e.user_id, e.business_id, e.ts
            FROM read_parquet({events}, hive_partitioning=true) e
            SEMI JOIN read_parquet({sql_path(dim_file)}) d
                ON e.business_id = d.business_id
            WHERE e.event_type = 'review' AND e.ts < TIMESTAMP '{SPLIT_T}'
            """
        )
        attach_store(con, historical_dir=store_dir, dim_file=dim_file)
        feature_sql = asof_feature_query(USER_FEATURES)
        columns = ", ".join(f"f.{name}" for name in USER_FEATURES)
        out_file.unlink(missing_ok=True)
        con.execute(
            f"""
            COPY (
                SELECT q.user_id, q.business_id, q.ts, {columns}
                FROM queries q JOIN ({feature_sql}) f USING (query_id)
                ORDER BY q.query_id
            ) TO {sql_path(out_file)} (FORMAT PARQUET)
            """
        )
        row = con.execute(f"SELECT count(*) FROM read_parquet({sql_path(out_file)})").fetchone()
        assert row is not None
        return int(row[0])
    finally:
        con.close()


def load_training_examples(
    user_ids: tuple[str, ...],
    item_ids: tuple[str, ...],
    *,
    examples_file: Path = TRAINING_EXAMPLES,
) -> TrainingExamples:
    """Map inspectable string IDs to the shared deterministic integer mappings."""
    user_to_index = {value: index for index, value in enumerate(user_ids)}
    item_to_index = {value: index for index, value in enumerate(item_ids)}
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT user_id, business_id, {', '.join(USER_FEATURES)} "
            f"FROM read_parquet({sql_path(examples_file)})"
        ).fetchnumpy()
    finally:
        con.close()
    raw_users = rows["user_id"]
    raw_items = rows["business_id"]
    try:
        user_indices = np.fromiter(
            (user_to_index[str(value)] for value in raw_users),
            dtype=np.int64,
            count=len(raw_users),
        )
        item_indices = np.fromiter(
            (item_to_index[str(value)] for value in raw_items),
            dtype=np.int64,
            count=len(raw_items),
        )
    except KeyError as exc:
        raise ValueError(f"training example has an ID outside its mapping: {exc}") from exc
    user_values = np.column_stack(
        [
            np.asarray(np.ma.filled(np.ma.asarray(rows[name], dtype=np.float32), np.nan))
            for name in USER_FEATURES
        ]
    ).astype(np.float32, copy=False)
    return TrainingExamples(user_indices, item_indices, user_values)


def load_item_inputs(item_ids: tuple[str, ...], *, dim_file: Path = DIM_BUSINESS) -> ItemInputs:
    """Dense static values plus an inspectable category multi-hot matrix."""
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT business_id, latitude, longitude, price_tier, categories "
            f"FROM read_parquet({sql_path(dim_file)}) ORDER BY business_id"
        ).fetchall()
    finally:
        con.close()
    ids = tuple(str(row[0]) for row in rows)
    if ids != item_ids:
        raise ValueError("business dimension and item ID mapping disagree")
    category_names = tuple(sorted({str(category) for row in rows for category in (row[4] or [])}))
    category_to_index = {name: index for index, name in enumerate(category_names)}
    categories = np.zeros((len(rows), len(category_names)), dtype=np.float32)
    dense = np.zeros((len(rows), 4), dtype=np.float32)
    for row_index, (_id, latitude, longitude, price, row_categories) in enumerate(rows):
        dense[row_index, 0] = float(latitude)
        dense[row_index, 1] = float(longitude)
        if price is not None:
            dense[row_index, 2] = float(price) / 4.0
            dense[row_index, 3] = 1.0
        for category in row_categories or []:
            categories[row_index, category_to_index[str(category)]] = 1.0
    return ItemInputs(dense=dense, categories=categories, category_names=category_names)


def main() -> None:
    count = build_training_examples()
    print(f"two-tower positive events: {count:,} -> {TRAINING_EXAMPLES}")
    print(f"features: {', '.join(USER_FEATURES)} (all through the as-of store path)")


if __name__ == "__main__":
    main()
