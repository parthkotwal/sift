"""The exact ALS index preserves raw dot product and deterministic ordering."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from sift.retrieval.als import FACTORS
from sift.retrieval.index import ExactItemIndex, exact_top_k, validate_index


def _artifacts(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    rng = np.random.default_rng(42)
    items = np.asarray(rng.normal(size=(200, FACTORS)), dtype=np.float32)
    users = np.asarray(rng.normal(size=(12, FACTORS)), dtype=np.float32)
    item_ids = [f"b{i:03}" for i in range(len(items))]
    user_ids = [f"u{i:03}" for i in range(len(users))]
    item_file = tmp_path / "items.npy"
    user_file = tmp_path / "users.npy"
    item_ids_file = tmp_path / "item_ids.json"
    user_ids_file = tmp_path / "user_ids.json"
    np.save(item_file, items, allow_pickle=False)
    np.save(user_file, users, allow_pickle=False)
    item_ids_file.write_text(json.dumps(item_ids))
    user_ids_file.write_text(json.dumps(user_ids))
    return item_file, item_ids_file, user_file, user_ids_file


def test_inner_product_is_not_cosine_similarity() -> None:
    query = np.zeros(FACTORS, dtype=np.float32)
    query[0] = 1.0
    items = np.zeros((2, FACTORS), dtype=np.float32)
    items[0, 0] = 1.0
    items[1, 0] = 3.0  # same direction, larger ALS dot product
    assert exact_top_k(query, items, ("small", "large"), 2) == ["large", "small"]


def test_score_ties_use_item_id_order() -> None:
    query = np.ones(FACTORS, dtype=np.float32)
    items = np.ones((3, FACTORS), dtype=np.float32)
    assert exact_top_k(query, items, ("a", "b", "c"), 3) == ["a", "b", "c"]


def test_loaded_index_returns_full_catalog_order(tmp_path: Path) -> None:
    item_file, item_ids_file, user_file, _user_ids_file = _artifacts(tmp_path)
    vector = np.load(user_file, allow_pickle=False)[0]
    index = ExactItemIndex(factors_file=item_file, ids_file=item_ids_file)
    assert index.search(vector, 25) == exact_top_k(
        vector, np.load(item_file, allow_pickle=False), index.item_ids, 25
    )


def test_index_rejects_unsorted_id_mapping(tmp_path: Path) -> None:
    item_file, item_ids_file, _user_file, _user_ids_file = _artifacts(tmp_path)
    item_ids_file.write_text(json.dumps(["z"] + [f"b{i:03}" for i in range(199)]))
    with pytest.raises(ValueError, match="sorted"):
        ExactItemIndex(factors_file=item_file, ids_file=item_ids_file)


def test_validation_runs_real_queries(tmp_path: Path) -> None:
    item_file, item_ids_file, user_file, user_ids_file = _artifacts(tmp_path)
    report = validate_index(
        ExactItemIndex(factors_file=item_file, ids_file=item_ids_file),
        user_factors_file=user_file,
        user_ids_file=user_ids_file,
        sample_size=12,
        k=25,
    )
    assert report.sampled_users == 12
    assert report.k == 25
    assert report.latency_ms["p99"] > 0
