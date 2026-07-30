"""Build step 6: hard filters and a diversity pass, 50 -> 10.

The last stage, and the only one whose inputs are *deliberately* absent from
training. `is_open` is the cleanest example the project has of a signal that is
legitimate online and unconstructible historically (D13): the dump records whether a
business is open *now*, and no as-of value exists for 2019. A feature like that
cannot be learned without leaking, so it is applied here, after the model, where it
is a business rule rather than a signal.

The same reasoning covers the already-reviewed filter. Both inputs therefore sit
outside the training/serving skew check by construction — there is no training-side
value to compare against. That is a property of the stage, not a gap in the check.

**Both filters were measured before being adopted, and one result was a surprise.**
Businesses the user already reviewed are 1.3% of the ranker's 500-candidate pool but
**13.0%** of its top-10 — a 10x concentration, because `ui_als_score` and
`ui_category_affinity` both spike hardest on a pair the user actually visited. So
dropping them frees 13% of the final slots while forfeiting the ~1.6% of holdout
targets that are repeat visits (D18). Closed businesses are 27.6% of the catalog and
27.9% of the pool, but only 10.9% of the top-10 — the ranker has already partially
learned that signal through review recency, so the filter is less destructive than
the catalog share suggests. Full reasoning in DECISIONS.md D29.

Diversity is deliberately the simplest thing that works: a cap on how many of the
final list may share a primary category. It never shortens the list — capped
candidates are deferred, not discarded, and backfill in score order if the list would
otherwise come up short. A diversity pass that silently returns 7 results instead of
10 is a worse failure than a monotonous 10.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

RERANK_POOL = 50  # the ranker cuts 500 -> 50; this stage cuts 50 -> k
CATEGORY_CAP = 2  # at most this many of the final list share a primary category


@dataclass(frozen=True)
class Candidate:
    """One ranked candidate plus the serving-time attributes this stage filters on.

    `is_open` and `categories` are read from the online store's business record, not
    through the feature read path: they are not registered features and must never
    become any (D13). Keeping them off `FeatureQuery` is what stops that drift.
    """

    business_id: str
    score: float
    is_open: bool
    categories: tuple[str, ...] = ()

    @property
    def primary_category(self) -> str | None:
        """The category diversity is measured on, or None if the business has none.

        An uncategorised business cannot be said to duplicate another's category, so
        it is left uncapped rather than pooled into a shared empty-string bucket —
        which would cap them against each other for no reason.
        """
        return self.categories[0] if self.categories else None


def rerank(
    candidates: Sequence[Candidate],
    k: int,
    reviewed: Iterable[str] = (),
    *,
    filter_closed: bool = True,
    category_cap: int = CATEGORY_CAP,
) -> list[Candidate]:
    """Apply the hard filters, then diversify, preserving the ranker's order within.

    `filter_closed` exists so the offline harness can measure the same code path both
    ways (D29 / I12): the closed-business filter removes 8.64% of holdout targets
    outright, and that loss is an artifact of a 2022 snapshot judging 2019 behaviour,
    not a ranking regression. Reporting only the filtered number would hide which is
    which. It is never False in serving.
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    already_seen = set(reviewed)
    eligible = [
        candidate
        for candidate in candidates
        if candidate.business_id not in already_seen
        and (candidate.is_open or not filter_closed)
    ]

    chosen: list[Candidate] = []
    deferred: list[Candidate] = []
    per_category: dict[str, int] = {}
    for candidate in eligible:
        if len(chosen) == k:
            break
        category = candidate.primary_category
        if category is None:
            chosen.append(candidate)
            continue
        if per_category.get(category, 0) >= category_cap:
            deferred.append(candidate)
            continue
        per_category[category] = per_category.get(category, 0) + 1
        chosen.append(candidate)

    if len(chosen) < k:
        # Diversity yields to completeness. The loop above only breaks once `chosen` is
        # full, so reaching here means `eligible` was exhausted and every candidate not
        # chosen is in `deferred`, still in ranker order.
        chosen.extend(deferred[: k - len(chosen)])
    return chosen
