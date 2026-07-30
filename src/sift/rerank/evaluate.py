"""Measure build step 6 on the frozen holdout, with and without the closed filter.

Both numbers are reported on purpose (I12). The `is_open` filter removes 8.64% of the
holdout's target pairs outright — 7,732 of 89,519 — because the dump records whether a
business is open in 2022 while the ground truth is 2019 behaviour. A business the user
reviewed in 2019 and that has since closed is a *correct* recommendation to suppress
today and an unreachable target forever. Reporting only the filtered figure would let
a dataset-vintage artifact read as a ranking regression; reporting only the unfiltered
one would overstate what the product actually returns. So: both, side by side.

The reviewed set here is strictly **pre-T**, unlike the online one. Redis publishes
reviewed history as-of the generation's `as_of` (2022), which is right for serving and
catastrophic for evaluation: at that cutoff it contains the post-T reviews that *are*
the targets, so filtering through it would remove the ground truth and report recall
near zero. Serving and eval legitimately disagree about when "now" is — the same
reason `is_open` cannot be a feature at all (D13).

Run: ``python -m sift.rerank.evaluate``
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import duckdb
import lightgbm as lgb

from sift.config import SPLIT_T, sql_path
from sift.eval.holdout import load_ground_truth
from sift.eval.run import evaluate
from sift.offline.dim_business import DIM_BUSINESS
from sift.offline.ingest import EVENTS_DIR, REVIEW_EVENT, events_glob
from sift.ranking.rank import SERVING_POOL, reranked_candidate_lists
from sift.ranking.train import ALS_RANKER_MODEL
from sift.rerank.rerank import CATEGORY_CAP, RERANK_POOL, Candidate, rerank
from sift.retrieval.evaluate import ALSRetriever

# Rerank cuts to 10 in serving; the harness reranks to RERANK_POOL so recall@50 stays
# meaningful. The head is identical either way — the walk is greedy and prefix-stable,
# so the first 10 chosen do not depend on how many more were asked for.
EVAL_KS: tuple[int, ...] = (10, 50)


def load_catalog_attributes(
    dim_file: Path = DIM_BUSINESS,
) -> tuple[dict[str, bool], dict[str, tuple[str, ...]]]:
    """`is_open` and categories from the dimension — the offline twin of what Redis
    publishes in its business record, so both stages filter on the same values."""
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT business_id, is_open, categories FROM read_parquet({sql_path(dim_file)})"
        ).fetchall()
    finally:
        con.close()
    is_open = {str(business): bool(open_now) for business, open_now, _ in rows}
    categories = {str(business): tuple(cats or ()) for business, _, cats in rows}
    return is_open, categories


def load_pre_t_reviewed(
    events_dir: Path = EVENTS_DIR, cutoff: date = SPLIT_T
) -> dict[str, set[str]]:
    """Businesses each user reviewed strictly before T — right-exclusive, like every
    other training-time read. Deliberately not the online set; see the module docstring.

    Filtered to review events for the same reason the publisher is: the canonical event
    table is designed to carry tips and check-ins later (D2), and an unfiltered query
    would start calling a tipped business "reviewed" the day one lands. The offline and
    online definitions have to widen together or the eval stops describing serving.
    """
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT user_id, business_id FROM "
            f"read_parquet({sql_path(Path(events_glob(events_dir)))}) "
            f"WHERE event_type = '{REVIEW_EVENT}' AND ts < TIMESTAMP '{cutoff}'"
        ).fetchall()
    finally:
        con.close()
    reviewed: dict[str, set[str]] = {}
    for user_id, business_id in rows:
        reviewed.setdefault(str(user_id), set()).add(str(business_id))
    return reviewed


def reranked(
    ranked_lists: Mapping[str, Sequence[str]],
    is_open: Mapping[str, bool],
    categories: Mapping[str, tuple[str, ...]],
    reviewed: Mapping[str, set[str]],
    *,
    filter_closed: bool = True,
    category_cap: int = CATEGORY_CAP,
) -> dict[str, list[str]]:
    """Apply the serving stage to precomputed ranker output, user by user.

    Each of the three mechanisms can be disabled independently, which is what lets
    `main` attribute the recall change to one filter rather than reporting a single
    combined number that hides which part cost what: pass `reviewed={}` to keep
    repeats, `filter_closed=False` to keep closed businesses, and a `category_cap`
    above the pool size to disable diversity.
    """
    out: dict[str, list[str]] = {}
    for user_id, ordered in ranked_lists.items():
        pool = [
            Candidate(
                business_id=business_id,
                score=float(len(ordered) - position),
                is_open=is_open.get(business_id, False),
                categories=categories.get(business_id, ()),
            )
            for position, business_id in enumerate(ordered[:RERANK_POOL])
        ]
        out[user_id] = [
            candidate.business_id
            for candidate in rerank(
                pool,
                RERANK_POOL,
                reviewed.get(user_id, set()),
                filter_closed=filter_closed,
                category_cap=category_cap,
            )
        ]
    return out


def repeat_hit_share(
    ranked_lists: Mapping[str, Sequence[str]],
    ground_truth: Mapping[str, set[str]],
    reviewed: Mapping[str, set[str]],
    k: int = 10,
) -> tuple[int, int]:
    """(hits in the ranker's top-k, how many were businesses the user already reviewed).

    The number that explains everything else in this report. Already-reviewed
    businesses are ~1.3% of the candidate pool but carry roughly a third of the hits:
    a return visit is the easiest thing in this dataset to predict, so the repeats
    filter does not remove an average slice of the model's success, it removes the
    densest one.
    """
    hits = repeats = 0
    for user_id, ordered in ranked_lists.items():
        seen = reviewed.get(user_id, set())
        for business_id in ordered[:k]:
            if business_id in ground_truth[user_id]:
                hits += 1
                repeats += business_id in seen
    return hits, repeats


def main() -> None:
    ground_truth = load_ground_truth()
    users = list(ground_truth)
    retriever = ALSRetriever.load()
    candidate_lists = {user_id: retriever.recommend(user_id, SERVING_POOL) for user_id in users}
    ranked_lists = reranked_candidate_lists(
        lgb.Booster(model_file=str(ALS_RANKER_MODEL)),
        users,
        candidate_lists,
        str(SPLIT_T),
        progress=True,
    )

    is_open, categories = load_catalog_attributes()
    reviewed = load_pre_t_reviewed()
    closed_targets = sum(
        1
        for user_id, targets in ground_truth.items()
        for business_id in targets
        if not is_open.get(business_id, False)
    )
    pairs = sum(len(targets) for targets in ground_truth.values())

    # Each mechanism alone, then all three. A single combined number would say the
    # stage costs 36% of recall@10 without saying which part of it did, and the answer
    # is not the one the slot arithmetic predicted.
    no_repeats: dict[str, set[str]] = {}
    all_open = dict.fromkeys(is_open, True)
    no_cap = RERANK_POOL + 1
    variants = (
        # Each row turns on exactly one mechanism. `no_cap` disables diversity, an
        # all-open catalog disables the closed filter, and an empty reviewed map
        # disables the repeats filter — so the first row must carry CATEGORY_CAP, not
        # `no_cap`. Getting that wrong once produced a row identical to the baseline to
        # four decimals, which is the shape a disabled variant always has: a diff of
        # exactly +0.0% is a bug report, not a finding.
        ("diversity cap only", no_repeats, all_open, categories, CATEGORY_CAP, True),
        ("closed filter only", no_repeats, is_open, categories, no_cap, True),
        ("repeats filter only", reviewed, all_open, categories, no_cap, True),
        ("all three <- what serving does", reviewed, is_open, categories, CATEGORY_CAP, True),
    )

    baseline = evaluate(
        lambda user_id, k: ranked_lists[user_id][:k],
        ground_truth,
        name="ALS -> ranker (no rerank)",
        ks=EVAL_KS,
        measure_latency=False,
    )
    rows = [("ALS -> ranker (no rerank)", baseline)]
    for label, rev, opened, cats, cap, closed_on in variants:
        lists = reranked(
            ranked_lists, opened, cats, rev, filter_closed=closed_on, category_cap=cap
        )

        def recommend(user_id: str, k: int, lists: dict[str, list[str]] = lists) -> Sequence[str]:
            return lists[user_id][:k]

        rows.append(
            (
                label,
                evaluate(
                    recommend, ground_truth, name=label, ks=EVAL_KS, measure_latency=False
                ),
            )
        )

    print(f"\nrerank on the frozen holdout — {baseline.n_users:,} eval users\n")
    print(f"  {'variant':<34} {'recall@10':>10} {'recall@50':>10} {'NDCG@10':>9} {'vs ranker':>11}")
    base10 = baseline.recall[10]
    for label, report in rows:
        delta = "" if report is baseline else f"{100 * (report.recall[10] / base10 - 1):+.1f}%"
        print(
            f"  {label:<34} {report.recall[10]:>10.4f} {report.recall[50]:>10.4f} "
            f"{report.ndcg_at_10:>9.4f} {delta:>11}"
        )

    hits, repeats = repeat_hit_share(ranked_lists, ground_truth, reviewed)
    print(
        f"\n  Why the repeats filter dominates: {repeats:,} of the ranker's {hits:,} top-10 "
        f"hits ({100 * repeats / hits:.1f}%) are businesses the user had already reviewed,"
        "\n  from ~1.3% of the candidate pool. A return visit is the easiest thing here to"
        "\n  predict, so the filter removes the densest slice of the model's success, not an"
        "\n  average one. The pre-rerank row is therefore a weaker claim about discovery"
        "\n  than it looks (D29 / I5)."
    )
    # Read the closed filter's cost off this run rather than quoting it. A number typed
    # into an explanation is a number that goes stale the next time the data moves, and
    # this sentence already shipped once claiming "well under 1%" against a measured
    # 1.7% — the exact failure the surrounding decision is about.
    closed_only = next(report for label, report in rows if label == "closed filter only")
    closed_cost = 100 * (1 - closed_only.recall[10] / base10)
    print(
        f"\n  Closed businesses are {closed_targets:,}/{pairs:,} "
        f"({100 * closed_targets / pairs:.2f}%) of holdout targets, unreachable once filtered,"
        f"\n  yet cost only {closed_cost:.1f}% of recall@10 — the model was rarely finding them"
        "\n  anyway (I12). Both numbers are reported so a dataset-vintage artifact cannot read"
        "\n  as a ranking regression."
    )


if __name__ == "__main__":
    main()
