"""Train the fixed v1 ALS retriever and export inspectable factor artifacts.

One configuration, no sweep (AGENTS.md): 64 factors, confidence scale 40,
regularization 0.01, 15 iterations, seed 42. Training is single-threaded so the
same interaction artifact produces bit-identical factors rather than depending on
OpenMP reduction order.

Run: ``python -m sift.retrieval.als``.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
from implicit.als import AlternatingLeastSquares  # type: ignore[import-untyped]
from numpy.typing import NDArray

from sift.config import sql_path
from sift.features.definitions import BEHAVIORAL_EMBEDDING_DIM
from sift.retrieval.interactions import ALS_DIR, InteractionData, load_interactions

FACTORS = BEHAVIORAL_EMBEDDING_DIM
REGULARIZATION = 0.01
ALPHA = 40.0
ITERATIONS = 15
SEED = 42

USER_FACTORS = ALS_DIR / "user_embedding_behavioral_v1.npy"
ITEM_FACTORS = ALS_DIR / "item_embedding_behavioral_v1.npy"
USER_IDS = ALS_DIR / "user_ids.json"
ITEM_IDS = ALS_DIR / "item_ids.json"
USER_FACTOR_PARQUET = ALS_DIR / "user_embedding_behavioral_v1.parquet"
ITEM_FACTOR_PARQUET = ALS_DIR / "item_embedding_behavioral_v1.parquet"
ALS_MANIFEST = ALS_DIR / "manifest.json"


def write_factor_parquet(
    ids: tuple[str, ...],
    factors: NDArray[np.float32],
    *,
    id_column: str,
    out_file: Path,
) -> None:
    """Write ``(entity_id, 64-element FLOAT[])`` without pandas or Arrow."""
    if factors.shape != (len(ids), FACTORS):
        raise ValueError(f"factor shape {factors.shape} does not match {len(ids)} ids")
    id_values = np.asarray(ids)
    # DuckDB scans a 2D ndarray as one column per first-axis entry. Transposing
    # therefore exposes 64 scalar columns over N rows; SQL packs them into one
    # fixed-length vector column before Parquet is written.
    factor_columns = np.ascontiguousarray(factors.T)
    con = duckdb.connect()
    try:
        con.register("factor_ids", id_values)
        con.register("factor_values", factor_columns)
        vector = ", ".join(f"f.column{i}" for i in range(FACTORS))
        con.execute(
            f"COPY (SELECT CAST(i.column0 AS VARCHAR) AS {id_column}, "
            f"CAST([{vector}] AS FLOAT[{FACTORS}]) AS value "
            "FROM factor_ids i POSITIONAL JOIN factor_values f "
            f"ORDER BY {id_column}) TO {sql_path(out_file)} (FORMAT PARQUET)"
        )
    finally:
        con.close()


def train_als(
    data: InteractionData | None = None,
    *,
    out_dir: Path = ALS_DIR,
    show_progress: bool = True,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Train and write NPY (runtime) plus Parquet (inspection/store) factors."""
    interactions = load_interactions() if data is None else data
    model = AlternatingLeastSquares(
        factors=FACTORS,
        regularization=REGULARIZATION,
        alpha=ALPHA,
        iterations=ITERATIONS,
        random_state=SEED,
        num_threads=1,
        dtype=np.float32,
    )
    model.fit(interactions.matrix, show_progress=show_progress)
    user_factors = np.asarray(model.user_factors, dtype=np.float32)
    item_factors = np.asarray(model.item_factors, dtype=np.float32)

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "user_factors": out_dir / USER_FACTORS.name,
        "item_factors": out_dir / ITEM_FACTORS.name,
        "user_ids": out_dir / USER_IDS.name,
        "item_ids": out_dir / ITEM_IDS.name,
        "user_parquet": out_dir / USER_FACTOR_PARQUET.name,
        "item_parquet": out_dir / ITEM_FACTOR_PARQUET.name,
        "manifest": out_dir / ALS_MANIFEST.name,
    }
    np.save(paths["user_factors"], user_factors, allow_pickle=False)
    np.save(paths["item_factors"], item_factors, allow_pickle=False)
    paths["user_ids"].write_text(json.dumps(interactions.user_ids))
    paths["item_ids"].write_text(json.dumps(interactions.item_ids))
    write_factor_parquet(
        interactions.user_ids,
        user_factors,
        id_column="user_id",
        out_file=paths["user_parquet"],
    )
    write_factor_parquet(
        interactions.item_ids,
        item_factors,
        id_column="business_id",
        out_file=paths["item_parquet"],
    )
    paths["manifest"].write_text(
        json.dumps(
            {
                "name": "embedding_behavioral_v1",
                "factors": FACTORS,
                "regularization": REGULARIZATION,
                "alpha": ALPHA,
                "iterations": ITERATIONS,
                "seed": SEED,
                "num_threads": 1,
                "users": len(interactions.user_ids),
                "items": len(interactions.item_ids),
                "interactions": int(interactions.matrix.nnz),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return user_factors, item_factors


def main() -> None:
    users, items = train_als()
    print(f"user factors: {users.shape} -> {USER_FACTOR_PARQUET}")
    print(f"item factors: {items.shape} -> {ITEM_FACTOR_PARQUET}")
    print(f"manifest: {ALS_MANIFEST}")


if __name__ == "__main__":
    main()
