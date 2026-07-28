"""Temporal interaction matrix and deterministic ALS factor export."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np

from sift.config import sql_path
from sift.offline.dim_business import build_dim_business
from sift.offline.ingest import build_events
from sift.retrieval.als import FACTORS, train_als
from sift.retrieval.interactions import build_interactions, load_interactions


def _artifacts(tmp_path: Path) -> tuple[Path, Path, Path]:
    businesses = [
        {
            "business_id": business_id,
            "name": business_id,
            "city": "Philadelphia",
            "state": "PA",
            "latitude": 40.0,
            "longitude": -75.0,
            "is_open": 1,
            "categories": "Restaurants",
            "attributes": None,
        }
        for business_id in ("b1", "b2", "b3")
    ]
    reviews = [
        {"user_id": "u1", "business_id": "b1", "stars": 5, "date": "2017-01-01"},
        {"user_id": "u1", "business_id": "b1", "stars": 1, "date": "2018-01-01"},
        {"user_id": "u1", "business_id": "b2", "stars": 5, "date": "2019-01-01"},
        {"user_id": "u2", "business_id": "b2", "stars": 2, "date": "2018-06-01"},
    ]
    business_json = tmp_path / "business.json"
    review_json = tmp_path / "review.json"
    business_json.write_text("\n".join(json.dumps(row) for row in businesses) + "\n")
    review_json.write_text("\n".join(json.dumps(row) for row in reviews) + "\n")
    events = tmp_path / "events"
    dim = tmp_path / "dim.parquet"
    interactions = tmp_path / "interactions.parquet"
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
    build_interactions(events_dir=events, dim_file=dim, out_file=interactions)
    return interactions, dim, events


def test_interactions_are_pre_split_engagement_counts(tmp_path: Path) -> None:
    interactions, dim, _events = _artifacts(tmp_path)
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT * FROM read_parquet({sql_path(interactions)}) ORDER BY ALL"
    ).fetchall()
    con.close()
    # u1's two differently-rated reviews are both engagement; the event exactly at
    # T is holdout and absent.
    assert rows == [("u1", "b1", 2), ("u2", "b2", 1)]
    data = load_interactions(interactions_file=interactions, dim_file=dim)
    assert data.user_ids == ("u1", "u2")
    assert data.item_ids == ("b1", "b2", "b3")
    assert data.matrix.shape == (2, 3)
    assert data.matrix.toarray().tolist() == [[2.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


def test_interaction_artifact_is_idempotent(tmp_path: Path) -> None:
    interactions, dim, events = _artifacts(tmp_path)
    first = interactions.read_bytes()
    build_interactions(events_dir=events, dim_file=dim, out_file=interactions)
    assert interactions.read_bytes() == first


def test_catalog_only_items_get_an_exact_zero_vector(tmp_path: Path) -> None:
    """`item_embedding_behavioral_v1`'s registered leakage argument claims a
    catalog item with no pre-T interaction "receives a zero vector rather than
    future state". That is a property of the ALS solve — the item's normal
    equations reduce to `(lambda*I) y = 0` when no user has touched it — but
    nothing tested it, so a change of solver or of the confidence encoding could
    leave b3 holding its random initialisation and put an untrained vector into
    the index. b3 is in the dimension and in no review.
    """
    interactions, dim, _events = _artifacts(tmp_path)
    data = load_interactions(interactions_file=interactions, dim_file=dim)
    assert data.item_ids == ("b1", "b2", "b3")
    assert data.matrix[:, 2].nnz == 0  # b3 is catalog-only

    _users, items = train_als(data, out_dir=tmp_path / "cold", show_progress=False)
    assert np.array_equal(items[2], np.zeros(FACTORS, dtype=np.float32))
    assert np.any(items[0] != 0.0)  # the interacted items are not trivially zero


def test_als_is_reproducible_and_exports_fixed_length_vectors(tmp_path: Path) -> None:
    interactions, dim, _events = _artifacts(tmp_path)
    data = load_interactions(interactions_file=interactions, dim_file=dim)
    first_users, first_items = train_als(data, out_dir=tmp_path / "first", show_progress=False)
    second_users, second_items = train_als(data, out_dir=tmp_path / "second", show_progress=False)
    assert np.array_equal(first_users, second_users)
    assert np.array_equal(first_items, second_items)
    assert first_users.shape == (2, FACTORS)
    assert first_items.shape == (3, FACTORS)

    con = duckdb.connect()
    schema = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet("
        f"{sql_path(tmp_path / 'first' / 'user_embedding_behavioral_v1.parquet')})"
    ).fetchall()
    row = con.execute(
        f"SELECT user_id, len(value) FROM read_parquet("
        f"{sql_path(tmp_path / 'first' / 'user_embedding_behavioral_v1.parquet')}) "
        "ORDER BY user_id LIMIT 1"
    ).fetchone()
    con.close()
    assert [(column[0], column[1]) for column in schema] == [
        ("user_id", "VARCHAR"),
        # Parquet represents DuckDB's fixed array as a list; row-length checks pin
        # the shape after round-tripping through the file format.
        ("value", "FLOAT[]"),
    ]
    assert row == ("u1", FACTORS)
