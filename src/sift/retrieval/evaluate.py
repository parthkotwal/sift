"""Evaluate ALS retrieval against the frozen full-catalog popularity baseline.

ALS is scored over every metro business. We deliberately do not remove businesses
the user reviewed before T: D18 counts a repeat post-T interaction as relevant, so
filtering history here would make valid targets structurally unreachable.

Run: ``python -m sift.retrieval.evaluate``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from sift.eval.holdout import load_ground_truth
from sift.eval.run import EvalReport, evaluate, popularity_recommender
from sift.retrieval.als import ITEM_FACTORS, ITEM_IDS, USER_FACTORS, USER_IDS
from sift.retrieval.index import exact_top_k


class ALSRetriever:
    """Loaded ALS factors behind the common ``(user_id, k) -> ids`` seam."""

    def __init__(
        self,
        user_factors: NDArray[np.float32],
        item_factors: NDArray[np.float32],
        user_ids: Sequence[str],
        item_ids: Sequence[str],
        *,
        cold_fallback: Sequence[str] = (),
    ) -> None:
        if user_factors.shape[0] != len(user_ids):
            raise ValueError("user factors and IDs disagree")
        if item_factors.shape[0] != len(item_ids):
            raise ValueError("item factors and IDs disagree")
        if user_factors.shape[1:] != item_factors.shape[1:]:
            raise ValueError("user and item factor dimensions disagree")
        self.user_factors = user_factors
        self.item_factors = item_factors
        self.user_to_index = {user_id: index for index, user_id in enumerate(user_ids)}
        self.item_ids = tuple(item_ids)
        self.cold_fallback = tuple(cold_fallback)

    @classmethod
    def load(
        cls,
        *,
        user_factors_file: Path = USER_FACTORS,
        item_factors_file: Path = ITEM_FACTORS,
        user_ids_file: Path = USER_IDS,
        item_ids_file: Path = ITEM_IDS,
        cold_fallback: Sequence[str] = (),
    ) -> ALSRetriever:
        user_ids: list[str] = json.loads(user_ids_file.read_text())
        item_ids: list[str] = json.loads(item_ids_file.read_text())
        return cls(
            np.load(user_factors_file, mmap_mode="r", allow_pickle=False),
            np.load(item_factors_file, mmap_mode="r", allow_pickle=False),
            user_ids,
            item_ids,
            cold_fallback=cold_fallback,
        )

    def recommend(self, user_id: str, k: int) -> list[str]:
        user_index = self.user_to_index.get(user_id)
        if user_index is None:
            return list(self.cold_fallback[:k])
        vector = np.asarray(self.user_factors[user_index], dtype=np.float32)
        return exact_top_k(vector, self.item_factors, self.item_ids, k)


@dataclass(frozen=True)
class MarginalHits:
    """Target-pair attribution for two candidate sources at the same cutoff."""

    target_pairs: int
    both: int
    als_only: int
    popularity_only: int
    neither: int

    def render(self, k: int, first: str = "ALS", second: str = "popularity") -> str:
        def share(value: int) -> float:
            return value / self.target_pairs if self.target_pairs else 0.0

        return (
            f"target-pair overlap@{k}\n"
            f"  both             {self.both:>7,}  {share(self.both):.4f}\n"
            f"  {first + ' only':<17}{self.als_only:>7,}  {share(self.als_only):.4f}\n"
            f"  {second + ' only':<17}{self.popularity_only:>7,}  "
            f"{share(self.popularity_only):.4f}\n"
            f"  neither          {self.neither:>7,}  {share(self.neither):.4f}"
        )


def marginal_hits(
    als: Mapping[str, Sequence[str]],
    popularity: Mapping[str, Sequence[str]],
    ground_truth: Mapping[str, set[str]],
    *,
    k: int = 500,
) -> MarginalHits:
    both = als_only = popularity_only = neither = 0
    for user_id, relevant in ground_truth.items():
        als_set = set(als[user_id][:k])
        popularity_set = set(popularity[user_id][:k])
        for item_id in relevant:
            in_als = item_id in als_set
            in_popularity = item_id in popularity_set
            if in_als and in_popularity:
                both += 1
            elif in_als:
                als_only += 1
            elif in_popularity:
                popularity_only += 1
            else:
                neither += 1
    return MarginalHits(
        target_pairs=both + als_only + popularity_only + neither,
        both=both,
        als_only=als_only,
        popularity_only=popularity_only,
        neither=neither,
    )


def compare() -> tuple[EvalReport, EvalReport, MarginalHits]:
    ground_truth = load_ground_truth()
    popularity = popularity_recommender()
    als = ALSRetriever.load()
    popularity_report = evaluate(
        popularity,
        ground_truth,
        name="popularity (pre-T review count)",
        measure_latency=False,
    )
    als_report = evaluate(als.recommend, ground_truth, name="ALS (exact full catalog)")

    # Cache only for attribution so its cost is not confused with request latency.
    als_lists = {user_id: als.recommend(user_id, 500) for user_id in ground_truth}
    popularity_lists = {user_id: popularity(user_id, 500) for user_id in ground_truth}
    marginal = marginal_hits(als_lists, popularity_lists, ground_truth)
    return popularity_report, als_report, marginal


def main() -> None:
    popularity, als, marginal = compare()
    print(popularity.render())
    print(als.render())
    print(marginal.render(500))


if __name__ == "__main__":
    main()
