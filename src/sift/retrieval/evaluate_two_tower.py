"""Gate the fixed two-tower against ALS on the frozen full-catalog holdout.

Run: ``python -m sift.retrieval.evaluate_two_tower``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from sift.eval.holdout import load_ground_truth
from sift.eval.ledger import report_and_exit_code
from sift.eval.run import evaluate
from sift.retrieval import two_tower
from sift.retrieval.evaluate import ALSRetriever, marginal_hits


class _CachedRecommendations:
    """Cache after the timed first call so attribution does not score twice."""

    def __init__(self, retriever: ALSRetriever) -> None:
        self.retriever = retriever
        self.values: dict[str, list[str]] = {}

    def recommend(self, user_id: str, k: int) -> Sequence[str]:
        cached = self.values.get(user_id)
        if cached is None or len(cached) < k:
            cached = self.retriever.recommend(user_id, k)
            self.values[user_id] = cached
        return cached[:k]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accept",
        action="store_true",
        help="record a regression as the new baseline; say why in DECISIONS.md",
    )
    args = parser.parse_args()
    ground_truth = load_ground_truth()
    als_cached = _CachedRecommendations(ALSRetriever.load())
    tower_cached = _CachedRecommendations(
        ALSRetriever.load(
            user_factors_file=two_tower.USER_FACTORS,
            item_factors_file=two_tower.ITEM_FACTORS,
            user_ids_file=two_tower.USER_IDS,
            item_ids_file=two_tower.ITEM_IDS,
        )
    )
    als_report = evaluate(als_cached.recommend, ground_truth, name="ALS (exact full catalog)")
    tower_report = evaluate(
        tower_cached.recommend,
        ground_truth,
        name="two-tower v1 (exact full catalog)",
    )
    marginal = marginal_hits(
        tower_cached.values,
        als_cached.values,
        ground_truth,
    )
    print(als_report.render())
    print(tower_report.render())
    print("two-tower vs ALS")
    print(marginal.render(500, first="two-tower", second="ALS"))
    # The challenger is tracked too. D26 rejected it, but "nothing lands without
    # beating its predecessor" is a rule about candidates as much as incumbents, and a
    # rejected model whose number silently drifts makes the rejection unverifiable.
    raise SystemExit(
        report_and_exit_code([als_report, tower_report], accept=args.accept)
    )


if __name__ == "__main__":
    main()
