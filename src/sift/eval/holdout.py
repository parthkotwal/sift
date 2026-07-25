"""The frozen evaluation ground truth: who we evaluate, and on what.

Definitions fixed alongside SPLIT_T (DECISIONS.md D18). Changing any of them
invalidates every eval number Sift has produced:

  eval users  users with >=1 metro review strictly BEFORE T. Cold-start users
              (71% of post-T users here) are excluded: with no history, no model
              can personalize, so including them compresses the gap between a
              good model and the popularity floor. Cold-start is a separate
              problem, reported separately if we choose to.
  relevant    ANY post-T metro review by that user, regardless of star rating —
              retrieval's target is engagement ("did we surface where they went").
  repeats     INCLUDED: a business reviewed both before and after T still counts.
              Repeat visits are genuine recommendations. NOTE (open, build step 6):
              rerank plans an already-reviewed hard filter, which would make these
              ~1.6% of targets unreachable — that filter must be revisited there.

Built offline into an artifact; the harness reads it. Deterministic (sorted) so
re-running is byte-identical.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from sift.config import DERIVED_DIR, REVIEW_JSON, SPLIT_T
from sift.offline.popularity import load_metro_catalog, parse_review_date

HOLDOUT_ARTIFACT = DERIVED_DIR / "phl_holdout.json"


def build_ground_truth(
    catalog: set[str],
    reviews: Iterable[tuple[str, str, date]],
    split_t: date,
) -> dict[str, set[str]]:
    """user_id -> set of post-T business_ids, for users who have pre-T history.

    One pass: events before split_t mark a user as eligible; events on/after it
    become that user's targets. Right-exclusive, so a review dated exactly
    split_t is a target, never history.
    """
    users_with_history: set[str] = set()
    targets: dict[str, set[str]] = defaultdict(set)
    for user_id, business_id, event_date in reviews:
        if business_id not in catalog:
            continue
        if event_date < split_t:
            users_with_history.add(user_id)
        else:
            targets[user_id].add(business_id)
    return {
        user_id: items
        for user_id, items in targets.items()
        if user_id in users_with_history
    }


def _iter_review_events() -> Iterable[tuple[str, str, date]]:
    with REVIEW_JSON.open() as f:
        for line in f:
            r = json.loads(line)
            yield r["user_id"], r["business_id"], parse_review_date(r["date"])


def build() -> Path:
    catalog = set(load_metro_catalog())
    ground_truth = build_ground_truth(catalog, _iter_review_events(), SPLIT_T)
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    HOLDOUT_ARTIFACT.write_text(
        json.dumps(
            {user: sorted(items) for user, items in sorted(ground_truth.items())}
        )
    )
    return HOLDOUT_ARTIFACT


def load_ground_truth() -> dict[str, set[str]]:
    raw: dict[str, list[str]] = json.loads(HOLDOUT_ARTIFACT.read_text())
    return {user: set(items) for user, items in raw.items()}


def main() -> None:
    out = build()
    truth = load_ground_truth()
    pairs = sum(len(v) for v in truth.values())
    print(f"eval users: {len(truth):,}   ground-truth pairs: {pairs:,}")
    print(f"artifact: {out}")


if __name__ == "__main__":
    main()
