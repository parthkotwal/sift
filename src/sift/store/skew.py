"""Training/serving skew check: Redis current values vs Parquet as-of-now.

The sample is deterministic so a failure can be reproduced. Every sampled pair is
read twice: once through the historical right-exclusive path at the Redis
manifest's ``as_of`` timestamp, and once through the current Redis lookup. Any
difference means the two materialisations no longer implement the same definition.

Run: ``python -m sift.store.skew``. A mismatch exits non-zero for CI/monitoring.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from sift.config import sql_path
from sift.features.definitions import feature_names
from sift.offline.dim_business import DIM_BUSINESS
from sift.store.materialize import HISTORICAL_DIR, state_path
from sift.store.online import FeatureQuery, OnlineFeatureStore
from sift.store.read import attach_store, read_features


@dataclass(frozen=True)
class SkewMismatch:
    query_id: int
    feature: str
    offline: object
    online: object


@dataclass(frozen=True)
class SkewReport:
    sampled_pairs: int
    compared_values: int
    offline_as_of: datetime
    online_as_of: datetime
    mismatches: tuple[SkewMismatch, ...]

    @property
    def ok(self) -> bool:
        return self.offline_as_of == self.online_as_of and not self.mismatches


def _equal(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, (float, int)) and isinstance(right, (float, int)):
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    return left == right


def check_skew(
    store: OnlineFeatureStore,
    *,
    sample_size: int = 100,
    historical_dir: Path = HISTORICAL_DIR,
    dim_file: Path = DIM_BUSINESS,
) -> SkewReport:
    """Compare all registered features for deterministic sampled entity pairs."""
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    manifest = store.manifest()
    con = duckdb.connect()
    try:
        attach_store(con, historical_dir=historical_dir, dim_file=dim_file)
        max_row = con.execute(
            "SELECT max(ts) FROM ("
            "SELECT max(ts) AS ts FROM user_state UNION ALL "
            "SELECT max(ts) AS ts FROM item_state UNION ALL "
            "SELECT max(ts) AS ts FROM user_category_state)"
        ).fetchone()
        if max_row is None or max_row[0] is None:
            raise ValueError("historical store has no timeline state")
        source_max_ts = max_row[0]
        assert isinstance(source_max_ts, datetime)
        offline_as_of = source_max_ts + timedelta(microseconds=1)
        users = [
            str(row[0])
            for row in con.execute(
                f"SELECT DISTINCT user_id FROM read_parquet("
                f"{sql_path(state_path('user', historical_dir))}) "
                "ORDER BY hash(user_id), user_id LIMIT ?",
                [sample_size],
            ).fetchall()
        ]
        businesses = [
            str(row[0])
            for row in con.execute(
                f"SELECT business_id FROM read_parquet({sql_path(dim_file)}) "
                "ORDER BY hash(business_id), business_id LIMIT ?",
                [sample_size],
            ).fetchall()
        ]
        n = min(len(users), len(businesses))
        queries = [FeatureQuery(i, users[i], businesses[i]) for i in range(n)]
        con.execute(
            "CREATE TABLE queries(query_id BIGINT, user_id VARCHAR, "
            "business_id VARCHAR, ts TIMESTAMP)"
        )
        con.executemany(
            "INSERT INTO queries VALUES (?, ?, ?, ?)",
            [(q.query_id, q.user_id, q.business_id, offline_as_of) for q in queries],
        )
        offline = read_features(con)
    finally:
        con.close()

    online = store.lookup(queries)
    names = feature_names()
    mismatches: list[SkewMismatch] = []
    for offline_row, online_row in zip(offline, online, strict=True):
        if offline_row[0] != online_row[0]:
            raise AssertionError("offline and online query ordering diverged")
        for index, name in enumerate(names, start=1):
            if not _equal(offline_row[index], online_row[index]):
                query_id = offline_row[0]
                assert isinstance(query_id, int)
                mismatches.append(
                    SkewMismatch(
                        query_id=query_id,
                        feature=name,
                        offline=offline_row[index],
                        online=online_row[index],
                    )
                )
    return SkewReport(
        sampled_pairs=len(queries),
        compared_values=len(queries) * len(names),
        offline_as_of=offline_as_of,
        online_as_of=manifest.as_of,
        mismatches=tuple(mismatches),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=100)
    args = parser.parse_args()
    report = check_skew(OnlineFeatureStore(), sample_size=args.sample_size)
    print(f"sampled {report.sampled_pairs} pairs / {report.compared_values} feature values")
    if report.offline_as_of != report.online_as_of:
        print(
            "skew check: FAIL (Redis snapshot is stale: "
            f"online={report.online_as_of.isoformat()} "
            f"offline={report.offline_as_of.isoformat()})"
        )
        raise SystemExit(1)
    if report.ok:
        print("skew check: PASS (Redis equals Parquet as-of-now)")
        return
    print(f"skew check: FAIL ({len(report.mismatches)} mismatches)")
    for mismatch in report.mismatches[:20]:
        print(
            f"  query={mismatch.query_id} feature={mismatch.feature} "
            f"offline={mismatch.offline!r} online={mismatch.online!r}"
        )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
