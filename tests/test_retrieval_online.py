"""Online ALS uses Redis vectors for warm users and popularity only for cold ones."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import numpy as np

from sift.offline.popularity import PopularityEntry
from sift.retrieval.als import FACTORS
from sift.retrieval.index import ExactItemIndex
from sift.retrieval.online import OnlineALSRetriever
from sift.store.online import OnlineFeatureStore


class _EmbeddingStore:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def lookup_user_embedding(self, user_id: str) -> list[float] | None:
        return self.vectors.get(user_id)


def _index(tmp_path: Path) -> ExactItemIndex:
    factors = np.zeros((3, FACTORS), dtype=np.float32)
    factors[:, 0] = [1.0, 3.0, 2.0]
    factor_file = tmp_path / "items.npy"
    ids_file = tmp_path / "ids.json"
    np.save(factor_file, factors, allow_pickle=False)
    ids_file.write_text(json.dumps(["b1", "b2", "b3"]))
    return ExactItemIndex(factors_file=factor_file, ids_file=ids_file)


def test_warm_user_is_ordered_by_redis_vector_dot_product(tmp_path: Path) -> None:
    vector = [0.0] * FACTORS
    vector[0] = 1.0
    retriever = OnlineALSRetriever(
        _index(tmp_path),
        cast(OnlineFeatureStore, _EmbeddingStore({"warm": vector})),
        [
            PopularityEntry("b1", "one", 30),
            PopularityEntry("b2", "two", 20),
            PopularityEntry("b3", "three", 10),
        ],
    )
    result = retriever.recommend("warm", 3)
    assert [entry.business_id for entry in result.results] == ["b2", "b3", "b1"]
    assert result.latency.ranking_ms == 0.0


def test_cold_user_gets_declared_popularity_fallback(tmp_path: Path) -> None:
    retriever = OnlineALSRetriever(
        _index(tmp_path),
        cast(OnlineFeatureStore, _EmbeddingStore({})),
        [
            PopularityEntry("b3", "three", 30),
            PopularityEntry("b1", "one", 20),
            PopularityEntry("b2", "two", 10),
        ],
    )
    assert [entry.business_id for entry in retriever.recommend("cold", 2).results] == [
        "b3",
        "b1",
    ]
