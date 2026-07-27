"""Two-tower temporal inputs, negative masking, and deterministic export."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
from scipy.sparse import csr_matrix  # type: ignore[import-untyped]

from sift.config import sql_path
from sift.offline.dim_business import build_dim_business
from sift.offline.ingest import build_events
from sift.retrieval.interactions import InteractionData
from sift.retrieval.two_tower import (
    OUTPUT_DIM,
    fit_user_normalization,
    known_positive_mask,
    train_two_tower,
    transform_user_values,
)
from sift.retrieval.two_tower_data import (
    ItemInputs,
    TrainingExamples,
    build_training_examples,
    load_item_inputs,
)
from sift.store.materialize import materialize_historical


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def _offline_artifacts(tmp_path: Path) -> tuple[Path, Path, Path]:
    business_json = tmp_path / "business.json"
    review_json = tmp_path / "review.json"
    _write_jsonl(
        business_json,
        [
            {
                "business_id": "b1",
                "name": "one",
                "city": "Philadelphia",
                "state": "PA",
                "latitude": 40.0,
                "longitude": -75.0,
                "is_open": 1,
                "categories": "Restaurants, Pizza",
                "attributes": {"RestaurantsPriceRange2": "2"},
            },
            {
                "business_id": "b2",
                "name": "two",
                "city": "Philadelphia",
                "state": "PA",
                "latitude": 40.1,
                "longitude": -75.1,
                "is_open": 0,
                "categories": "Coffee & Tea",
                "attributes": None,
            },
        ],
    )
    _write_jsonl(
        review_json,
        [
            {
                "user_id": "u1",
                "business_id": "b1",
                "stars": 5,
                "date": "2017-01-01 12:00:00",
            },
            {
                "user_id": "u1",
                "business_id": "b2",
                "stars": 3,
                "date": "2018-01-01 12:00:00",
            },
            {
                "user_id": "u1",
                "business_id": "b1",
                "stars": 1,
                "date": "2019-01-01 00:00:00",
            },
        ],
    )
    events = tmp_path / "events"
    dim = tmp_path / "dim.parquet"
    store = tmp_path / "store"
    build_events(
        business_json=business_json,
        review_json=review_json,
        out_dir=events,
        metro_city="Philadelphia",
        metro_state="PA",
    )
    build_dim_business(
        business_json=business_json,
        out_file=dim,
        metro_city="Philadelphia",
        metro_state="PA",
    )
    materialize_historical(events_dir=events, dim_file=dim, out_dir=store)
    return events, dim, store


def test_training_examples_are_right_exclusive_and_pre_split(tmp_path: Path) -> None:
    events, dim, store = _offline_artifacts(tmp_path)
    out = tmp_path / "examples.parquet"
    assert (
        build_training_examples(events_dir=events, dim_file=dim, store_dir=store, out_file=out) == 2
    )
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT user_id, business_id, u_reviews_to_date, "
        f"u_mean_stars_to_date, u_days_since_last "
        f"FROM read_parquet({sql_path(out)}) ORDER BY ts"
    ).fetchall()
    con.close()
    assert rows[0] == ("u1", "b1", 0, None, None)
    assert rows[1] == ("u1", "b2", 1, 5.0, 365)
    first = out.read_bytes()
    build_training_examples(events_dir=events, dim_file=dim, store_dir=store, out_file=out)
    assert out.read_bytes() == first


def test_item_inputs_use_static_attributes_but_not_is_open(tmp_path: Path) -> None:
    _events, dim, _store = _offline_artifacts(tmp_path)
    inputs = load_item_inputs(("b1", "b2"), dim_file=dim)
    assert inputs.dense.shape == (2, 4)
    assert inputs.category_names == ("Coffee & Tea", "Pizza", "Restaurants")
    assert inputs.categories.tolist() == [[0.0, 1.0, 1.0], [1.0, 0.0, 0.0]]
    assert inputs.dense[:, 3].tolist() == [1.0, 0.0]  # price-present, not is_open


def test_user_transform_preserves_missingness_as_explicit_inputs() -> None:
    raw = np.asarray([[0.0, np.nan, np.nan], [4.0, 5.0, 10.0]], dtype=np.float32)
    transformed = transform_user_values(raw, fit_user_normalization(raw))
    assert np.isfinite(transformed).all()
    assert transformed[0, 2] == 0.0 and transformed[0, 4] == 0.0
    assert transformed[1, 2] == 1.0 and transformed[1, 4] == 1.0


def test_known_positive_mask_keeps_diagonal_and_masks_other_history() -> None:
    history = csr_matrix(np.asarray([[1, 1, 0], [0, 1, 0]], dtype=np.float32))
    mask = known_positive_mask(
        history,
        np.asarray([0, 1], dtype=np.int64),
        np.asarray([0, 1, 2], dtype=np.int64),
        batch_positives=2,
    )
    assert mask.tolist() == [[False, True, False], [False, False, False]]


def test_training_is_reproducible_and_exports_unit_vectors(tmp_path: Path) -> None:
    user_ids = ("u1", "u2", "u3")
    item_ids = ("b1", "b2", "b3", "b4")
    matrix = csr_matrix(np.asarray([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]], dtype=np.float32))
    interactions = InteractionData(matrix, user_ids, item_ids)
    examples = TrainingExamples(
        np.asarray([0, 1, 2, 0, 1, 2], dtype=np.int64),
        np.asarray([0, 1, 2, 0, 1, 2], dtype=np.int64),
        np.asarray(
            [
                [0, np.nan, np.nan],
                [0, np.nan, np.nan],
                [0, np.nan, np.nan],
                [1, 5, 10],
                [1, 4, 20],
                [1, 3, 30],
            ],
            dtype=np.float32,
        ),
    )
    item_inputs = ItemInputs(
        dense=np.asarray(
            [[0, 0, 0.25, 1], [1, 1, 0.5, 1], [2, 2, 0, 0], [3, 3, 1, 1]],
            dtype=np.float32,
        ),
        categories=np.asarray([[1, 0], [0, 1], [1, 1], [0, 0]], dtype=np.float32),
        category_names=("A", "B"),
    )
    export = examples.user_values[:3]
    first = train_two_tower(
        examples,
        interactions,
        item_inputs,
        export,
        out_dir=tmp_path / "first",
        epochs=2,
        progress=False,
    )
    second = train_two_tower(
        examples,
        interactions,
        item_inputs,
        export,
        out_dir=tmp_path / "second",
        epochs=2,
        progress=False,
    )
    assert np.array_equal(first[0], second[0])
    assert np.array_equal(first[1], second[1])
    assert first[0].shape == (3, OUTPUT_DIM)
    assert first[1].shape == (4, OUTPUT_DIM)
    assert np.allclose(np.linalg.norm(first[0], axis=1), 1.0)
    assert np.allclose(np.linalg.norm(first[1], axis=1), 1.0)
