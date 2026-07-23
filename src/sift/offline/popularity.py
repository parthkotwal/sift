"""The dumb-path baseline: rank a metro's businesses by pre-split popularity.

Build step 1's retrieval. Popularity is counted from the review *event stream*,
including only reviews dated strictly before SPLIT_T — never from the dump-time
`business.review_count` snapshot, which counts through 2022 and would leak the
post-T holdout into the baseline it's meant to be measured against.

The pure functions (`count_pre_t_reviews`, `rank`) take iterables so tests feed
synthetic events; the IO wrapper (`build`) streams the raw JSON and writes a
ranked artifact you can open and eyeball. Deterministic + overwrite = idempotent:
re-running on the same dump produces byte-identical output.

Run: python -m sift.offline.popularity
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from sift.config import (
    BUSINESS_JSON,
    DERIVED_DIR,
    METRO_CITY,
    METRO_STATE,
    REVIEW_JSON,
    SPLIT_T,
)

POPULARITY_ARTIFACT = DERIVED_DIR / "phl_popularity.json"


class PopularityEntry(NamedTuple):
    business_id: str
    name: str
    pre_t_reviews: int


def parse_review_date(raw: str) -> date:
    """Yelp review dates are 'YYYY-MM-DD HH:MM:SS'; we split on the date only.

    Timestamp hygiene (AGENTS.md): dates are treated as naive local calendar
    dates, one convention, fixed here. Popularity is a day-grain count, so the
    time-of-day is irrelevant to the < SPLIT_T boundary.
    """
    return date.fromisoformat(raw[:10])


def count_pre_t_reviews(
    catalog: set[str],
    reviews: Iterable[tuple[str, date]],
    split_t: date,
) -> Counter[str]:
    """Count reviews per business, keeping only catalog members dated < split_t.

    The `review_date < split_t` test is the leak boundary: right-exclusive, so a
    review dated exactly split_t belongs to the eval side and must not be counted.
    """
    counts: Counter[str] = Counter()
    for business_id, review_date in reviews:
        if review_date < split_t and business_id in catalog:
            counts[business_id] += 1
    return counts


def rank(catalog: dict[str, str], counts: Counter[str]) -> list[PopularityEntry]:
    """Rank the whole catalog by pre-T review count.

    Every catalog member is included, even those with zero pre-T reviews —
    retrieval is evaluated against the full catalog, so the ranking must cover it.
    Order is (count desc, business_id asc): the id tie-break makes the output
    deterministic, hence idempotent.
    """
    entries = [
        PopularityEntry(business_id, name, counts.get(business_id, 0))
        for business_id, name in catalog.items()
    ]
    entries.sort(key=lambda e: (-e.pre_t_reviews, e.business_id))
    return entries


def load_metro_catalog() -> dict[str, str]:
    """business_id -> name for every business in the frozen metro."""
    catalog: dict[str, str] = {}
    with BUSINESS_JSON.open() as f:
        for line in f:
            b = json.loads(line)
            if (b.get("city") or "").strip() == METRO_CITY and (
                b.get("state") or ""
            ).strip() == METRO_STATE:
                catalog[b["business_id"]] = b.get("name") or ""
    return catalog


def _iter_review_events() -> Iterable[tuple[str, date]]:
    with REVIEW_JSON.open() as f:
        for line in f:
            r = json.loads(line)
            yield r["business_id"], parse_review_date(r["date"])


def build() -> Path:
    """Compute the metro popularity ranking and write it to the artifact path."""
    catalog = load_metro_catalog()
    counts = count_pre_t_reviews(set(catalog), _iter_review_events(), SPLIT_T)
    ranking = rank(catalog, counts)
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    POPULARITY_ARTIFACT.write_text(
        json.dumps([e._asdict() for e in ranking], ensure_ascii=False)
    )
    return POPULARITY_ARTIFACT


@lru_cache(maxsize=1)
def load_ranking() -> list[PopularityEntry]:
    """Read the ranked artifact the serving path returns. Cached: read once.

    Raises FileNotFoundError if the artifact hasn't been built yet — the caller
    (the serving layer) turns that into a clear 'run the offline job' error.
    """
    return [
        PopularityEntry(**entry)
        for entry in json.loads(POPULARITY_ARTIFACT.read_text())
    ]


def main() -> None:
    catalog_size = 0
    out = build()
    ranking = [PopularityEntry(**e) for e in json.loads(out.read_text())]
    catalog_size = len(ranking)
    nonzero = sum(1 for e in ranking if e.pre_t_reviews > 0)
    print(f"metro: {METRO_CITY}, {METRO_STATE}   split T (exclusive): {SPLIT_T}")
    print(f"catalog businesses: {catalog_size:,}   with >=1 pre-T review: {nonzero:,}")
    print(f"artifact: {out}")
    print("\ntop 10 by pre-T review count:")
    for e in ranking[:10]:
        print(f"  {e.pre_t_reviews:>6,}  {e.name}")


if __name__ == "__main__":
    main()
