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
from sift.store.online import FeatureQuery, OnlineFeatureStore, RerankInputs


class _Store:
    """Redis stand-in: user vectors, one controllable feature, and rerank inputs."""

    def __init__(
        self,
        vectors: dict[str, list[float]],
        values: dict[str, float],
        *,
        closed: set[str] | None = None,
        reviewed: dict[str, set[str]] | None = None,
        categories: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.vectors = vectors
        self.values = values
        self.closed = closed or set()
        self.reviewed = reviewed or {}
        self.categories = categories or {}
        self.queried: list[list[str]] = []
        self.rerank_queried: list[list[str]] = []

    def lookup_user_embedding(self, user_id: str) -> list[float] | None:
        return self.vectors.get(user_id)

    def lookup(
        self, queries: Sequence[FeatureQuery], features: Sequence[str] | None = None
    ) -> list[tuple[object, ...]]:
        self.queried.append([query.business_id for query in queries])
        return [(query.query_id, self.values[query.business_id]) for query in queries]

    def rerank_inputs(self, user_id: str, business_ids: Sequence[str]) -> RerankInputs:
        self.rerank_queried.append(list(business_ids))
        return RerankInputs(
            reviewed=frozenset(self.reviewed.get(user_id, set())),
            is_open={b: b not in self.closed for b in business_ids},
            categories={b: self.categories.get(b, ()) for b in business_ids},
        )


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


def test_closed_businesses_are_dropped_from_the_served_list(tmp_path: Path) -> None:
    """`is_open` reaches the funnel only here, after the model — it is legitimate
    online and unconstructible historically, so it can never be a feature (D13)."""
    store = _Store(
        {"warm": _warm_vector()},
        {"b1": 10.0, "b2": 5.0, "b3": 1.0},
        closed={"b1"},
    )
    result = _retriever(tmp_path, store).recommend("warm", 3)
    assert [entry.business_id for entry in result.results] == ["b2", "b3"]
    assert result.latency.rerank_ms > 0.0


def test_already_reviewed_businesses_are_dropped_from_the_served_list(tmp_path: Path) -> None:
    store = _Store(
        {"warm": _warm_vector()},
        {"b1": 10.0, "b2": 5.0, "b3": 1.0},
        reviewed={"warm": {"b2"}},
    )
    result = _retriever(tmp_path, store).recommend("warm", 3)
    assert [entry.business_id for entry in result.results] == ["b1", "b3"]


def test_rerank_reads_the_ranked_pool_not_just_the_k_returned(tmp_path: Path) -> None:
    """Truncating to k before rerank would leave nothing to backfill from, and the
    response would come up short exactly when the filters bite hardest."""
    store = _Store(
        {"warm": _warm_vector()},
        {"b1": 10.0, "b2": 5.0, "b3": 1.0},
        closed={"b1"},
    )
    result = _retriever(tmp_path, store).recommend("warm", 1)
    assert store.rerank_queried == [["b1", "b2", "b3"]]
    # b1 is the ranker's winner but closed, so the single slot goes to the runner-up
    # rather than coming back empty.
    assert [entry.business_id for entry in result.results] == ["b2"]


def test_the_cold_fallback_is_reranked_too(tmp_path: Path) -> None:
    """A closed restaurant is no better a recommendation for a user we know nothing
    about, so the popularity path takes a pool and filters it rather than serving
    the head of the list untouched."""
    store = _Store({}, {}, closed={"b3"})
    catalog = [
        PopularityEntry("b3", "three", 30),
        PopularityEntry("b1", "one", 20),
        PopularityEntry("b2", "two", 10),
    ]
    result = _retriever(tmp_path, store, catalog).recommend("cold", 2)
    assert [entry.business_id for entry in result.results] == ["b1", "b2"]
    assert store.queried == [], "a cold user has no personalized pool to score"


def test_diversity_caps_one_category_from_dominating_the_served_list(tmp_path: Path) -> None:
    """Needs four candidates to mean anything: with three the cap defers one and then
    backfills it, so the result is identical to no cap and the test proves nothing."""
    # IDs must be sorted — the index enforces it for deterministic score ties — so the
    # factors are ordered to match c1, p1, p2, p3 rather than by intended rank.
    factors = np.zeros((4, FACTORS), dtype=np.float32)
    factors[:, 0] = [1.0, 4.0, 3.0, 2.0]
    factor_file = tmp_path / "items4.npy"
    ids_file = tmp_path / "ids4.json"
    np.save(factor_file, factors, allow_pickle=False)
    ids_file.write_text(json.dumps(["c1", "p1", "p2", "p3"]))

    store = _Store(
        {"warm": _warm_vector()},
        {"p1": 40.0, "p2": 30.0, "p3": 20.0, "c1": 10.0},
        categories={
            "p1": ("Pizza",),
            "p2": ("Pizza",),
            "p3": ("Pizza",),
            "c1": ("Coffee",),
        },
    )
    retriever = OnlineALSRetriever(
        ExactItemIndex(factors_file=factor_file, ids_file=ids_file),
        cast(OnlineFeatureStore, store),
        [
            PopularityEntry("p1", "P1", 4),
            PopularityEntry("p2", "P2", 3),
            PopularityEntry("p3", "P3", 2),
            PopularityEntry("c1", "C1", 1),
        ],
        cast(lgb.Booster, _Model()),
    )
    # The ranker orders p1 > p2 > p3 > c1. The cap of 2 defers the third Pizza, so the
    # lowest-scored candidate takes the slot instead — that swap is the whole stage.
    kept = [entry.business_id for entry in retriever.recommend("warm", 3).results]
    assert kept == ["p1", "p2", "c1"]
