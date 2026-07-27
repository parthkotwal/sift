"""Evaluate the ranker trained on, and served over, ALS top-500 candidates.

Run: ``python -m sift.retrieval.evaluate_ranker``.
"""

from __future__ import annotations

from collections.abc import Sequence

import lightgbm as lgb

from sift.config import SPLIT_T
from sift.eval.holdout import load_ground_truth
from sift.eval.run import evaluate
from sift.ranking.rank import SERVING_POOL, reranked_candidate_lists
from sift.ranking.train import ALS_RANKER_MODEL
from sift.retrieval.evaluate import ALSRetriever


def main() -> None:
    ground_truth = load_ground_truth()
    users = list(ground_truth)
    retriever = ALSRetriever.load()
    candidate_lists = {user_id: retriever.recommend(user_id, SERVING_POOL) for user_id in users}
    als = evaluate(
        lambda user_id, k: candidate_lists[user_id][:k],
        ground_truth,
        name="ALS (exact full catalog)",
        measure_latency=False,
    )
    ranked_lists = reranked_candidate_lists(
        lgb.Booster(model_file=str(ALS_RANKER_MODEL)),
        users,
        candidate_lists,
        str(SPLIT_T),
        progress=True,
    )

    def recommend(user_id: str, k: int) -> Sequence[str]:
        return ranked_lists[user_id][:k]

    ranked = evaluate(
        recommend,
        ground_truth,
        name="ALS -> ALS-conditioned LightGBM ranker",
        measure_latency=False,
    )
    print(als.render())
    print(ranked.render())


if __name__ == "__main__":
    main()
