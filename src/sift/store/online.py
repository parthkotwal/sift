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

import hashlib
import json
import os
import warnings
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock, local
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
from sift.offline.ingest import EVENTS_DIR, REVIEW_EVENT, events_glob
from sift.store.materialize import HISTORICAL_DIR, state_path
from sift.store.read import attach_store, read_current_features

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
KEY_PREFIX = "sift:online"
ACTIVE_GENERATION_KEY = f"{KEY_PREFIX}:active"
GENERATION_INDEX_KEY = f"{KEY_PREFIX}:generations"
# The entity id under which a catalog-wide record is stored. Item state, the business
# dimension and the item ALS vectors all use it: each is one Redis record per
# generation rather than one per business (I31).
CATALOG_RECORD = "__catalog__"
ITEM_ALS_ALL = CATALOG_RECORD  # historical name, kept for existing imports
# Intra-request DuckDB parallelism, deliberately 1. A lookup projects a few hundred
# rows out of relations small enough that parallelism buys nothing per request, while
# DuckDB's default (one thread per core) multiplies against request concurrency: at 8
# concurrent requests the default oversubscribed a 4-performance-core machine badly
# enough to dominate the stage (I31). A serving process wants concurrency to come
# from requests, not from inside one.
LOOKUP_THREADS = 1
# 6: added the `user_reviewed` group for the rerank filter (D29). Additive, but the
# bump is the point: a rerank-capable reader against a schema-5 generation would find
# no reviewed records, filter nothing, and serve the user businesses they have already
# been to — with no error raised. Silent degradation is exactly what this guard exists
# to convert into a refusal.
# 7: item state and the business dimension became one catalog record each (I31). Not
# additive — a schema-6 generation stores them per business, so a schema-7 reader would
# find nothing under the catalog key and project an empty relation, returning NULL for
# every item-side feature. That is the same silent-degradation shape, which is why the
# version is what stops it rather than a runtime check somewhere downstream.
SCHEMA_VERSION = 7
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
class RerankInputs:
    """Serving-time attributes the rerank stage filters on — deliberately not features.

    These reach rerank through their own read path rather than `lookup()`, and that
    separation is load-bearing. `is_open` is the project's cleanest example of a signal
    that is legitimate online and unconstructible historically (D13); the moment it can
    arrive via `FeatureQuery` it is one careless registry entry away from being trained
    on. Keeping it off that path makes the rule structural instead of remembered.
    """

    reviewed: frozenset[str]
    is_open: dict[str, bool]
    categories: dict[str, tuple[str, ...]]


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
    user_als: int = 0
    item_als: int = 0
    user_reviewed: int = 0


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
    events_dir: Path = EVENTS_DIR,
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
        "user_als": 0,
        "item_als": 0,
        "user_reviewed": 0,
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

        # Item state goes in ONE record, like the ALS vectors above and for the same
        # reason (I31): it is catalog-wide and immutable within a generation, so a
        # 500-candidate request was fetching and decoding 500 records that are identical
        # for every request. Only the user side genuinely varies per request.
        item_catalog = {
            str(business_id): {
                "ts": _string(ts),
                "cum_count": _string(count),
                "cum_sum": _string(stars),
            }
            for business_id, ts, count, stars in con.execute(
                _last_rows("item_state", "business_id")
            ).fetchall()
        }
        write_record(
            _key(generation, "item", CATALOG_RECORD),
            {"rows": json.dumps(item_catalog, ensure_ascii=False)},
        )
        counts["items"] = len(item_catalog)

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

        # Same treatment for the business dimension, which is quasi-static by
        # construction (D21) and so even more obviously per-generation than per-request.
        business_catalog = {
            str(business_id): {
                "name": name,
                "latitude": lat,
                "longitude": lng,
                "categories": list(categories or []),
                "price_tier": price,
                "is_open": bool(is_open),
            }
            for business_id, name, lat, lng, categories, price, is_open in con.execute(
                "SELECT business_id, name, latitude, longitude, categories, price_tier, "
                "is_open FROM dim_business ORDER BY business_id"
            ).fetchall()
        }
        write_record(
            _key(generation, "business", CATALOG_RECORD),
            {"rows": json.dumps(business_catalog, ensure_ascii=False, default=_string)},
        )
        counts["businesses"] = len(business_catalog)

        # Businesses the user has already reviewed, for the rerank filter (D29).
        #
        # Serving-only state. Like `is_open`, it is a business rule applied *after* the
        # model rather than a signal learned by it (D13), so it has no training-side
        # counterpart and is deliberately outside the skew check — there is nothing to
        # compare it against, which is a property of the stage, not a gap in the check.
        # It is therefore not a state group in `ONLINE_STATE_GROUPS`: no
        # FeatureDefinition reads it, and listing it there would claim otherwise.
        #
        # `ts < as_of` matches every other record in this generation. That means it is
        # as-of the data's end (2022 here), not as-of T. Serving wants exactly that.
        # The offline rerank harness must NOT use it: at as_of it contains the post-T
        # reviews that *are* the eval targets, so evaluating through it would filter
        # away the ground truth and report recall near zero. `rerank.evaluate` reads
        # pre-T pairs directly for that reason.
        # `event_type = 'review'` is load-bearing even though ingest emits nothing else
        # today. The canonical event table exists so tips and check-ins can land as
        # pure ingest additions (D2), and the moment one does, an unfiltered query here
        # would treat a tipped business as reviewed and silently suppress it from every
        # recommendation — a filter widening itself as a side effect of an unrelated
        # ingest change, with no test failing. "Reviewed" is a claim about reviews.
        reviewed_rows = con.execute(
            "SELECT user_id, list(DISTINCT business_id) AS seen "
            f"FROM read_parquet({sql_path(Path(events_glob(events_dir)))}) "
            f"WHERE event_type = '{REVIEW_EVENT}' AND ts < ? "
            "GROUP BY user_id ORDER BY user_id",
            [as_of],
        )
        while batch := reviewed_rows.fetchmany(PIPELINE_SIZE):
            for user_id, seen in batch:
                write_record(
                    _key(generation, "user_reviewed", str(user_id)),
                    {"seen": json.dumps(sorted(str(b) for b in seen))},
                )
                counts["user_reviewed"] += 1

        # ALS slice state (D27). Only the newest slice matters online: `_last_rows`
        # picks it per entity, which is the same row the offline as-of read selects
        # for a query at serving "now", so the skew check compares like with like.
        if state_path("user_als", historical_dir).exists():
            user_als_rows = con.execute(_last_rows("user_als_state", "user_id"))
            while batch := user_als_rows.fetchmany(PIPELINE_SIZE):
                for user_id, ts, vector in batch:
                    values = _mapping(ts=ts)
                    values["value"] = json.dumps([float(v) for v in vector])
                    write_record(_key(generation, "user_als", str(user_id)), values)
                    counts["user_als"] += 1

        # Item ALS vectors are catalog-wide and identical across requests, so they go
        # in ONE record rather than 14.5k. Per-item keys meant a 500-candidate request
        # fetched and reparsed 500 vectors that never change between requests — the
        # dominant cost behind 49ms feature lookup (I29).
        if state_path("item_als", historical_dir).exists():
            item_als_rows = con.execute(
                _last_rows("item_als_state", "business_id")
            ).fetchall()
            catalog = {
                str(business_id): [float(v) for v in vector]
                for business_id, _ts, vector in item_als_rows
            }
            write_record(
                _key(generation, "item_als", CATALOG_RECORD),
                {"vectors": json.dumps(catalog)},
            )
            counts["item_als"] = len(catalog)

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
            user_als=counts["user_als"],
            item_als=counts["item_als"],
            user_reviewed=counts["user_reviewed"],
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
        user_als=counts["user_als"],
        item_als=counts["item_als"],
        user_reviewed=counts["user_reviewed"],
        embeddings=(((definition.name, definition.version),) if user_embedding_file else ()),
    )


class OnlineFeatureStore:
    """Batch lookup client: Redis fetch plus registry-expression projection."""

    def __init__(self, client: Redis | None = None) -> None:
        self.client = client or redis_client()
        self._thread = local()
        # One DuckDB database per store, with a cursor per serving thread. Catalog
        # state (the item-ALS relation) is shared across cursors so it is built once
        # per process; per-request relations are TEMP, which DuckDB scopes to the
        # cursor, so concurrent requests cannot see each other's rows. A connection
        # per thread instead would re-pay a ~640ms build and duplicate ~19.6MB for
        # every worker the threadpool creates (I31).
        self._database = duckdb.connect()
        self._database.execute(f"SET threads TO {LOOKUP_THREADS}")
        self._catalog_lock = Lock()
        self._catalog_relations_by_generation: dict[str, dict[str, str]] = {}
        self._rerank_catalog_by_generation: dict[str, dict[str, tuple[bool, tuple[str, ...]]]] = {}

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
            user_als=int(raw.get("user_als", 0)),
            item_als=int(raw.get("item_als", 0)),
            user_reviewed=int(raw.get("user_reviewed", 0)),
        )
        self._thread.manifest = manifest
        return manifest

    def snapshot(self) -> OnlineManifest:
        """Pin the active generation for the life of one request.

        Every read below resolves the active generation independently unless handed a
        snapshot, so a publication landing mid-request would let one request retrieve
        with one generation, rank with the next, and filter with a third — serving a
        list whose user embedding, features, and filters come from three different
        atomic snapshots, with nothing raised.

        The generation switch is a single Redis `SET` and the store is explicitly
        designed around that atomicity, so the fix is not more locking: it is capturing
        the generation *once per request* and requiring every read to name it. That is
        the same property the per-generation item-ALS relations gave the DuckDB side —
        an in-flight request finishes against the snapshot it started on.
        """
        return self.manifest()

    def lookup_user_embedding(
        self,
        user_id: str,
        name: str = USER_BEHAVIORAL_EMBEDDING,
        *,
        snapshot: OnlineManifest | None = None,
    ) -> list[float] | None:
        """Read one registered user vector from the active atomic snapshot."""
        definition = get_embedding(name)
        manifest = snapshot or self.manifest()
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

    def rerank_inputs(
        self,
        user_id: str,
        business_ids: Sequence[str],
        *,
        snapshot: OnlineManifest | None = None,
    ) -> RerankInputs:
        """Read the rerank stage's serving-only inputs from the active generation.

        One `mget` of the user's reviewed set plus the candidates' business records —
        the same records `lookup()` reads for location and price, but projected here
        without going through the feature query, because these fields are not features
        and must not become reachable as such (see :class:`RerankInputs`).

        A business absent from the catalog record is treated as **closed**. Failing
        closed is the safe direction: the alternative surfaces a business the store
        knows nothing about, and rerank's job is to be the last thing that can say no.
        """
        manifest = snapshot or self.manifest()
        generation = manifest.generation
        unique = list(dict.fromkeys(business_ids))

        # One Redis key: the user's own history. `is_open` and categories come from the
        # per-generation business relation, already built once per process — so this
        # stage reads one record per request rather than one per candidate (I31).
        try:
            raw = self.client.get(_key(generation, "user_reviewed", user_id))
        except RedisError as exc:
            raise OnlineStoreUnavailable(f"cannot read Redis: {exc}") from exc
        seen_raw = _decode_record(raw).get("seen")
        reviewed = frozenset(json.loads(seen_raw)) if seen_raw else frozenset()

        found = self._rerank_catalog(generation)

        # A business missing from the catalog fails **closed**. That is the safe
        # direction: the alternative surfaces one the store knows nothing about, and
        # rerank is the last stage that can say no.
        is_open = {business_id: found.get(business_id, (False, ()))[0] for business_id in unique}
        categories = {business_id: found.get(business_id, (False, ()))[1] for business_id in unique}
        return RerankInputs(reviewed=reviewed, is_open=is_open, categories=categories)

    def _connection(self) -> duckdb.DuckDBPyConnection:
        """One cursor per serving thread over the store's shared database.

        A cursor, not a fresh `duckdb.connect()`: cursors share the catalog and
        buffer pool, so the catalog-wide item-ALS relation is visible to every thread
        without being rebuilt or duplicated, while TEMP relations stay cursor-local.
        """
        connection = getattr(self._thread, "connection", None)
        if not isinstance(connection, duckdb.DuckDBPyConnection):
            connection = self._database.cursor()
            self._thread.connection = connection
        return connection

    def lookup(
        self,
        queries: Sequence[FeatureQuery],
        features: Sequence[str] | None = None,
        *,
        snapshot: OnlineManifest | None = None,
    ) -> list[tuple[object, ...]]:
        """Return current features for arbitrary user/business query pairs."""
        if not queries:
            return []
        manifest = snapshot or self.manifest()
        generation = manifest.generation
        users = tuple(dict.fromkeys(query.user_id for query in queries))

        # Only the user side is fetched per request. Item state, the business dimension
        # and the ALS vectors are catalog-wide within a generation, so they live in
        # per-generation relations built once per process instead of being re-fetched
        # and re-decoded for every candidate — that was 1,000 of the 1,003 records a
        # 500-candidate lookup pulled, and ~16ms of a ~19ms stage (I31).
        keys: list[str] = []
        for user_id in users:
            keys.append(_key(generation, "user", user_id))
            keys.append(_key(generation, "user_category", user_id))
            keys.append(_key(generation, "user_als", user_id))
        try:
            responses = self.client.mget(keys)
        except RedisError as exc:
            raise OnlineStoreUnavailable(f"cannot read Redis: {exc}") from exc

        cursor = 0
        user_state: dict[str, dict[str, str]] = {}
        user_categories: dict[str, dict[str, str]] = {}
        user_als: dict[str, dict[str, str]] = {}
        for user_id in users:
            user_state[user_id] = _decode_record(responses[cursor])
            user_categories[user_id] = _decode_record(responses[cursor + 1])
            user_als[user_id] = _decode_record(responses[cursor + 2])
            cursor += 3

        con = self._connection()
        catalog = self._catalog_relations(generation)
        self._load_relations(con, queries, manifest.as_of, user_state, user_categories, user_als)
        return read_current_features(
            con,
            features,
            item_state=catalog["item"],
            dim=catalog["business"],
            item_als_state=catalog["item_als"],
        )

    # The catalog-wide groups, and the SQL that projects each Redis record into a
    # relation. All three share one property that makes them cacheable: they are facts
    # about the *catalog* within a generation, identical for every request, so fetching
    # them per request meant a 500-candidate lookup pulled 1,000 records that never
    # differ. Only the three user-side records genuinely vary (I31).
    _CATALOG_PROJECTIONS: dict[str, str] = {
        "item_als": (
            "SELECT key AS business_id, value::FLOAT[] AS value FROM json_each(?::JSON)"
        ),
        "item": (
            "SELECT key AS business_id, "
            "json_extract(value, '$.ts')::TIMESTAMP AS ts, "
            "json_extract(value, '$.cum_count')::BIGINT AS cum_count, "
            "json_extract(value, '$.cum_sum')::BIGINT AS cum_sum "
            "FROM json_each(?::JSON)"
        ),
        "business": (
            "SELECT key AS business_id, "
            "json_extract_string(value, '$.name') AS name, "
            "json_extract(value, '$.latitude')::DOUBLE AS latitude, "
            "json_extract(value, '$.longitude')::DOUBLE AS longitude, "
            "json_extract(value, '$.categories')::VARCHAR[] AS categories, "
            "json_extract(value, '$.price_tier')::SMALLINT AS price_tier, "
            "json_extract(value, '$.is_open')::BOOLEAN AS is_open "
            "FROM json_each(?::JSON)"
        ),
    }
    # Each record stores its payload under this field. `item_als` predates the
    # generalisation and kept "vectors"; the rest use "rows".
    _CATALOG_FIELD: dict[str, str] = {"item_als": "vectors"}

    def _catalog_relations(self, generation: str) -> dict[str, str]:
        """Build the catalog relations once per (process, generation); return their names.

        These do not vary by request, and they do not vary by *thread* either — the
        first fix missed that and re-paid a ~640ms build for every worker the threadpool
        created, collapsing p99 under concurrency (I31).

        The generation is part of every relation name, which Redis's snapshot contract
        requires: an old request may still be reading the previous generation after the
        active pointer changes. Replacing one global relation would let that request and
        a new one swap the table underneath each other, mixing generations. Immutable
        per-generation relations let both finish safely.

        Each Redis payload is already a JSON object keyed by `business_id`, so DuckDB
        parses it directly. The route this replaced decoded in Python, re-encoded with
        `_rows_json`, and had `json_each` parse a third time — three passes for two that
        bought nothing.
        """
        relations = self._catalog_relations_by_generation.get(generation)
        if relations is not None:
            return relations
        with self._catalog_lock:
            # Re-check inside the lock: several threads can arrive together on a cold
            # process, and only the first should pay for the build.
            relations = self._catalog_relations_by_generation.get(generation)
            if relations is not None:
                return relations
            # Redis generations are opaque external values, so do not interpolate one
            # into SQL. A digest gives DuckDB a safe, deterministic identifier.
            digest = hashlib.sha256(generation.encode()).hexdigest()
            built: dict[str, str] = {}
            try:
                for group, projection in self._CATALOG_PROJECTIONS.items():
                    relation = f"{group}_{digest}"
                    raw = self.client.get(_key(generation, group, CATALOG_RECORD))
                    field = self._CATALOG_FIELD.get(group, "rows")
                    blob = _decode_record(raw).get(field, "{}") if raw else "{}"
                    self._database.execute(
                        f"CREATE TABLE {relation} AS {projection}", [blob]
                    )
                    built[group] = relation
                self._check_item_als(built["item_als"], generation)
            except Exception:
                # A half-built set must not be cached, or every later request in this
                # process would query relations that do not exist.
                for relation in built.values():
                    with suppress(duckdb.Error):
                        self._database.execute(f"DROP TABLE IF EXISTS {relation}")
                raise
            self._catalog_relations_by_generation[generation] = built
            return built

    def _rerank_catalog(self, generation: str) -> dict[str, tuple[bool, tuple[str, ...]]]:
        """`business_id -> (is_open, categories)` for the whole catalog, once per process.

        Rerank needs these for ~50 candidates per request. Querying the DuckDB relation
        for them costs a scan of all 14,568 rows per request — measurably worse than the
        per-candidate Redis reads it replaced (2.9ms -> 5.0ms p50). A plain dict built
        once from the same relation makes the stage ~50 hash lookups instead, and the
        catalog is small enough that holding it twice is not worth a smarter join.
        """
        cached = self._rerank_catalog_by_generation.get(generation)
        if cached is not None:
            return cached
        # Resolve the relation *before* taking the lock: `_catalog_relations` acquires
        # the same lock, and `Lock` is not reentrant, so calling it from inside the
        # critical section below would deadlock the first request on a cold process.
        relation = self._catalog_relations(generation)["business"]
        with self._catalog_lock:
            cached = self._rerank_catalog_by_generation.get(generation)
            if cached is not None:
                return cached
            rows = self._database.execute(
                f"SELECT business_id, is_open, categories FROM {relation}"
            ).fetchall()
            cached = {
                str(business_id): (bool(open_now), tuple(cats or ()))
                for business_id, open_now, cats in rows
            }
            self._rerank_catalog_by_generation[generation] = cached
            return cached

    def _check_item_als(self, relation: str, generation: str) -> None:
        """Structural guard, per I26: a NULL vector is invisible downstream.

        `ui_als_score` simply comes back NULL for the affected businesses and nothing
        raises. Assert the marshalling worked rather than trusting it, using
        `list_filter(... IS NULL)` — `list_contains(value, NULL)` returns NULL rather
        than true and so can never fire (I27).

        Width is checked for *self-consistency*, not against a constant: the definition
        works at any matching length by design, so hardcoding 64 would reject a
        legitimately narrower slice (see tests/conftest.py).
        """
        broken = self._database.execute(
            "SELECT count(*) FILTER ("
            "  value IS NULL OR len(list_filter(value, x -> x IS NULL)) > 0"
            f"), count(DISTINCT len(value)) FROM {relation}"
        ).fetchone()
        if broken is not None and (broken[0] or broken[1] > 1):
            raise OnlineStoreUnavailable(
                f"item ALS state in generation {generation} is unusable: "
                f"{broken[0]} vectors are NULL or contain NULLs, and "
                f"{broken[1]} distinct widths are present; rematerialize"
            )

    @staticmethod
    def _load_relations(
        con: duckdb.DuckDBPyConnection,
        queries: Sequence[FeatureQuery],
        as_of: datetime,
        users: Mapping[str, Mapping[str, str]],
        user_categories: Mapping[str, Mapping[str, str]],
        user_als: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        """Build the per-request relations — the user side only.

        Item state and the business dimension used to be built here too, from records
        fetched per candidate. They are catalog-wide within a generation, so they moved
        to `_catalog_relations` (I31).
        """
        query_rows = [
            (query.query_id, query.user_id, query.business_id, as_of) for query in queries
        ]
        con.execute(
            "CREATE OR REPLACE TEMP TABLE queries AS SELECT "
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
            "CREATE OR REPLACE TEMP TABLE user_current AS SELECT "
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

        category_rows = [
            (user_id, category, int(count))
            for user_id, state in user_categories.items()
            for category, count in state.items()
        ]
        con.execute(
            "CREATE OR REPLACE TEMP TABLE user_category_current AS SELECT "
            "json_extract_string(value, '$.user_id') AS user_id, "
            "json_extract_string(value, '$.category') AS category, "
            "json_extract(value, '$.cum_count')::BIGINT AS cum_count "
            "FROM json_each(?::JSON)",
            [_rows_json(("user_id", "category", "cum_count"), category_rows)],
        )

        # Only the user side varies per request. Each generation's catalog ALS
        # vectors are built once per store by `_ensure_item_als`; rebuilding them
        # here per request was the dominant cost in feature lookup (I29).
        user_als_rows: list[tuple[object, ...]] = [
            (entity_id, json.loads(state["value"]))
            for entity_id, state in (user_als or {}).items()
            if state
        ]
        con.execute(
            "CREATE OR REPLACE TEMP TABLE user_als_current AS SELECT "
            "json_extract_string(value, '$.user_id') AS user_id, "
            # FLOAT[], not DOUBLE[]: the offline state is FLOAT[64] and
            # list_dot_product accumulates at the element type, so widening here
            # made the online path disagree with the offline one in the eighth
            # significant digit — which the skew check duly flagged.
            "json_extract(value, '$.value')::FLOAT[] AS value "
            "FROM json_each(?::JSON)",
            [_rows_json(("user_id", "value"), user_als_rows)],
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
    print(f"  user ALS vectors {manifest.user_als:,}")
    print(f"  item ALS vectors {manifest.item_als:,}")
    # I29 happened here: a count was added to Redis, to the manifest hash, and to the
    # read path, but not to this report — so the publish step printed 0 for state that
    # was correct all along, and the next reader went debugging a working system.
    print(f"  reviewed history {manifest.user_reviewed:,}")


if __name__ == "__main__":
    main()
