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

**This stage lowers the headline metric, and that is the finding rather than a
defect.** Measured on the frozen holdout (26,489 users): the already-reviewed filter
costs **42.0%** of recall@10, the closed filter **1.7%**, the diversity cap **1.8%**.

The repeats figure is large because **949 of the ranker's 2,579 top-10 hits (36.8%)
are businesses the user had already reviewed**, drawn from ~1.3% of the candidate
pool — a repeat converts at roughly 28x an average candidate, since a return visit is
the easiest thing in this dataset to predict. So the filter removes the *densest*
slice of the model's success, and more than a third of the pre-rerank number was
credit for predicting that people go back to their regular places.

An earlier version of this docstring justified the filter by arguing it frees 13% of
the final slots to forfeit only ~1.6% of holdout targets. That argument is wrong and
is kept here as a warning: it treats the freed slots as converting at the average
rate, when the slots a good model puts at the top are by construction the best ones.
Share-of-slots and share-of-hits are not interchangeable. Full reasoning, and the
decision to keep the filter anyway, in DECISIONS.md D29 and ISSUES.md I5.

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
