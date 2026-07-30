"""Exact inner-product index over ALS item factors.

The architecture expected ANN, but the measured Philadelphia catalog has only
14,568 items. Exact NumPy scoring is 0.961ms p99; HNSW achieved just 0.889
overlap@500, and a denser graph remained below the 0.99 fidelity gate while
becoming slower than exact search. At this scale, approximation pays no rent.

Run: ``python -m sift.retrieval.index``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from sift.eval.metrics import percentile
from sift.retrieval.artifacts import (
    FACTORS,
    ITEM_FACTORS,
    ITEM_IDS,
    USER_FACTORS,
    USER_IDS,
)

VALIDATION_USERS = 100
VALIDATION_K = 500


def exact_top_k(
    vector: NDArray[np.float32],
    item_factors: NDArray[np.float32],
    item_ids: tuple[str, ...],
    k: int,
) -> list[str]:
    """Full-catalog dot-product order, with item ID as deterministic tie-break."""
    if k < 1 or k > len(item_ids):
        raise ValueError(f"k must be in [1, {len(item_ids)}]")
    scores = np.asarray(item_factors @ vector, dtype=np.float32)
    # IDs are lexicographically sorted when exported. Stable sorting therefore
    # preserves the required item-ID tie-break without allocating strings/query.
    order = np.argsort(-scores, kind="stable")[:k]
    return [item_ids[int(index)] for index in order]


class ExactItemIndex:
    """Memory-mapped item factors behind the retrieval index interface."""

    def __init__(
        self,
        *,
        factors_file: Path = ITEM_FACTORS,
        ids_file: Path = ITEM_IDS,
    ) -> None:
        raw: list[str] = json.loads(ids_file.read_text())
        self.item_ids = tuple(raw)
        if tuple(sorted(self.item_ids)) != self.item_ids:
            raise ValueError("item IDs must be sorted for deterministic score ties")
        self.factors = np.load(factors_file, mmap_mode="r", allow_pickle=False)
        if self.factors.shape != (len(self.item_ids), FACTORS):
            raise ValueError("item factors and IDs disagree")

    def search(self, vector: NDArray[np.float32], k: int) -> list[str]:
        query = np.asarray(vector, dtype=np.float32).reshape(FACTORS)
        return exact_top_k(query, self.factors, self.item_ids, k)

    def search_scored(
        self, vector: NDArray[np.float32], k: int
    ) -> tuple[list[str], NDArray[np.float32]]:
        if k < 1 or k > len(self.item_ids):
            raise ValueError(f"k must be in [1, {len(self.item_ids)}]")
        query = np.asarray(vector, dtype=np.float32).reshape(FACTORS)
        scores = np.asarray(self.factors @ query, dtype=np.float32)
        order = np.argsort(-scores, kind="stable")[:k]
        return (
            [self.item_ids[int(index)] for index in order],
            scores[order],
        )


@dataclass(frozen=True)
class IndexValidation:
    sampled_users: int
    k: int
    latency_ms: dict[str, float]


def validate_index(
    index: ExactItemIndex,
    *,
    user_factors_file: Path = USER_FACTORS,
    user_ids_file: Path = USER_IDS,
    sample_size: int = VALIDATION_USERS,
    k: int = VALIDATION_K,
) -> IndexValidation:
    """Measure actual full-catalog query latency on a deterministic user sample."""
    user_factors = np.load(user_factors_file, mmap_mode="r", allow_pickle=False)
    user_ids: list[str] = json.loads(user_ids_file.read_text())
    if user_factors.shape != (len(user_ids), FACTORS):
        raise ValueError("user factor artifact and ID mapping disagree")
    sampled = sorted(
        range(len(user_ids)),
        key=lambda i: hashlib.sha256(user_ids[i].encode()).digest(),
    )[:sample_size]
    latencies: list[float] = []
    for user_index in sampled:
        started = time.perf_counter()
        index.search(np.asarray(user_factors[user_index], dtype=np.float32), k)
        latencies.append((time.perf_counter() - started) * 1_000)
    return IndexValidation(
        sampled_users=len(sampled),
        k=k,
        latency_ms={
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
        },
    )


def main() -> None:
    validation = validate_index(ExactItemIndex())
    values = validation.latency_ms
    print(
        f"exact full-catalog top-{validation.k} over {validation.sampled_users} users\n"
        f"  p50={values['p50']:.3f}ms p95={values['p95']:.3f}ms "
        f"p99={values['p99']:.3f}ms"
    )


if __name__ == "__main__":
    main()
