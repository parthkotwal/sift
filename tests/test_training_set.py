"""Negative sampling properties (pure) + end-to-end assembly on synthetic data."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import duckdb

from sift.config import sql_path
from sift.offline.dim_business import build_dim_business
from sift.offline.ingest import build_events
from sift.offline.training_set import build_training_set, sample_negatives

# ---------- sample_negatives (pure) ----------

_POOL = [f"b{i}" for i in range(20)]


def _neg(
    positives: list[tuple[int, str, str]],
    user_history: Mapping[str, set[str]],
    k: int,
    seed: int = 0,
) -> list[tuple[int, str]]:
    return list(
        sample_negatives(positives, _POOL, user_history, k, random.Random(seed))
    )


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    row = con.execute(sql).fetchone()
    assert row is not None
    return row[0]


def test_never_samples_a_reviewed_business() -> None:
    history = {"u1": {"b0", "b1", "b2", "b3", "b4"}}
    negs = _neg([(1, "u1", "b0")], history, k=5)
    assert all(b not in history["u1"] for _, b in negs)


def test_never_samples_the_positive() -> None:
    negs = _neg([(1, "u1", "b7")], {}, k=10)
    assert all(b != "b7" for _, b in negs)


def test_yields_k_when_pool_is_large_enough() -> None:
    negs = _neg([(1, "u1", "b0")], {}, k=4)
    assert len(negs) == 4
    assert len({b for _, b in negs}) == 4  # distinct within the group


def test_yields_fewer_when_pool_is_exhausted() -> None:
    # 20-business pool, user reviewed 18, positive is 1 more -> only 1 eligible.
    reviewed = {f"b{i}" for i in range(18)}
    negs = _neg([(1, "u1", "b18")], {"u1": reviewed}, k=4)
    assert len(negs) == 1
    assert negs[0][1] == "b19"


def test_is_deterministic_for_a_fixed_seed() -> None:
    pos = [(1, "u1", "b0"), (2, "u2", "b1")]
    assert _neg(pos, {}, k=3, seed=42) == _neg(pos, {}, k=3, seed=42)


# ---------- build_training_set (end to end, synthetic) ----------

T = date(2019, 1, 1)


def _write_ndjson(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _synthetic_events(tmp_path: Path) -> tuple[Path, Path]:
    """Build the events table and the business dimension from one synthetic dump.
    Returns (events_dir, dim_file)."""
    business = tmp_path / "business.json"
    review = tmp_path / "review.json"
    events = tmp_path / "events"
    dim = tmp_path / "dim_business.parquet"
    _write_ndjson(
        business,
        [
            {
                "business_id": f"b{i}", "city": "Philadelphia", "state": "PA",
                "latitude": 39.95 + i / 100, "longitude": -75.16, "is_open": 1,
                "categories": "Restaurants, Pizza" if i % 2 else "Bars",
                "attributes": {"RestaurantsPriceRange2": str(i % 4 + 1)},
            }
            for i in range(8)
        ],
    )
    reviews = [
        ("u1", "b0", "2016-01-01"), ("u1", "b1", "2017-01-01"),
        ("u2", "b0", "2016-06-01"), ("u2", "b2", "2017-06-01"), ("u2", "b3", "2018-06-01"),
        ("u3", "b1", "2016-03-01"),
    ]
    _write_ndjson(
        review,
        [{"user_id": u, "business_id": b, "stars": 5, "date": f"{d} 12:00:00"}
         for u, b, d in reviews],
    )
    build_events(
        business_json=business, review_json=review, out_dir=events,
        metro_city="Philadelphia", metro_state="PA",
    )
    build_dim_business(
        business_json=business, out_file=dim,
        metro_city="Philadelphia", metro_state="PA",
    )
    return events, dim


def test_assembly_has_one_positive_per_group_and_valid_negatives(tmp_path: Path) -> None:
    events, dim = _synthetic_events(tmp_path)
    out = tmp_path / "training_set.parquet"
    n = build_training_set(
        events_dir=events, dim_file=dim, split_t=T, pool_size=6, k=2, seed=1, out_file=out,
    )
    con = duckdb.connect()
    glob = sql_path(out)
    # 6 positives: all reviews are pre-T, and only 4 businesses have any pre-T
    # review, so pool_size=6 admits every one of them. Each group has one label=1.
    assert _scalar(con, f"SELECT sum(label) FROM read_parquet({glob})") == 6
    bad_groups = _scalar(
        con,
        f"SELECT count(*) FROM (SELECT group_id, sum(label) s "
        f"FROM read_parquet({glob}) GROUP BY group_id HAVING s <> 1)",
    )
    assert bad_groups == 0
    # No negative is a business the user actually reviewed (no false negatives).
    reviewed = {("u1", "b0"), ("u1", "b1"), ("u2", "b0"), ("u2", "b2"),
                ("u2", "b3"), ("u3", "b1")}
    negatives = con.execute(
        f"SELECT user_id, business_id FROM read_parquet({glob}) WHERE label = 0"
    ).fetchall()
    con.close()
    assert negatives  # sampling actually produced some
    assert all((u, b) not in reviewed for u, b in negatives)
    assert n >= 6


def test_positives_are_restricted_to_the_candidate_pool(tmp_path: Path) -> None:
    """D20: the ranker only reorders retrieval's output, so every training positive
    must be a business retrieval can actually return. Pre-T counts are b0=2, b1=2,
    b2=1, b3=1, so pool_size=3 admits {b0, b1, b2} and drops u2's review of b3."""
    events, dim = _synthetic_events(tmp_path)
    out = tmp_path / "training_set.parquet"
    build_training_set(
        events_dir=events, dim_file=dim, split_t=T, pool_size=3, k=2, seed=1, out_file=out,
    )
    con = duckdb.connect()
    glob = sql_path(out)
    assert _scalar(con, f"SELECT sum(label) FROM read_parquet({glob})") == 5
    off_pool = con.execute(
        f"SELECT DISTINCT business_id FROM read_parquet({glob}) "
        "WHERE business_id NOT IN ('b0', 'b1', 'b2')"
    ).fetchall()
    con.close()
    # Holds for negatives as well: nothing outside the pool appears at all.
    assert off_pool == []


def test_row_order_within_a_group_does_not_encode_the_label(tmp_path: Path) -> None:
    """LightGBM reads groups as consecutive blocks in file order, and resolves tied
    scores in that order. If the artifact were sorted with the positive first, an
    under-trained model — which ties almost everything — would score the positive at
    rank 1 for free, inflating validation NDCG for the *least*-trained model and
    making early stopping choose a stump. Order within a group must therefore be
    label-independent (and still deterministic, for idempotency)."""
    events, dim = _synthetic_events(tmp_path)
    out = tmp_path / "training_set.parquet"
    build_training_set(
        events_dir=events, dim_file=dim, split_t=T, pool_size=6, k=2, seed=1, out_file=out,
    )
    con = duckdb.connect()
    con.execute("SET threads TO 1")  # single-threaded scan preserves file order
    rows = con.execute(
        f"SELECT group_id, business_id, label FROM read_parquet({sql_path(out)})"
    ).fetchall()
    con.close()
    per_group: dict[int, list[tuple[str, int]]] = {}
    for group_id, business_id, label in rows:
        per_group.setdefault(int(group_id), []).append((str(business_id), int(label)))
    assert per_group
    for members in per_group.values():
        ids = [b for b, _ in members]
        assert ids == sorted(ids), "rows within a group must be ordered by business_id"
    # And the label must not sit at a fixed slot: with the positive placed by id, it
    # cannot be first in every group (it is first only when its id sorts first).
    firsts = [members[0][1] for members in per_group.values() if len(members) > 1]
    assert set(firsts) != {1}, "positive is first in every group — label leaks via order"


def test_assembly_is_idempotent(tmp_path: Path) -> None:
    events, dim = _synthetic_events(tmp_path)
    out = tmp_path / "training_set.parquet"
    kw = dict(events_dir=events, dim_file=dim, split_t=T, pool_size=6, k=2, seed=1,
              out_file=out)
    first = build_training_set(**kw)  # type: ignore[arg-type]
    second = build_training_set(**kw)  # type: ignore[arg-type]
    assert first == second
