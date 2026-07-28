"""Redis materialisation and lookup for current feature state.

Historical Parquet stores every state change; Redis stores only the last row for
each entity. A refresh writes a new generation and changes one pointer only after
all records and its manifest exist. Readers resolve that pointer once per request,
so they see either the complete old snapshot or the complete new one.

The lookup client does not implement feature math. It decodes Redis state into the
standard aliases and asks :mod:`sift.store.read` to project the registry's SQL
expressions. This is the online half of one-definition/two-materialisations (D23).

Run: ``python -m sift.store.online`` after the historical store is materialised.
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import local
from typing import cast
from uuid import uuid4

import duckdb
from redis import Redis
from redis.exceptions import RedisError
from redis.typing import EncodableT

from sift.config import sql_path
from sift.features.definitions import (
    EMBEDDING_REGISTRY,
    USER_BEHAVIORAL_EMBEDDING,
    get,
    get_embedding,
    online_features,
)
from sift.offline.dim_business import DIM_BUSINESS
from sift.store.materialize import HISTORICAL_DIR
from sift.store.read import attach_store, read_current_features

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
KEY_PREFIX = "sift:online"
ACTIVE_GENERATION_KEY = f"{KEY_PREFIX}:active"
GENERATION_INDEX_KEY = f"{KEY_PREFIX}:generations"
SCHEMA_VERSION = 3
PIPELINE_SIZE = 1_000
OLD_GENERATION_TTL_SECONDS = 3_600


class OnlineStoreUnavailable(RuntimeError):
    """The online store is unreachable or has not been materialised."""


@dataclass(frozen=True)
class FeatureQuery:
    query_id: int
    user_id: str
    business_id: str


@dataclass(frozen=True)
class OnlineManifest:
    generation: str
    as_of: datetime
    users: int
    items: int
    user_categories: int
    businesses: int
    user_embeddings: int = 0
    embeddings: tuple[tuple[str, int], ...] = ()


def redis_client(url: str | None = None) -> Redis:
    """Create the decoded-text client used by the materialiser and API."""
    return Redis.from_url(
        url or os.environ.get("SIFT_REDIS_URL", DEFAULT_REDIS_URL),
        decode_responses=True,
    )


def _key(generation: str, group: str, entity_id: str) -> str:
    return f"{KEY_PREFIX}:{generation}:{group}:{entity_id}"


def _manifest_key(generation: str) -> str:
    return f"{KEY_PREFIX}:{generation}:manifest"


def _keys_key(generation: str) -> str:
    return f"{KEY_PREFIX}:{generation}:keys"


def _string(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _mapping(**values: object) -> dict[str, str]:
    return {name: _string(value) for name, value in values.items() if value is not None}


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, str):
        return value
    raise TypeError(f"expected Redis text response, got {type(value).__name__}")


def _decode_hash(raw: Mapping[bytes | str, bytes | str]) -> dict[str, str]:
    return {_decode(key): _decode(value) for key, value in raw.items()}


def _decode_record(raw: object | None) -> dict[str, str]:
    if raw is None:
        return {}
    value = json.loads(_decode(raw))
    if not isinstance(value, dict):
        raise TypeError(f"expected Redis JSON object, got {type(value).__name__}")
    return {str(key): str(item) for key, item in value.items()}


def _redis_mapping(values: Mapping[str, str]) -> Mapping[EncodableT, EncodableT]:
    """Widen a text mapping to redis-py's invariant encodable key/value type."""
    return cast(Mapping[EncodableT, EncodableT], values)


def _optional_int(state: Mapping[str, str], name: str) -> int | None:
    value = state.get(name)
    return int(value) if value is not None else None


def _rows_json(columns: Sequence[str], rows: Sequence[tuple[object, ...]]) -> str:
    """Encode a small relation as one parameter, avoiding row-wise DuckDB binding."""
    return json.dumps(
        [dict(zip(columns, row, strict=True)) for row in rows],
        ensure_ascii=False,
        default=_string,
    )


def _last_rows(relation: str, keys: str) -> str:
    return (
        f"SELECT * FROM {relation} "
        f"QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY ts DESC) = 1 "
        f"ORDER BY {keys}"
    )


def _expire_generation(client: Redis, generation: str) -> None:
    """Keep the previous snapshot alive long enough for in-flight readers."""
    index = _keys_key(generation)
    members = client.smembers(index)
    pipe = client.pipeline(transaction=False)
    pending = 0
    for member in members:
        pipe.expire(_decode(member), OLD_GENERATION_TTL_SECONDS)
        pending += 1
        if pending >= PIPELINE_SIZE:
            pipe.execute()
            pipe = client.pipeline(transaction=False)
            pending = 0
    pipe.expire(index, OLD_GENERATION_TTL_SECONDS)
    pipe.expire(_manifest_key(generation), OLD_GENERATION_TTL_SECONDS)
    pipe.execute()


def materialize_online(
    *,
    client: Redis | None = None,
    historical_dir: Path = HISTORICAL_DIR,
    dim_file: Path = DIM_BUSINESS,
    user_embedding_file: Path | None = None,
    user_embedding_name: str = USER_BEHAVIORAL_EMBEDDING,
) -> OnlineManifest:
    """Write latest entity state to a fresh Redis generation, then activate it."""
    store = client or redis_client()
    try:
        store.ping()
    except RedisError as exc:
        raise OnlineStoreUnavailable(f"cannot reach Redis: {exc}") from exc

    generation = uuid4().hex
    index_key = _keys_key(generation)
    con = duckdb.connect()
    counts = {
        "users": 0,
        "items": 0,
        "user_categories": 0,
        "businesses": 0,
        "user_embeddings": 0,
    }
    pipe = store.pipeline(transaction=False)
    pending = 0
    published = False

    def write_record(key: str, values: Mapping[str, str]) -> None:
        nonlocal pipe, pending
        pipe.set(key, json.dumps(values, ensure_ascii=False))
        pipe.sadd(index_key, key)
        pending += 2
        if pending >= PIPELINE_SIZE:
            pipe.execute()
            pipe = store.pipeline(transaction=False)
            pending = 0

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
        as_of = source_max_ts + timedelta(microseconds=1)

        user_rows = con.execute(_last_rows("user_state", "user_id"))
        while batch := user_rows.fetchmany(PIPELINE_SIZE):
            for row in batch:
                user_id, ts, count, stars, lat, lng, geo_n, price, price_n = row
                write_record(
                    _key(generation, "user", str(user_id)),
                    _mapping(
                        ts=ts,
                        cum_count=count,
                        cum_sum=stars,
                        cum_lat_e7=lat,
                        cum_lng_e7=lng,
                        cum_geo_n=geo_n,
                        cum_price=price,
                        cum_price_n=price_n,
                    ),
                )
                counts["users"] += 1

        item_rows = con.execute(_last_rows("item_state", "business_id"))
        while batch := item_rows.fetchmany(PIPELINE_SIZE):
            for business_id, ts, count, stars in batch:
                write_record(
                    _key(generation, "item", str(business_id)),
                    _mapping(ts=ts, cum_count=count, cum_sum=stars),
                )
                counts["items"] += 1

        # One record per user, one field per category. This keeps a user's taste
        # vector to one Redis value rather than one key per category.
        category_rows = con.execute(_last_rows("user_category_state", "user_id, category"))
        current_user: str | None = None
        category_values: dict[str, str] = {}
        while batch := category_rows.fetchmany(PIPELINE_SIZE):
            for user_id_raw, category, _ts, count in batch:
                user_id = str(user_id_raw)
                if current_user is not None and user_id != current_user:
                    write_record(
                        _key(generation, "user_category", current_user),
                        category_values,
                    )
                    category_values = {}
                current_user = user_id
                category_values[str(category)] = _string(count)
                counts["user_categories"] += 1
        if current_user is not None:
            write_record(_key(generation, "user_category", current_user), category_values)

        business_rows = con.execute(
            "SELECT business_id, name, latitude, longitude, categories, price_tier, "
            "is_open FROM dim_business ORDER BY business_id"
        )
        while batch := business_rows.fetchmany(PIPELINE_SIZE):
            for business_id, name, lat, lng, categories, price, is_open in batch:
                values = _mapping(
                    name=name,
                    latitude=lat,
                    longitude=lng,
                    price_tier=price,
                    is_open=is_open,
                )
                values["categories"] = json.dumps(categories or [], ensure_ascii=False)
                write_record(_key(generation, "business", str(business_id)), values)
                counts["businesses"] += 1

        if user_embedding_file is not None:
            definition = get_embedding(user_embedding_name)
            embedding_rows = con.execute(
                f"SELECT user_id, value FROM read_parquet({sql_path(user_embedding_file)}) "
                "ORDER BY user_id"
            )
            while batch := embedding_rows.fetchmany(PIPELINE_SIZE):
                for user_id, value in batch:
                    vector = [float(component) for component in value]
                    if len(vector) != definition.shape[0]:
                        raise ValueError(
                            f"{definition.name} has {len(vector)} values, expected "
                            f"{definition.shape[0]}"
                        )
                    write_record(
                        _key(generation, "embedding", str(user_id)),
                        {definition.name: json.dumps(vector)},
                    )
                    counts["user_embeddings"] += 1

        if pending:
            pipe.execute()

        manifest_values = _mapping(
            schema_version=SCHEMA_VERSION,
            as_of=as_of,
            source_max_ts=source_max_ts,
            materialized_at=datetime.now(UTC),
            users=counts["users"],
            items=counts["items"],
            user_categories=counts["user_categories"],
            businesses=counts["businesses"],
            definitions=json.dumps(
                {name: get(name).version for name in online_features()}, sort_keys=True
            ),
            embeddings=json.dumps(
                ({definition.name: definition.version} if user_embedding_file else {}),
                sort_keys=True,
            ),
        )
        store.hset(_manifest_key(generation), mapping=_redis_mapping(manifest_values))
        old_raw = store.get(ACTIVE_GENERATION_KEY)
        store.sadd(GENERATION_INDEX_KEY, generation)
        # The sole publication step. Everything above is unreachable to readers
        # until this single command succeeds.
        store.set(ACTIVE_GENERATION_KEY, generation)
        published = True
        if old_raw is not None:
            old = _decode(old_raw)
            if old != generation:
                try:
                    _expire_generation(store, old)
                except RedisError as exc:
                    warnings.warn(
                        f"new generation is active, but old generation {old} "
                        f"could not be expired: {exc}",
                        stacklevel=2,
                    )
    except Exception:
        if not published:
            with suppress(RedisError):
                _expire_generation(store, generation)
        raise
    finally:
        con.close()

    return OnlineManifest(
        generation=generation,
        as_of=as_of,
        users=counts["users"],
        items=counts["items"],
        user_categories=counts["user_categories"],
        businesses=counts["businesses"],
        user_embeddings=counts["user_embeddings"],
        embeddings=(((definition.name, definition.version),) if user_embedding_file else ()),
    )


class OnlineFeatureStore:
    """Batch lookup client: Redis fetch plus registry-expression projection."""

    def __init__(self, client: Redis | None = None) -> None:
        self.client = client or redis_client()
        self._thread = local()

    def manifest(self) -> OnlineManifest:
        try:
            generation_raw = self.client.get(ACTIVE_GENERATION_KEY)
            if generation_raw is None:
                raise OnlineStoreUnavailable(
                    "online store is empty; run `python -m sift.store.online`"
                )
            generation = _decode(generation_raw)
            cached = getattr(self._thread, "manifest", None)
            if isinstance(cached, OnlineManifest) and cached.generation == generation:
                return cached
            raw = _decode_hash(self.client.hgetall(_manifest_key(generation)))
        except RedisError as exc:
            raise OnlineStoreUnavailable(f"cannot read Redis: {exc}") from exc
        if not raw:
            raise OnlineStoreUnavailable(f"active Redis generation {generation!r} has no manifest")
        if int(raw["schema_version"]) != SCHEMA_VERSION:
            raise OnlineStoreUnavailable(
                f"Redis schema {raw['schema_version']} != client schema {SCHEMA_VERSION}"
            )
        expected_definitions = json.dumps(
            {name: get(name).version for name in online_features()}, sort_keys=True
        )
        if raw.get("definitions") != expected_definitions:
            raise OnlineStoreUnavailable(
                "Redis feature versions do not match the active registry; rematerialize"
            )
        raw_embeddings: dict[str, int] = json.loads(raw.get("embeddings", "{}"))
        for name, version in raw_embeddings.items():
            if name not in EMBEDDING_REGISTRY or get_embedding(name).version != version:
                raise OnlineStoreUnavailable(
                    "Redis embedding versions do not match the active registry; rematerialize"
                )
        manifest = OnlineManifest(
            generation=generation,
            as_of=datetime.fromisoformat(raw["as_of"]),
            users=int(raw["users"]),
            items=int(raw["items"]),
            user_categories=int(raw["user_categories"]),
            businesses=int(raw["businesses"]),
            user_embeddings=int(raw.get("user_embeddings", 0)),
            embeddings=tuple(sorted(raw_embeddings.items())),
        )
        self._thread.manifest = manifest
        return manifest

    def lookup_user_embedding(
        self, user_id: str, name: str = USER_BEHAVIORAL_EMBEDDING
    ) -> list[float] | None:
        """Read one registered user vector from the active atomic snapshot."""
        definition = get_embedding(name)
        manifest = self.manifest()
        if (name, definition.version) not in manifest.embeddings:
            raise OnlineStoreUnavailable(
                f"{name} is not materialized in Redis; rematerialize with that definition"
            )
        generation = manifest.generation
        try:
            raw = self.client.get(_key(generation, "embedding", user_id))
        except RedisError as exc:
            raise OnlineStoreUnavailable(f"online store lookup failed: {exc}") from exc
        record = _decode_record(raw)
        encoded = record.get(name)
        if encoded is None:
            return None
        vector = [float(value) for value in json.loads(encoded)]
        if len(vector) != definition.shape[0]:
            raise OnlineStoreUnavailable(
                f"stored {name} has {len(vector)} values, expected {definition.shape[0]}"
            )
        return vector

    def _connection(self) -> duckdb.DuckDBPyConnection:
        """One reusable DuckDB connection per serving thread."""
        connection = getattr(self._thread, "connection", None)
        if not isinstance(connection, duckdb.DuckDBPyConnection):
            connection = duckdb.connect()
            self._thread.connection = connection
        return connection

    def lookup(
        self,
        queries: Sequence[FeatureQuery],
        features: Sequence[str] | None = None,
    ) -> list[tuple[object, ...]]:
        """Return current features for arbitrary user/business query pairs."""
        if not queries:
            return []
        manifest = self.manifest()
        generation = manifest.generation
        users = tuple(dict.fromkeys(query.user_id for query in queries))
        businesses = tuple(dict.fromkeys(query.business_id for query in queries))

        keys: list[str] = []
        for user_id in users:
            keys.append(_key(generation, "user", user_id))
            keys.append(_key(generation, "user_category", user_id))
        for business_id in businesses:
            keys.append(_key(generation, "item", business_id))
            keys.append(_key(generation, "business", business_id))
        try:
            responses = self.client.mget(keys)
        except RedisError as exc:
            raise OnlineStoreUnavailable(f"cannot read Redis: {exc}") from exc

        cursor = 0
        user_state: dict[str, dict[str, str]] = {}
        user_categories: dict[str, dict[str, str]] = {}
        for user_id in users:
            user_state[user_id] = _decode_record(responses[cursor])
            user_categories[user_id] = _decode_record(responses[cursor + 1])
            cursor += 2
        item_state: dict[str, dict[str, str]] = {}
        business_state: dict[str, dict[str, str]] = {}
        for business_id in businesses:
            item_state[business_id] = _decode_record(responses[cursor])
            business_state[business_id] = _decode_record(responses[cursor + 1])
            cursor += 2

        con = self._connection()
        self._load_relations(
            con,
            queries,
            manifest.as_of,
            user_state,
            user_categories,
            item_state,
            business_state,
        )
        return read_current_features(con, features)

    @staticmethod
    def _load_relations(
        con: duckdb.DuckDBPyConnection,
        queries: Sequence[FeatureQuery],
        as_of: datetime,
        users: Mapping[str, Mapping[str, str]],
        user_categories: Mapping[str, Mapping[str, str]],
        items: Mapping[str, Mapping[str, str]],
        businesses: Mapping[str, Mapping[str, str]],
    ) -> None:
        query_rows = [
            (query.query_id, query.user_id, query.business_id, as_of) for query in queries
        ]
        con.execute(
            "CREATE OR REPLACE TABLE queries AS SELECT "
            "json_extract(value, '$.query_id')::BIGINT AS query_id, "
            "json_extract_string(value, '$.user_id') AS user_id, "
            "json_extract_string(value, '$.business_id') AS business_id, "
            "json_extract(value, '$.ts')::TIMESTAMP AS ts "
            "FROM json_each(?::JSON)",
            [_rows_json(("query_id", "user_id", "business_id", "ts"), query_rows)],
        )
        user_rows: list[tuple[object, ...]] = []
        for user_id, state in users.items():
            if state:
                user_rows.append(
                    (
                        user_id,
                        state["ts"],
                        int(state["cum_count"]),
                        int(state["cum_sum"]),
                        _optional_int(state, "cum_lat_e7"),
                        _optional_int(state, "cum_lng_e7"),
                        int(state["cum_geo_n"]),
                        _optional_int(state, "cum_price"),
                        int(state["cum_price_n"]),
                    )
                )
        con.execute(
            "CREATE OR REPLACE TABLE user_current AS SELECT "
            "json_extract_string(value, '$.user_id') AS user_id, "
            "json_extract(value, '$.ts')::TIMESTAMP AS ts, "
            "json_extract(value, '$.cum_count')::BIGINT AS cum_count, "
            "json_extract(value, '$.cum_sum')::BIGINT AS cum_sum, "
            "json_extract(value, '$.cum_lat_e7')::BIGINT AS cum_lat_e7, "
            "json_extract(value, '$.cum_lng_e7')::BIGINT AS cum_lng_e7, "
            "json_extract(value, '$.cum_geo_n')::BIGINT AS cum_geo_n, "
            "json_extract(value, '$.cum_price')::BIGINT AS cum_price, "
            "json_extract(value, '$.cum_price_n')::BIGINT AS cum_price_n "
            "FROM json_each(?::JSON)",
            [
                _rows_json(
                    (
                        "user_id",
                        "ts",
                        "cum_count",
                        "cum_sum",
                        "cum_lat_e7",
                        "cum_lng_e7",
                        "cum_geo_n",
                        "cum_price",
                        "cum_price_n",
                    ),
                    user_rows,
                )
            ],
        )

        item_rows = [
            (business_id, state["ts"], int(state["cum_count"]), int(state["cum_sum"]))
            for business_id, state in items.items()
            if state
        ]
        con.execute(
            "CREATE OR REPLACE TABLE item_current AS SELECT "
            "json_extract_string(value, '$.business_id') AS business_id, "
            "json_extract(value, '$.ts')::TIMESTAMP AS ts, "
            "json_extract(value, '$.cum_count')::BIGINT AS cum_count, "
            "json_extract(value, '$.cum_sum')::BIGINT AS cum_sum "
            "FROM json_each(?::JSON)",
            [_rows_json(("business_id", "ts", "cum_count", "cum_sum"), item_rows)],
        )

        category_rows = [
            (user_id, category, int(count))
            for user_id, state in user_categories.items()
            for category, count in state.items()
        ]
        con.execute(
            "CREATE OR REPLACE TABLE user_category_current AS SELECT "
            "json_extract_string(value, '$.user_id') AS user_id, "
            "json_extract_string(value, '$.category') AS category, "
            "json_extract(value, '$.cum_count')::BIGINT AS cum_count "
            "FROM json_each(?::JSON)",
            [_rows_json(("user_id", "category", "cum_count"), category_rows)],
        )

        business_rows: list[tuple[object, ...]] = []
        for business_id, state in businesses.items():
            if state:
                business_rows.append(
                    (
                        business_id,
                        state.get("name"),
                        float(state["latitude"]) if "latitude" in state else None,
                        float(state["longitude"]) if "longitude" in state else None,
                        json.loads(state.get("categories", "[]")),
                        int(state["price_tier"]) if "price_tier" in state else None,
                        state.get("is_open") == "1",
                    )
                )
        con.execute(
            "CREATE OR REPLACE TABLE business_current AS SELECT "
            "json_extract_string(value, '$.business_id') AS business_id, "
            "json_extract_string(value, '$.name') AS name, "
            "json_extract(value, '$.latitude')::DOUBLE AS latitude, "
            "json_extract(value, '$.longitude')::DOUBLE AS longitude, "
            "json_extract(value, '$.categories')::VARCHAR[] AS categories, "
            "json_extract(value, '$.price_tier')::SMALLINT AS price_tier, "
            "json_extract(value, '$.is_open')::BOOLEAN AS is_open "
            "FROM json_each(?::JSON)",
            [
                _rows_json(
                    (
                        "business_id",
                        "name",
                        "latitude",
                        "longitude",
                        "categories",
                        "price_tier",
                        "is_open",
                    ),
                    business_rows,
                )
            ],
        )


def main() -> None:
    manifest = materialize_online(
        user_embedding_file=get_embedding(USER_BEHAVIORAL_EMBEDDING).artifact
    )
    print(f"activated Redis generation {manifest.generation}")
    print(f"  as-of            {manifest.as_of.isoformat()}")
    print(f"  users            {manifest.users:,}")
    print(f"  items            {manifest.items:,}")
    print(f"  user categories  {manifest.user_categories:,}")
    print(f"  businesses       {manifest.businesses:,}")
    print(f"  user embeddings  {manifest.user_embeddings:,}")


if __name__ == "__main__":
    main()
