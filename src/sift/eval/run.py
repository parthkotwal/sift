"""The eval harness. Judges any recommender against the frozen holdout.

A recommender is just `(user_id, k) -> ranked business_ids`. That signature is
the seam: the popularity baseline, ALS, and the two-tower all plug in here, and
every one of them is measured on the same ground truth. Nothing lands without
beating its predecessor on these numbers.

Run: python -m sift.eval.run
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from sift.eval.holdout import load_ground_truth
from sift.eval.metrics import ndcg_at_k, percentile, recall_at_k
from sift.offline.popularity import load_ranking

Recommender = Callable[[str, int], Sequence[str]]

DEFAULT_KS: tuple[int, ...] = (10, 50, 100, 500)
NDCG_K = 10


@dataclass
class EvalReport:
    name: str
    n_users: int
    recall: dict[int, float] = field(default_factory=dict)
    ndcg_at_10: float = 0.0
    latency_ms: dict[str, float] = field(default_factory=dict)

    def render(self) -> str:
        lines = [
            f"{self.name}",
            f"  eval users: {self.n_users:,}",
        ]
        for k in sorted(self.recall):
            lines.append(f"  recall@{k:<4} {self.recall[k]:.4f}")
        lines.append(f"  NDCG@{NDCG_K}    {self.ndcg_at_10:.4f}")
        if self.latency_ms:
            lat = " ".join(f"{name}={value:.3f}ms" for name, value in self.latency_ms.items())
            lines.append(f"  recommender-call latency: {lat}")
        return "\n".join(lines)


def evaluate(
    recommend: Recommender,
    ground_truth: Mapping[str, set[str]],
    name: str,
    ks: Sequence[int] = DEFAULT_KS,
    *,
    measure_latency: bool = True,
) -> EvalReport:
    """Macro-average: every user counts equally, regardless of activity level.

    ``measure_latency=False`` is required for a recommender backed by precomputed
    lists: timing that closure would measure a dict lookup, not serving work (I3).
    """
    max_k = max(ks)
    recall_totals = dict.fromkeys(ks, 0.0)
    ndcg_total = 0.0
    latencies: list[float] = []

    for user_id, relevant in ground_truth.items():
        start = time.perf_counter() if measure_latency else 0.0
        recommended = recommend(user_id, max_k)
        if measure_latency:
            latencies.append((time.perf_counter() - start) * 1000.0)
        for k in ks:
            recall_totals[k] += recall_at_k(recommended, relevant, k)
        ndcg_total += ndcg_at_k(recommended, relevant, NDCG_K)

    n = len(ground_truth)
    return EvalReport(
        name=name,
        n_users=n,
        recall={k: recall_totals[k] / n for k in ks} if n else dict.fromkeys(ks, 0.0),
        ndcg_at_10=ndcg_total / n if n else 0.0,
        latency_ms=(
            {
                "p50": percentile(latencies, 50),
                "p95": percentile(latencies, 95),
                "p99": percentile(latencies, 99),
            }
            if measure_latency
            else {}
        ),
    )


def popularity_recommender() -> Recommender:
    """The build-step-1 baseline as a recommender: same ranked list for everyone."""
    ranked_ids = [entry.business_id for entry in load_ranking()]

    def recommend(user_id: str, k: int) -> Sequence[str]:
        return ranked_ids[:k]

    return recommend


def main() -> None:
    ground_truth = load_ground_truth()
    report = evaluate(
        popularity_recommender(), ground_truth, name="popularity (pre-T review count)"
    )
    print(report.render())


if __name__ == "__main__":
    main()
