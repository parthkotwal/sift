"""FastAPI entry point for Redis-backed ALS retrieval."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel
from redis.exceptions import RedisError

from sift.config import METRO_CITY, METRO_STATE
from sift.retrieval.online import OnlineALSRetriever
from sift.store.online import OnlineStoreUnavailable

app = FastAPI(title="Sift", version="0.0.0")


@app.middleware("http")
async def add_app_timing(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Expose app-level time, including routing and response serialization."""
    started = time.perf_counter()
    response = await call_next(request)
    app_ms = (time.perf_counter() - started) * 1_000
    stages = response.headers.get("Server-Timing")
    response.headers["Server-Timing"] = (
        f"{stages}, app;dur={app_ms:.3f}" if stages else f"app;dur={app_ms:.3f}"
    )
    return response


class Recommendation(BaseModel):
    rank: int
    business_id: str
    name: str
    pre_t_reviews: int
    score: float


class LatencyBreakdown(BaseModel):
    retrieval_ms: float
    feature_lookup_ms: float
    ranking_ms: float
    overhead_ms: float
    total_ms: float


class RecommendResponse(BaseModel):
    user_id: str
    metro: str
    path: str
    latency: LatencyBreakdown
    results: list[Recommendation]


@lru_cache(maxsize=1)
def _load_online_retriever() -> OnlineALSRetriever:
    return OnlineALSRetriever.load()


def get_online_retriever() -> OnlineALSRetriever:
    """Load the winning warm-user path and its explicit cold fallback."""
    try:
        return _load_online_retriever()
    except (FileNotFoundError, RedisError, OnlineStoreUnavailable) as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "online retriever unavailable; build the popularity/ALS artifacts, "
                "start Redis, and run `python -m sift.store.online`"
            ),
        ) from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/recommend")
def recommend(
    user_id: str,
    response: Response,
    retriever: Annotated[OnlineALSRetriever, Depends(get_online_retriever)],
    k: Annotated[int, Query(ge=1, le=50)] = 10,
) -> RecommendResponse:
    try:
        result = retriever.recommend(user_id, k)
    except (RedisError, OnlineStoreUnavailable) as exc:
        raise HTTPException(status_code=503, detail="online feature store unavailable") from exc
    latency = result.latency
    response.headers["Server-Timing"] = (
        f"retrieval;dur={latency.retrieval_ms:.3f}, "
        f"features;dur={latency.feature_lookup_ms:.3f}, "
        f"ranking;dur={latency.ranking_ms:.3f}, "
        f"total;dur={latency.total_ms:.3f}"
    )
    return RecommendResponse(
        user_id=user_id,
        metro=f"{METRO_CITY}, {METRO_STATE}",
        path=(
            "Redis user embedding -> exact ALS retrieval -> online features -> "
            "LightGBM ranker (unranked popularity cold fallback)"
        ),
        latency=LatencyBreakdown(
            retrieval_ms=latency.retrieval_ms,
            feature_lookup_ms=latency.feature_lookup_ms,
            ranking_ms=latency.ranking_ms,
            overhead_ms=latency.overhead_ms,
            total_ms=latency.total_ms,
        ),
        results=[
            Recommendation(
                rank=index,
                business_id=entry.business_id,
                name=entry.name,
                pre_t_reviews=entry.pre_t_reviews,
                score=entry.score,
            )
            for index, entry in enumerate(result.results, start=1)
        ],
    )
