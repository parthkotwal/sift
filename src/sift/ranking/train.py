"""Train the LightGBM LambdaRank ranker on the assembled training set.

Objective `lambdarank`: for each group (1 positive + its sampled negatives) the
model learns to score the positive above the negatives, with pair gradients
weighted by their impact on NDCG — the metric we grade on. Trees handle the
cold-start NaNs natively (no imputation).

Early stopping uses a *temporal* validation slice (the last pre-T year), so the
stopping signal is itself point-in-time honest — we never tune against the future.

Run: python -m sift.ranking.train
"""

from __future__ import annotations

from datetime import date

import duckdb
import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray

from sift.config import DERIVED_DIR, SPLIT_T, sql_path
from sift.features.definitions import feature_names
from sift.offline.training_set import TRAINING_SET

FEATURES: list[str] = list(feature_names())
VAL_START = date(2018, 1, 1)  # groups in [VAL_START, T) are validation
RANKER_MODEL = DERIVED_DIR / "ranker.txt"


def to_float(array: object) -> NDArray[np.float64]:
    """DuckDB returns a masked array for nullable columns; fill NULLs with NaN
    (LightGBM's native 'missing') rather than a sentinel number. Cast to float
    first — a masked int64 array cannot hold NaN as its fill value.

    Shared with the serving path so both sides turn NULL into the same value.
    """
    masked = np.ma.asarray(array, dtype=np.float64)
    return np.asarray(np.ma.filled(masked, np.nan), dtype=np.float64)


def _load_split(
    con: duckdb.DuckDBPyConnection, glob: str, where: str
) -> tuple[NDArray[np.float64], NDArray[np.int32], list[int]]:
    cols = ", ".join(FEATURES)
    data = con.execute(
        f"SELECT {cols}, label FROM read_parquet({glob}) WHERE {where} ORDER BY group_id"
    ).fetchnumpy()
    x = np.column_stack([to_float(data[c]) for c in FEATURES])
    y = to_float(data["label"]).astype(np.int32)
    # Group sizes in the same group_id order as the rows above -> LightGBM reads
    # the feature matrix as consecutive query groups of these lengths.
    sizes = [
        int(r[0])
        for r in con.execute(
            f"SELECT count(*) FROM read_parquet({glob}) WHERE {where} "
            "GROUP BY group_id ORDER BY group_id"
        ).fetchall()
    ]
    return x, y, sizes


def train() -> lgb.Booster:
    glob = sql_path(TRAINING_SET)
    con = duckdb.connect()
    try:
        x_tr, y_tr, g_tr = _load_split(con, glob, f"ts < TIMESTAMP '{VAL_START}'")
        x_val, y_val, g_val = _load_split(
            con, glob, f"ts >= TIMESTAMP '{VAL_START}' AND ts < TIMESTAMP '{SPLIT_T}'"
        )
    finally:
        con.close()

    print(f"train: {len(y_tr):,} rows / {len(g_tr):,} groups   "
          f"val: {len(y_val):,} rows / {len(g_val):,} groups")

    train_ds = lgb.Dataset(x_tr, label=y_tr, group=g_tr, feature_name=FEATURES)
    val_ds = lgb.Dataset(x_val, label=y_val, group=g_val, reference=train_ds)
    params = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [10],
        "label_gain": [0, 1],  # binary relevance
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 100,
        "verbosity": -1,
        "seed": 42,
    }
    model = lgb.train(
        params,
        train_ds,
        num_boost_round=1000,
        valid_sets=[val_ds],
        valid_names=["val"],
        callbacks=[lgb.early_stopping(50), lgb.log_evaluation(50)],
    )
    RANKER_MODEL.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(RANKER_MODEL), num_iteration=model.best_iteration)
    return model


def main() -> None:
    model = train()
    print(f"\nbest iteration: {model.best_iteration}")
    print(f"best val NDCG@10: {model.best_score['val']['ndcg@10']:.4f}")
    print("\nfeature importance (gain):")
    gains = model.feature_importance(importance_type="gain")
    for name, gain in sorted(zip(FEATURES, gains, strict=True), key=lambda t: -t[1]):
        print(f"  {name:<22} {gain:,.0f}")
    print(f"\nmodel saved -> {RANKER_MODEL}")


if __name__ == "__main__":
    main()
