"""Endpoint behavior for the dumb popularity path.

Hermetic: the ranking dependency is overridden with a synthetic list, so these
never touch the git-ignored real artifact.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from sift.api.main import app, get_ranking
from sift.offline.popularity import PopularityEntry

_FAKE_RANKING = [
    PopularityEntry("b1", "Alpha", 100),
    PopularityEntry("b2", "Beta", 50),
    PopularityEntry("b3", "Gamma", 0),
]


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_ranking] = lambda: _FAKE_RANKING
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_recommend_returns_top_k_in_order(client: TestClient) -> None:
    resp = client.get("/recommend", params={"user_id": "u1", "k": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert [r["business_id"] for r in body["results"]] == ["b1", "b2"]
    assert [r["rank"] for r in body["results"]] == [1, 2]


def test_recommend_is_user_independent(client: TestClient) -> None:
    """The defining property of the baseline: same list for every user."""
    a = client.get("/recommend", params={"user_id": "alice"}).json()["results"]
    b = client.get("/recommend", params={"user_id": "bob"}).json()["results"]
    assert a == b


def test_recommend_rejects_out_of_range_k(client: TestClient) -> None:
    assert client.get("/recommend", params={"user_id": "u1", "k": 0}).status_code == 422
    assert client.get("/recommend", params={"user_id": "u1", "k": 999}).status_code == 422
