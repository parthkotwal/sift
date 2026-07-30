"""Online materialisation, lookup equivalence, and the skew alarm."""

from __future__ import annotations

import builtins
import json
from collections.abc import Callable
from pathlib import Path
from threading import Thread
from typing import cast

import duckdb
import pytest
from redis import Redis

from conftest import write_als_state
from sift.config import sql_path
from sift.offline.dim_business import build_dim_business
from sift.offline.ingest import build_events
from sift.store.materialize import materialize_historical
from sift.store.online import (
    ACTIVE_GENERATION_KEY,
    ITEM_ALS_ALL,
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


def _artifacts(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Synthetic historical store, dimension, and *events*.

    The events directory is returned and threaded into `materialize_online` on purpose.
    It defaults to the real `data/silver/events`, so a test that omits it silently
    publishes 213k production users into the fake Redis and still passes — the I1
    defect exactly ("synthetic tests silently read the real dim_business"). The suite
    tripling in runtime was the only symptom.
    """
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
    # ALS slice state so the publisher has something to snapshot; without it
    # `ui_als_score` would be quietly absent from every online lookup and the skew
    # check would compare eight features while claiming to cover nine.
    write_als_state(
        historical,
        {f"u{i}": [float(i + 1), 1.0, 0.0] for i in range(1, 6)},
        {f"b{j}": [1.0, float(j + 1), 0.0] for j in range(0, 8)},
        boundary="2015-01-01 00:00:00",
    )
    return historical, dim, events


def test_online_lookup_matches_parquet_as_of_now(tmp_path: Path) -> None:
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    manifest = materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    rows = OnlineFeatureStore(_redis(fake)).lookup(
        [FeatureQuery(1, "u1", "b1"), FeatureQuery(2, "u1", "b2")]
    )
    assert manifest.users == 3
    assert manifest.items == 3
    # ui_als_score is asserted by value, not passed through like the distance: u1's
    # published vector is [2, 1, 0] and b1's is [1, 2, 0], so a correct
    # publish -> Redis -> decode -> dot product round trip gives exactly 4.0. A
    # transposed or truncated vector (I26) would not.
    assert rows[0][1:] == (2, 4.0, 31, 2, 4.5, rows[0][6], 1.5, 4.0, 0.5)
    assert rows[1][1:6] == (2, 4.0, 31, 1, 3.0)
    no_price = OnlineFeatureStore(_redis(fake)).lookup([FeatureQuery(3, "u3", "b3")])
    assert no_price[0][-1] is None


def test_user_embedding_is_published_and_read_from_same_generation(tmp_path: Path) -> None:
    historical, dim, events = _artifacts(tmp_path)
    embedding_file = tmp_path / "users.parquet"
    vector = [float(index) for index in range(64)]
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT 'u1' AS user_id, ?::FLOAT[64] AS value) TO "
        f"{sql_path(embedding_file)} (FORMAT PARQUET)",
        [vector],
    )
    con.close()
    fake = _MemoryRedis()
    manifest = materialize_online(
        client=_redis(fake),
        historical_dir=historical,
        dim_file=dim,
        events_dir=events,
        user_embedding_file=embedding_file,
    )
    store = OnlineFeatureStore(_redis(fake))
    assert manifest.user_embeddings == 1
    assert store.lookup_user_embedding("u1") == vector
    assert store.lookup_user_embedding("cold") is None
    generation = fake.values[ACTIVE_GENERATION_KEY]
    key = f"{KEY_PREFIX}:{generation}:embedding:u1"
    record = json.loads(cast(str, fake.values[key]))
    corrupted = json.loads(record["user_embedding_behavioral_v1"])
    corrupted[0] = -999.0
    record["user_embedding_behavioral_v1"] = json.dumps(corrupted)
    fake.values[key] = json.dumps(record)
    report = check_skew(
        store,
        sample_size=1,
        historical_dir=historical,
        dim_file=dim,
        user_embedding_file=embedding_file,
    )
    assert any(m.feature == "user_embedding_behavioral_v1[0]" for m in report.mismatches)


def test_refresh_publishes_a_new_generation_and_retires_the_old(tmp_path: Path) -> None:
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    first = materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    second = materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
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
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    store = OnlineFeatureStore(_redis(fake))
    assert check_skew(
        store, sample_size=2, historical_dir=historical, dim_file=dim
    ).ok

    generation = fake.values[ACTIVE_GENERATION_KEY]
    for key, value in fake.values.items():
        if key.startswith(f"{KEY_PREFIX}:{generation}:user:"):
            assert isinstance(value, str)
            record = json.loads(value)
            record["cum_count"] = "999"
            fake.values[key] = json.dumps(record)
    report = check_skew(
        store, sample_size=2, historical_dir=historical, dim_file=dim
    )
    assert not report.ok
    assert any(m.feature == "u_reviews_to_date" for m in report.mismatches)


def test_skew_check_rejects_a_stale_redis_snapshot(tmp_path: Path) -> None:
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    manifest = materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
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


def test_concurrent_lookups_through_one_store_do_not_contaminate_each_other(
    tmp_path: Path,
) -> None:
    """The property that makes a shared DuckDB database safe (I31).

    Per-request relations are TEMP, which DuckDB scopes to the cursor, so threads
    cannot see each other's `queries`/`user_current` rows. Before the shared database
    each thread had its own connection and this held trivially; it is now load-bearing,
    so it gets a test that would fail if those relations stopped being cursor-local.
    """
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    store = OnlineFeatureStore(_redis(fake))

    pairs = [("u1", "b1"), ("u1", "b2"), ("u3", "b3"), ("u2", "b1")]
    expected = {pair: store.lookup([FeatureQuery(0, *pair)])[0][1:] for pair in pairs}

    wrong: list[str] = []
    failed: list[str] = []

    def hammer(pair: tuple[str, str]) -> None:
        try:
            for _ in range(30):
                got = store.lookup([FeatureQuery(0, *pair)])[0][1:]
                if got != expected[pair]:
                    wrong.append(f"{pair} got {got!r}, expected {expected[pair]!r}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{pair}: {type(exc).__name__}: {exc}")

    threads = [Thread(target=hammer, args=(pair,)) for pair in pairs * 2]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failed, failed[:3]
    assert not wrong, wrong[:3]


def test_item_als_catalog_is_fetched_once_per_process_not_once_per_thread(
    tmp_path: Path,
) -> None:
    """I31's actual defect: the catalog-wide relation was cached per thread, so every
    worker the threadpool created re-paid a ~640ms build over a 19.6MB payload."""
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    manifest = materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    catalog_key = f"{KEY_PREFIX}:{manifest.generation}:item_als:{ITEM_ALS_ALL}"

    fetches: list[str] = []
    inner = fake.get

    def counting_get(key: object) -> object | None:
        if key == catalog_key:
            fetches.append(str(key))
        return inner(key)

    fake.get = counting_get  # type: ignore[method-assign]
    store = OnlineFeatureStore(_redis(fake))

    threads = [Thread(target=lambda: store.lookup([FeatureQuery(0, "u1", "b1")])) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(fetches) == 1, f"catalog fetched {len(fetches)} times across 8 threads"


def test_item_als_relations_are_pinned_to_the_request_generation(tmp_path: Path) -> None:
    """An active-generation swap must not replace state under an old request.

    Redis deliberately keeps the old generation alive for in-flight readers. The
    shared DuckDB cache must do the same: both generation relations remain addressable
    and the read path explicitly selects the one captured by that request.
    """
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    first = materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    store = OnlineFeatureStore(_redis(fake))
    first_row = store.lookup([FeatureQuery(0, "u1", "b1")])
    assert first_row[0][-2] == 4.0

    second = materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    second_key = f"{KEY_PREFIX}:{second.generation}:item_als:{ITEM_ALS_ALL}"
    record = fake.values[second_key]
    assert isinstance(record, str)
    vectors = json.loads(json.loads(record)["vectors"])
    vectors["b1"] = [10.0, 0.0, 0.0]
    fake.values[second_key] = json.dumps({"vectors": json.dumps(vectors)})

    second_row = store.lookup([FeatureQuery(0, "u1", "b1")])
    assert second_row[0][-2] == 20.0

    first_relation = store._item_als_relations[first.generation]
    second_relation = store._item_als_relations[second.generation]
    assert first_relation != second_relation
    assert store._database.execute(
        f"SELECT value FROM {first_relation} WHERE business_id = 'b1'"
    ).fetchone() == ([1.0, 2.0, 0.0],)
    assert store._database.execute(
        f"SELECT value FROM {second_relation} WHERE business_id = 'b1'"
    ).fetchone() == ([10.0, 0.0, 0.0],)


def test_null_item_als_vectors_are_refused_rather_than_served_as_null_features(
    tmp_path: Path,
) -> None:
    """The guard must actually fire. A NULL-filled vector is invisible downstream —
    `ui_als_score` just comes back NULL and nothing raises (I26) — and the obvious
    check for it, `list_contains(value, NULL)`, can never fire (I27). So assert the
    corrupted payload is genuinely rejected, not merely that a guard exists.
    """
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    manifest = materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    catalog_key = f"{KEY_PREFIX}:{manifest.generation}:item_als:{ITEM_ALS_ALL}"

    record = fake.values[catalog_key]
    assert isinstance(record, str)
    vectors = json.loads(json.loads(record)["vectors"])
    corrupted = {business: [None] * len(vector) for business, vector in vectors.items()}
    fake.values[catalog_key] = json.dumps({"vectors": json.dumps(corrupted)})

    store = OnlineFeatureStore(_redis(fake))
    with pytest.raises(OnlineStoreUnavailable, match="NULL"):
        store.lookup([FeatureQuery(0, "u1", "b1")])


def test_reviewed_history_is_published_and_reaches_the_rerank_stage(tmp_path: Path) -> None:
    """The rerank filter's input, whose payoff is measured: already-reviewed
    businesses are 1.3% of the candidate pool but 13% of the served top-10."""
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    manifest = materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    # u1 reviewed b1 and b2; u3 reviewed only b3.
    assert manifest.user_reviewed == 3, "the count must reach the RETURNED manifest (I29)"

    store = OnlineFeatureStore(_redis(fake))
    inputs = store.rerank_inputs("u1", ["b1", "b2", "b3"])
    assert inputs.reviewed == frozenset({"b1", "b2"})
    assert store.rerank_inputs("u3", ["b1"]).reviewed == frozenset({"b3"})
    # A user with no history filters nothing, rather than raising.
    assert store.rerank_inputs("nobody", ["b1"]).reviewed == frozenset()


def test_rerank_inputs_supply_open_state_and_categories(tmp_path: Path) -> None:
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    inputs = OnlineFeatureStore(_redis(fake)).rerank_inputs("u1", ["b1", "b3"])
    assert inputs.is_open == {"b1": True, "b3": True}
    assert inputs.categories["b1"] == ("Restaurants", "Pizza")
    assert inputs.categories["b3"] == ("Coffee",)


def test_a_business_absent_from_the_catalog_is_treated_as_closed(tmp_path: Path) -> None:
    """Fail closed: the alternative surfaces a business the store knows nothing about,
    and rerank is the last stage that can say no."""
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    inputs = OnlineFeatureStore(_redis(fake)).rerank_inputs("u1", ["ghost"])
    assert inputs.is_open["ghost"] is False
    assert inputs.categories["ghost"] == ()


def test_a_generation_written_under_an_older_schema_is_refused(tmp_path: Path) -> None:
    """Why the schema 5 -> 6 bump exists. A rerank-capable reader against a schema-5
    generation would find no reviewed records, filter nothing, and serve the user
    places they have already been — with nothing raised. The guard turns that silent
    degradation into a refusal, so it has to actually fire."""
    historical, dim, events = _artifacts(tmp_path)
    fake = _MemoryRedis()
    manifest = materialize_online(
        client=_redis(fake), historical_dir=historical, dim_file=dim, events_dir=events
    )
    stored = fake.values[f"{KEY_PREFIX}:{manifest.generation}:manifest"]
    assert isinstance(stored, dict)
    stored["schema_version"] = "5"

    store = OnlineFeatureStore(_redis(fake))
    with pytest.raises(OnlineStoreUnavailable, match="schema"):
        store.rerank_inputs("u1", ["b1"])
