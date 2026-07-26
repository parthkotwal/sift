"""Registry invariants — the properties that make the registry a chokepoint rather
than a lookup table.

The important one is `test_every_expression_compiles`: an expression is SQL text, so
a typo is not a Python error. It fails at query-compile time, in a job, minutes in.
Compiling each definition against a synthetic fixture moves that failure to the test
suite — the cost D23 accepted for the expression model, paid here.
"""

from __future__ import annotations

import duckdb
import pytest

from sift.features import definitions as defs


def test_registry_is_ordered_and_unique() -> None:
    """The ranker builds its matrix positionally from this order, so duplicates or a
    name/key mismatch would feed the model a permuted or truncated matrix silently.
    (Read-side order is asserted against the registry in test_spine.py.)"""
    names = defs.feature_names()
    assert len(names) == len(set(names))
    assert all(defs.get(n).name == n for n in names)


def test_every_definition_states_a_leakage_argument() -> None:
    """AGENTS.md requires one per feature. Non-defaulted field + this test = a
    definition cannot exist without one."""
    for name, d in defs.REGISTRY.items():
        assert d.leakage.strip(), f"{name} has no leakage argument"
        assert len(d.leakage) > 40, f"{name}'s leakage argument is too thin to be real"


def test_definitions_declare_known_state_groups() -> None:
    for name, d in defs.REGISTRY.items():
        assert d.reads, f"{name} reads no state"
        for group in d.reads:
            assert group in defs.STATE_GROUPS, f"{name} reads unknown group {group!r}"


def test_user_x_item_features_read_more_than_one_entity() -> None:
    """The property that made D23 necessary: these cannot be materialised under a
    single entity key, which is why they are derived at read time."""
    for name, d in defs.REGISTRY.items():
        if d.entity == "user x item":
            assert len(d.reads) >= 2, f"{name} claims user x item but reads {d.reads}"


def test_no_definition_sources_a_blocklisted_snapshot_column() -> None:
    """Chokepoint 4. `stars`/`review_count` are never ingested, but `is_open` is (rerank
    needs it), so only discipline keeps it out of a model feature — assert it."""
    for name, d in defs.REGISTRY.items():
        expr = d.expr.lower()
        assert "is_open" not in expr, f"{name} sources is_open — D13 forbids it"
        assert "review_count" not in expr, f"{name} sources a dump-time counter"


def test_versions_are_positive_integers() -> None:
    for name, d in defs.REGISTRY.items():
        assert isinstance(d.version, int) and d.version >= 1, name


def test_get_rejects_unknown_names_with_a_useful_message() -> None:
    with pytest.raises(KeyError, match="unknown feature"):
        defs.get("u_reviews_next_year")


def test_required_groups_is_the_union_of_what_is_asked_for() -> None:
    assert defs.required_groups(("u_reviews_to_date",)) == {"user"}
    assert defs.required_groups(("ui_distance_km",)) == {"user", "business"}
    assert defs.required_groups() == {"user", "item", "user_category", "business"}


def _fixture() -> duckdb.DuckDBPyConnection:
    """Minimal relations matching the aliases every expression is written against."""
    con = duckdb.connect()
    con.execute(
        """
        CREATE TABLE q AS SELECT 1::BIGINT AS query_id, 'u1' AS user_id,
               'b1' AS business_id, TIMESTAMP '2018-06-01' AS ts;
        -- lat/long state is fixed-point (state.GEO_SCALE): 79.9 and -150.32 degrees
        -- summed over two businesses, scaled by 1e7 and held as exact integers.
        CREATE TABLE u AS SELECT 'u1' AS user_id, TIMESTAMP '2018-03-01' AS ts,
               2::BIGINT AS cum_count, 8.0 AS cum_sum,
               799000000::BIGINT AS cum_lat_e7, -1503200000::BIGINT AS cum_lng_e7,
               2::BIGINT AS cum_geo_n, 3.0 AS cum_price,
               2::BIGINT AS cum_price_n;
        CREATE TABLE i AS SELECT 'b1' AS business_id, TIMESTAMP '2018-02-01' AS ts,
               2::BIGINT AS cum_count, 9.0 AS cum_sum;
        CREATE TABLE uc AS SELECT 1::BIGINT AS query_id, 3.0 AS matched;
        CREATE TABLE b AS SELECT 'b1' AS business_id, 39.95 AS latitude,
               -75.16 AS longitude, 2::SMALLINT AS price_tier;
        """
    )
    return con


@pytest.mark.parametrize("name", defs.feature_names())
def test_every_expression_compiles_and_returns_one_value(name: str) -> None:
    con = _fixture()
    definition = defs.get(name)
    rows = con.execute(
        f"SELECT {definition.expr} AS {name} "
        "FROM q LEFT JOIN u ON q.user_id = u.user_id "
        "LEFT JOIN i ON q.business_id = i.business_id "
        "LEFT JOIN uc ON q.query_id = uc.query_id "
        "LEFT JOIN b ON q.business_id = b.business_id"
    ).fetchall()
    con.close()
    assert len(rows) == 1 and len(rows[0]) == 1


@pytest.mark.parametrize("name", defs.feature_names())
def test_every_expression_survives_a_cold_start_entity(name: str) -> None:
    """A user with no history joins to NULL state. Expressions must yield NULL, not
    raise and not fabricate a zero — trees read NULL as 'missing', which is the truth."""
    con = _fixture()
    con.execute("DELETE FROM u")  # no prior events for this user
    con.execute("DELETE FROM uc")
    definition = defs.get(name)
    (row,) = con.execute(
        f"SELECT {definition.expr} AS {name} "
        "FROM q LEFT JOIN u ON q.user_id = u.user_id "
        "LEFT JOIN i ON q.business_id = i.business_id "
        "LEFT JOIN uc ON q.query_id = uc.query_id "
        "LEFT JOIN b ON q.business_id = b.business_id"
    ).fetchall()
    con.close()
    # u_* counts COALESCE to 0; everything depending on absent state must be NULL.
    if name in {"u_reviews_to_date", "i_reviews_to_date", "i_mean_stars_to_date"}:
        return
    assert row[0] is None, f"{name} fabricated {row[0]!r} for a user with no history"
