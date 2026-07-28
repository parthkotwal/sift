"""Two-tower temporal inputs, negative masking, and deterministic export."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import torch
from scipy.sparse import csr_matrix  # type: ignore[import-untyped]

from sift.config import sql_path
from sift.offline.dim_business import build_dim_business
from sift.offline.ingest import build_events
from sift.retrieval.interactions import InteractionData
from sift.retrieval.two_tower import (
    OUTPUT_DIM,
    TEMPERATURE,
    UNIFORM_NEGATIVES,
    corrected_logits,
    fit_user_normalization,
    known_positive_mask,
    load_export_user_values,
    mixture_sampling_probability,
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


_DEFAULT_REVIEWS: list[dict[str, object]] = [
    {"user_id": "u1", "business_id": "b1", "stars": 5, "date": "2017-01-01 12:00:00"},
    {"user_id": "u1", "business_id": "b2", "stars": 3, "date": "2018-01-01 12:00:00"},
    {"user_id": "u1", "business_id": "b1", "stars": 1, "date": "2019-01-01 00:00:00"},
]


def _offline_artifacts(
    tmp_path: Path, reviews: list[dict[str, object]] | None = None
) -> tuple[Path, Path, Path]:
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
    _write_jsonl(review_json, _DEFAULT_REVIEWS if reviews is None else reviews)
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


def test_mixture_sampling_probability_blends_both_samplers_and_floors_at_uniform() -> None:
    """q is the mixture the sampler actually draws from, not either component."""
    n_items = 4
    # Two in-batch positives drawn from a popularity-skewed empirical distribution;
    # items 2 and 3 never appear as a positive at all.
    item_q = np.asarray([0.75, 0.25, 0.0, 0.0], dtype=np.float64)
    candidates = np.asarray([0, 1, 2], dtype=np.int64)
    q = mixture_sampling_probability(item_q, candidates, 2, n_items)

    expected = (2 * item_q[candidates] + UNIFORM_NEGATIVES / n_items) / (2 + UNIFORM_NEGATIVES)
    assert np.allclose(q, expected)
    assert q[0] > q[1] > q[2]  # the popular item is proposed most often
    # The uniform component is what keeps log q finite for an item with no
    # positive event — without it the correction would be -inf on those columns.
    assert q[2] > 0.0 and np.isfinite(np.log(q[2]))


def test_logq_correction_is_subtracted_in_nats_after_temperature_scaling() -> None:
    """Pin the *magnitude* of the debias term, not just the shape of the logits.

    The estimator corrects the model's score function, and the score function is
    cosine/T — so `log q` is subtracted from the already-scaled logit, in nats.
    Correcting before the division would scale the term by 1/T (~14x here), which
    is the estimator for a proposal distribution q^(1/T), not q. This test fails
    if the order is ever "fixed" the other way.
    """
    similarities = torch.tensor([[0.5, -0.25]], dtype=torch.float32)
    mixture_q = np.asarray([0.1, 0.001], dtype=np.float64)

    logits = corrected_logits(similarities, mixture_q)
    corrections = (logits - similarities / TEMPERATURE).numpy()
    assert np.allclose(corrections, -np.log(mixture_q), atol=1e-5)
    # A 100x rarer candidate is penalised by log(100) = 4.6 nats relative to the
    # popular one — not log(100)/T = 65.8, which would swamp similarities that are
    # bounded by +-1/T.
    assert np.isclose(corrections[0, 1] - corrections[0, 0], np.log(100.0), atol=1e-5)


def test_export_user_values_follow_the_id_mapping_not_the_scan_order(tmp_path: Path) -> None:
    """Row i must be `user_ids[i]`: the caller encodes it as user index i.

    Reading the same store twice under different ID orders pins the mapping to the
    argument rather than to anything the store or the scan decides. Honest limit
    (I8): this cannot *force* a parallel reorder, so it would not have caught the
    old `row_number() OVER ()` at this fixture size — it fails any rewrite that
    keys rows off store order, and the ordering guarantee itself now comes from
    POSITIONAL JOIN rather than from observation.
    """
    reviews: list[dict[str, object]] = [
        {"user_id": "u1", "business_id": "b1", "stars": 5, "date": "2017-01-01 12:00:00"},
        {"user_id": "u2", "business_id": "b1", "stars": 4, "date": "2017-01-02 12:00:00"},
        {"user_id": "u2", "business_id": "b2", "stars": 2, "date": "2017-01-03 12:00:00"},
        {"user_id": "u3", "business_id": "b1", "stars": 3, "date": "2017-01-04 12:00:00"},
        {"user_id": "u3", "business_id": "b2", "stars": 3, "date": "2017-01-05 12:00:00"},
        {"user_id": "u3", "business_id": "b1", "stars": 1, "date": "2017-01-06 12:00:00"},
    ]
    _events, dim, store = _offline_artifacts(tmp_path, reviews)

    ordered = load_export_user_values(("u1", "u2", "u3"), ("b1",), store_dir=store, dim_file=dim)
    # Column 0 is u_reviews_to_date as-of T, which identifies the user here.
    assert ordered[:, 0].tolist() == [1.0, 2.0, 3.0]

    shuffled = load_export_user_values(("u3", "u1", "u2"), ("b1",), store_dir=store, dim_file=dim)
    assert shuffled[:, 0].tolist() == [3.0, 1.0, 2.0]


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
