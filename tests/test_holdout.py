"""The frozen eval-set definitions, asserted on synthetic events (D18)."""

from __future__ import annotations

from datetime import date

from sift.eval.holdout import build_ground_truth

T = date(2019, 1, 1)
CATALOG = {"b1", "b2", "b3"}


def test_user_without_pre_t_history_is_excluded() -> None:
    """Cold-start users are not in the eval set — nothing to personalize from."""
    events = [("cold_user", "b1", date(2020, 5, 1))]
    assert build_ground_truth(CATALOG, events, T) == {}


def test_user_with_pre_t_history_is_included() -> None:
    events = [
        ("u1", "b1", date(2018, 5, 1)),  # history -> makes u1 eligible
        ("u1", "b2", date(2020, 5, 1)),  # target
    ]
    assert build_ground_truth(CATALOG, events, T) == {"u1": {"b2"}}


def test_repeat_business_is_kept_as_a_target() -> None:
    """Frozen choice: a business reviewed both before and after T still counts."""
    events = [
        ("u1", "b1", date(2018, 5, 1)),
        ("u1", "b1", date(2020, 5, 1)),  # same business again, post-T
    ]
    assert build_ground_truth(CATALOG, events, T) == {"u1": {"b1"}}


def test_split_boundary_is_right_exclusive() -> None:
    """A review dated exactly T is a target, never history."""
    events = [
        ("u1", "b1", date(2018, 12, 31)),  # history
        ("u1", "b2", T),                   # exactly T -> target
    ]
    assert build_ground_truth(CATALOG, events, T) == {"u1": {"b2"}}


def test_events_outside_the_catalog_are_ignored() -> None:
    """Another metro's businesses are neither history nor targets."""
    events = [
        ("u1", "elsewhere", date(2018, 1, 1)),  # does not grant eligibility
        ("u1", "b1", date(2020, 1, 1)),
    ]
    assert build_ground_truth(CATALOG, events, T) == {}


def test_all_star_ratings_count_as_relevant() -> None:
    """Relevance is engagement: the builder never sees stars, by construction."""
    events = [
        ("u1", "b1", date(2018, 1, 1)),
        ("u1", "b2", date(2019, 6, 1)),
        ("u1", "b3", date(2019, 7, 1)),
    ]
    assert build_ground_truth(CATALOG, events, T) == {"u1": {"b2", "b3"}}
