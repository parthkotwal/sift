"""ALS evaluation preserves D18 repeats and reports source-marginal hits."""

from __future__ import annotations

import numpy as np

from sift.retrieval.evaluate import ALSRetriever, marginal_hits


def test_als_retriever_scores_whole_catalog_and_keeps_seen_items() -> None:
    users = np.asarray([[1.0, 0.0]], dtype=np.float32)
    items = np.asarray([[3.0, 0.0], [2.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    retriever = ALSRetriever(users, items, ["u1"], ["seen", "new", "other"])

    # "seen" is intentionally first: D18 says a repeat visit remains relevant.
    assert retriever.recommend("u1", 3) == ["seen", "new", "other"]


def test_als_retriever_uses_explicit_cold_start_fallback() -> None:
    users = np.asarray([[1.0]], dtype=np.float32)
    items = np.asarray([[1.0], [0.0]], dtype=np.float32)
    retriever = ALSRetriever(
        users,
        items,
        ["warm"],
        ["b1", "b2"],
        cold_fallback=["popular", "second"],
    )
    assert retriever.recommend("cold", 1) == ["popular"]


def test_marginal_hits_attributes_each_target_pair_once() -> None:
    truth = {"u1": {"both", "als", "pop", "miss"}, "u2": {"als2"}}
    als = {"u1": ["both", "als"], "u2": ["als2"]}
    popularity = {"u1": ["both", "pop"], "u2": ["irrelevant"]}
    report = marginal_hits(als, popularity, truth, k=2)
    assert report.target_pairs == 5
    assert report.both == 1
    assert report.als_only == 2
    assert report.popularity_only == 1
    assert report.neither == 1
