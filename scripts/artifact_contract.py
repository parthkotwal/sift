"""Shared deployment artifact contract for packaging and task startup."""

from __future__ import annotations

from pathlib import Path

from sift.config import DATA_DIR
from sift.features.definitions import (
    USER_BEHAVIORAL_EMBEDDING,
    get,
    get_embedding,
    online_features,
)
from sift.offline.dim_business import DIM_BUSINESS
from sift.offline.popularity import POPULARITY_ARTIFACT
from sift.ranking.train import ALS_RANKER_MODEL
from sift.retrieval.artifacts import FACTORS, ITEM_FACTORS, ITEM_IDS
from sift.store.materialize import HISTORICAL_DIR, state_path

MANIFEST_SCHEMA_VERSION = 1
STATE_GROUPS = ("user", "item", "user_category", "user_als", "item_als")
EVENTS_PREFIX = Path("data/silver/events")


def _bundle_path(path: Path) -> Path:
    return Path("data") / path.relative_to(DATA_DIR)


def required_fixed_bundle_paths() -> tuple[Path, ...]:
    """Return required serving paths whose names do not depend on event partitions."""
    configured = (
        ITEM_FACTORS,
        ITEM_IDS,
        get_embedding(USER_BEHAVIORAL_EMBEDDING).artifact,
        POPULARITY_ARTIFACT,
        ALS_RANKER_MODEL,
        DIM_BUSINESS,
        *(state_path(group, HISTORICAL_DIR) for group in STATE_GROUPS),
    )
    return tuple(_bundle_path(path) for path in configured)


def expected_selected_models() -> dict[str, object]:
    """Return the model-selection contract understood by this image."""
    return {
        "retrieval": {
            "kind": "exact_als",
            "item_embedding": "item_embedding_behavioral_v1",
            "dimensions": FACTORS,
        },
        "ranker": {
            "kind": "lightgbm",
            "artifact": _bundle_path(ALS_RANKER_MODEL).as_posix(),
        },
        "rerank": {"decision": "D29"},
    }


def expected_feature_versions() -> dict[str, int]:
    """Return online feature versions understood by this image."""
    return {name: get(name).version for name in online_features()}


def expected_embedding_versions() -> dict[str, int]:
    """Return embedding versions understood by this image."""
    embedding = get_embedding(USER_BEHAVIORAL_EMBEDDING)
    return {embedding.name: embedding.version}
