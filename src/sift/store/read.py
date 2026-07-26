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
DIM = "dim_business"


def attach_store(
    con: duckdb.DuckDBPyConnection,
    *,
    historical_dir: Path = HISTORICAL_DIR,
    dim_file: Path = DIM_BUSINESS,
) -> None:
    """Expose the materialised store to a connection under the standard names."""
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

    projection = ",\n    ".join(
        f"{defs.get(name).expr} AS {name}" for name in names
    )
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
