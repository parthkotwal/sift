"""Frozen project constants.

These are decisions, not tunables. Changing METRO or SPLIT_T invalidates every
eval number Sift has ever produced (see DECISIONS.md D17) — treat an edit here as
a major decision, not a tweak.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

# Build step 1 metro (DECISIONS.md D17). City-level: the business `city` string,
# not the Census metro area — the unambiguous frozen unit. Widen only at scale.
METRO_CITY = "Philadelphia"
METRO_STATE = "PA"

# Frozen temporal split (D17). Train = events strictly before T; eval = on/after T.
# Right-exclusive everywhere: an event dated exactly T is eval, never train. This
# is the `< t` of the as-of join (CONCEPTS.md → As-of join) at the dataset scale.
SPLIT_T = date(2019, 1, 1)

# Paths. data/ is gitignored forever (Yelp license); nothing here is committed.
DATA_DIR = Path("data")
RAW_DIR = DATA_DIR / "raw"
DERIVED_DIR = DATA_DIR / "derived"  # small offline artifacts (rankings, indexes)

BUSINESS_JSON = RAW_DIR / "yelp_academic_dataset_business.json"
REVIEW_JSON = RAW_DIR / "yelp_academic_dataset_review.json"
TIP_JSON = RAW_DIR / "yelp_academic_dataset_tip.json"
CHECKIN_JSON = RAW_DIR / "yelp_academic_dataset_checkin.json"
USER_JSON = RAW_DIR / "yelp_academic_dataset_user.json"
