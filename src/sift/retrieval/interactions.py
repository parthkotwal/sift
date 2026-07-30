"""Pre-split implicit-feedback matrix for ALS retrieval.

Rows are users with at least one review strictly before the frozen split; columns
cover the entire metro catalog, including businesses with no pre-split interaction.
Each non-zero value is the number of reviews by that user of that business. Stars
are deliberately absent: retrieval's frozen target is engagement (D18).

Run: ``python -m sift.retrieval.interactions``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
from scipy.sparse import csr_matrix  # type: ignore[import-untyped]

from sift.config import SPLIT_T, sql_path
from sift.offline.dim_business import DIM_BUSINESS
from sift.offline.ingest import EVENTS_DIR
from sift.retrieval.artifacts import ALS_DIR, INTERACTIONS

# Re-exported for the existing training imports. The definitions moved to `artifacts`
# so that reading an artifact path no longer costs a `scipy.sparse` import: this module
# builds the CSR matrix ALS is fitted on, which serving never touches.
__all__ = ["ALS_DIR", "INTERACTIONS", "InteractionData", "build_interactions", "load_interactions"]


@dataclass(frozen=True)
class InteractionData:
    matrix: csr_matrix
    user_ids: tuple[str, ...]
    item_ids: tuple[str, ...]


def build_interactions(
    *,
    events_dir: Path = EVENTS_DIR,
    dim_file: Path = DIM_BUSINESS,
    out_file: Path = INTERACTIONS,
    cutoff: date = SPLIT_T,
) -> int:
    """Materialise deterministic ``(user, item, review_count)`` rows.

    `cutoff` is the exclusive upper bound on event time. It defaults to the frozen
    split, which is the headline retrieval model; `als_slices` passes earlier
    boundaries to build the point-in-time slices (D27).
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.unlink(missing_ok=True)
    events = sql_path(events_dir / "**" / "*.parquet")
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
                SELECT e.user_id, e.business_id, CAST(count(*) AS INTEGER) AS confidence
                FROM read_parquet({events}, hive_partitioning=true) e
                SEMI JOIN read_parquet({sql_path(dim_file)}) d
                    ON e.business_id = d.business_id
                WHERE e.event_type = 'review' AND e.ts < TIMESTAMP '{cutoff}'
                GROUP BY e.user_id, e.business_id
                ORDER BY e.user_id, e.business_id
            ) TO {sql_path(out_file)} (FORMAT PARQUET)
            """
        )
        row = con.execute(f"SELECT count(*) FROM read_parquet({sql_path(out_file)})").fetchone()
        assert row is not None
        return int(row[0])
    finally:
        con.close()


def load_interactions(
    *,
    interactions_file: Path = INTERACTIONS,
    dim_file: Path = DIM_BUSINESS,
) -> InteractionData:
    """Load the artifact as a CSR matrix with stable lexicographic ID mappings."""
    con = duckdb.connect()
    try:
        con.execute(
            f"CREATE VIEW interactions AS SELECT * FROM read_parquet({sql_path(interactions_file)})"
        )
        con.execute(
            f"CREATE VIEW catalog AS SELECT business_id FROM read_parquet({sql_path(dim_file)})"
        )
        user_ids = tuple(
            str(row[0])
            for row in con.execute(
                "SELECT DISTINCT user_id FROM interactions ORDER BY user_id"
            ).fetchall()
        )
        item_ids = tuple(
            str(row[0])
            for row in con.execute(
                "SELECT business_id FROM catalog ORDER BY business_id"
            ).fetchall()
        )
        con.execute(
            "CREATE TEMP TABLE user_map AS SELECT user_id, "
            "row_number() OVER (ORDER BY user_id) - 1 AS user_idx "
            "FROM (SELECT DISTINCT user_id FROM interactions)"
        )
        con.execute(
            "CREATE TEMP TABLE item_map AS SELECT business_id, "
            "row_number() OVER (ORDER BY business_id) - 1 AS item_idx FROM catalog"
        )
        arrays = con.execute(
            "SELECT u.user_idx, i.item_idx, x.confidence "
            "FROM interactions x JOIN user_map u USING (user_id) "
            "JOIN item_map i USING (business_id) "
            "ORDER BY u.user_idx, i.item_idx"
        ).fetchnumpy()
    finally:
        con.close()

    matrix = csr_matrix(
        (
            np.asarray(arrays["confidence"], dtype=np.float32),
            (
                np.asarray(arrays["user_idx"], dtype=np.int32),
                np.asarray(arrays["item_idx"], dtype=np.int32),
            ),
        ),
        shape=(len(user_ids), len(item_ids)),
        dtype=np.float32,
    )
    matrix.sort_indices()
    return InteractionData(matrix=matrix, user_ids=user_ids, item_ids=item_ids)


def main() -> None:
    rows = build_interactions()
    data = load_interactions()
    print(f"interaction pairs: {rows:,} -> {INTERACTIONS}")
    print(f"matrix: {data.matrix.shape[0]:,} users x {data.matrix.shape[1]:,} items")
    print(f"non-zero confidence values: {data.matrix.nnz:,}")


if __name__ == "__main__":
    main()
