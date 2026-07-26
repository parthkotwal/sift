"""Point-in-time features: for each query (user, business, ts), aggregate only
that user's and that business's events with event_ts < query_ts.

This is the spine (ARCHITECTURE.md → "The spine"). The `< query_ts` boundary is
right-exclusive: an event at exactly query_ts is the label event's instant and
must never fold into its own features. Enforced here with DuckDB ASOF JOIN, which
matches each query to the single most-recent history row strictly before it.

The builder takes table/relation names in an existing connection, so the same SQL
serves synthetic test events and the real Parquet event table — one definition,
which is the point. Verified by the future-invariance and leak tests.

## The `ui_*` features and what bounds their leakage argument

The three user x item features (ARCHITECTURE.md → Ranker features) each combine a
strictly-as-of-t user aggregate with a quasi-static business attribute from
`dim_business`:

  ui_distance_km        haversine(user's activity centroid as-of t, business location)
  ui_category_affinity  user's category-share vector as-of t · business's categories
  ui_price_delta        |business price tier - user's mean price tier as-of t|

Every *time-varying* half flows through the same cumulative-timeline + ASOF join as
the older features, so future events cannot reach them and the invariance test
covers them. The *static* half (lat/long, categories, price_tier) is a dump-time
value, which the invariance test does NOT cover because those are not events — see
`dim_business.py` and D21 for why they are admissible and what guards them instead.

The centroid averages raw lat/long rather than computing a true spherical centroid:
at metro scale the error is metres, and the alternative is trigonometry no one
reading this could check by hand.
"""

from __future__ import annotations

import duckdb

from sift.features.state import GEO_SCALE, state_query

# Order matters: the ranker's feature matrix is built in exactly this order.
FEATURE_COLUMNS: tuple[str, ...] = (
    "u_reviews_to_date",
    "u_mean_stars_to_date",
    "u_days_since_last",
    "i_reviews_to_date",
    "i_mean_stars_to_date",
    "ui_distance_km",
    "ui_category_affinity",
    "ui_price_delta",
)

# Great-circle distance in km between the user's centroid (w) and the business (d).
_HAVERSINE = """
    2 * 6371.0 * asin(sqrt(
        pow(sin(radians(d.latitude - w.u_lat) / 2), 2)
        + cos(radians(w.u_lat)) * cos(radians(d.latitude))
          * pow(sin(radians(d.longitude - w.u_lng) / 2), 2)
    ))
"""

# `queries` must expose (query_id, user_id, business_id, ts).
# `events`  must expose (user_id, business_id, event_type, ts, stars).
# `dim`     must expose (business_id, latitude, longitude, price_tier, categories).
_SQL = """
WITH
dim_cat AS (
    SELECT business_id, unnest(categories) AS category FROM {dim}
),
-- The three cumulative state groups, inlined from `sift.features.state` — the
-- identical SQL text the store materialises to Parquet (D23). Inlining it here
-- rather than keeping a second copy is what makes "one definition" structural:
-- the recomputed path and the persisted path cannot drift, because there is only
-- one string.
user_tl AS ({user_state}),
item_tl AS ({item_state}),
user_cat_tl AS ({user_cat_state}),
-- Explode each query by its business's categories, look each one up as-of t, and
-- sum: the dot product of the user's share vector with the business's indicator.
query_cat AS (
    SELECT q.query_id, q.user_id, q.ts, c.category
    FROM {queries} q JOIN dim_cat c ON q.business_id = c.business_id
),
affinity AS (
    SELECT qc.query_id, sum(COALESCE(tl.cum_count, 0)) AS matched
    FROM query_cat qc
    ASOF LEFT JOIN user_cat_tl tl
        ON qc.user_id = tl.user_id AND qc.category = tl.category AND qc.ts > tl.ts
    GROUP BY qc.query_id
),
with_user AS (
    SELECT q.query_id, q.business_id, q.ts,
        COALESCE(ut.cum_count, 0)                AS u_reviews_to_date,
        ut.cum_sum / ut.cum_count                AS u_mean_stars_to_date,
        date_diff('day', ut.ts, q.ts)            AS u_days_since_last,
        -- Fixed-point sums divided down (state.GEO_SCALE). Integer state means this
        -- is one exact operation on exact inputs, so no rounding is needed to keep
        -- it deterministic (ISSUES.md I18). Must stay textually equivalent to
        -- definitions._U_LAT / _U_LNG — the store-read equivalence test enforces it.
        ut.cum_lat_e7 / ({geo_scale}.0 * NULLIF(ut.cum_geo_n, 0)) AS u_lat,
        ut.cum_lng_e7 / ({geo_scale}.0 * NULLIF(ut.cum_geo_n, 0)) AS u_lng,
        ut.cum_price / NULLIF(ut.cum_price_n, 0) AS u_price
    FROM {queries} q
    ASOF LEFT JOIN user_tl ut
        ON q.user_id = ut.user_id AND q.ts > ut.ts
)
SELECT w.query_id,
    w.u_reviews_to_date,
    w.u_mean_stars_to_date,
    w.u_days_since_last,
    COALESCE(it.cum_count, 0)              AS i_reviews_to_date,
    it.cum_sum / it.cum_count              AS i_mean_stars_to_date,
    {haversine}                            AS ui_distance_km,
    a.matched / NULLIF(w.u_reviews_to_date, 0) AS ui_category_affinity,
    abs(d.price_tier - w.u_price)          AS ui_price_delta
FROM with_user w
ASOF LEFT JOIN item_tl it
    ON w.business_id = it.business_id AND w.ts > it.ts
LEFT JOIN {dim} d ON w.business_id = d.business_id
LEFT JOIN affinity a ON w.query_id = a.query_id
ORDER BY w.query_id
"""


def feature_query(
    events: str = "events", queries: str = "queries", dim: str = "dim_business"
) -> str:
    """The point-in-time feature SQL over the named events/queries/dim relations."""
    return _SQL.format(
        queries=queries,
        dim=dim,
        haversine=_HAVERSINE.strip(),
        geo_scale=GEO_SCALE,
        user_state=state_query("user", events, dim),
        item_state=state_query("item", events, dim),
        user_cat_state=state_query("user_category", events, dim),
    )


def compute(
    con: duckdb.DuckDBPyConnection,
    events: str = "events",
    queries: str = "queries",
    dim: str = "dim_business",
) -> list[tuple[object, ...]]:
    """Run the feature query; rows are (query_id, *FEATURE_COLUMNS)."""
    return con.execute(feature_query(events, queries, dim)).fetchall()
