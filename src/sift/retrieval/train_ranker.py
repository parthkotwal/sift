"""Build and train the ranker conditioned on ALS's personalized candidates.

The popularity-conditioned ranker remains runnable as the displaced baseline.
Candidate source is part of a ranker's training contract (D20), not a serving-time
switch that can safely reuse the same model.

Run: ``python -m sift.retrieval.train_ranker``.
"""

from __future__ import annotations

from sift.offline.training_set import ALS_TRAINING_SET, build_training_set
from sift.ranking.train import ALS_RANKER_MODEL, train
from sift.retrieval.evaluate import ALSRetriever


def main() -> None:
    retriever = ALSRetriever.load()
    rows = build_training_set(
        out_file=ALS_TRAINING_SET,
        candidate_provider=retriever.recommend,
    )
    print(f"ALS-conditioned training rows: {rows:,} -> {ALS_TRAINING_SET}")
    model = train(training_set=ALS_TRAINING_SET, model_file=ALS_RANKER_MODEL)
    print(f"best iteration: {model.best_iteration}")
    print(f"best val NDCG@10: {model.best_score['val']['ndcg@10']:.4f}")
    print(f"model saved -> {ALS_RANKER_MODEL}")


if __name__ == "__main__":
    main()
