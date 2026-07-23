"""Correctness properties for the popularity baseline.

The load-bearing test is the leak boundary: a review dated exactly SPLIT_T must
not count toward pre-T popularity. Fixtures are synthetic (Yelp license forbids
real records in the repo).
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from sift.offline.popularity import count_pre_t_reviews, parse_review_date, rank


def test_parse_review_date_strips_time() -> None:
    assert parse_review_date("2018-07-07 22:09:11") == date(2018, 7, 7)


def test_split_is_right_exclusive() -> None:
    """Reviews before T count; a review dated exactly T (or later) does not."""
    split = date(2019, 1, 1)
    reviews = [
        ("b1", date(2018, 12, 31)),  # before T -> counts
        ("b1", date(2019, 1, 1)),    # exactly T -> eval side, must NOT count
        ("b1", date(2019, 6, 1)),    # after T  -> must NOT count
    ]
    counts = count_pre_t_reviews({"b1"}, reviews, split)
    assert counts["b1"] == 1


def test_only_catalog_members_are_counted() -> None:
    counts = count_pre_t_reviews({"b1"}, [("b2", date(2018, 1, 1))], date(2019, 1, 1))
    assert counts["b1"] == 0
    assert "b2" not in counts


def test_ranking_is_deterministic_and_covers_the_catalog() -> None:
    catalog = {"b1": "Alpha", "b2": "Beta", "b3": "Gamma"}
    counts: Counter[str] = Counter({"b1": 5, "b2": 5})  # b3 has zero pre-T reviews
    ranked = rank(catalog, counts)
    # count desc, then business_id asc for ties; the zero-count member is last,
    # not dropped — retrieval is evaluated against the full catalog.
    assert [e.business_id for e in ranked] == ["b1", "b2", "b3"]
    assert ranked[-1].pre_t_reviews == 0
