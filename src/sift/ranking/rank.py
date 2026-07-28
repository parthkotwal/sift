"""Plug the trained ranker into the funnel and measure it against the holdout.

Serving path for the dumb+ranker funnel: popularity retrieves the top-500, then
the LightGBM ranker reorders those 500 by predicted score. Features are computed
point-in-time as of T (serving "now" = the split point) by reading the feature
store — the same definitions and the same as-of read path training used, so the
two cannot disagree.

Because the ranker only reorders retrieval's 500, recall@500 is identical to
popularity's (a sanity check); what can move is the ordering — recall@{10,50,100}
and NDCG@10.

## Why this scores the full cross product

The v1 feature set used to be separable — no feature depended on both sides — so
this module scored 26k users + 500 candidates and broadcast the outer product,
buying a ~500x saving. The `ui_*` features deliberately break that: a user x item
feature is not expressible as anything x anything. The saving is therefore gone,
and the tempting replacement — recompute distance/affinity/price in numpy on the
serving side — is exactly the training/serving skew the project exists to prevent
(AGENTS.md: never compute a feature inline in the service).

So the cross product goes through the *same* SQL, in user batches to bound memory:
one definition, one code path, ~13M rows instead of 26.5k. Batching slices the
input, never the definition. Offline eval has no latency budget; the online path
will read materialized values from the store (build step 4) rather than either of
these routes; this module already reads it, just from Parquet rather than Redis.

Run: python -m sift.ranking.rank
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

import duckdb
import lightgbm as lgb
import numpy as np

from sift.config import SPLIT_T
from sift.eval.holdout import load_ground_truth
from sift.eval.run import evaluate, popularity_recommender
from sift.features.definitions import feature_names
from sift.offline.dim_business import DIM_BUSINESS
from sift.offline.popularity import load_ranking
from sift.ranking.train import RANKER_MODEL, to_float
from sift.store.read import asof_feature_query, attach_store

SERVING_POOL = 500  # retrieval returns the top-500; the ranker reorders them
USER_BATCH = 2000  # 2000 x 500 = 1M feature rows per batch


def _batches(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def reranked_lists(
    model: lgb.Booster,
    users: Sequence[str],
    pool: Sequence[str],
    ts: str,
    batch_size: int = USER_BATCH,
    progress: bool = False,
) -> dict[str, list[str]]:
    """Return user_id -> the pool reordered by ranker score (best first)."""
    n_cand = len(pool)
    feat_cols = ", ".join(f"f.{name}" for name in feature_names())
    lists: dict[str, list[str]] = {}

    con = duckdb.connect()
    try:
        # The serving path touches the store and nothing else — no event log is
        # opened here at all, which is chokepoint 2 made visible rather than argued.
        attach_store(con, dim_file=DIM_BUSINESS)
        con.execute("CREATE TABLE pool_t(idx BIGINT, business_id VARCHAR)")
        con.executemany("INSERT INTO pool_t VALUES (?, ?)", list(enumerate(pool)))
        for batch_no, batch in enumerate(_batches(users, batch_size), start=1):
            con.execute("CREATE OR REPLACE TEMP TABLE ub(idx BIGINT, user_id VARCHAR)")
            con.executemany("INSERT INTO ub VALUES (?, ?)", list(enumerate(batch)))
            # query_id = user_index * n_cand + pool_index makes the result rows
            # user-major and pool-ordered, so the reshape below is exact rather
            # than a hope about DuckDB's row order.
            con.execute(
                f"""
                CREATE OR REPLACE TEMP TABLE candidates AS
                SELECT u.idx * {n_cand} + p.idx AS query_id,
                       u.user_id, p.business_id, TIMESTAMP '{ts}' AS ts
                FROM ub u CROSS JOIN pool_t p
                """
            )
            feat = asof_feature_query(queries="candidates")
            data = con.execute(
                f"""
                SELECT {feat_cols}
                FROM candidates c JOIN ({feat}) f ON c.query_id = f.query_id
                ORDER BY c.query_id
                """
            ).fetchnumpy()
            x = np.column_stack([to_float(data[name]) for name in feature_names()])
            assert x.shape[0] == len(batch) * n_cand, "feature rows lost in the join"
            scores = np.asarray(model.predict(x), dtype=np.float64).reshape(len(batch), n_cand)
            # Unstable on purpose, and it is not the same choice as the personalized
            # path below: I6 defers a stable tie-break here so the popularity-pool
            # ranker's numbers keep showing that it has almost no score resolution
            # (18 distinct scores over 500). Making it stable would quietly drag them
            # toward popularity's and hide that finding.
            order = np.argsort(-scores, axis=1)  # best score first, per user
            for i, user_id in enumerate(batch):
                lists[user_id] = [pool[j] for j in order[i]]
            if progress:
                print(f"  scored batch {batch_no}: {len(lists):,}/{len(users):,} users")
    finally:
        con.close()
    return lists


def reranked_candidate_lists(
    model: lgb.Booster,
    users: Sequence[str],
    candidates_by_user: Mapping[str, Sequence[str]],
    ts: str,
    batch_size: int = USER_BATCH,
    progress: bool = False,
) -> dict[str, list[str]]:
    """Rerank a personalized candidate list per user through store features."""
    feat_cols = ", ".join(f"f.{name}" for name in feature_names())
    lists: dict[str, list[str]] = {}
    con = duckdb.connect()
    try:
        attach_store(con, dim_file=DIM_BUSINESS)
        for batch_no, batch in enumerate(_batches(users, batch_size), start=1):
            lengths = {len(candidates_by_user[user_id]) for user_id in batch}
            if len(lengths) != 1:
                raise ValueError("every user in a ranking batch needs the same pool size")
            n_cand = lengths.pop()
            query_ids = np.arange(len(batch) * n_cand, dtype=np.int64)
            user_values = np.repeat(np.asarray(batch), n_cand)
            business_values = np.asarray(
                [business for user in batch for business in candidates_by_user[user]]
            )
            con.register("query_ids", query_ids)
            con.register("users", user_values)
            con.register("businesses", business_values)
            con.execute(
                f"""
                CREATE OR REPLACE TEMP TABLE candidates AS
                SELECT q.column0 AS query_id, u.column0 AS user_id,
                       b.column0 AS business_id, TIMESTAMP '{ts}' AS ts
                FROM query_ids q POSITIONAL JOIN users u POSITIONAL JOIN businesses b
                """
            )
            feat = asof_feature_query(queries="candidates")
            data = con.execute(
                f"""
                SELECT {feat_cols}
                FROM candidates c JOIN ({feat}) f ON c.query_id = f.query_id
                ORDER BY c.query_id
                """
            ).fetchnumpy()
            x = np.column_stack([to_float(data[name]) for name in feature_names()])
            assert x.shape[0] == len(batch) * n_cand, "feature rows lost in the join"
            scores = np.asarray(model.predict(x), dtype=np.float64).reshape(len(batch), n_cand)
            # Stable, unlike `reranked_lists`: ties fall back to the candidate order
            # the provider gave, which for a personalized pool is retrieval's own
            # ranking — the correct funnel behaviour I6 describes. The divergence is
            # deliberate and recorded in I6; do not "unify" them without remeasuring
            # both baselines.
            order = np.argsort(-scores, axis=1, kind="stable")
            for index, user_id in enumerate(batch):
                pool = candidates_by_user[user_id]
                lists[user_id] = [pool[item] for item in order[index]]
            if progress:
                print(f"  scored batch {batch_no}: {len(lists):,}/{len(users):,} users")
    finally:
        con.close()
    return lists


def _resolution(lists: dict[str, list[str]]) -> None:
    """How much of the ranker's output is actually personalised? A user-independent
    reordering of a user-independent baseline cannot beat it (see D20)."""
    full = {tuple(v) for v in lists.values()}
    top10 = {tuple(v[:10]) for v in lists.values()}
    print(
        f"\nresolution: {len(full):,} distinct orderings, "
        f"{len(top10):,} distinct top-10s across {len(lists):,} users"
    )


def main() -> None:
    ground_truth = load_ground_truth()
    users = list(ground_truth)
    baseline = evaluate(popularity_recommender(), ground_truth, name="popularity")
    model = lgb.Booster(model_file=str(RANKER_MODEL))
    pool = [entry.business_id for entry in load_ranking()[:SERVING_POOL]]
    lists = reranked_lists(model, users, pool, str(SPLIT_T), progress=True)

    def recommend(user_id: str, k: int) -> Sequence[str]:
        return lists.get(user_id, pool)[:k]

    ranked = evaluate(
        recommend,
        ground_truth,
        name="popularity -> LightGBM ranker",
        measure_latency=False,
    )
    print()
    print(baseline.render())
    print()
    print(ranked.render())
    _resolution(lists)


if __name__ == "__main__":
    main()
