"""dim_business ingest: the documented parsing traps, and the snapshot blocklist.

Fixtures are synthetic (Yelp license). Each row below encodes one trap named in
DATA.md or found while probing the real dump.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from sift.config import sql_path
from sift.offline.dim_business import DIM_BUSINESS, build_dim_business

# b0 ordinary; b1 price as the *string* 'None'; b2 attributes absent entirely;
# b3 price out of the documented 1-4 band; b4 categories NULL; b5 wrong metro;
# b6 price present but attributes also carry a Python-2 unicode repr value.
_ROWS: list[dict[str, Any]] = [
    {"business_id": "b0", "name": "N0", "city": "Philadelphia", "state": "PA",
     "latitude": 39.95, "longitude": -75.16, "is_open": 1,
     "categories": "Restaurants, Pizza , Italian",
     "attributes": {"RestaurantsPriceRange2": "2"}},
    {"business_id": "b1", "name": "N1", "city": "Philadelphia", "state": "PA",
     "latitude": 39.96, "longitude": -75.17, "is_open": 0,
     "categories": "Bars", "attributes": {"RestaurantsPriceRange2": "None"}},
    {"business_id": "b2", "name": "N2", "city": "Philadelphia", "state": "PA",
     "latitude": 39.97, "longitude": -75.18, "is_open": 1,
     "categories": "Coffee & Tea", "attributes": None},
    {"business_id": "b3", "name": "N3", "city": "Philadelphia", "state": "PA",
     "latitude": 39.98, "longitude": -75.19, "is_open": 1,
     "categories": "Shopping", "attributes": {"RestaurantsPriceRange2": "9"}},
    {"business_id": "b4", "name": "N4", "city": "Philadelphia", "state": "PA",
     "latitude": 39.99, "longitude": -75.20, "is_open": 1,
     "categories": None, "attributes": {"RestaurantsPriceRange2": "1"}},
    {"business_id": "b5", "name": "N5", "city": "Pittsburgh", "state": "PA",
     "latitude": 40.44, "longitude": -79.99, "is_open": 1,
     "categories": "Restaurants", "attributes": {"RestaurantsPriceRange2": "3"}},
    {"business_id": "b6", "name": "N6", "city": "Philadelphia", "state": "PA",
     "latitude": 39.94, "longitude": -75.15, "is_open": 1,
     "categories": "Nightlife", "attributes": {"RestaurantsPriceRange2": "4",
                                               "WiFi": "u'free'"}},
]


def _build(tmp_path: Path) -> tuple[duckdb.DuckDBPyConnection, str]:
    src = tmp_path / "business.json"
    src.write_text("\n".join(json.dumps(r) for r in _ROWS) + "\n")
    out = tmp_path / "dim_business.parquet"
    build_dim_business(
        business_json=src, out_file=out, metro_city="Philadelphia", metro_state="PA"
    )
    return duckdb.connect(), sql_path(out)


def _rows(tmp_path: Path) -> dict[str, tuple[Any, ...]]:
    con, glob = _build(tmp_path)
    out = {
        str(r[0]): tuple(r[1:])
        for r in con.execute(
            f"SELECT business_id, categories, price_tier, is_open, latitude "
            f"FROM read_parquet({glob})"
        ).fetchall()
    }
    con.close()
    return out


def test_metro_scope_excludes_other_cities(tmp_path: Path) -> None:
    assert set(_rows(tmp_path)) == {"b0", "b1", "b2", "b3", "b4", "b6"}


def test_categories_are_split_and_trimmed(tmp_path: Path) -> None:
    rows = _rows(tmp_path)
    assert rows["b0"][0] == ["Restaurants", "Pizza", "Italian"]
    assert rows["b1"][0] == ["Bars"]


def test_null_categories_become_an_empty_list(tmp_path: Path) -> None:
    # Empty list, not NULL: downstream unnest of a NULL would silently drop the row.
    assert _rows(tmp_path)["b4"][0] == []


def test_price_tier_traps(tmp_path: Path) -> None:
    rows = _rows(tmp_path)
    assert rows["b0"][1] == 2       # ordinary quoted digit
    assert rows["b1"][1] is None    # the literal string 'None'
    assert rows["b2"][1] is None    # attributes absent entirely
    assert rows["b3"][1] is None    # outside the documented 1-4 band
    assert rows["b6"][1] == 4       # survives a unicode-repr sibling attribute


def test_is_open_is_ingested_as_a_boolean(tmp_path: Path) -> None:
    rows = _rows(tmp_path)
    assert rows["b0"][2] is True
    assert rows["b1"][2] is False


def test_snapshot_counters_are_not_materialized(tmp_path: Path) -> None:
    """Chokepoint 4, enforced by absence: `stars` and `review_count` are dump-time
    accumulating counters that contain the label event. You cannot leak a column
    that was never ingested."""
    con, glob = _build(tmp_path)
    columns = {
        str(r[0]) for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({glob})").fetchall()
    }
    con.close()
    assert "stars" not in columns
    assert "review_count" not in columns
    assert columns == {
        "business_id", "name", "latitude", "longitude",
        "categories", "price_tier", "is_open",
    }


def test_is_open_is_never_read_as_a_model_feature() -> None:
    """D13: `is_open` is legitimate as a rerank filter and leakage as a feature, so
    it is the one snapshot column presence cannot protect. Assert no feature module
    references it."""
    from sift.features import pit

    source = Path(pit.__file__).read_text()
    assert "is_open" not in source
    assert "is_open" not in " ".join(pit.FEATURE_COLUMNS)


def test_build_is_idempotent(tmp_path: Path) -> None:
    src = tmp_path / "business.json"
    src.write_text("\n".join(json.dumps(r) for r in _ROWS) + "\n")
    out = tmp_path / "dim_business.parquet"
    kw = dict(business_json=src, out_file=out, metro_city="Philadelphia", metro_state="PA")
    first = build_dim_business(**kw)  # type: ignore[arg-type]
    firstbytes = out.read_bytes()
    second = build_dim_business(**kw)  # type: ignore[arg-type]
    assert first == second
    assert out.read_bytes() == firstbytes  # byte-identical, not merely same count


def test_artifact_path_is_under_silver() -> None:
    assert DIM_BUSINESS.parent.name == "silver"
