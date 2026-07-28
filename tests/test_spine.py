"""The spine's property tests: exact correctness, right-exclusivity, future-
invariance, and leak tests proving the invariance checks have teeth.

These run through the *store* — synthetic events are materialised into state groups
and read back via `store.read`, the same two steps production takes. Testing a
separate inline path would prove a property about code nobody runs.

All synthetic (Yelp license). The event stream is small and hand-checkable.
"""

from __future__ import annotations

from collections.abc import Sequence

import duckdb
import pytest

from sift.features.definitions import feature_names
from sift.store.materialize import materialize_into
from sift.store.read import asof_feature_query, read_features

# Base history, all strictly before the queries below.
#   u1: reviews b1@Jan(5 stars), b2@Mar(3 stars)   -> 2 reviews, mean 4.0
#   b1: reviewed by u1@Jan(5), by u9@Feb(4)         -> 2 reviews, mean 4.5
_BASE_EVENTS: list[tuple[object, ...]] = [
    ("u1", "b1", "review", "2018-01-01 00:00:00", 5),
    ("u9", "b1", "review", "2018-02-01 00:00:00", 4),
    ("u1", "b2", "review", "2018-03-01 00:00:00", 3),
]
# Events strictly AFTER the query instant — the "future" the features must ignore.
# Chosen to MOVE every feature if the boundary leaked; an invariance test whose
# future cannot perturb the value under test passes vacuously (ISSUES.md I8). The b2
# event is there specifically for category affinity: b3 is a Coffee venue sharing no
# category with the b1 queried below, so alone it leaves affinity unchanged whether
# the code leaks or not.
_FUTURE_EVENTS: list[tuple[object, ...]] = [
    ("u1", "b3", "review", "2019-01-01 00:00:00", 1),
    ("u5", "b1", "review", "2018-12-01 00:00:00", 2),
    ("u1", "b2", "review", "2018-11-01 00:00:00", 4),  # shares 'Restaurants' with b1
]
# b2 sits exactly 0.05 degrees of latitude north of b1 (~5.56 km); b3 is co-located
# with b1. Round numbers so every expectation below can be checked by hand.
_DIM: list[tuple[object, ...]] = [
    ("b1", 39.95, -75.16, 2, ["Restaurants", "Pizza"]),
    ("b2", 40.00, -75.16, 1, ["Restaurants", "Bars"]),
    ("b3", 39.95, -75.16, 4, ["Coffee"]),
]


def _con(
    events: Sequence[tuple[object, ...]],
    queries: Sequence[tuple[object, ...]],
    dim: Sequence[tuple[object, ...]] = tuple(_DIM),
) -> duckdb.DuckDBPyConnection:
    """Synthetic events -> materialised store -> ready to read. Both production steps."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE events(user_id VARCHAR, business_id VARCHAR, "
        "event_type VARCHAR, ts TIMESTAMP, stars SMALLINT)"
    )
    con.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", events)
    con.execute(
        "CREATE TABLE queries(query_id BIGINT, user_id VARCHAR, "
        "business_id VARCHAR, ts TIMESTAMP)"
    )
    con.executemany("INSERT INTO queries VALUES (?, ?, ?, ?)", queries)
    con.execute(
        "CREATE TABLE dim_business(business_id VARCHAR, latitude DOUBLE, "
        "longitude DOUBLE, price_tier SMALLINT, categories VARCHAR[])"
    )
    con.executemany("INSERT INTO dim_business VALUES (?, ?, ?, ?, ?)", dim)
    materialize_into(con)
    # ALS slice state (D27). One boundary at 2018-01-01, so a query before it gets
    # NULL and a query after it scores against the slice — the as-of selection that
    # keeps retrieval's score from reporting the label.
    con.execute(
        "CREATE TABLE user_als_state(user_id VARCHAR, ts TIMESTAMP, value FLOAT[3])"
    )
    con.execute(
        "CREATE TABLE item_als_state(business_id VARCHAR, ts TIMESTAMP, value FLOAT[3])"
    )
    con.executemany(
        "INSERT INTO user_als_state VALUES (?, ?, ?)",
        [("u1", "2018-01-01 00:00:00", [1.0, 2.0, 0.0])],
    )
    con.executemany(
        "INSERT INTO item_als_state VALUES (?, ?, ?)",
        [("b1", "2018-01-01 00:00:00", [3.0, 1.0, 0.0]),
         ("b2", "2018-01-01 00:00:00", [0.0, 1.0, 0.0])],
    )
    return con


def test_features_match_hand_computed_values() -> None:
    # q1: u1 x b1 as of 2018-06-01.
    #   user: 2 reviews, mean 4.0, 92d since Mar-01;  item(b1): 2 reviews, mean 4.5
    #   centroid = midpoint of b1,b2 = (39.975, -75.16) -> 0.025 deg from b1 ~ 2.78km
    #   affinity: Restaurants seen 2x, Pizza 1x -> matched 3 / 2 reviews = 1.5
    #   price:   user mean (2+1)/2 = 1.5; b1 = 2 -> delta 0.5
    con = _con(_BASE_EVENTS, [(1, "u1", "b1", "2018-06-01 00:00:00")])
    (row,) = read_features(con)
    assert row[:6] == (1, 2, 4.0, 92, 2, 4.5)
    assert row[6] == pytest.approx(2.7799, abs=1e-3)  # ui_distance_km
    assert row[7] == pytest.approx(1.5)               # ui_category_affinity
    # ALS slice boundary is 2018-01-01, which is NOT strictly before this query at
    # 2018-06-01 -- wait, it is: the 2018 slice applies. u1.b1 = 1*3 + 2*1 = 5.
    assert row[8] == pytest.approx(5.0)               # ui_als_score
    assert row[9] == pytest.approx(0.5)               # ui_price_delta


def test_boundary_is_right_exclusive() -> None:
    # q at exactly b2's timestamp: u1's Mar-01 review must NOT count yet.
    #   -> user: 1 review (only Jan-01 @ b1), mean 5.0, 59d; item(b2): 0, NULL
    #   centroid is b1 alone -> full 0.05 deg to b2 ~ 5.56km
    #   affinity: Restaurants 1, Bars 0 -> 1 / 1 review = 1.0
    #   price:   user mean = 2 (b1 only); b2 = 1 -> delta 1.0
    con = _con(_BASE_EVENTS, [(1, "u1", "b2", "2018-03-01 00:00:00")])
    (row,) = read_features(con)
    assert row[:6] == (1, 1, 5.0, 59, 0, None)
    assert row[6] == pytest.approx(5.5598, abs=1e-3)
    assert row[7] == pytest.approx(1.0)
    # Query is 2018-03-01; the only slice boundary is 2018-01-01, strictly before
    # it, so the slice applies: u1.b2 = 1*0 + 2*1 = 2.
    assert row[8] == pytest.approx(2.0)
    assert row[9] == pytest.approx(1.0)


def test_cold_start_query_yields_zero_and_nulls() -> None:
    con = _con(_BASE_EVENTS, [(1, "u_new", "b1", "2018-06-01 00:00:00")])
    (row,) = read_features(con)
    # No history -> no centroid, no taste vector, no price mix: every ui_* is NULL,
    # which LightGBM consumes as a native missing value rather than a fake zero.
    assert row == (1, 0, None, None, 2, 4.5, None, None, None, None)


def test_business_absent_from_the_dimension_yields_null_not_a_crash() -> None:
    con = _con(_BASE_EVENTS, [(1, "u1", "b_unknown", "2018-06-01 00:00:00")])
    (row,) = read_features(con)
    assert row[6] is None and row[7] is None and row[9] is None


def test_future_invariance() -> None:
    """Mutating only post-query events must not change any feature value."""
    query = [(1, "u1", "b1", "2018-06-01 00:00:00")]
    before = read_features(_con(_BASE_EVENTS, query))
    after = read_features(_con(_BASE_EVENTS + _FUTURE_EVENTS, query))
    assert before == after


def test_future_invariance_covers_the_user_x_item_features() -> None:
    """The ui_* features must be invariant for the same reason the others are — and
    the future events chosen here would move all three if the boundary leaked."""
    query = [(1, "u1", "b1", "2018-06-01 00:00:00")]
    (before,) = read_features(_con(_BASE_EVENTS, query))
    (after,) = read_features(_con(_BASE_EVENTS + _FUTURE_EVENTS, query))
    ui = slice(6, 10)
    assert before[ui] == after[ui]
    assert all(v is not None for v in before[ui])  # invariance is not vacuous here


def test_invariance_holds_after_rematerialising_the_store() -> None:
    """The store adds a step the old inline path did not have: state is written once
    and read many times. A stale or over-eager materialisation would break
    point-in-time correctness without touching the read SQL, so re-materialising over
    mutated future events must still leave as-of values untouched."""
    query = [(1, "u1", "b1", "2018-06-01 00:00:00")]
    con = _con(_BASE_EVENTS, query)
    before = read_features(con)
    con.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", _FUTURE_EVENTS)
    materialize_into(con)  # rebuild state over the enlarged event log
    assert read_features(con) == before


def test_leak_test_has_teeth() -> None:
    """A deliberately leaking feature (all-history count, no < t bound) MUST be
    caught by the same future-invariance comparison — else the test is vacuous."""
    query: list[tuple[object, ...]] = [(1, "u1", "b1", "2018-06-01 00:00:00")]
    leaky_sql = (
        "SELECT q.query_id, "
        "(SELECT count(*) FROM events e WHERE e.user_id = q.user_id) "
        "AS u_reviews_all_history "
        "FROM queries q ORDER BY q.query_id"
    )

    def run_leaky(events: Sequence[tuple[object, ...]]) -> list[tuple[object, ...]]:
        return _con(events, query).execute(leaky_sql).fetchall()

    before = run_leaky(_BASE_EVENTS)                  # u1 all-history = 2
    after = run_leaky(_BASE_EVENTS + _FUTURE_EVENTS)  # u1 all-history = 4
    assert before != after  # invariance check correctly flags the leak


def test_leaky_category_affinity_is_caught() -> None:
    """The same teeth check, aimed at the newer machinery: an affinity computed over
    the user's whole history (no as-of bound) must be flagged by invariance."""
    query: list[tuple[object, ...]] = [(1, "u1", "b1", "2018-06-01 00:00:00")]
    leaky_sql = """
        SELECT q.query_id, count(*) AS affinity_all_history
        FROM queries q
        JOIN events e ON e.user_id = q.user_id
        JOIN (SELECT business_id, unnest(categories) AS category FROM dim_business) c
             ON c.business_id = e.business_id
        JOIN (SELECT business_id, unnest(categories) AS category FROM dim_business) qc
             ON qc.business_id = q.business_id AND qc.category = c.category
        GROUP BY q.query_id ORDER BY q.query_id
    """

    def run_leaky(events: Sequence[tuple[object, ...]]) -> list[tuple[object, ...]]:
        return _con(events, query).execute(leaky_sql).fetchall()

    assert run_leaky(_BASE_EVENTS) != run_leaky(_BASE_EVENTS + _FUTURE_EVENTS)


def test_read_column_order_matches_the_registry() -> None:
    """The ranker builds its matrix positionally, so a column emitted out of registry
    order would feed the model a permuted matrix with nothing raising."""
    con = _con(_BASE_EVENTS, [(1, "u1", "b1", "2018-06-01 00:00:00")])
    names = [d[0] for d in con.execute(asof_feature_query()).description]
    assert names == ["query_id", *feature_names()]
