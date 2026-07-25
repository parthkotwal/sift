"""Ranking metrics. Pure functions over (ranked ids, relevant ids) — no IO.

recall@k measures *not missing* (order-blind) — retrieval's metric, large k.
NDCG@k measures *ordering* (position-discounted) — ranking's metric, small k.
See CONCEPTS.md → "Temporal splits; recall@k; NDCG".
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def recall_at_k(recommended: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the user's relevant items that appear anywhere in the top-k.

    Position-blind by design: retrieval's only sin is dropping an item, because
    anything outside the top-k is unrecoverable by later stages.
    """
    if not relevant:
        return 0.0
    hits = sum(1 for item in recommended[:k] if item in relevant)
    return hits / len(relevant)


def dcg(gains: Sequence[float]) -> float:
    """Discounted cumulative gain: sum of gain_i / log2(i + 1), i 1-based.

    Position 1 divides by log2(2)=1.0, position 10 by log2(11)=3.46 — a hit at
    the top is worth ~3.5x the same hit at rank 10.
    """
    return sum(gain / math.log2(i + 2) for i, gain in enumerate(gains))


def ndcg_at_k(recommended: Sequence[str], relevant: set[str], k: int) -> float:
    """DCG@k normalized by the best achievable DCG@k (all relevant items first).

    Binary relevance: 1 if the item is in the user's held-out set, else 0.
    Normalizing makes users with different numbers of relevant items comparable.
    """
    if not relevant:
        return 0.0
    gains = [1.0 if item in relevant else 0.0 for item in recommended[:k]]
    ideal = [1.0] * min(len(relevant), k)
    ideal_dcg = dcg(ideal)
    if ideal_dcg == 0.0:
        return 0.0
    return dcg(gains) / ideal_dcg


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile: the smallest value >= p% of the samples.

    Latency is a distribution and the mean lies; p99 is the unluckiest 1%.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(p / 100.0 * len(ordered))
    index = min(max(rank - 1, 0), len(ordered) - 1)
    return ordered[index]
