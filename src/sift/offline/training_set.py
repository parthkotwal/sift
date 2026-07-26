"""Assemble the ranker training set: positives + sampled negatives + PIT features.

Positives: every pre-T review event **of a business in the candidate pool**
(label 1, engagement — DECISIONS.md D19, narrowed by D20). The pool restriction is
the serving conditional: the ranker only ever reorders retrieval's output, so a row
whose positive is unretrievable trains it on a question it is never asked, and — as
D20 documents — lets it separate the classes by pool membership instead of by taste.
Negatives: NEG_RATIO presumed-negatives per positive (label 0), sampled from the same
pool, never a business the user actually reviewed. Features: point-in-time, as-of each
row's ts, read from the feature store — the path the invariance/leak tests guard.

Each positive and its negatives share a `group_id`: the ranker learns to order
the real pick above its sampled alternatives within that group (LightGBM's query
grouping, used in 3c).

Sampling is a pure, seeded function so it is unit-testable and idempotent; the
whole job rebuilds the artifact deterministically. `sample_negatives` yields only
(group_id, business_id) pairs — the user/ts/label come back via a SQL join, so no
multi-million-row structure crosses the Python boundary.

Run: python -m sift.offline.training_set
"""

from __future__ import annotations

import csv
import random
import tempfile
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from datetime import date
from pathlib import Path

import duckdb

from sift.config import DERIVED_DIR, SPLIT_T, sql_path
from sift.features.definitions import feature_names
from sift.offline.dim_business import DIM_BUSINESS
from sift.offline.ingest import EVENTS_DIR
from sift.store.materialize import HISTORICAL_DIR
from sift.store.read import asof_feature_query, attach_store

NEG_RATIO = 4  # negatives per positive (D19); un-tuned, standard starting point
POOL_SIZE = 500  # negatives drawn from the top-500, matching retrieval's output
SEED = 20260722  # fixes the sampled negatives so the training set is reproducible

TRAINING_SET = DERIVED_DIR / "training_set.parquet"


def sample_negatives(
    positives: Iterable[tuple[int, str, str]],
    pool: Sequence[str],
    user_history: Mapping[str, set[str]],
    k: int,
    rng: random.Random,
) -> Iterator[tuple[int, str]]:
    """Yield (group_id, negative_business_id): up to k presumed-negatives per
    positive, drawn uniformly from `pool`, never the positive itself and never a
    business the user reviewed (no false negatives). Fewer than k only when the
    pool cannot supply enough distinct eligible businesses.

    Rejection sampling (draw, skip if ineligible) rather than filtering the whole
    pool per positive — O(k) expected work per positive instead of O(pool).
    """
    n = len(pool)
    if n == 0:
        return
    empty: frozenset[str] = frozenset()
    for group_id, user_id, positive_business in positives:
        reviewed: Collection[str] = user_history.get(user_id, empty)
        picked: set[str] = set()
        attempts = 0
        while len(picked) < k and attempts < k * 20:
            attempts += 1
            candidate = pool[rng.randrange(n)]
            if candidate == positive_business or candidate in reviewed or candidate in picked:
                continue
            picked.add(candidate)
            yield group_id, candidate


def build_training_set(
    *,
    events_dir: Path = EVENTS_DIR,
    dim_file: Path = DIM_BUSINESS,
    store_dir: Path = HISTORICAL_DIR,
    split_t: date = SPLIT_T,
    pool_size: int = POOL_SIZE,
    k: int = NEG_RATIO,
    seed: int = SEED,
    out_file: Path = TRAINING_SET,
) -> int:
    """Build the training-set Parquet and return its row count."""
    glob = sql_path(events_dir / "**" / "*.parquet")
    t = str(split_t)
    con = duckdb.connect()
    try:
        con.execute(
            f"CREATE VIEW events AS SELECT * FROM read_parquet({glob}, hive_partitioning=true)"
        )
        # Features come from the store, never recomputed here (chokepoint 2).
        attach_store(con, historical_dir=store_dir, dim_file=dim_file)
        # The candidate pool, built first because it now scopes the positives too:
        # this is the set retrieval serves, so it defines the ranker's whole world.
        con.execute(
            f"""
            CREATE TABLE pool AS
            SELECT business_id FROM events
            WHERE event_type = 'review' AND ts < TIMESTAMP '{t}'
            GROUP BY business_id
            ORDER BY count(*) DESC, business_id
            LIMIT {pool_size}
            """
        )
        pool = [str(r[0]) for r in con.execute("SELECT business_id FROM pool").fetchall()]
        # Positives: deterministic group_id per pre-T review event of a pool business
        # (D20). Reviews of non-pool businesses are retrieval's problem, not ranking's.
        con.execute(
            f"""
            CREATE TABLE positives AS
            SELECT row_number() OVER (ORDER BY user_id, ts, business_id) AS group_id,
                   user_id, business_id, ts
            FROM (
                SELECT e.user_id, e.business_id, e.ts
                FROM events e
                SEMI JOIN pool p ON e.business_id = p.business_id
                WHERE e.event_type = 'review' AND e.ts < TIMESTAMP '{t}'
            )
            """
        )
        # Exclusion set for negative sampling. Reading it off the (now pool-scoped)
        # positives loses nothing: negatives are drawn from the pool, so a user's
        # reviews of non-pool businesses could never be sampled anyway.
        user_history: dict[str, set[str]] = {}
        for user_id, business_id in con.execute(
            "SELECT DISTINCT user_id, business_id FROM positives"
        ).fetchall():
            user_history.setdefault(str(user_id), set()).add(str(business_id))

        pos_rows = [
            (int(g), str(u), str(b))
            for g, u, b in con.execute(
                "SELECT group_id, user_id, business_id FROM positives"
            ).fetchall()
        ]
        rng = random.Random(seed)
        # Stream the sampled negatives through a temp CSV: DuckDB's bulk CSV read
        # is ~1000x faster than executemany for millions of rows, and nothing
        # larger than one row is held in Python at a time.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", dir=out_file.parent, delete=False, newline=""
        ) as handle:
            neg_csv = Path(handle.name)
            csv.writer(handle).writerows(
                sample_negatives(pos_rows, pool, user_history, k, rng)
            )
        try:
            con.execute(
                f"CREATE TABLE neg AS SELECT * FROM read_csv({sql_path(neg_csv)}, "
                "header=false, columns={'group_id': 'BIGINT', 'business_id': 'VARCHAR'})"
            )
        finally:
            neg_csv.unlink(missing_ok=True)

        # Candidates = positives (label 1) + negatives (label 0), joined to the
        # positive's user/ts by group_id. query_id keys the feature join.
        con.execute(
            """
            CREATE TABLE candidates AS
            SELECT row_number() OVER () AS query_id, group_id, user_id, business_id, ts, label
            FROM (
                SELECT group_id, user_id, business_id, ts, 1 AS label FROM positives
                UNION ALL
                SELECT p.group_id, p.user_id, n.business_id, p.ts, 0 AS label
                FROM neg n JOIN positives p USING (group_id)
            )
            """
        )

        feat = asof_feature_query(queries="candidates")
        # Projected from the registry rather than spelled out, so a feature added
        # to the definition cannot silently fail to reach the training artifact.
        feat_cols = ", ".join(f"f.{name}" for name in feature_names())
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.unlink(missing_ok=True)
        con.execute(
            f"""
            COPY (
                SELECT c.group_id, c.user_id, c.business_id, c.ts, c.label, {feat_cols}
                FROM candidates c
                JOIN ({feat}) f ON c.query_id = f.query_id
                -- Row order within a group must NOT depend on the label. It used to
                -- be `label DESC`, which put the positive first in every group; an
                -- under-trained model scores many candidates identically, ties are
                -- resolved in data order, and the positive therefore landed at rank 1
                -- for free. That inflated the validation NDCG of the *least*-trained
                -- model, so early stopping selected a single tree. business_id alone
                -- is still fully deterministic, so the artifact stays idempotent.
                ORDER BY c.group_id, c.business_id
            ) TO {sql_path(out_file)} (FORMAT PARQUET)
            """
        )
        row = con.execute(
            f"SELECT count(*) FROM read_parquet({sql_path(out_file)})"
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        con.close()


def main() -> None:
    n = build_training_set()
    glob = sql_path(TRAINING_SET)
    con = duckdb.connect()
    try:
        counts = con.execute(
            f"SELECT sum(label), sum(1 - label), count(DISTINCT group_id) "
            f"FROM read_parquet({glob})"
        ).fetchone()
        assert counts is not None
        pos, neg, groups = int(counts[0]), int(counts[1]), int(counts[2])
        print(f"training rows: {n:,}  (positives {pos:,}, negatives {neg:,})")
        print(f"groups: {groups:,}   avg rows/group: {n / groups:.2f}")
        print("\nfeature coverage (non-null fraction):")
        for col in feature_names():
            row = con.execute(
                f"SELECT avg(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END) "
                f"FROM read_parquet({glob})"
            ).fetchone()
            assert row is not None
            print(f"  {col:<22} {row[0]:.3f}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
