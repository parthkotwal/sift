"""Materialised state groups: schema, cumulative semantics, and idempotency.

Synthetic (Yelp license). The fixture is small enough that every persisted value
below is hand-computed in the test's own comments.

Note what is NOT asserted here: that reading this state reproduces the inline
point-in-time features. That equivalence is the read path's property and lands with
`store/read.py` — asserting it before the reader exists would mean writing the
reader twice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from sift.config import sql_path
from sift.features.state import state_query
from sift.offline.dim_business import build_dim_business
from sift.offline.ingest import build_events
from sift.store.materialize import materialize_historical, state_path

# b0 and b1 both 'Restaurants'; b2 shares no category and carries no price at all.
_BUSINESSES: list[dict[str, Any]] = [
    {"business_id": "b0", "city": "Philadelphia", "state": "PA", "latitude": 39.95,
     "longitude": -75.16, "is_open": 1, "categories": "Restaurants, Pizza",
     "attributes": {"RestaurantsPriceRange2": "2"}},
    {"business_id": "b1", "city": "Philadelphia", "state": "PA", "latitude": 40.00,
     "longitude": -75.16, "is_open": 1, "categories": "Restaurants, Bars",
     "attributes": {"RestaurantsPriceRange2": "1"}},
    {"business_id": "b2", "city": "Philadelphia", "state": "PA", "latitude": 39.90,
     "longitude": -75.10, "is_open": 1, "categories": "Coffee", "attributes": None},
]
# u1 reviews b0(5) then b1(3), and b2(2) AFTER the frozen split; u2 reviews b0(4).
_REVIEWS = [
    ("u1", "b0", "2016-01-01", 5),
    ("u2", "b0", "2016-06-01", 4),
    ("u1", "b1", "2017-01-01", 3),
    ("u1", "b2", "2019-06-01", 2),
]


@pytest.fixture
def store(tmp_path: Path) -> tuple[Path, duckdb.DuckDBPyConnection]:
    business = tmp_path / "business.json"
    review = tmp_path / "review.json"
    business.write_text("\n".join(json.dumps(r) for r in _BUSINESSES) + "\n")
    review.write_text(
        "\n".join(
            json.dumps({"user_id": u, "business_id": b, "stars": s,
                        "date": f"{d} 12:00:00"})
            for u, b, d, s in _REVIEWS
        )
        + "\n"
    )
    events = tmp_path / "events"
    dim = tmp_path / "dim_business.parquet"
    out = tmp_path / "historical"
    build_events(business_json=business, review_json=review, out_dir=events,
                 metro_city="Philadelphia", metro_state="PA")
    build_dim_business(business_json=business, out_file=dim,
                       metro_city="Philadelphia", metro_state="PA")
    materialize_historical(events_dir=events, dim_file=dim, out_dir=out)
    return out, duckdb.connect()


def _rows(con: duckdb.DuckDBPyConnection, out: Path, group: str, cols: str,
          where: str = "TRUE") -> list[tuple[Any, ...]]:
    return con.execute(
        f"SELECT {cols} FROM read_parquet({sql_path(state_path(group, out))}) "
        f"WHERE {where} ORDER BY ALL"
    ).fetchall()


def test_user_state_is_cumulative_and_includes_its_own_instant(
    store: tuple[Path, duckdb.DuckDBPyConnection],
) -> None:
    """A state row at ts holds the value INCLUDING that instant's events. The `< t`
    boundary is the reader's job, not the writer's — storing inclusive state is what
    lets one materialisation answer a query at any timestamp."""
    out, con = store
    rows = _rows(con, out, "user", "ts, cum_count, cum_sum, cum_geo_n, cum_price_n",
                 "user_id = 'u1'")
    # 2016 b0(5): 1 review, 5 stars, 1 geo, price 2 known
    # 2017 b1(3): 2 reviews, 8 stars, 2 geo, price 1 known -> 2 priced
    # 2019 b2(2): 3 reviews, 10 stars, 3 geo, b2 has NO price -> still 2 priced
    assert [(r[1], r[2], r[3], r[4]) for r in rows] == [
        (1, 5, 1, 1), (2, 8, 2, 2), (3, 10, 3, 2)
    ]


def test_user_state_centroid_sums_are_exact_fixed_point_integers(
    store: tuple[Path, duckdb.DuckDBPyConnection],
) -> None:
    """Scaled by GEO_SCALE and held as BIGINT, so the running sum is associative and
    therefore identical whatever order the aggregation happens to run in (I18)."""
    out, con = store
    rows = _rows(con, out, "user", "cum_lat_e7, cum_geo_n", "user_id = 'u1'")
    # 39.95, then +40.00 = 79.95, then +39.90 = 119.85 — times 1e7, exactly.
    assert [r[0] for r in rows] == [399_500_000, 799_500_000, 1_198_500_000]
    assert all(isinstance(r[0], int) for r in rows)


def test_item_state_accumulates_across_users(
    store: tuple[Path, duckdb.DuckDBPyConnection],
) -> None:
    out, con = store
    rows = _rows(con, out, "item", "ts, cum_count, cum_sum", "business_id = 'b0'")
    # u1 in Jan-2016 (5 stars), then u2 in Jun-2016 (4) -> 2 reviews, 9 stars
    assert [(r[1], r[2]) for r in rows] == [(1, 5), (2, 9)]


def test_user_category_state_has_one_timeline_per_category_touched(
    store: tuple[Path, duckdb.DuckDBPyConnection],
) -> None:
    out, con = store
    rows = _rows(con, out, "user_category", "category, ts, cum_count", "user_id = 'u1'")
    # b0 = {Restaurants, Pizza}, b1 = {Restaurants, Bars}, b2 = {Coffee}
    # Restaurants is the only category seen twice, and only from 2017 onward.
    by_category: dict[str, list[int]] = {}
    for category, _ts, cum in rows:
        by_category.setdefault(str(category), []).append(int(cum))
    assert by_category == {
        "Restaurants": [1, 2], "Pizza": [1], "Bars": [1], "Coffee": [1]
    }


def test_state_is_not_truncated_at_the_split(
    store: tuple[Path, duckdb.DuckDBPyConnection],
) -> None:
    """The store holds all history; right-exclusivity is applied by the reader. If
    the writer truncated at T, the store could never serve a later 'now'."""
    out, con = store
    post = _rows(con, out, "user", "ts", "ts >= TIMESTAMP '2019-01-01'")
    assert post, "post-split state rows are missing — the writer truncated"


def test_materialization_is_idempotent(tmp_path: Path) -> None:
    """Re-running yields identical content. Compared column-wise rather than by file
    bytes because the user group's cum_lat/cum_lng are float sums, whose last bits
    move with aggregation order (ISSUES.md I18); the integer state is exact and the
    float state is compared at a tolerance far below feature precision."""
    business = tmp_path / "business.json"
    review = tmp_path / "review.json"
    business.write_text("\n".join(json.dumps(r) for r in _BUSINESSES) + "\n")
    review.write_text(
        "\n".join(
            json.dumps({"user_id": u, "business_id": b, "stars": s,
                        "date": f"{d} 12:00:00"})
            for u, b, d, s in _REVIEWS
        )
        + "\n"
    )
    events = tmp_path / "events"
    dim = tmp_path / "dim_business.parquet"
    build_events(business_json=business, review_json=review, out_dir=events,
                 metro_city="Philadelphia", metro_state="PA")
    build_dim_business(business_json=business, out_file=dim,
                       metro_city="Philadelphia", metro_state="PA")

    first_dir, second_dir = tmp_path / "h1", tmp_path / "h2"
    a = materialize_historical(events_dir=events, dim_file=dim, out_dir=first_dir)
    b = materialize_historical(events_dir=events, dim_file=dim, out_dir=second_dir)
    assert a == b

    con = duckdb.connect()
    for group in a:
        diff = con.execute(
            f"""
            SELECT count(*) FROM (
                (SELECT * FROM read_parquet({sql_path(state_path(group, first_dir))})
                 EXCEPT
                 SELECT * FROM read_parquet({sql_path(state_path(group, second_dir))}))
            )
            """
        ).fetchone()
        assert diff is not None
        assert diff[0] == 0, f"{group} state differs between runs"
    con.close()


def test_state_query_rejects_an_unknown_group() -> None:
    with pytest.raises(KeyError, match="unknown state group"):
        state_query("user_embedding")
