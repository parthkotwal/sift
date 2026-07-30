"""The online funnel: Redis vector -> exact ALS pool -> ranker, popularity when cold."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray

from sift.offline.popularity import PopularityEntry
from sift.retrieval.als import FACTORS
from sift.retrieval.index import ExactItemIndex
from sift.retrieval.online import OnlineALSRetriever
from sift.store.online import FeatureQuery, OnlineFeatureStore


class _Store:
    """Redis stand-in: user vectors plus one controllable feature per business."""

    def __init__(self, vectors: dict[str, list[float]], values: dict[str, float]) -> None:
        self.vectors = vectors
        self.values = values
        self.queried: list[list[str]] = []

    def lookup_user_embedding(self, user_id: str) -> list[float] | None:
        return self.vectors.get(user_id)

    def lookup(
        self, queries: Sequence[FeatureQuery], features: Sequence[str] | None = None
    ) -> list[tuple[object, ...]]:
        self.queried.append([query.business_id for query in queries])
        return [(query.query_id, self.values[query.business_id]) for query in queries]


class _Model:
    """Booster stand-in scoring the single feature column the fake store returns."""

    def predict(self, data: NDArray[np.float64], **kwargs: object) -> NDArray[np.float64]:
        return np.asarray(data[:, 0], dtype=np.float64)


def _index(tmp_path: Path) -> ExactItemIndex:
    factors = np.zeros((3, FACTORS), dtype=np.float32)
    factors[:, 0] = [1.0, 3.0, 2.0]  # ALS order: b2, b3, b1
    factor_file = tmp_path / "items.npy"
    ids_file = tmp_path / "ids.json"
    np.save(factor_file, factors, allow_pickle=False)
    ids_file.write_text(json.dumps(["b1", "b2", "b3"]))
    return ExactItemIndex(factors_file=factor_file, ids_file=ids_file)


def _catalog() -> list[PopularityEntry]:
    return [
        PopularityEntry("b1", "one", 30),
        PopularityEntry("b2", "two", 20),
        PopularityEntry("b3", "three", 10),
    ]


def _retriever(
    tmp_path: Path, store: _Store, catalog: list[PopularityEntry] | None = None
) -> OnlineALSRetriever:
    return OnlineALSRetriever(
        _index(tmp_path),
        cast(OnlineFeatureStore, store),
        _catalog() if catalog is None else catalog,
        cast(lgb.Booster, _Model()),
    )


def _warm_vector() -> list[float]:
    vector = [0.0] * FACTORS
    vector[0] = 1.0
    return vector


def test_ranker_order_overrides_the_als_order_it_was_given(tmp_path: Path) -> None:
    # ALS ranks b2 > b3 > b1; the ranker prefers exactly the reverse.
    store = _Store({"warm": _warm_vector()}, {"b1": 10.0, "b2": 0.0, "b3": 5.0})
    result = _retriever(tmp_path, store).recommend("warm", 3)
    assert [entry.business_id for entry in result.results] == ["b1", "b3", "b2"]
    assert [entry.score for entry in result.results] == [10.0, 5.0, 0.0]
    assert result.latency.feature_lookup_ms > 0.0
    assert result.latency.ranking_ms > 0.0


def test_tied_ranker_scores_fall_back_to_retrieval_order(tmp_path: Path) -> None:
    """The funnel property I6 describes: a ranker with no resolution must not
    scramble the incumbent ordering it was asked to refine."""
    store = _Store({"warm": _warm_vector()}, {"b1": 1.0, "b2": 1.0, "b3": 1.0})
    result = _retriever(tmp_path, store).recommend("warm", 3)
    assert [entry.business_id for entry in result.results] == ["b2", "b3", "b1"]


def test_the_whole_pool_is_ranked_not_just_the_k_returned(tmp_path: Path) -> None:
    """Ranking after truncation would make the stage a no-op: the winner here is
    ALS's *last* candidate, so it can only surface if all 3 were scored."""
    store = _Store({"warm": _warm_vector()}, {"b1": 10.0, "b2": 0.0, "b3": 5.0})
    result = _retriever(tmp_path, store).recommend("warm", 1)
    assert [entry.business_id for entry in result.results] == ["b1"]
    assert store.queried == [["b2", "b3", "b1"]]


def test_cold_user_gets_declared_popularity_fallback_unranked(tmp_path: Path) -> None:
    store = _Store({}, {})
    catalog = [
        PopularityEntry("b3", "three", 30),
        PopularityEntry("b1", "one", 20),
        PopularityEntry("b2", "two", 10),
    ]
    result = _retriever(tmp_path, store, catalog).recommend("cold", 2)
    assert [entry.business_id for entry in result.results] == ["b3", "b1"]
    assert store.queried == []  # no embedding means no personalized pool to rank
    assert result.latency.feature_lookup_ms == 0.0
    assert result.latency.ranking_ms == 0.0
