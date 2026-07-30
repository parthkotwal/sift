"""Endpoint behavior for the Redis-backed online ALS path."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from lightgbm.basic import LightGBMError

import sift.api.main as api_main
from sift.api.main import app, get_online_retriever
from sift.ranking.online import (
    OnlineRecommendation,
    RankedBusiness,
    StageLatency,
)


class _FakeRanker:
    def recommend(self, user_id: str, k: int = 10) -> OnlineRecommendation:
        entries = (
            RankedBusiness(f"{user_id}-b1", "Alpha", 100, 0.9),
            RankedBusiness(f"{user_id}-b2", "Beta", 50, 0.7),
            RankedBusiness(f"{user_id}-b3", "Gamma", 0, 0.1),
        )
        return OnlineRecommendation(
            results=entries[:k],
            latency=StageLatency(0.1, 2.0, 1.0, 0.5, 0.2, 3.3),
        )


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_online_retriever] = _FakeRanker
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_recommend_returns_ranked_online_results(client: TestClient) -> None:
    resp = client.get("/recommend", params={"user_id": "u1", "k": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert [r["business_id"] for r in body["results"]] == ["u1-b1", "u1-b2"]
    assert [r["rank"] for r in body["results"]] == [1, 2]
    # Assert what the description must convey, not its exact wording: the response
    # names the stages actually in the path. A stale `path` is how a displaced model
    # goes unnoticed (I30), so the ranker has to appear here once it serves.
    path = body["path"]
    for stage in ("user embedding", "ALS retrieval", "online features", "ranker", "rerank"):
        assert stage in path, f"{stage!r} missing from {path!r}"
    assert "cold fallback" in path


def test_recommend_exposes_real_stage_timings(client: TestClient) -> None:
    resp = client.get("/recommend", params={"user_id": "u1"})
    assert resp.json()["latency"] == {
        "retrieval_ms": 0.1,
        "feature_lookup_ms": 2.0,
        "ranking_ms": 1.0,
        "rerank_ms": 0.5,
        "overhead_ms": 0.2,
        "total_ms": 3.3,
    }
    assert "features;dur=2.000" in resp.headers["server-timing"]
    assert "app;dur=" in resp.headers["server-timing"]


def test_recommend_is_user_conditioned(client: TestClient) -> None:
    a = client.get("/recommend", params={"user_id": "alice"}).json()["results"]
    b = client.get("/recommend", params={"user_id": "bob"}).json()["results"]
    assert a != b


def test_recommend_rejects_out_of_range_k(client: TestClient) -> None:
    assert client.get("/recommend", params={"user_id": "u1", "k": 0}).status_code == 422
    assert client.get("/recommend", params={"user_id": "u1", "k": 51}).status_code == 422


def test_missing_ranker_artifact_is_an_actionable_availability_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_ranker() -> OnlineRecommendation:
        raise LightGBMError("Could not open ranker_als.txt")

    monkeypatch.setattr(api_main, "_load_online_retriever", missing_ranker)
    with pytest.raises(HTTPException) as caught:
        api_main.get_online_retriever()
    assert caught.value.status_code == 503
    assert "ranker artifacts" in str(caught.value.detail)
