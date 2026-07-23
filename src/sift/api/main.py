"""FastAPI application entry point.

Build step 1 — the dumb path. `/recommend` returns the metro's most-reviewed
businesses (pre-split popularity), the same list for every user. That user-
independence is the baseline's defining weakness and the entire point: it is the
floor every later stage (retrieval, ranking) must beat on the frozen eval.

Serving reads the artifact written by `sift.offline.popularity` — one source of
truth shared with the offline job, so online and offline can't disagree.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from sift.config import METRO_CITY, METRO_STATE
from sift.offline.popularity import PopularityEntry, load_ranking

app = FastAPI(title="Sift", version="0.0.0")


class Recommendation(BaseModel):
    rank: int
    business_id: str
    name: str
    pre_t_reviews: int


class RecommendResponse(BaseModel):
    user_id: str
    metro: str
    baseline: str
    results: list[Recommendation]


def get_ranking() -> list[PopularityEntry]:
    """Dependency: the popularity ranking. Overridable in tests."""
    try:
        return load_ranking()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail="popularity artifact not built; run `python -m sift.offline.popularity`",
        ) from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/recommend")
def recommend(
    user_id: str,
    ranking: Annotated[list[PopularityEntry], Depends(get_ranking)],
    k: Annotated[int, Query(ge=1, le=500)] = 10,
) -> RecommendResponse:
    # user_id is accepted but ignored: the popularity baseline is identical for
    # everyone. When retrieval (step 5) lands, the response becomes user-specific.
    top = ranking[:k]
    return RecommendResponse(
        user_id=user_id,
        metro=f"{METRO_CITY}, {METRO_STATE}",
        baseline="popularity (pre-T review count)",
        results=[
            Recommendation(
                rank=i,
                business_id=e.business_id,
                name=e.name,
                pre_t_reviews=e.pre_t_reviews,
            )
            for i, e in enumerate(top, start=1)
        ],
    )
