# DATA.md — the Yelp Open Dataset

Mandatory reading before writing any ingestion or transform code. Facts below were checked against the official dataset page and terms (July 2026); items marked **VERIFY ON LOAD** must be confirmed against the actual download and recorded here.

## What it is

Yelp's public research dump: a compressed TAR (~4.35 GB, ~8.65 GB uncompressed) of **5 JSON-lines files** plus a PDF of the terms. Official figures: **150,346 businesses**, **6,990,280 reviews**, **11 metropolitan areas**. Tip/check-in/user record counts are not stated officially — **VERIFY ON LOAD**. (A separate photos ZIP exists; Sift doesn't use it.)

Download from [yelp.com/dataset](https://www.yelp.com/dataset) (redirects to business.yelp.com/data/resources/open-dataset): agree to the terms, download the TAR. A Kaggle mirror exists but may lag the official release — prefer the official download.

This is a **subset** of Yelp's corpus (selected metros, selected businesses), not all of Yelp. Consequences below.

## Files and schemas

All files are JSON lines: one object per line. Dates are strings, `YYYY-MM-DD` (check-in timestamps add `HH:MM:SS`). Overall date range: **VERIFY ON LOAD** — record actual min/max event dates here after first bronze load.

### review.json — the primary event stream

`review_id` (22-char id), `user_id`, `business_id`, `stars` (int 1–5), `useful`/`funny`/`cool` (vote counts), `text`, `date`.

- Explicit feedback (stars) + text. Maps to silver as `event_type = review`.
- The vote counts are as-of-dump-time, not as-of-review-time — they are **not** point-in-time safe and must not become features attached at the review's own timestamp.

### tip.json — short recommendations

`user_id`, `business_id`, `text`, `date`, `compliment_count`.

- No star rating. Implicit-ish signal ("cared enough to leave a tip"). Maps to `event_type = tip`.
- `compliment_count` has the same as-of-dump-time problem as review votes.

### checkin.json — the wrinkle

`business_id`, `date` — where `date` is a **single comma-separated string of timestamps** (`"2016-04-26 19:49:16, 2016-08-30 18:36:57, ..."`).

- **There is no `user_id`.** Check-ins are not attributed to users. They normalize into silver as user-less events (`user_id = null`, `event_type = checkin`), one row per timestamp — usable for business-side features (activity, velocity) only, never for user features. Do not invent a user.
- This one file requires an exploding transform at silver (split the string, one event per timestamp).

### user.json — user dimension

`user_id`, `name`, `review_count`, `yelping_since`, `friends`, `useful`/`funny`/`cool`, `fans`, `elite`, `average_stars`, various `compliment_*` counts.

- **`friends` is a comma-separated string of user_ids, not a JSON array** — and it's why this file is large (multi-GB). Heavy users have thousands of friends. Drop or defer the friends column at bronze→silver unless/until the social graph is actually used (it is out of scope pre-V4).
- `review_count`, `average_stars`, `fans` etc. are **as-of-dump-time snapshots** — not point-in-time safe. Usable as static dimension attributes with that caveat documented; never as time-varying features. Time-varying user features are computed from the event stream in gold instead.
- `yelping_since` is genuinely temporal and safe.

### business.json — business dimension

`business_id`, `name`, `address`, `city`, `state`, `postal_code`, `latitude`, `longitude`, `stars`, `review_count`, `is_open` (0/1), `attributes`, `categories`, `hours`.

- **`categories` is a comma-separated string** (`"Tours, Breweries, Pizza, Restaurants, ..."`) — needs splitting into an array at silver.
- **`attributes` nested values are Python-literal strings, not JSON**: `"BusinessParking": "{'garage': False, 'street': True, ...}"` — single quotes, `True`/`False`. Parsing requires `ast.literal_eval` (safely wrapped), not `json.loads`. Some flat values are the strings `"True"`/`"False"` rather than booleans. This is the dataset's best-known parsing trap.
- `stars` and `review_count` are as-of-dump-time snapshots (same caveat as user.json) — and `review_count` **does not match** the number of that business's reviews actually present in review.json (documented upstream; the dump is a subset). Never validate referential integrity against `review_count`; count actual rows.
- `is_open = 0` businesses (closed) are included. Keep them — they have history — but **`is_open` is a rerank-stage serving filter, never a model feature** (`DECISIONS.md` D13): it reflects dump-time state, so a business open in 2016 but closed by dump time carries `0` into 2016 training rows — leakage. It's the dataset's cleanest example of a signal that's legitimate online and unconstructible historically.

## Mapping to silver (summary)

| Source | Becomes |
|---|---|
| review.json | events: `(user_id, business_id, 'review', ts, {stars, text_len, …})` |
| tip.json | events: `(user_id, business_id, 'tip', ts, {…})` |
| checkin.json | events: `(null, business_id, 'checkin', ts, {})` — exploded, user-less |
| business.json | `dim_business` (categories split, attributes parsed, snapshot fields flagged) |
| user.json | `dim_user` (friends deferred, snapshot fields flagged) |

**Recurring trap, named once:** every dump-time snapshot field (`stars`, `review_count`, `fans`, vote counts…) is a value *from the future* relative to any historical event. Attaching one to a historical row is leakage by construction. Time-varying quantities are computed from events in gold, as of a date, or not used.

## License — read this, it's stricter than you'd guess

Per the Yelp Dataset Terms of Use (July 2023 version, reviewed July 2026 — reread the PDF that ships in the TAR):

- Grant is **academic use only** (education, research, not-for-profit), 12-month term from download.
- **Prohibited:** sharing the Data with third parties; redistributing it; public display of reviews/UGC; commercial use; and — notably — creating or disclosing "any summary of, or metrics related to, the Data" on websites or media not covered by the agreement, *except* disclosures necessary for academic purposes.
- The terms also ask that findings be submitted to Yelp for review before public presentation/publication of results involving the Data.

Practical rules for this repo:

1. `data/` is **gitignored forever**. No raw records, no derived Parquet, no sample rows with real user names or review text in committed code, docs, or tests. Test fixtures are synthetic.
2. The public repo contains **code and architecture** — that's ours. Be conservative about publishing dataset-derived *metrics* (counts, distributions, model scores on the data) in the public README; keep result-y numbers in local/private notes. Where a doc needs scale context, official public figures (150K businesses, ~7M reviews) are used since Yelp publishes those itself.
3. Anything resume/portfolio-facing describes the **system**, not the data's contents. This is aligned with the project's framing anyway: the pipeline is the deliverable.

## Local layout

```
data/                    # gitignored
  raw/                   # the untarred JSON files, untouched
  bronze/ silver/ gold/  # pipeline-managed Parquet (paths configurable)
```

## First-load checklist (do this once, record results here)

- [ ] Verify record counts per file; record them above.
- [ ] Record actual min/max event dates (reviews, tips, check-ins separately).
- [ ] Tabulate events per day and per metro → picks the initial metro for build step 1 and the frozen split date T.
- [ ] Confirm `checkin.json` shape and exploded row count.
- [ ] Note dump version/date downloaded and the 12-month license clock.
