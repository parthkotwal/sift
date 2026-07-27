"""Online ALS retrieval: Redis user vector -> exact item-factor search.

The ALS-conditioned ranker did not beat ALS's own ordering, so it does not land.
Warm users are served by the winning model; users absent from the pre-T factor
artifact receive the still-runnable popularity baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import time

import numpy as np

from sift.eval.holdout import load_ground_truth
from sift.eval.metrics import percentile
from sift.offline.popularity import PopularityEntry, load_ranking
from sift.ranking.online import (
    OnlineRecommendation,
    RankedBusiness,
    StageLatency,
)
from sift.retrieval.index import ExactItemIndex
from sift.store.online import OnlineFeatureStore


class OnlineALSRetriever:
    def __init__(
        self,
        index: ExactItemIndex,
        store: OnlineFeatureStore,
        catalog: list[PopularityEntry],
    ) -> None:
        self.index = index
        self.store = store
        self.popularity = tuple(catalog)
        self.metadata = {entry.business_id: entry for entry in catalog}

    @classmethod
    def load(cls) -> OnlineALSRetriever:
        return cls(ExactItemIndex(), OnlineFeatureStore(), load_ranking())

    def recommend(self, user_id: str, k: int = 10) -> OnlineRecommendation:
        started = time.perf_counter()
        feature_started = time.perf_counter()
        vector = self.store.lookup_user_embedding(user_id)
        feature_done = time.perf_counter()

        retrieval_started = time.perf_counter()
        if vector is None:
            candidates = [
                (entry.business_id, float(entry.pre_t_reviews)) for entry in self.popularity[:k]
            ]
        else:
            ids, scores = self.index.search_scored(np.asarray(vector, dtype=np.float32), k)
            candidates = list(zip(ids, (float(score) for score in scores), strict=True))
        retrieval_done = time.perf_counter()
        results = tuple(
            RankedBusiness(
                business_id=business_id,
                name=self.metadata[business_id].name,
                pre_t_reviews=self.metadata[business_id].pre_t_reviews,
                score=score,
            )
            for business_id, score in candidates
        )
        done = time.perf_counter()
        feature_ms = (feature_done - feature_started) * 1_000
        retrieval_ms = (retrieval_done - retrieval_started) * 1_000
        total_ms = (done - started) * 1_000
        overhead_ms = max(0.0, total_ms - feature_ms - retrieval_ms)
        return OnlineRecommendation(
            results=results,
            latency=StageLatency(
                retrieval_ms=retrieval_ms,
                feature_lookup_ms=feature_ms,
                ranking_ms=0.0,
                overhead_ms=overhead_ms,
                total_ms=total_ms,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    args = parser.parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    retriever = OnlineALSRetriever.load()
    users = sorted(load_ground_truth(), key=lambda value: hashlib.sha256(value.encode()).digest())[
        : args.samples
    ]
    retriever.recommend(users[0])  # warm Redis connection and mapped factor pages
    samples: dict[str, list[float]] = {
        "embedding_lookup": [],
        "retrieval": [],
        "overhead": [],
        "total": [],
    }
    for user_id in users:
        latency = retriever.recommend(user_id).latency
        samples["embedding_lookup"].append(latency.feature_lookup_ms)
        samples["retrieval"].append(latency.retrieval_ms)
        samples["overhead"].append(latency.overhead_ms)
        samples["total"].append(latency.total_ms)
    print(f"online ALS latency over {len(users)} warm requests")
    print(f"  {'stage':<18} {'p50':>9} {'p95':>9} {'p99':>9}")
    for stage, values in samples.items():
        print(
            f"  {stage:<18} {percentile(values, 50):>8.3f}ms "
            f"{percentile(values, 95):>8.3f}ms {percentile(values, 99):>8.3f}ms"
        )


if __name__ == "__main__":
    main()
