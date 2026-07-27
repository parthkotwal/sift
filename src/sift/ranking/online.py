"""Real serving path: popularity retrieval -> Redis features -> LightGBM ranking.

The candidate source remains the accepted popularity baseline until retrieval step
5 replaces it with ALS. What changes here is the expensive middle: each request
fetches current state from Redis and scores the 500 candidates, rather than reading
Parquet or returning a precomputed per-user list. Timings cover where work actually
happens, closing ISSUES.md I3's misleading cached-lookup measurement.

Run ``python -m sift.ranking.online`` for a small per-stage latency sample.
"""

from __future__ import annotations

import argparse
import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass

import lightgbm as lgb
import numpy as np

from sift.eval.holdout import load_ground_truth
from sift.eval.metrics import percentile
from sift.offline.popularity import PopularityEntry, load_ranking
from sift.ranking.rank import SERVING_POOL
from sift.ranking.train import RANKER_MODEL, to_float
from sift.store.online import FeatureQuery, OnlineFeatureStore


@dataclass(frozen=True)
class StageLatency:
    retrieval_ms: float
    feature_lookup_ms: float
    ranking_ms: float
    overhead_ms: float
    total_ms: float


@dataclass(frozen=True)
class RankedBusiness:
    business_id: str
    name: str
    pre_t_reviews: int
    score: float


@dataclass(frozen=True)
class OnlineRecommendation:
    results: tuple[RankedBusiness, ...]
    latency: StageLatency


class OnlineRanker:
    """One request's actual retrieval, feature lookup, and model inference."""

    def __init__(
        self,
        model: lgb.Booster,
        store: OnlineFeatureStore,
        candidates: Sequence[PopularityEntry],
    ) -> None:
        self.model = model
        self.store = store
        self.candidates = tuple(candidates[:SERVING_POOL])

    @classmethod
    def load(cls) -> OnlineRanker:
        return cls(
            lgb.Booster(model_file=str(RANKER_MODEL)),
            OnlineFeatureStore(),
            load_ranking(),
        )

    def recommend(self, user_id: str, k: int = 10) -> OnlineRecommendation:
        started = time.perf_counter()

        retrieval_started = time.perf_counter()
        candidates = self.candidates
        retrieval_done = time.perf_counter()

        queries = [
            FeatureQuery(index, user_id, candidate.business_id)
            for index, candidate in enumerate(candidates)
        ]
        feature_started = time.perf_counter()
        feature_rows = self.store.lookup(queries)
        features_done = time.perf_counter()

        columns = tuple(zip(*(row[1:] for row in feature_rows), strict=True))
        x = np.column_stack([to_float(np.asarray(column, dtype=object)) for column in columns])
        scores = np.asarray(self.model.predict(x), dtype=np.float64)
        # Keep the existing tie behavior until the deliberately deferred I6 choice
        # is revisited; changing it here would silently resolve that open decision.
        order = np.argsort(-scores)
        ranked = tuple(
            RankedBusiness(
                business_id=candidates[index].business_id,
                name=candidates[index].name,
                pre_t_reviews=candidates[index].pre_t_reviews,
                score=float(scores[index]),
            )
            for index in order[:k]
        )
        ranking_done = time.perf_counter()
        done = time.perf_counter()

        retrieval_ms = (retrieval_done - retrieval_started) * 1_000
        feature_ms = (features_done - feature_started) * 1_000
        ranking_ms = (ranking_done - features_done) * 1_000
        total_ms = (done - started) * 1_000
        overhead_ms = max(0.0, total_ms - retrieval_ms - feature_ms - ranking_ms)
        return OnlineRecommendation(
            results=ranked,
            latency=StageLatency(
                retrieval_ms=retrieval_ms,
                feature_lookup_ms=feature_ms,
                ranking_ms=ranking_ms,
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

    ranker = OnlineRanker.load()
    users = sorted(load_ground_truth(), key=lambda value: hashlib.sha256(value.encode()).digest())[
        : args.samples
    ]
    if not users:
        raise SystemExit("holdout has no users; build it before benchmarking")

    # Warm native libraries and the Redis connection before recording percentiles.
    ranker.recommend(users[0])
    samples: dict[str, list[float]] = {
        "retrieval": [],
        "feature_lookup": [],
        "ranking": [],
        "overhead": [],
        "total": [],
    }
    for user_id in users:
        latency = ranker.recommend(user_id).latency
        samples["retrieval"].append(latency.retrieval_ms)
        samples["feature_lookup"].append(latency.feature_lookup_ms)
        samples["ranking"].append(latency.ranking_ms)
        samples["overhead"].append(latency.overhead_ms)
        samples["total"].append(latency.total_ms)

    print(f"online latency over {len(users)} requests (500 candidates each)")
    print(f"  {'stage':<17} {'p50':>9} {'p95':>9} {'p99':>9}")
    for stage, values in samples.items():
        print(
            f"  {stage:<17} {percentile(values, 50):>8.3f}ms "
            f"{percentile(values, 95):>8.3f}ms {percentile(values, 99):>8.3f}ms"
        )


if __name__ == "__main__":
    main()
