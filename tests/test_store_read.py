"""The store's read path, and the property that justifies the whole refactor:
reading materialised state must produce *exactly* what recomputing from raw events
produces. If those two ever disagree, the store has become a second implementation —
which is the training/serving skew it exists to prevent.

Synthetic (Yelp license).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from sift.config import sql_path
from sift.features import definitions as defs
from sift.features.pit import compute as pit_compute
from sift.offline.dim_business import build_dim_business
from sift.offline.ingest import build_events
from sift.store.materialize import materialize_historical
from sift.store.read import asof_feature_query, attach_store, get_asof, read_features

_BUSINESSES: list[dict[str, Any]] = [
    {"business_id": "b0", "city": "Philadelphia", "state": "PA", "latitude": 39.95,
     "longitude": -75.16, "is_open": 1, "categories": "Restaurants, Pizza",
     "attributes": {"RestaurantsPriceRange2": "2"}},
    {"business_id": "b1", "city": "Philadelphia", "state": "PA", "latitude": 40.00,
     "longitude": -75.16, "is_open": 1, "categories": "Restaurants, Bars",
     "attributes": {"RestaurantsPriceRange2": "1"}},
    {"business_id": "b2", "city": "Philadelphia", "state": "PA", "latitude": 39.90,
     "longitude": -75.10, "is_open": 0, "categories": "Coffee", "attributes": None},
    {"business_id": "b3", "city": "Philadelphia", "state": "PA", "latitude": 39.98,
     "longitude": -75.20, "is_open": 1, "categories": "Restaurants",
     "attributes": {"RestaurantsPriceRange2": "4"}},
]
_REVIEWS = [
    ("u1", "b0", "2016-01-01", 5), ("u2", "b0", "2016-06-01", 4),
    ("u1", "b1", "2017-01-01", 3), ("u3", "b3", "2017-03-01", 2),
    ("u1", "b3", "2017-09-01", 4), ("u2", "b1", "2018-02-01", 5),
    ("u1", "b2", "2018-05-01", 1), ("u3", "b0", "2018-08-01", 3),
]
# Deliberately mixed: mid-history, an exact event instant (right-exclusive boundary),
# a business the user has already reviewed, a cold-start user, and an unknown business.
_QUERIES: list[tuple[int, str, str, str]] = [
    (1, "u1", "b3", "2018-01-01 00:00:00"),
    (2, "u1", "b1", "2017-01-01 00:00:00"),   # exactly b1's own review instant
    (3, "u2", "b2", "2018-06-01 00:00:00"),
    (4, "u_new", "b0", "2018-06-01 00:00:00"),  # no history at all
    (5, "u3", "b_missing", "2018-09-01 00:00:00"),  # not in the dimension
    (6, "u1", "b0", "2019-01-01 00:00:00"),
]


@pytest.fixture
def con(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    business = tmp_path / "business.json"
    review = tmp_path / "review.json"
    business.write_text("\n".join(json.dumps(r) for r in _BUSINESSES) + "\n")
    review.write_text(
        "\n".join(
            json.dumps({"user_id": u, "business_id": b, "stars": s,
                        "date": f"{d} 12:00:00"})
            for u, b, d, s in _REVIEWS
        ) + "\n"
    )
    events, dim = tmp_path / "events", tmp_path / "dim_business.parquet"
    historical = tmp_path / "historical"
    build_events(business_json=business, review_json=review, out_dir=events,
                 metro_city="Philadelphia", metro_state="PA")
    build_dim_business(business_json=business, out_file=dim,
                       metro_city="Philadelphia", metro_state="PA")
    materialize_historical(events_dir=events, dim_file=dim, out_dir=historical)

    connection = duckdb.connect()
    # Raw sources for the inline path...
    glob = sql_path(events / "**" / "*.parquet")
    connection.execute(
        f"CREATE VIEW events AS SELECT * FROM read_parquet({glob}, hive_partitioning=true)"
    )
    # ...and the materialised store for the read path. Both in one connection so the
    # two can be compared row for row.
    attach_store(connection, historical_dir=historical, dim_file=dim)
    connection.execute(
        "CREATE TABLE queries(query_id BIGINT, user_id VARCHAR, "
        "business_id VARCHAR, ts TIMESTAMP)"
    )
    connection.executemany("INSERT INTO queries VALUES (?, ?, ?, ?)", _QUERIES)
    return connection


def test_store_read_equals_inline_computation(con: duckdb.DuckDBPyConnection) -> None:
    """The headline property. Exact equality, not approximate: both paths divide the
    same integer state with the same arithmetic, so any difference is a real defect,
    not floating-point noise."""
    inline = pit_compute(con)
    stored = read_features(con)
    assert stored == inline


def test_equality_holds_feature_by_feature(con: duckdb.DuckDBPyConnection) -> None:
    """Same property, reported per feature — a whole-row assertion says 'something
    differs', this says which."""
    inline = {row[0]: row[1:] for row in pit_compute(con)}
    for index, name in enumerate(defs.feature_names()):
        stored = {row[0]: row[1] for row in read_features(con, [name])}
        expected = {qid: values[index] for qid, values in inline.items()}
        assert stored == expected, f"{name} differs between the store and inline paths"


def test_boundary_is_right_exclusive_through_the_store(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Query 2 sits exactly on u1's b1 review. That review must not count toward the
    features of the row that predicts it."""
    rows = {r[0]: r for r in read_features(con, ["u_reviews_to_date"])}
    # u1's prior reviews before 2017-01-01: only b0 in 2016 -> 1
    assert rows[2][1] == 1


def test_cold_start_entity_yields_nulls_not_zeros(
    con: duckdb.DuckDBPyConnection,
) -> None:
    stored = {r[0]: r[1:] for r in read_features(con)}
    names = defs.feature_names()
    row = stored[4]  # u_new, no history
    for name, value in zip(names, row, strict=True):
        if name.startswith(("u_reviews", "i_reviews")):
            continue  # counts legitimately COALESCE to 0
        if name == "i_mean_stars_to_date":
            continue  # the business has history even though the user does not
        assert value is None, f"{name} fabricated {value!r} for a user with no history"


def test_business_missing_from_the_dimension_does_not_drop_the_row(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """LEFT JOIN, not JOIN: an unknown business must yield NULL ui_* features rather
    than silently vanishing from the result and misaligning the feature matrix."""
    stored = {r[0]: r for r in read_features(con)}
    assert 5 in stored, "row with an unknown business was dropped"


def test_only_the_required_state_is_joined() -> None:
    """`required_groups` drives the FROM clause: user-only features must not pay for
    the user_category explosion, which is ~5x the other groups."""
    user_only = asof_feature_query(["u_reviews_to_date"])
    assert "user_state" in user_only
    assert "user_category_state" not in user_only
    assert "item_state" not in user_only

    everything = asof_feature_query()
    for relation in ("user_state", "item_state", "user_category_state", "dim_business"):
        assert relation in everything


def test_get_asof_matches_the_bulk_path(con: duckdb.DuckDBPyConnection) -> None:
    """The scalar helper is a calling convention over the same query, so it must
    agree with the bulk read exactly."""
    bulk = {r[0]: r[1] for r in read_features(con, ["ui_distance_km"])}
    scalar = get_asof(
        con, "ui_distance_km", user_id="u1", business_id="b3", ts="2018-01-01 00:00:00"
    )
    assert scalar == bulk[1]


def test_unknown_feature_is_rejected_before_any_sql_is_built() -> None:
    with pytest.raises(KeyError, match="unknown feature"):
        asof_feature_query(["u_reviews_next_year"])
