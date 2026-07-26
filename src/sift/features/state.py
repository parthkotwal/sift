"""State groups: the entity-keyed timelines the feature store persists (D23).

A state group is a table of *cumulative values as-of every timestamp where they
change* — one row per (entity, event instant), holding the running aggregates that
feature expressions project from. This is the compute-and-storage unit; definitions
(`sift.features.definitions`) are the reference unit.

The insight D23 rests on: these were already the CTEs inside the point-in-time
query. Materialising them is persisting what was being recomputed on every run, not
inventing new machinery — which is why the store can be honest about "one
definition, two materialisations" rather than growing a second implementation.

Three groups here, all derived from the review event stream:

  user           cum_count, cum_sum(stars), and the running lat/long/price sums the
                 centroid and price-mix features divide down
  item           cum_count, cum_sum(stars)
  user_category  cum_count per (user, category) — the taste vector, one row per
                 category the user has touched, as-of each of their events

The fourth group, `business`, is not here: it has no time axis (categories, lat/long,
price tier are quasi-static attributes, D21) and is already materialised by
`sift.offline.dim_business`.

**Right-exclusivity does not live in these tables.** A state row at ts carries the
value *including* that instant's events; the `< t` boundary is applied by the ASOF
join in the read path. Storing inclusive state and reading exclusively is what lets
one materialisation serve every possible query timestamp.

These strings are the single source: both `sift.store.materialize` (which persists
them) and `sift.features.pit` (which inlines them) format the same text.
"""

from __future__ import annotations

# Latitude and longitude are accumulated as fixed-point integers scaled by this
# factor, NOT as floats (ISSUES.md I18). Float addition is not associative, so a
# parallel sum reorders the additions and the last mantissa bits move between runs;
# integer addition is associative, so the sum is bit-exact whatever the order. That
# makes every column in every state group an integer, and the artifacts provably
# byte-reproducible rather than reproducible-in-practice.
#
# 1e7 is ~1.1 cm of latitude — finer than the source coordinates. Headroom is ample:
# a 40-degree latitude scales to 4e8, so even a user with 10^4 reviews sums to 4e12
# against BIGINT's 9.2e18.
GEO_SCALE = 10_000_000

# Reviews enriched with the business's static attributes. The join is on identity;
# all time filtering happens in the read path's ASOF joins.
_EV = """
    SELECT e.user_id, e.business_id, e.ts, e.stars,
           d.latitude, d.longitude, d.price_tier
    FROM {events} e
    LEFT JOIN {dim} d ON e.business_id = d.business_id
    WHERE e.event_type = 'review'
"""

# Ties on (entity, ts) are collapsed before the cumulative sum, so an entity with
# several events at the same instant still has one unambiguous state row.
_USER = f"""
WITH ev AS ({_EV}),
per_ts AS (
    SELECT user_id, ts, count(*) AS n, sum(stars) AS s,
           sum(CAST(round(latitude * {GEO_SCALE}) AS BIGINT))  AS lat_s,
           sum(CAST(round(longitude * {GEO_SCALE}) AS BIGINT)) AS lng_s,
           count(latitude) AS geo_n,
           sum(price_tier) AS price_s, count(price_tier) AS price_n
    FROM ev GROUP BY user_id, ts
)
-- Cast back to BIGINT: DuckDB widens sum(BIGINT) to HUGEINT to be overflow-safe,
-- which doubles the stored width and surfaces in Python as Decimal. These are store
-- artifacts, so a tight, predictable schema is worth the explicit cast. Magnitudes
-- are bounded far below BIGINT (see GEO_SCALE).
SELECT user_id, ts,
    CAST(sum(n) OVER w AS BIGINT)       AS cum_count,
    CAST(sum(s) OVER w AS BIGINT)       AS cum_sum,
    CAST(sum(lat_s) OVER w AS BIGINT)   AS cum_lat_e7,
    CAST(sum(lng_s) OVER w AS BIGINT)   AS cum_lng_e7,
    CAST(sum(geo_n) OVER w AS BIGINT)   AS cum_geo_n,
    CAST(sum(price_s) OVER w AS BIGINT) AS cum_price,
    CAST(sum(price_n) OVER w AS BIGINT) AS cum_price_n
FROM per_ts
WINDOW w AS (PARTITION BY user_id ORDER BY ts ROWS UNBOUNDED PRECEDING)
"""

_ITEM = """
WITH per_ts AS (
    SELECT business_id, ts, count(*) AS n, sum(stars) AS s
    FROM {events} WHERE event_type = 'review' GROUP BY business_id, ts
)
SELECT business_id, ts,
    CAST(sum(n) OVER w AS BIGINT) AS cum_count,
    CAST(sum(s) OVER w AS BIGINT) AS cum_sum
FROM per_ts
WINDOW w AS (PARTITION BY business_id ORDER BY ts ROWS UNBOUNDED PRECEDING)
"""

_USER_CATEGORY = """
WITH dim_cat AS (
    SELECT business_id, unnest(categories) AS category FROM {dim}
),
per_ts AS (
    SELECT e.user_id, c.category, e.ts, count(*) AS n
    FROM {events} e
    JOIN dim_cat c ON e.business_id = c.business_id
    WHERE e.event_type = 'review'
    GROUP BY 1, 2, 3
)
SELECT user_id, category, ts,
    CAST(sum(n) OVER (
        PARTITION BY user_id, category ORDER BY ts ROWS UNBOUNDED PRECEDING
    ) AS BIGINT) AS cum_count
FROM per_ts
"""

_SQL: dict[str, str] = {
    "user": _USER,
    "item": _ITEM,
    "user_category": _USER_CATEGORY,
}

# Sort keys that make each materialised artifact deterministic. The key is unique
# within its group, so the ordering is total, hence reproducible (I18's lesson).
SORT_KEYS: dict[str, str] = {
    "user": "user_id, ts",
    "item": "business_id, ts",
    "user_category": "user_id, category, ts",
}

# Groups with a time axis — the ones this module builds. `business` is static.
TIMELINE_GROUPS: tuple[str, ...] = ("user", "item", "user_category")


def state_query(
    group: str,
    events: str = "events",
    dim: str = "dim_business",
    ordered: bool = False,
) -> str:
    """SQL producing one state group over the named relations.

    `ordered=True` appends the group's total sort key — used when materialising to
    Parquet, where a deterministic row order is what makes the job idempotent.
    Inlined as a CTE (read path) the order is irrelevant, so it is left off.
    """
    if group not in _SQL:
        raise KeyError(
            f"unknown state group {group!r}; known: {', '.join(_SQL)}"
        )
    sql = _SQL[group].format(events=events, dim=dim)
    if ordered:
        sql = f"{sql}\nORDER BY {SORT_KEYS[group]}"
    return sql
