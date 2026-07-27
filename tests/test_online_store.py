"""Online materialisation, lookup equivalence, and the skew alarm."""

from __future__ import annotations

import builtins
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

from redis import Redis

from sift.offline.dim_business import build_dim_business
from sift.offline.ingest import build_events
from sift.store.materialize import materialize_historical
from sift.store.online import (
    ACTIVE_GENERATION_KEY,
    KEY_PREFIX,
    FeatureQuery,
    OnlineFeatureStore,
    OnlineStoreUnavailable,
    materialize_online,
)
from sift.store.skew import check_skew


class _Pipeline:
    def __init__(self, redis: _MemoryRedis) -> None:
        self.redis = redis
        self.calls: list[tuple[Callable[..., object], tuple[object, ...], dict[str, object]]] = []

    def _queue(self, fn: Callable[..., object], *args: object, **kwargs: object) -> _Pipeline:
        self.calls.append((fn, args, kwargs))
        return self

    def hset(self, *args: object, **kwargs: object) -> _Pipeline:
        return self._queue(self.redis.hset, *args, **kwargs)

    def hgetall(self, *args: object, **kwargs: object) -> _Pipeline:
        return self._queue(self.redis.hgetall, *args, **kwargs)

    def set(self, *args: object, **kwargs: object) -> _Pipeline:
        return self._queue(self.redis.set, *args, **kwargs)

    def sadd(self, *args: object, **kwargs: object) -> _Pipeline:
        return self._queue(self.redis.sadd, *args, **kwargs)

    def expire(self, *args: object, **kwargs: object) -> _Pipeline:
        return self._queue(self.redis.expire, *args, **kwargs)

    def execute(self) -> list[object]:
        calls, self.calls = self.calls, []
        return [fn(*args, **kwargs) for fn, args, kwargs in calls]


class _MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.expired: set[str] = set()

    def ping(self) -> bool:
        return True

    def pipeline(self, transaction: bool = False) -> _Pipeline:
        del transaction
        return _Pipeline(self)

    def hset(self, key: object, mapping: object) -> int:
        assert isinstance(key, str) and isinstance(mapping, dict)
        current = self.values.setdefault(key, {})
        assert isinstance(current, dict)
        current.update(mapping)
        return len(mapping)

    def hgetall(self, key: object) -> dict[object, object]:
        assert isinstance(key, str)
        value = self.values.get(key, {})
        assert isinstance(value, dict)
        return dict(value)

    def get(self, key: object) -> object | None:
        assert isinstance(key, str)
        return self.values.get(key)

    def mget(self, keys: object) -> list[object | None]:
        assert isinstance(keys, list)
        return [self.get(key) for key in keys]

    def set(self, key: object, value: object) -> bool:
        assert isinstance(key, str)
        self.values[key] = value
        return True

    def sadd(self, key: object, *values: object) -> int:
        assert isinstance(key, str)
        current = self.values.setdefault(key, set())
        assert isinstance(current, set)
        current.update(values)
        return len(values)

    def smembers(self, key: object) -> builtins.set[object]:
        assert isinstance(key, str)
        value = self.values.get(key, set())
        assert isinstance(value, set)
        return set(value)

    def expire(self, key: object, seconds: object) -> bool:
        assert isinstance(key, str) and isinstance(seconds, int)
        self.expired.add(key)
        return True


def _redis(fake: _MemoryRedis) -> Redis:
    return cast(Redis, fake)


def _artifacts(tmp_path: Path) -> tuple[Path, Path]:
    businesses = [
        {
            "business_id": "b1",
            "name": "Alpha",
            "city": "Philadelphia",
            "state": "PA",
            "latitude": 39.95,
            "longitude": -75.16,
            "is_open": 1,
            "categories": "Restaurants, Pizza",
            "attributes": {"RestaurantsPriceRange2": "2"},
        },
        {
            "business_id": "b2",
            "name": "Beta",
            "city": "Philadelphia",
            "state": "PA",
            "latitude": 40.00,
            "longitude": -75.16,
            "is_open": 1,
            "categories": "Restaurants, Bars",
            "attributes": {"RestaurantsPriceRange2": "1"},
        },
        {
            "business_id": "b3",
            "name": "No Price",
            "city": "Philadelphia",
            "state": "PA",
            "latitude": 39.90,
            "longitude": -75.10,
            "is_open": 1,
            "categories": "Coffee",
            "attributes": None,
        },
    ]
    reviews = [
        {"user_id": "u1", "business_id": "b1", "stars": 5, "date": "2018-01-01 12:00:00"},
        {"user_id": "u2", "business_id": "b1", "stars": 4, "date": "2018-02-01 12:00:00"},
        {"user_id": "u1", "business_id": "b2", "stars": 3, "date": "2018-03-01 12:00:00"},
        {"user_id": "u3", "business_id": "b3", "stars": 4, "date": "2018-04-01 12:00:00"},
    ]
    business_json = tmp_path / "business.json"
    review_json = tmp_path / "review.json"
    business_json.write_text("\n".join(json.dumps(row) for row in businesses) + "\n")
    review_json.write_text("\n".join(json.dumps(row) for row in reviews) + "\n")
    events = tmp_path / "events"
    dim = tmp_path / "dim.parquet"
    historical = tmp_path / "historical"
    build_events(
        business_json=business_json,
        review_json=review_json,
        out_dir=events,
        metro_city="Philadelphia",
        metro_state="PA",
    )
    build_dim_business(
        business_json=business_json,
        out_file=dim,
        metro_city="Philadelphia",
        metro_state="PA",
    )
    materialize_historical(events_dir=events, dim_file=dim, out_dir=historical)
    return historical, dim


def test_online_lookup_matches_parquet_as_of_now(tmp_path: Path) -> None:
    historical, dim = _artifacts(tmp_path)
    fake = _MemoryRedis()
    manifest = materialize_online(client=_redis(fake), historical_dir=historical, dim_file=dim)
    rows = OnlineFeatureStore(_redis(fake)).lookup(
        [FeatureQuery(1, "u1", "b1"), FeatureQuery(2, "u1", "b2")]
    )
    assert manifest.users == 3
    assert manifest.items == 3
    assert rows[0][1:] == (2, 4.0, 31, 2, 4.5, rows[0][6], 1.5, 0.5)
    assert rows[1][1:6] == (2, 4.0, 31, 1, 3.0)
    no_price = OnlineFeatureStore(_redis(fake)).lookup([FeatureQuery(3, "u3", "b3")])
    assert no_price[0][-1] is None


def test_refresh_publishes_a_new_generation_and_retires_the_old(tmp_path: Path) -> None:
    historical, dim = _artifacts(tmp_path)
    fake = _MemoryRedis()
    first = materialize_online(client=_redis(fake), historical_dir=historical, dim_file=dim)
    second = materialize_online(client=_redis(fake), historical_dir=historical, dim_file=dim)
    assert first.generation != second.generation
    assert fake.values[ACTIVE_GENERATION_KEY] == second.generation
    assert any(f":{first.generation}:" in key for key in fake.expired)


def test_empty_store_fails_with_an_actionable_error() -> None:
    store = OnlineFeatureStore(_redis(_MemoryRedis()))
    try:
        store.manifest()
    except OnlineStoreUnavailable as exc:
        assert "python -m sift.store.online" in str(exc)
    else:
        raise AssertionError("empty Redis unexpectedly produced a manifest")


def test_skew_check_detects_a_corrupted_online_value(tmp_path: Path) -> None:
    historical, dim = _artifacts(tmp_path)
    fake = _MemoryRedis()
    materialize_online(client=_redis(fake), historical_dir=historical, dim_file=dim)
    store = OnlineFeatureStore(_redis(fake))
    assert check_skew(store, sample_size=2, historical_dir=historical, dim_file=dim).ok

    generation = fake.values[ACTIVE_GENERATION_KEY]
    for key, value in fake.values.items():
        if key.startswith(f"{KEY_PREFIX}:{generation}:user:"):
            assert isinstance(value, str)
            record = json.loads(value)
            record["cum_count"] = "999"
            fake.values[key] = json.dumps(record)
    report = check_skew(store, sample_size=2, historical_dir=historical, dim_file=dim)
    assert not report.ok
    assert any(m.feature == "u_reviews_to_date" for m in report.mismatches)


def test_skew_check_rejects_a_stale_redis_snapshot(tmp_path: Path) -> None:
    historical, dim = _artifacts(tmp_path)
    fake = _MemoryRedis()
    manifest = materialize_online(client=_redis(fake), historical_dir=historical, dim_file=dim)
    manifest_key = f"{KEY_PREFIX}:{manifest.generation}:manifest"
    raw = fake.values[manifest_key]
    assert isinstance(raw, dict)
    raw["as_of"] = "2000-01-01T00:00:00.000001"

    report = check_skew(
        OnlineFeatureStore(_redis(fake)),
        sample_size=2,
        historical_dir=historical,
        dim_file=dim,
    )
    assert not report.ok
    assert report.online_as_of != report.offline_as_of
