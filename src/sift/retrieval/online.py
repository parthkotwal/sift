"""Online serving path: Redis user vector -> exact ALS search -> LightGBM ranker.

The full funnel for a warm user, in one request: the versioned user embedding comes
from Redis, exact inner-product search over the item factors narrows the metro
catalog to `SERVING_POOL` candidates, the online store supplies the registered
features for those candidates, and the ALS-conditioned ranker orders them.

The ranker earns this position: once retrieval's own score reached it as time-sliced
ALS state (D27) it beat ALS's raw ordering on the frozen holdout — recall@10
0.0350 -> 0.0386, NDCG@10 0.0269 -> 0.0294. Before that feature existed it *lowered*
both, which is why an earlier version of this module served ALS order directly.

Users absent from the pre-T factor artifact receive the still-runnable popularity
baseline (D25) and are not ranked: with no embedding there is no personalized pool
for the ranker to reorder.
"""

from __future__ import annotations

import argparse
import hashlib
import time

import lightgbm as lgb
import numpy as np

from sift.eval.holdout import load_ground_truth
from sift.eval.metrics import percentile
from sift.offline.popularity import PopularityEntry, load_ranking
from sift.ranking.online import (
    OnlineRecommendation,
    RankedBusiness,
    StageLatency,
)
from sift.ranking.rank import SERVING_POOL
from sift.ranking.train import ALS_RANKER_MODEL, to_float
from sift.rerank.rerank import RERANK_POOL, Candidate, rerank
from sift.retrieval.index import ExactItemIndex
from sift.store.online import LOOKUP_THREADS, FeatureQuery, OnlineFeatureStore

RANKER_THREADS = LOOKUP_THREADS


class OnlineALSRetriever:
    def __init__(
        self,
        index: ExactItemIndex,
        store: OnlineFeatureStore,
        catalog: list[PopularityEntry],
        model: lgb.Booster,
    ) -> None:
        self.index = index
        self.store = store
        self.model = model
        self.popularity = tuple(catalog)
        self.metadata = {entry.business_id: entry for entry in catalog}

    @classmethod
    def load(cls) -> OnlineALSRetriever:
        return cls(
            ExactItemIndex(),
            OnlineFeatureStore(),
            load_ranking(),
            lgb.Booster(model_file=str(ALS_RANKER_MODEL)),
        )

    def _pool_size(self, k: int) -> int:
        """Candidates to retrieve before ranking, clamped to the catalog.

        Production always lands on SERVING_POOL: k is capped at 50 by the API and the
        metro catalog holds 14,568 items. The clamps exist so a small catalog (tests,
        a new metro) cannot ask the index for more items than it has.
        """
        return min(max(k, SERVING_POOL), len(self.index.item_ids))

    def recommend(self, user_id: str, k: int = 10) -> OnlineRecommendation:
        started = time.perf_counter()

        # The embedding lookup is retrieval's own input, so it is charged to the
        # retrieval stage — matching how D25 reported "Redis vector lookup + exact
        # retrieval" as one number. `feature_lookup_ms` below is the ranker's
        # candidate feature fetch, which is what the 20ms allocation was written for.
        #
        # Pinning the generation is charged here too. It is a real Redis round trip
        # (~1.1ms) that every stage depends on, and leaving it before the first timer
        # put it in `overhead` — a bucket that had meant "routing and serialization"
        # and was quietly carrying a network call. An unattributed millisecond is the
        # thing per-stage instrumentation exists to prevent.
        retrieval_started = time.perf_counter()

        # Pin the generation once, for the whole request. The three store reads below
        # each resolve the active generation independently otherwise, so a publication
        # landing mid-request would retrieve with one snapshot, rank with the next, and
        # filter with a third — three atomic snapshots in one response, silently.
        snapshot = self.store.snapshot()
        vector = self.store.lookup_user_embedding(user_id, snapshot=snapshot)
        cold = vector is None
        if cold:
            # RERANK_POOL, not k: the cold path is filtered too, so it needs slack to
            # backfill from. A closed restaurant is no better a recommendation for a
            # user we know nothing about.
            head = self.popularity[: max(k, RERANK_POOL)]
            candidate_ids = [entry.business_id for entry in head]
            fallback_scores = [float(entry.pre_t_reviews) for entry in head]
        else:
            ids, scores = self.index.search_scored(
                np.asarray(vector, dtype=np.float32), self._pool_size(k)
            )
            candidate_ids = ids
            fallback_scores = [float(score) for score in scores]
        retrieval_done = time.perf_counter()

        feature_ms = 0.0
        ranking_ms = 0.0
        if cold:
            chosen = list(zip(candidate_ids, fallback_scores, strict=True))
        else:
            feature_started = time.perf_counter()
            rows = self.store.lookup(
                [
                    FeatureQuery(position, user_id, business_id)
                    for position, business_id in enumerate(candidate_ids)
                ],
                snapshot=snapshot,
            )
            feature_done = time.perf_counter()

            columns = tuple(zip(*(row[1:] for row in rows), strict=True))
            x = np.column_stack([to_float(np.asarray(column, dtype=object)) for column in columns])
            # num_threads=1 for the same reason the store pins DuckDB to one thread:
            # LightGBM defaults to every core, which multiplies against request
            # concurrency instead of adding to it (I31). Scoring 500 rows through a
            # shallow model is ~2.6ms single-threaded, so there is nothing to win.
            ranker_scores = np.asarray(
                self.model.predict(x, num_threads=RANKER_THREADS), dtype=np.float64
            )
            # Stable, so ties fall back to the order retrieval supplied — which for
            # this personalized pool is ALS's own ranking. It matches
            # `reranked_candidate_lists`, the offline path that produced D27's
            # numbers: serving must not order candidates differently from the run
            # that decided the ranker lands.
            # RERANK_POOL, not k: rerank drops candidates, so truncating to k here
            # would leave it nothing to backfill from and the response would come back
            # short exactly when the filters bite hardest.
            order = np.argsort(-ranker_scores, kind="stable")[:RERANK_POOL]
            chosen = [(candidate_ids[int(i)], float(ranker_scores[int(i)])) for i in order]
            ranking_done = time.perf_counter()
            feature_ms = (feature_done - feature_started) * 1_000
            ranking_ms = (ranking_done - feature_done) * 1_000

        # Build step 6. The stage's inputs come from their own store read, not through
        # `lookup()`: `is_open` is legitimate online and unconstructible historically
        # (D13), so it must not be reachable as a feature. See D29.
        rerank_started = time.perf_counter()
        inputs = self.store.rerank_inputs(
            user_id, [business_id for business_id, _ in chosen], snapshot=snapshot
        )
        final = rerank(
            [
                Candidate(
                    business_id=business_id,
                    score=score,
                    is_open=inputs.is_open.get(business_id, False),
                    categories=inputs.categories.get(business_id, ()),
                )
                for business_id, score in chosen
            ],
            k,
            inputs.reviewed,
        )
        rerank_done = time.perf_counter()

        # `score` is whichever stage decided the order: the ranker's score for warm
        # users, pre-T review count for the cold popularity fallback. Rerank removes
        # and reorders but never rescores, so these still mean what they meant.
        results = tuple(
            RankedBusiness(
                business_id=candidate.business_id,
                name=self.metadata[candidate.business_id].name,
                pre_t_reviews=self.metadata[candidate.business_id].pre_t_reviews,
                score=candidate.score,
            )
            for candidate in final
        )
        done = time.perf_counter()
        retrieval_ms = (retrieval_done - retrieval_started) * 1_000
        rerank_ms = (rerank_done - rerank_started) * 1_000
        total_ms = (done - started) * 1_000
        overhead_ms = max(
            0.0, total_ms - retrieval_ms - feature_ms - ranking_ms - rerank_ms
        )
        return OnlineRecommendation(
            results=results,
            latency=StageLatency(
                retrieval_ms=retrieval_ms,
                feature_lookup_ms=feature_ms,
                ranking_ms=ranking_ms,
                rerank_ms=rerank_ms,
                overhead_ms=overhead_ms,
                total_ms=total_ms,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Default 500, not 100: a p99 over 100 samples is the 99th of 100 points, i.e.
    # effectively the maximum, and it moved 27 -> 35ms on this path when the sample
    # grew (I31). This loop is also single-threaded, so it profiles one warm thread
    # rather than the threaded server -- see I31 before quoting these as serving p99s.
    parser.add_argument("--samples", type=int, default=500)
    args = parser.parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    retriever = OnlineALSRetriever.load()
    users = sorted(load_ground_truth(), key=lambda value: hashlib.sha256(value.encode()).digest())[
        : args.samples
    ]
    # Warm Redis, the mapped factor pages, LightGBM's native library, and the
    # per-generation item-ALS relation before recording percentiles.
    retriever.recommend(users[0])
    samples: dict[str, list[float]] = {
        "retrieval": [],
        "feature_lookup": [],
        "ranking": [],
        "rerank": [],
        "overhead": [],
        "total": [],
    }
    for user_id in users:
        latency = retriever.recommend(user_id).latency
        samples["retrieval"].append(latency.retrieval_ms)
        samples["feature_lookup"].append(latency.feature_lookup_ms)
        samples["ranking"].append(latency.ranking_ms)
        samples["rerank"].append(latency.rerank_ms)
        samples["overhead"].append(latency.overhead_ms)
        samples["total"].append(latency.total_ms)
    print(
        f"online ALS -> ranker latency over {len(users)} warm requests "
        f"({SERVING_POOL} candidates each)"
    )
    print(f"  {'stage':<18} {'p50':>9} {'p95':>9} {'p99':>9}")
    for stage, values in samples.items():
        print(
            f"  {stage:<18} {percentile(values, 50):>8.3f}ms "
            f"{percentile(values, 95):>8.3f}ms {percentile(values, 99):>8.3f}ms"
        )


if __name__ == "__main__":
    main()
