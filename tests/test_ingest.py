"""Canonical event-table ingestion, on synthetic JSON in a temp dir (hermetic)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from sift.offline.ingest import build_events, events_glob


def _write_ndjson(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    business = tmp_path / "business.json"
    review = tmp_path / "review.json"
    out = tmp_path / "events"
    _write_ndjson(
        business,
        [
            {"business_id": "phl1", "city": "Philadelphia", "state": "PA"},
            {"business_id": "phl2", "city": "Philadelphia", "state": "PA"},
            {"business_id": "nyc1", "city": "New York", "state": "NY"},
        ],
    )
    _write_ndjson(
        review,
        [
            {"user_id": "u1", "business_id": "phl1", "stars": 5, "date": "2018-03-01 10:00:00"},
            {"user_id": "u2", "business_id": "phl2", "stars": 3, "date": "2019-06-02 12:00:00"},
            {"user_id": "u3", "business_id": "nyc1", "stars": 4, "date": "2018-01-01 09:00:00"},
        ],
    )
    return business, review, out


def test_only_metro_events_are_ingested(tmp_path: Path) -> None:
    business, review, out = _fixture(tmp_path)
    n = build_events(
        business_json=business, review_json=review, out_dir=out,
        metro_city="Philadelphia", metro_state="PA",
    )
    assert n == 2  # the New York review is filtered out
    con = duckdb.connect()
    ids = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT business_id FROM read_parquet(?, hive_partitioning=true)",
            [events_glob(out)],
        ).fetchall()
    }
    con.close()
    assert ids == {"phl1", "phl2"}


def test_canonical_schema_and_types(tmp_path: Path) -> None:
    business, review, out = _fixture(tmp_path)
    build_events(
        business_json=business, review_json=review, out_dir=out,
        metro_city="Philadelphia", metro_state="PA",
    )
    con = duckdb.connect()
    rows = con.execute(
        "SELECT user_id, business_id, event_type, ts, stars, year "
        "FROM read_parquet(?, hive_partitioning=true) ORDER BY ts",
        [events_glob(out)],
    ).fetchall()
    con.close()
    assert [r[2] for r in rows] == ["review", "review"]  # event_type
    assert [r[0] for r in rows] == ["u1", "u2"]           # user_id
    assert [r[4] for r in rows] == [5, 3]                 # stars carried through
    assert [r[5] for r in rows] == [2018, 2019]           # partition year from ts


def test_ingestion_is_idempotent(tmp_path: Path) -> None:
    business, review, out = _fixture(tmp_path)
    first = build_events(
        business_json=business, review_json=review, out_dir=out,
        metro_city="Philadelphia", metro_state="PA",
    )
    second = build_events(
        business_json=business, review_json=review, out_dir=out,
        metro_city="Philadelphia", metro_state="PA",
    )
    assert first == second == 2  # re-run overwrites cleanly, same result
