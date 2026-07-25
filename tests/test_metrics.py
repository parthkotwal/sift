"""Metric correctness, including the worked NDCG example from the design notes."""

from __future__ import annotations

import math

from sift.eval.metrics import dcg, ndcg_at_k, percentile, recall_at_k


def test_recall_counts_hits_anywhere_in_top_k() -> None:
    recommended = ["a", "b", "c", "d"]
    assert recall_at_k(recommended, {"a", "z"}, 4) == 0.5  # 1 of 2 relevant found
    assert recall_at_k(recommended, {"d"}, 3) == 0.0       # d is outside the top-3
    assert recall_at_k(recommended, {"d"}, 4) == 1.0


def test_recall_is_position_blind() -> None:
    """Rank 1 and rank 4 are worth exactly the same to recall."""
    assert recall_at_k(["a", "x", "y", "z"], {"a"}, 4) == recall_at_k(
        ["x", "y", "z", "a"], {"a"}, 4
    )


def test_recall_with_no_relevant_items_is_zero() -> None:
    assert recall_at_k(["a"], set(), 1) == 0.0


def test_dcg_discount_matches_log2_positions() -> None:
    # position 1 -> /log2(2)=1, position 2 -> /log2(3), position 3 -> /log2(4)=2
    assert dcg([1.0]) == 1.0
    assert math.isclose(dcg([0.0, 1.0, 1.0]), 1 / math.log2(3) + 1 / math.log2(4))


def test_ndcg_worked_example() -> None:
    """2 relevant items, top-3 = [miss, hit, hit] -> 0.693 (see design notes)."""
    value = ndcg_at_k(["x", "a", "b"], {"a", "b"}, 3)
    assert math.isclose(value, 0.6934264036172708, rel_tol=1e-9)


def test_ndcg_is_one_for_perfect_ordering() -> None:
    assert ndcg_at_k(["a", "b", "x"], {"a", "b"}, 3) == 1.0


def test_ndcg_rewards_higher_positions() -> None:
    """Same single hit, earlier position must score strictly higher."""
    assert ndcg_at_k(["a", "x"], {"a"}, 2) > ndcg_at_k(["x", "a"], {"a"}, 2)


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]  # 1..100
    assert percentile(values, 50) == 50.0
    assert percentile(values, 99) == 99.0
    assert percentile(values, 100) == 100.0
    assert percentile([], 99) == 0.0
