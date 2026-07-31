"""The store's read path — the one legal way a feature reaches a consumer.

Chokepoint 2 (ARCHITECTURE → "The spine"): training assembly and serving both get
features from here and nowhere else. The as-of join exists in exactly one place, so
right-exclusivity is a property of one query rather than a convention several call
sites are trusted to follow.

## What this assembles

Materialised state (`sift.store.materialize`) joined to a `queries` relation under
the five aliases every definition expression is written against:

    q   the query row                (query_id, user_id, business_id, ts)
    u   user state as-of q.ts        ASOF LEFT JOIN ... AND q.ts > u.ts
    i   item state as-of q.ts        ASOF LEFT JOIN ... AND q.ts > i.ts
    uc  the taste-vector dot product aggregated from user_category state, as-of q.ts
    b   business attributes          plain join on identity — static (D21)

`ASOF LEFT JOIN` with `q.ts > state.ts` is the whole point-in-time guarantee: it
matches the single most recent state row *strictly* before the query instant, so an
event at exactly q.ts — the label's own instant — can never contribute. Storing
inclusive state and reading exclusively is what lets one materialisation answer a
2013 training row and a serving query at today's "now" (D23).

## Only what is asked for is joined

`definitions.required_groups` drives the FROM clause, so asking for user features
alone costs one ASOF join, not the category explosion. That matters: the
`user_category` state is ~5x the size of the others, and the affinity aggregate
multiplies the query rows by each business's category count.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import duckdb

from sift.config import sql_path
from sift.features import definitions as defs
from sift.offline.dim_business import DIM_BUSINESS
from sift.store.materialize import HISTORICAL_DIR, state_path

# Relation names this module expects; `attach_store` creates them as views.
USER_STATE = "user_state"
ITEM_STATE = "item_state"
USER_CATEGORY_STATE = "user_category_state"
USER_ALS_STATE = "user_als_state"
ITEM_ALS_STATE = "item_als_state"
DIM = "dim_business"


def attach_store(
    con: duckdb.DuckDBPyConnection,
    *,
    historical_dir: Path = HISTORICAL_DIR,
    dim_file: Path = DIM_BUSINESS,
) -> None:
    """Expose the materialised store to a connection under the standard names.

    The ALS slice groups are attached only when their artifacts exist: they are
    produced by a later build step (D27), and a store without them still serves the
    other eight features. Requesting `ui_als_score` against a store that lacks them
    fails loudly on the missing relation rather than silently returning NULL, which
    is the behaviour we want — a silently absent retrieval score would look like a
    feature that simply does not help.
    """
    for group, relation in (
        ("user", USER_STATE),
        ("item", ITEM_STATE),
        ("user_category", USER_CATEGORY_STATE),
    ):
        path = state_path(group, historical_dir)
        con.execute(
            f"CREATE OR REPLACE VIEW {relation} AS "
            f"SELECT * FROM read_parquet({sql_path(path)})"
        )
    for group, relation in (("user_als", USER_ALS_STATE), ("item_als", ITEM_ALS_STATE)):
        path = state_path(group, historical_dir)
        if path.exists():
            con.execute(
                f"CREATE OR REPLACE VIEW {relation} AS "
                f"SELECT * FROM read_parquet({sql_path(path)})"
            )
    con.execute(
        f"CREATE OR REPLACE VIEW {DIM} AS "
        f"SELECT * FROM read_parquet({sql_path(dim_file)})"
    )


def asof_feature_query(
    features: Sequence[str] | None = None,
    queries: str = "queries",
    *,
    user_state: str = USER_STATE,
    item_state: str = ITEM_STATE,
    user_category_state: str = USER_CATEGORY_STATE,
    dim: str = DIM,
    user_als_state: str = USER_ALS_STATE,
    item_als_state: str = ITEM_ALS_STATE,
) -> str:
    """SQL returning (query_id, *features) for every row of `queries`, as-of its ts."""
    names = tuple(features) if features is not None else defs.feature_names()
    for name in names:
        defs.get(name)  # raises on an unregistered name before any SQL is built
    groups = defs.required_groups(names)

    ctes: list[str] = []
    joins: list[str] = []

    if "user" in groups:
        joins.append(
            f"ASOF LEFT JOIN {user_state} u "
            "ON q.user_id = u.user_id AND q.ts > u.ts"
        )
    if "item" in groups:
        joins.append(
            f"ASOF LEFT JOIN {item_state} i "
            "ON q.business_id = i.business_id AND q.ts > i.ts"
        )
    if "user_category" in groups:
        # Explode each query by its business's categories, look each up as-of ts, and
        # sum: the dot product of the user's taste vector with the business's
        # category indicator. Aggregated to one row per query before joining back.
        ctes.append(
            f"""dim_cat AS (
    SELECT business_id, unnest(categories) AS category FROM {dim}
),
query_cat AS (
    SELECT q.query_id, q.user_id, q.ts, c.category
    FROM {queries} q JOIN dim_cat c ON q.business_id = c.business_id
),
uc AS (
    SELECT qc.query_id, sum(COALESCE(t.cum_count, 0)) AS matched
    FROM query_cat qc
    ASOF LEFT JOIN {user_category_state} t
        ON qc.user_id = t.user_id AND qc.category = t.category AND qc.ts > t.ts
    GROUP BY qc.query_id
)"""
        )
        joins.append("LEFT JOIN uc ON q.query_id = uc.query_id")
    if "business" in groups:
        joins.append(f"LEFT JOIN {dim} b ON q.business_id = b.business_id")
    if "user_als" in groups or "item_als" in groups:
        # The ALS slices are ordinary timestamped state, so the as-of join picks the
        # model fit strictly before this query — that is what stops retrieval's own
        # score from reporting the label it is meant to predict (D27). The dot
        # product lives in a CTE behind *inner* ASOF joins so it only ever sees rows
        # where both vectors exist; DuckDB's list_dot_product raises on an absent
        # LEFT-join payload instead of returning NULL. The outer LEFT JOIN restores
        # the missing rows with a NULL score.
        ctes.append(
            f"""als AS (
    SELECT q.query_id, list_dot_product(ua.value, ia.value) AS score
    FROM {queries} q
    ASOF JOIN {user_als_state} ua
        ON q.user_id = ua.user_id AND q.ts > ua.ts
    ASOF JOIN {item_als_state} ia
        ON q.business_id = ia.business_id AND q.ts > ia.ts
)"""
        )
        joins.append("LEFT JOIN als ON q.query_id = als.query_id")

    projection = ",\n    ".join(
        f"{defs.get(name).expr} AS {name}" for name in names
    )
    with_clause = "WITH " + ",\n".join(ctes) + "\n" if ctes else ""
    return (
        f"{with_clause}SELECT q.query_id,\n    {projection}\n"
        f"FROM {queries} q\n" + "\n".join(joins) + "\nORDER BY q.query_id"
    )


def current_feature_query(
    features: Sequence[str] | None = None,
    queries: str = "queries",
    *,
    user_state: str = "user_current",
    item_state: str = "item_current",
    user_category_state: str = "user_category_current",
    dim: str = "business_current",
    user_als_state: str = "user_als_current",
    item_als_state: str = "item_als_current",
) -> str:
    """SQL projecting current Redis state through the registered expressions.

    Redis has already selected the last state row per entity, so these are ordinary
    identity joins rather than temporal joins. The feature expressions are exactly
    the same ones used by :func:`asof_feature_query`; only the way their input aliases
    are populated differs. Keeping both assemblers here makes this module the single
    feature-read chokepoint for training and serving.
    """
    # Defaults to what the publisher actually materialises, not to the whole
    # registry: a feature whose state never reaches Redis cannot be served, and
    # silently returning NULL for it would be skew wearing a NULL's clothes.
    names = tuple(features) if features is not None else defs.online_features()
    for name in names:
        defs.get(name)
    groups = defs.required_groups(names)

    ctes: list[str] = []
    joins: list[str] = []
    if "user" in groups:
        joins.append(f"LEFT JOIN {user_state} u ON q.user_id = u.user_id")
    if "item" in groups:
        joins.append(f"LEFT JOIN {item_state} i ON q.business_id = i.business_id")
    if "user_category" in groups:
        ctes.append(
            f"""dim_cat AS (
    SELECT business_id, unnest(categories) AS category FROM {dim}
),
query_cat AS (
    SELECT q.query_id, q.user_id, c.category
    FROM {queries} q JOIN dim_cat c ON q.business_id = c.business_id
),
uc AS (
    SELECT qc.query_id, sum(COALESCE(t.cum_count, 0)) AS matched
    FROM query_cat qc
    LEFT JOIN {user_category_state} t
        ON qc.user_id = t.user_id AND qc.category = t.category
    GROUP BY qc.query_id
)"""
        )
        joins.append("LEFT JOIN uc ON q.query_id = uc.query_id")
    if "business" in groups:
        joins.append(f"LEFT JOIN {dim} b ON q.business_id = b.business_id")
    if "user_als" in groups or "item_als" in groups:
        # Identity joins: Redis already holds the newest slice per entity, so the
        # temporal selection happened at publish time. Inner joins for the same
        # reason as the as-of path — list_dot_product raises on an absent payload.
        ctes.append(
            f"""als AS (
    SELECT q.query_id, list_dot_product(ua.value, ia.value) AS score
    FROM {queries} q
    JOIN {user_als_state} ua ON q.user_id = ua.user_id
    JOIN {item_als_state} ia ON q.business_id = ia.business_id
)"""
        )
        joins.append("LEFT JOIN als ON q.query_id = als.query_id")

    projection = ",\n    ".join(f"{defs.get(name).expr} AS {name}" for name in names)
    with_clause = "WITH " + ",\n".join(ctes) + "\n" if ctes else ""
    return (
        f"{with_clause}SELECT q.query_id,\n    {projection}\n"
        f"FROM {queries} q\n" + "\n".join(joins) + "\nORDER BY q.query_id"
    )


def read_features(
    con: duckdb.DuckDBPyConnection,
    features: Sequence[str] | None = None,
    queries: str = "queries",
) -> list[tuple[object, ...]]:
    """Run the read path; rows are (query_id, *features)."""
    return con.execute(asof_feature_query(features, queries)).fetchall()


def read_current_features(
    con: duckdb.DuckDBPyConnection,
    features: Sequence[str] | None = None,
    queries: str = "queries",
    *,
    item_state: str = "item_current",
    dim: str = "business_current",
    item_als_state: str = "item_als_current",
) -> list[tuple[object, ...]]:
    """Project current state; rows are ``(query_id, *features)``.

    The catalog-wide relations are named by the caller because the online store builds
    one immutable set per Redis generation, so an in-flight request keeps reading the
    snapshot it started on while a newer one uses its own (I31). The defaults name the
    per-request relations, which is what a test or an ad-hoc projection would build.
    """
    return con.execute(
        current_feature_query(
            features,
            queries,
            item_state=item_state,
            dim=dim,
            item_als_state=item_als_state,
        )
    ).fetchall()


def get_asof(
    con: duckdb.DuckDBPyConnection,
    name: str,
    *,
    user_id: str,
    business_id: str,
    ts: str,
) -> object:
    """One feature value for one (user, business) as-of ts.

    A convenience for inspection and tests — every stage must be printable
    (AGENTS.md). It builds a one-row `queries` relation and runs the *same* query as
    the bulk path, so it cannot drift from it: this is a different calling
    convention, not a second implementation.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _asof_query AS "
        "SELECT 1::BIGINT AS query_id, ? AS user_id, ? AS business_id, "
        "CAST(? AS TIMESTAMP) AS ts",
        [user_id, business_id, ts],
    )
    row = con.execute(asof_feature_query([name], "_asof_query")).fetchone()
    assert row is not None
    return row[1]
