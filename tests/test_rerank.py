"""The rerank stage: hard filters, then diversity that never shortens the list."""

from __future__ import annotations

import json
from pathlib import Path

from sift.config import SPLIT_T
from sift.offline.dim_business import build_dim_business
from sift.offline.ingest import build_events
from sift.rerank.evaluate import load_catalog_attributes, load_pre_t_reviewed
from sift.rerank.rerank import Candidate, rerank


def _candidate(
    business_id: str,
    score: float,
    *,
    is_open: bool = True,
    categories: tuple[str, ...] = ("Restaurants",),
) -> Candidate:
    return Candidate(business_id, score, is_open, categories)


def _pool(n: int, category_of: dict[int, str] | None = None) -> list[Candidate]:
    """n candidates in descending ranker order, each in its own category by default."""
    category_of = category_of or {}
    return [
        _candidate(f"b{i}", float(n - i), categories=(category_of.get(i, f"cat{i}"),))
        for i in range(n)
    ]


def test_closed_businesses_are_dropped() -> None:
    candidates = [
        _candidate("open1", 3.0),
        _candidate("closed", 2.0, is_open=False),
        _candidate("open2", 1.0),
    ]
    assert [c.business_id for c in rerank(candidates, 3)] == ["open1", "open2"]


def test_already_reviewed_businesses_are_dropped() -> None:
    """The measured payoff of this filter: repeats take 13% of top-10 slots."""
    candidates = [_candidate("seen", 3.0), _candidate("fresh", 2.0)]
    assert [c.business_id for c in rerank(candidates, 2, reviewed={"seen"})] == ["fresh"]


def test_the_rankers_order_survives_filtering() -> None:
    """Rerank removes and caps; it must never reorder what it keeps, or it would be
    silently overriding the stage whose whole job is ordering."""
    candidates = _pool(6)
    kept = rerank(candidates, 6, reviewed={"b1", "b4"})
    assert [c.business_id for c in kept] == ["b0", "b2", "b3", "b5"]
    assert [c.score for c in kept] == sorted((c.score for c in kept), reverse=True)


def test_category_cap_limits_how_many_of_one_category_reach_the_final_list() -> None:
    candidates = _pool(6, category_of=dict.fromkeys(range(5), "Pizza"))
    kept = rerank(candidates, 3, category_cap=2)
    # b0, b1 fill the Pizza cap; b2..b4 are deferred; b5 has its own category.
    assert [c.business_id for c in kept] == ["b0", "b1", "b5"]


def test_diversity_defers_rather_than_discards_so_the_list_is_never_short() -> None:
    """A pass that returns 7 results instead of 10 is a worse failure than a
    monotonous 10, so capped candidates backfill in ranker order."""
    candidates = _pool(5, category_of=dict.fromkeys(range(5), "Pizza"))
    kept = rerank(candidates, 4, category_cap=2)
    assert [c.business_id for c in kept] == ["b0", "b1", "b2", "b3"]
    assert len(kept) == 4


def test_uncategorised_businesses_are_not_capped_against_each_other() -> None:
    """They share no category, so pooling them into one bucket would cap them for a
    similarity that does not exist."""
    candidates = [_candidate(f"b{i}", float(5 - i), categories=()) for i in range(5)]
    kept = rerank(candidates, 5, category_cap=1)
    assert [c.business_id for c in kept] == ["b0", "b1", "b2", "b3", "b4"]


def test_closed_filter_can_be_disabled_for_the_offline_comparison() -> None:
    """I12: the same code path must be measurable both ways, because the closed filter
    removes 8.64% of holdout targets for reasons that are not ranking quality."""
    candidates = [_candidate("open", 2.0), _candidate("closed", 1.0, is_open=False)]
    assert len(rerank(candidates, 2, filter_closed=False)) == 2
    assert len(rerank(candidates, 2, filter_closed=True)) == 1


def test_returns_fewer_than_k_when_the_pool_genuinely_lacks_eligible_candidates() -> None:
    """Honest shortfall rather than padding with filtered-out businesses."""
    candidates = [_candidate("a", 2.0, is_open=False), _candidate("b", 1.0)]
    assert [c.business_id for c in rerank(candidates, 10)] == ["b"]


def test_an_empty_pool_and_zero_k_are_handled_without_special_casing() -> None:
    assert rerank([], 10) == []
    assert rerank(_pool(3), 0) == []


def test_reranking_to_a_larger_k_does_not_change_the_head() -> None:
    """The assumption `rerank.evaluate` rests on: it reranks to RERANK_POOL so that
    recall@50 stays meaningful, and reports recall@10 from the same list. That is only
    honest if the first 10 are identical to what serving (k=10) would return."""
    candidates = _pool(20, category_of=dict.fromkeys(range(12), "Pizza"))
    served = rerank(candidates, 10)
    harness = rerank(candidates, 50)
    assert [c.business_id for c in harness[:10]] == [c.business_id for c in served]


def test_backfill_appends_and_never_reorders_the_capped_head() -> None:
    """Deferred candidates must land *after* the diversified head, not interleaved —
    otherwise the backfill would silently undo the diversity it is rescuing."""
    candidates = _pool(4, category_of=dict.fromkeys(range(3), "Pizza"))
    kept = [c.business_id for c in rerank(candidates, 4, category_cap=2)]
    assert kept == ["b0", "b1", "b3", "b2"]


def test_pre_t_reviewed_history_is_right_exclusive(tmp_path: Path) -> None:
    """The eval-side reviewed set is the one place rerank touches the temporal
    boundary, so it obeys the same right-exclusive rule as every training read: a
    review dated exactly T is a target, never history."""
    reviews = [
        {"user_id": "u1", "business_id": "b1", "stars": 5, "date": "2018-12-31 23:59:59"},
        {"user_id": "u1", "business_id": "b2", "stars": 5, "date": "2019-01-01 00:00:00"},
    ]
    businesses = [
        {
            "business_id": bid,
            "name": bid,
            "city": "Philadelphia",
            "state": "PA",
            "latitude": 39.95,
            "longitude": -75.16,
            "is_open": 1 if bid == "b1" else 0,
            "categories": "Restaurants",
            "attributes": None,
        }
        for bid in ("b1", "b2")
    ]
    business_json = tmp_path / "business.json"
    review_json = tmp_path / "review.json"
    business_json.write_text("\n".join(json.dumps(r) for r in businesses) + "\n")
    review_json.write_text("\n".join(json.dumps(r) for r in reviews) + "\n")
    events = tmp_path / "events"
    dim = tmp_path / "dim.parquet"
    build_events(
        business_json=business_json,
        review_json=review_json,
        out_dir=events,
        metro_city="Philadelphia",
        metro_state="PA",
    )
    build_dim_business(
        business_json=business_json,
        out_file=dim,
        metro_city="Philadelphia",
        metro_state="PA",
    )

    reviewed = load_pre_t_reviewed(events, SPLIT_T)
    assert reviewed == {"u1": {"b1"}}, "the review dated exactly T is a target, not history"

    is_open, categories = load_catalog_attributes(dim)
    assert is_open == {"b1": True, "b2": False}
    assert categories["b1"] == ("Restaurants",)


# --- the k contract -------------------------------------------------------------
#
# Every test below was written against a stage that returned 33-40 results for a legal
# k=50 and said nothing about it. The cause was upstream — callers sliced the ranked
# pool to a fixed 50 before filtering — but the guarantee belongs here, because this is
# the stage that decides how many results exist.


def test_exactly_k_when_the_pool_holds_k_eligible_candidates() -> None:
    """The contract: k means k, not "up to k", whenever the pool can pay for it.

    Filtering is heavy here — 30 closed and 20 already reviewed out of 100 — leaving
    exactly 50 eligible for a k=50 request. The old fixed 50-candidate slice could not
    have filled this even in principle.
    """
    candidates = _pool(100)
    closed = {f"b{i}" for i in range(30)}
    reviewed = {f"b{i}" for i in range(30, 50)}
    candidates = [
        _candidate(
            c.business_id, c.score, is_open=c.business_id not in closed, categories=c.categories
        )
        for c in candidates
    ]

    kept = rerank(candidates, 50, reviewed)

    assert len(kept) == 50
    assert not {c.business_id for c in kept} & (closed | reviewed)


def test_a_short_list_means_the_pool_ran_out_not_that_the_stage_gave_up() -> None:
    """Coming up short is legal only when eligibility, not slicing, is the constraint.

    The stage must not pad: restoring a closed or already-reviewed business to reach k
    would defeat the only stage that can say no, which is worse than a short list.
    """
    candidates = _pool(60)
    closed = {f"b{i}" for i in range(20)}
    candidates = [
        _candidate(
            c.business_id, c.score, is_open=c.business_id not in closed, categories=c.categories
        )
        for c in candidates
    ]
    reviewed = {f"b{i}" for i in range(20, 30)}

    kept = rerank(candidates, 50, reviewed)

    assert len(kept) == 30, "60 candidates, 20 closed and 10 reviewed, leaves 30"
    assert not {c.business_id for c in kept} & (closed | reviewed)


def test_a_fully_ineligible_pool_returns_nothing_rather_than_anything() -> None:
    """The degenerate end of the same rule. Returning a closed business here would be
    the stage inventing a result, which is the one thing it exists to prevent."""
    candidates = [_candidate(f"b{i}", float(10 - i), is_open=False) for i in range(10)]
    assert rerank(candidates, 10) == []
    assert rerank(_pool(5), 5, {f"b{i}" for i in range(5)}) == []


def test_diversity_still_never_shortens_the_list_at_large_k() -> None:
    """The docstring's rule — a monotonous 10 beats a silent 7 — has to hold at the top
    of the k range too, where the cap defers far more than it admits."""
    candidates = _pool(60, category_of=dict.fromkeys(range(60), "Pizza"))
    kept = rerank(candidates, 50, category_cap=2)
    assert len(kept) == 50
    assert len({c.business_id for c in kept}) == 50, "backfill must not duplicate"
