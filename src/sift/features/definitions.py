"""The feature definition registry — the seam consumers bind to (D12, D23).

A definition is *metadata plus an expression over materialised state*. The store
persists entity-keyed state (the cumulative timelines in `sift.store.materialize`);
a definition says how to project one feature out of it. That single shape covers
both cases the v1 feature set contains:

  u_reviews_to_date     one expression over one entity's state
  ui_distance_km        one expression over two entities' state

which is why `user x item` features are not an exception to "one definition, two
materialisations" (D23). Their *inputs* are stored per entity; the combining
expression is the definition, and the same expression text serves the offline
as-of read and the online lookup. Nothing computes a feature anywhere else.

## Expression aliases

An expression is SQL over these relation aliases, which the read path guarantees:

  q   the query row              (query_id, user_id, business_id, ts)
  u   user state, as-of q.ts     right-exclusive ASOF join, NULL if no history
  i   item state, as-of q.ts     right-exclusive ASOF join, NULL if no history
  uc  user-category aggregate    the taste-vector dot product, as-of q.ts
  b   business static attributes plain join on identity — no time axis (D21)

`u` and `i` are NULL for an entity with no prior events, so every expression must
survive NULL inputs: a cold-start user yields NULL features, which LightGBM
consumes as native missing values rather than a fabricated zero.

## The leakage field is not documentation

AGENTS.md requires a one-line "why can't this value contain the future?" for every
feature. Here it is a non-defaulted field, so a definition cannot be registered
without one, and `test_definitions.py` asserts none are blank. A rule that lived in
prose now fails the build.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sift.config import DERIVED_DIR
from sift.features.state import GEO_SCALE

BEHAVIORAL_EMBEDDING_DIM = 64
USER_BEHAVIORAL_EMBEDDING = "user_embedding_behavioral_v1"
ITEM_BEHAVIORAL_EMBEDDING = "item_embedding_behavioral_v1"
USER_TWO_TOWER_EMBEDDING = "user_embedding_two_tower_v1"
ITEM_TWO_TOWER_EMBEDDING = "item_embedding_two_tower_v1"

# Centroid of the businesses the user has reviewed so far. The state stores the
# lat/long sums as fixed-point integers (see state.GEO_SCALE), so dividing them down
# is a single exact operation on exact inputs — deterministic without any rounding.
# The earlier float sums needed a 6dp round to hide their last-bit drift (I18); the
# integer encoding removes the drift instead of masking it.
_U_LAT = f"(u.cum_lat_e7 / ({GEO_SCALE}.0 * NULLIF(u.cum_geo_n, 0)))"
_U_LNG = f"(u.cum_lng_e7 / ({GEO_SCALE}.0 * NULLIF(u.cum_geo_n, 0)))"
_U_PRICE = "u.cum_price / NULLIF(u.cum_price_n, 0)"

# Great-circle km from the user's centroid to the business.
_HAVERSINE = f"""2 * 6371.0 * asin(sqrt(
        pow(sin(radians(b.latitude - {_U_LAT}) / 2), 2)
        + cos(radians({_U_LAT})) * cos(radians(b.latitude))
          * pow(sin(radians(b.longitude - {_U_LNG}) / 2), 2)
    ))"""


@dataclass(frozen=True)
class FeatureDefinition:
    """One feature: what it is, where its inputs live, and why it can't see ahead."""

    name: str
    entity: str  # 'user' | 'item' | 'user x item' — what the value describes
    dtype: str  # DuckDB type of the projected value
    version: int  # bump when the expression's meaning changes, never in place
    reads: tuple[str, ...]  # state groups consumed: user | item | user_category | business
    expr: str  # SQL over the aliases documented above
    leakage: str  # required: why this cannot contain the future


@dataclass(frozen=True)
class EmbeddingDefinition:
    """A model-produced vector feature whose artifact is its compute function."""

    name: str
    entity: str
    dtype: str
    shape: tuple[int, ...]
    version: int
    artifact: Path
    leakage: str


_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    FeatureDefinition(
        name="u_reviews_to_date",
        entity="user",
        dtype="BIGINT",
        version=1,
        reads=("user",),
        expr="COALESCE(u.cum_count, 0)",
        leakage="Cumulative count read as-of q.ts by a right-exclusive ASOF join, so "
        "only the user's strictly-earlier events are summed.",
    ),
    FeatureDefinition(
        name="u_mean_stars_to_date",
        entity="user",
        dtype="DOUBLE",
        version=1,
        reads=("user",),
        expr="u.cum_sum / u.cum_count",
        leakage="Both operands come from the same as-of state row, so the mean spans "
        "exactly the events before q.ts and cannot include the label's stars.",
    ),
    FeatureDefinition(
        name="u_days_since_last",
        entity="user",
        dtype="BIGINT",
        version=1,
        reads=("user",),
        expr="date_diff('day', u.ts, q.ts)",
        leakage="u.ts is the timestamp of the last event strictly before q.ts, so the "
        "gap is measured backwards from the query and never spans forward.",
    ),
    FeatureDefinition(
        name="i_reviews_to_date",
        entity="item",
        dtype="BIGINT",
        version=1,
        reads=("item",),
        expr="COALESCE(i.cum_count, 0)",
        leakage="Same right-exclusive as-of read on the item's timeline; the review "
        "being predicted is at q.ts and is therefore excluded.",
    ),
    FeatureDefinition(
        name="i_mean_stars_to_date",
        entity="item",
        dtype="DOUBLE",
        version=1,
        reads=("item",),
        expr="i.cum_sum / i.cum_count",
        leakage="As-of state row only. This is the canonical leak (ARCHITECTURE: a "
        "business's average rating containing the very review predicted) and "
        "the right-exclusive boundary is exactly what prevents it.",
    ),
    FeatureDefinition(
        name="ui_distance_km",
        entity="user x item",
        dtype="DOUBLE",
        version=1,
        reads=("user", "business"),
        expr=_HAVERSINE,
        leakage="The centroid is an as-of aggregate over prior events; the business's "
        "coordinates are fixed when it opens and cannot vary with the review "
        "being predicted (D21).",
    ),
    FeatureDefinition(
        name="ui_category_affinity",
        entity="user x item",
        dtype="DOUBLE",
        version=1,
        reads=("user", "user_category", "business"),
        expr="uc.matched / NULLIF(COALESCE(u.cum_count, 0), 0)",
        leakage="Numerator is a per-category as-of count over prior events; "
        "denominator is the same as-of total. The business's category list is "
        "set at listing time and is independent of any later review (D21).",
    ),
    FeatureDefinition(
        name="ui_als_score",
        entity="user x item",
        dtype="DOUBLE",
        version=1,
        reads=("user_als", "item_als"),
        # Reads the `als` CTE the read path builds, the same way ui_category_affinity
        # reads `uc`. The dot product cannot be written inline here: DuckDB's
        # list_dot_product raises rather than returning NULL when a LEFT-join payload
        # is absent, and neither a CASE guard nor a WHERE filter reliably keeps it
        # from seeing one. Computing it behind inner ASOF joins means the function
        # only ever meets matched rows, and the LEFT JOIN back supplies NULL for the
        # rest — the honest value for an entity no ALS slice covers.
        expr="als.score",
        leakage="Retrieval's own score, which the ranker was previously blind to. Both "
                "vectors come from an ALS model fit only on interactions strictly "
                "before the row's timestamp (D27), chosen by the same right-exclusive "
                "as-of join as every other state group. The headline pre-T model is "
                "never read here: it was fit on the row's own positive pair, and its "
                "score for an observed pair reports the label rather than predicting "
                "it (0.4555 vs 0.0047 mean; 98.7% separation).",
    ),
    FeatureDefinition(
        name="ui_price_delta",
        entity="user x item",
        dtype="DOUBLE",
        version=1,
        reads=("user", "business"),
        expr=f"abs(b.price_tier - {_U_PRICE})",
        leakage="User's mean price tier is an as-of aggregate; the business's tier is a "
        "quasi-static attribute (D21, the weakest of the three static claims — "
        "a venue can change band over years).",
    ),
)

# Insertion order is the ranker's feature-matrix order; do not reorder casually.
REGISTRY: dict[str, FeatureDefinition] = {d.name: d for d in _DEFINITIONS}

_EMBEDDINGS: tuple[EmbeddingDefinition, ...] = (
    EmbeddingDefinition(
        name=USER_BEHAVIORAL_EMBEDDING,
        entity="user",
        dtype="FLOAT32",
        shape=(BEHAVIORAL_EMBEDDING_DIM,),
        version=1,
        artifact=DERIVED_DIR / "als" / "user_embedding_behavioral_v1.parquet",
        leakage="ALS is fit only to review events strictly before the frozen split T; "
        "the exported vector cannot contain a post-T interaction.",
    ),
    EmbeddingDefinition(
        name=ITEM_BEHAVIORAL_EMBEDDING,
        entity="item",
        dtype="FLOAT32",
        shape=(BEHAVIORAL_EMBEDDING_DIM,),
        version=1,
        artifact=DERIVED_DIR / "als" / "item_embedding_behavioral_v1.parquet",
        leakage="ALS is fit only to the pre-T interaction matrix; catalog-only items "
        "with no history receive a zero vector rather than future state.",
    ),
    EmbeddingDefinition(
        name=USER_TWO_TOWER_EMBEDDING,
        entity="user",
        dtype="FLOAT32",
        shape=(BEHAVIORAL_EMBEDDING_DIM,),
        version=1,
        artifact=DERIVED_DIR / "two_tower" / "user_embedding_two_tower_v1.parquet",
        leakage="The model sees only pre-T labels; exported user inputs are read "
        "right-exclusively as-of frozen T through the historical store.",
    ),
    EmbeddingDefinition(
        name=ITEM_TWO_TOWER_EMBEDDING,
        entity="item",
        dtype="FLOAT32",
        shape=(BEHAVIORAL_EMBEDDING_DIM,),
        version=1,
        artifact=DERIVED_DIR / "two_tower" / "item_embedding_two_tower_v1.parquet",
        leakage="The model sees only pre-T labels and item-side inputs are D21's "
        "quasi-static identity attributes, never accumulating snapshot counters.",
    ),
)

EMBEDDING_REGISTRY: dict[str, EmbeddingDefinition] = {d.name: d for d in _EMBEDDINGS}

STATE_GROUPS: tuple[str, ...] = (
    "user",
    "item",
    "user_category",
    "business",
    "user_als",
    "item_als",
)


def feature_names() -> tuple[str, ...]:
    """Registered feature names, in matrix order."""
    return tuple(REGISTRY)


def get(name: str) -> FeatureDefinition:
    """Look a definition up by name. Consumers bind by name, never by expression."""
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown feature {name!r}; registered: {', '.join(REGISTRY)}") from None


def get_embedding(name: str) -> EmbeddingDefinition:
    try:
        return EMBEDDING_REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown embedding {name!r}; registered: {', '.join(EMBEDDING_REGISTRY)}"
        ) from None


# State groups the Redis publisher materialises. The ALS slice groups (D27) are not
# among them: their vectors are large, per-slice, and only the newest slice matters
# online, so publishing them is its own build step gated on `ui_als_score` actually
# landing (ISSUES.md I25).
ONLINE_STATE_GROUPS: tuple[str, ...] = ("user", "item", "user_category", "business")


def online_features() -> tuple[str, ...]:
    """Features the online store can serve: those whose every input is published.

    Derived rather than listed, so it corrects itself the moment a state group joins
    the publisher. Training may use the full registry — but deploying a model that
    reads a feature serving cannot supply is training/serving skew by construction,
    which is the hazard the store exists to prevent, so this is what gates a swap.
    """
    servable = set(ONLINE_STATE_GROUPS)
    return tuple(
        name for name, d in REGISTRY.items() if set(d.reads) <= servable
    )


def required_groups(names: tuple[str, ...] | None = None) -> set[str]:
    """The state groups needed to serve these features — what the read path must join."""
    chosen = feature_names() if names is None else names
    return {group for name in chosen for group in get(name).reads}
