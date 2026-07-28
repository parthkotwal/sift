# ISSUES.md — known issues, traps, and deferred fixes

A running log of defects found, traps discovered, and fixes deliberately deferred.
Distinct from `DECISIONS.md`: that records *choices*, this records *problems*. An
entry stays here after it's fixed if the failure mode is worth recognising again —
several below are classes of bug, not one-off slips.

Yelp ToS: describe traps qualitatively here. Counts and distributions live in the
gitignored `data/PROFILE.md` and `data/RESULTS.md`.

Format: `### <id> — <title>   [open | fixed | deferred | accepted]`

---

## Fixed in the current build

### I23 — The two-tower's logQ correction is applied after the temperature   [accepted]

Raised in review of 26cfe46 as a defect: because `logits = sim / TEMPERATURE`
(÷0.07) happens *before* `- log(mixture_q)`, the debias term looked ~14x too weak
relative to the similarity term, which would make D26's negative verdict suspect.

**Reviewed, and the code is right as written — no change.** The sampled-softmax
estimator approximates the full-catalog denominator by importance weighting,
`sum_{j in S} exp(s_j)/q_j = sum_{j in S} exp(s_j - log q_j)`, where `s` is *the
model's score function*. The score function here is cosine/T — the temperature is
part of the model, not a post-hoc rescale — so `log q` is subtracted from the
scaled logit, in nats (Yi et al. 2019). Subtracting before the division computes
`(cos - log q)/T = s - log(q)/T`, which is the estimator for a proposal
distribution `q^(1/T)`: a ~-9 nat correction becomes ~-130 against similarities
bounded by ±1/T = ±14.3, so the objective would be dominated by the sampler rather
than by the model. That is a real bug, and it is the one the "fix" would have
introduced.

**What was actually missing was the test.** Coverage checked only shape and
masking, so either ordering passed. The correction is now
`two_tower.corrected_logits` / `mixture_sampling_probability`, with tests pinning
the term's *magnitude* (`-log q`, and a 100x-rarer candidate penalised by log(100)
not log(100)/T) and the uniform floor that keeps `log q` finite. Kept here because
the finding is plausible enough to be re-raised: **an order-of-operations question
inside an estimator needs a magnitude assertion, not a shape assertion.**

### I22 — Two-tower export mapped user vectors by scan order   [fixed]

`load_export_user_values` built its `query_id` with `row_number() OVER ()` over a
registered in-memory array and trusted that to line up index-for-index with
`interactions.user_ids`. Nothing guarantees that: the window has no `ORDER BY`, and
a parallelised scan may emit rows in any order — after which every user embedding
is written under the wrong `user_id`, silently, with no shape or dtype change to
notice. Same class as I18 (order assumptions that hold until DuckDB parallelises),
but this path was neither guarded nor tested.

**Fixed** by making the index an explicit array joined with `POSITIONAL JOIN` — the
pattern already used in `als.write_factor_parquet` and `rank.reranked_candidate_lists`
— whose row-position semantics are defined rather than incidental. The function now
takes `store_dir`/`dim_file` (I1's rule: no hidden dependency on machine state) and
`test_export_user_values_follow_the_id_mapping_not_the_scan_order` reads one store
under two different ID orders. **Honest limit:** that test cannot force a parallel
reorder at fixture size, so it would not have caught the old code — the guarantee
now comes from the join's semantics, and the test stops a rewrite from keying rows
off store order again.

### I21 — In-batch item features can cross temporal boundaries   [fixed]

A naive two-tower batch would encode each positive item with aggregates as-of that
item's own event. The resulting item vector is then a negative for every other row.
For an earlier row, that vector can contain item history from its future even though
every individual feature lookup was right-exclusive. The batch matrix creates a
new temporal edge the per-row leak test does not express.

**Fixed:** time-varying features exist only on the user/query tower. The item tower
uses learned ID parameters plus D21's quasi-static identity attributes, so one item
encoding is valid across all timestamps in the batch. Known user positives are also
masked from sampled-softmax negatives. The lesson is broader: point-in-time-correct
rows can still become time-incorrect when a training objective combines rows.

### I20 — Swapping retrieval silently invalidates the ranker's training conditional   [fixed]

The accepted ranker was trained only on popularity's candidate pool. Passing a
new user's ALS top-500 through that model would violate D20 even though the feature
schema is unchanged: candidate source is part of the model's input distribution.

**Fixed:** the training-set builder now accepts a personalized candidate provider
and tests that every positive and negative belongs to that user's pool. ALS got a
separate training artifact and model, leaving the popularity-conditioned baseline
runnable. The corrected model still lowered ALS NDCG@10, so it did not land; the
API serves ALS order directly. A future candidate-source swap must retrain and
remeasure the downstream ranker, never just change one line in serving.

### I3 — Reported ranker latency measures nothing   [fixed]

`eval/run.py` times the `recommend` callable, but `ranker_recommender` precomputes
every user's list *before* `evaluate` runs, so the timed callable is a dict lookup.
The ~0.002ms in `RESULTS.md` for the ranker path excludes feature computation and
model inference entirely — i.e. all of it. Latency numbers for anything other than
the popularity baseline are currently meaningless.

**Fix:** time the stages where the work happens, not the closure that serves the
cached answer. Matters because "measure latency per stage" is a stated project rule
and the <100ms p99 budget is a headline claim.

**Fixed (2026-07-26):** offline ranker evaluation now explicitly suppresses latency
for its precomputed-list closure, so it cannot publish a dict lookup as serving
latency. The Redis-backed `OnlineRanker` measures retrieval, feature lookup, model
inference, overhead, and total around the real per-request work; the API exposes the
same breakdown and `python -m sift.ranking.online` reports p50/p95/p99 over sampled
users. Metric evaluation and serving latency are now deliberately separate runs.

### I4 — `libomp` is an uncaptured system dependency   [fixed]

LightGBM needs `brew install libomp` on macOS. Not expressible in `pyproject`/
`uv.lock`, so a clean machine fails at import with no guidance. Note it in the
README's setup section or a bootstrap script before anyone else clones this.

**Fixed (2026-07-26):** the README's local setup now states the macOS `libomp`
requirement next to `uv sync`.

## Open

### I25 — The online store cannot serve `ui_als_score`   [open]

The Redis publisher materialises the user/item/user_category/business groups; the
ALS slice groups (D27) are not among them, so `online_features()` omits
`ui_als_score` and serving still runs the eight-feature model. The new ranker beats
ALS offline but **cannot be deployed** until the publisher emits current ALS state —
shipping a model that reads a feature serving cannot supply is training/serving skew
by construction, the exact hazard the store exists to prevent.

Deliberately gated rather than built ahead: "models pay rent", so the online work
waited on the offline verdict. The verdict is in, so this is now the blocker.
`online_features()` derives servability from `ONLINE_STATE_GROUPS`, so adding the
groups to the publisher closes this automatically rather than needing a list edited
in two places.

### I26 — A 2-D ndarray registers into DuckDB transposed   [fixed]

`als_slices._slice_rows` registered an (N, 64) factor array and selected
`column0..column63`. DuckDB scans a 2-D ndarray as **one column per first-axis
entry**, so the array arrived as 64 rows of N columns: the first 64 entities got
correct vectors and the remaining 691,178 were silently NULL-filled. Nothing raised
— the parquet wrote, the row counts were right, the schema was right, and the
feature simply came back NULL for 99.9% of rows.

`als.write_factor_parquet` already had this right and even documents it
(`np.ascontiguousarray(factors.T)`); the new code reimplemented the registration
without the transpose. **Fixed** with the transpose plus the shape assertion that
module already carried, and `build_slices` now refuses to write a group whose
vectors contain NULL elements — a structural guard, since the symptom is invisible
downstream.

**Rule:** when reimplementing a data-marshalling step that exists elsewhere, copy
the whole idiom including the parts that look decorative. The transpose looked like
a formatting detail and was load-bearing.

### I27 — `list_contains(value, NULL)` cannot detect NULL elements   [fixed]

While diagnosing I26 I checked for NULL vector elements with
`list_contains(value, NULL)` and got 0 for every row, which is how the corrupted
state initially looked clean. `list_contains` returns NULL rather than true when
searching for NULL — ordinary three-valued logic — so the check can never fire.
`sum(CASE WHEN ... THEN 1 ELSE 0 END)` over that then counts zero.

Use `len(list_filter(value, x -> x IS NULL)) > 0`, which is what the guard in
`build_slices` uses now. **Third instance of the I8 class in this project** (vacuous
leak test, undersized idempotency fixture, and now a NULL-blind diagnostic), and the
first where the vacuous check was a *diagnostic* rather than a test — it sent the
investigation to the wrong layer for two rounds.

### I28 — DuckDB `list_dot_product` raises on absent LEFT-join payloads   [fixed]

`list_dot_product(a, b)` errors with "left argument can not contain NULL values"
rather than returning NULL when an operand is missing. Neither a `CASE WHEN a IS
NULL` guard nor a `WHERE a IS NOT NULL` filter reliably prevents it: the function is
still evaluated across the vector.

**Fixed** by computing the score in a CTE behind *inner* ASOF joins, so the function
only ever meets rows where both vectors exist, and LEFT JOINing the result back by
`query_id` — the same shape `ui_category_affinity` already uses for `uc`. The
definition's expression is therefore `als.score`, not a dot product written inline.

### I24 — No regression test pins the headline retrieval metrics   [deferred]

ALS's 0.2519 recall@500 and the two-tower's 0.2399 (D25/D26) exist only as prose in
`DECISIONS.md`. Nothing fails if a refactor moves them: the suite covers component
properties (determinism, unit norms, masking, zero vectors for cold items) and the
metric *functions*, but never the end-to-end number that decides whether a model
lands. A silent quality regression would be caught by a human re-running eval, or
not at all.

**Deferred, with the reason stated rather than assumed:** the numbers are computed
on the real Yelp dump, which is gitignored and must stay that way, so a test that
asserts them either can't run in CI or reintroduces I1's hidden dependency on
machine state. A synthetic fixture large enough to make ALS beat popularity by a
stable margin is a fixture whose result is a property of the fixture, not of the
model — the I8 failure mode with extra steps.

**The shape a real fix takes:** treat the eval run as an artifact, not a test —
write `data/RESULTS.md` numbers to a versioned JSON alongside the model manifest,
and have the eval entrypoint diff against the previous run and refuse to overwrite
a regression without an explicit flag. That belongs to the build step that touches
eval next, not to a review fix.

### I5 — Repeats vs. the already-reviewed rerank filter   [deferred to build step 6]

D18 choice 3 counts a business reviewed both before and after T as a valid target;
ARCHITECTURE's rerank stage plans an already-reviewed hard filter that would make
those structurally unreachable, capping final-stage metrics. Recorded in D18 as
open. Resolve at step 6 — drop the filter, soften to a demotion, or keep it and
document the ceiling. Do not resolve silently.

### I6 — Tie-breaking in the ranker is arbitrary   [deferred, deliberately]

With a shallow model the ranker produces far fewer distinct scores than candidates
(measured: 18 distinct scores over 500). `np.argsort` breaks the ties by whatever
order it lands on, so the top-k can be an arbitrary slice of a large tie group
rather than the incumbent's ordering. A stable fallback to retrieval's own order is
the correct funnel behaviour.

**Deferred on purpose:** adding it now would drag the ranker's numbers toward
popularity's and disguise the finding that the ranker has no personalization
signal. Revisit once the ranker has real score resolution.

**The two rank paths deliberately differ (2026-07-27, from review of 26cfe46).**
`reranked_lists` (popularity pool) keeps the unstable `np.argsort` this entry
defers; `reranked_candidate_lists` (personalized pool) uses `kind="stable"`, so
ties fall back to the candidate order the provider supplied — for ALS that *is*
retrieval's own ranking, which is the correct funnel behaviour described above.
The divergence was undocumented when it landed; it is intentional, and the reason
is that the deferral's rationale does not transfer. Popularity's pool is a fixed
global list, so stable tie-breaking there means "fall back to global popularity" —
exactly the incumbent the ranker must beat, which is what would hide the ranker's
lack of resolution. ALS's pool is per-user, so stable tie-breaking is a personalized
fallback and hides nothing. Both call sites now carry that reasoning inline.
Unifying them requires remeasuring both baselines, not editing one line.

---

## Fixed — kept because the failure mode recurs

### I19 — Online decoding assumed a nullable aggregate was present   [fixed]

The first real Redis-vs-Parquet skew run failed before it could compare values:
the lookup decoder indexed `cum_price` as required, but Redis correctly omitted the
field when a user's entire history had unknown price tiers. The synthetic online
fixture gave every user at least one priced business, so it passed for the wrong
reason — another I1/I8-shaped fixture gap where the absence case was not represented.

**Fixed** by decoding nullable cumulative sums as `None`, which becomes SQL `NULL`
before the shared definition is evaluated, and by adding a user whose only business
has no price. The important positive result is that the real skew check caught this
at the store boundary before the API path was treated as valid.

### I18 — The training artifact was not byte-reproducible (float-sum association)   [fixed]

Two consecutive runs of `training_set.py` on identical input produce different
bytes. Content is *almost* identical: 130 rows of 1.49M differ, all of them only in
`ui_distance_km`, max absolute difference **2.09e-12 km** — two picometres.

**Cause, and it is exact:** `ui_distance_km` is the only feature derived from a
**float** sum (`sum(latitude)`/`sum(longitude)` for the user's activity centroid).
Floating-point addition is not associative, and DuckDB parallelises the aggregation,
so thread scheduling changes the summation order and the last mantissa bits move.
Every other feature sums integers — review counts, star ratings, price tiers — and
is bit-stable. Verified: zero rows differ in any other float column.

**Impact:** numerically nil. Retraining on a rebuilt artifact reproduced 579
iterations, identical feature gains, and identical eval metrics to four decimal
places. But `AGENTS.md` requires "re-running a job produces identical output", and
this quietly does not.

**Compounding test weakness:** `test_assembly_is_idempotent` compares only *row
counts* between two builds, so it cannot catch this — or drift orders of magnitude
larger. Compare content (or a fingerprint), not cardinality. Same family as I8: an
assertion too weak to fail is an assertion that reports safety it hasn't checked.

**Fixed** by rounding the centroid to 6 decimal degrees (~0.11 m) in `pit.py` before
the haversine. Two consecutive rebuilds of the real artifact are now byte-identical.
Retraining on the rounded artifact moved nothing that matters: recall@10 0.0224 →
0.0223, NDCG@10 0.0173 → 0.0175, and the ranker still lands.

**Honest limit of the fix:** rounding makes this *almost surely* deterministic, not
provably so — a value sitting within the noise of a rounding boundary could still
flip. 6dp was chosen so that argument is safe by a wide margin: the noise is ~1e-15
degrees against a 1e-6 quantum, nine orders of magnitude of headroom. The *provable*
alternative is to sum fixed-point integers (scaled lat/long as BIGINT) because
integer addition is associative regardless of order; not taken, because the rounding
is one line and reads plainly. Revisit only if a future feature sums floats where
the magnitudes are less forgiving.

**Test strengthened alongside:** `test_assembly_is_idempotent` now compares bytes
rather than row counts, so it would have caught this — and it also pins row order,
which D22 made load-bearing.

**Second manifestation, in the store's own artifacts (2026-07-26, open).** The same
cause reappears one layer down, and materialising the state groups isolated it
perfectly: across two runs on the real dump, `item_state.parquet` and
`user_category_state.parquet` are **byte-identical** (their state is integer counts
and star sums), while `user_state.parquet` is **not** — it is the only group carrying
`cum_lat` / `cum_lng`. Derived feature values are unaffected, because the centroid is
rounded to 6dp in the definition's expression before it reaches a feature.

**Caveat on the test that "covers" this:** `test_materialization_is_idempotent`
passes, but only because the synthetic fixture sums two or three values per user —
too few for DuckDB to parallelise, so the reordering never occurs. It is green for
the wrong reason. This is the I8 class again, found in a test I had just written: a
fixture too small to exhibit the failure it is meant to catch.

**Resolved (2026-07-26) by option (b): fixed-point.** `cum_lat_e7` / `cum_lng_e7` are
now BIGINTs scaled by `state.GEO_SCALE` (1e7, ~1.1 cm). Integer addition is
associative, so the sum is bit-exact whatever order the aggregation runs in — all
three state groups are byte-identical across runs on the real dump, and the property
is now *provable* from the encoding rather than observed and hoped for.

**The rounding is gone, not moved.** With integer state the centroid is one exact
division on exact inputs, so `round(..., 6)` is no longer needed anywhere — the
earlier fix masked the drift, this one removes it. Every column in every state group
is now an integer, which also dropped the user group from 34.0 MB to 29.4 MB (BIGINT
rather than the HUGEINT DuckDB widens `sum()` to).

**Lesson worth keeping:** the two defensible fixes were not equally good. Rounding
was one line and probabilistically safe; changing the *encoding* so the failure
cannot occur is the same size and categorically safe. Prefer removing a failure mode
to sizing a tolerance against it.

### I1 — Synthetic training-set tests silently read the real `dim_business`   [fixed]

`build_training_set` defaults `dim_file=DIM_BUSINESS`, so for a window the synthetic
tests built events with ids `b0..b7` and joined them against the **real**
Philadelphia dimension. The ids didn't match, every `ui_*` feature LEFT JOINed to
NULL, and the assertions passed anyway — green for the wrong reason, and only green
at all because a gitignored artifact happened to exist locally.

**Fixed** by giving the fixture its own dimension, built through `build_dim_business`
from a synthetic `business.json` — which also exercises the real ingest seam rather
than hand-rolling a fake table. **Rule this instantiates:** a test that relies on a
default path has a hidden dependency on machine state; pass the path explicitly.

### I2 — `rank.py` assumed user/item feature separability   [fixed]

The serving path scored 26k users + 500 candidates and broadcast the outer product,
which is only valid while no feature depends on both sides. The three `user×item`
features break that by construction.

**Fixed** by running the full cross product through the *same* `feature_query` SQL in
user batches (2000 x 500 = 1M feature rows each). The tempting alternative —
recomputing distance/affinity/price in numpy on the serving side — would have been a
second implementation of one definition, i.e. exactly the train/serve skew the
feature store exists to prevent (D9/D14). **Rule:** batching may slice the input,
never the definition.

### I7 — Ranker training set sampled positives and negatives from different populations   [fixed → D20]

Negatives were drawn 100% from the popularity top-500; positives were any pre-T
review, a minority of which were in that pool. The model separated the classes by
pool membership — learning popularity *inverted* — and its confident range sat
entirely below the lowest-popularity candidate it would ever be asked to score.
This is what put the step-3 ranker below the baseline it reorders.

**Class of bug:** when positives and negatives come from different distributions, a
model can score well in-group by detecting the sampling scheme rather than the
signal. **Tell:** a feature whose learned response is confident in a range the
serving inputs never occupy. Full diagnosis in D20 and `data/RESULTS.md`.

### I8 — A leak test can pass vacuously   [fixed]

The first version of `test_leaky_category_affinity_is_caught` passed while testing
nothing: the deliberately-leaking feature produced an identical value before and
after mutation, because the "future" event was a Coffee venue and the queried
business was Restaurants/Pizza — no shared category, so the leak had nothing to
show. A future-invariance test is only as strong as the future it mutates.

**Class of bug, and it aims straight at the spine:** an invariance assertion whose
mutation cannot move the value under test passes for free, and `AGENTS.md` says the
leak test is never weakened to make a build pass — a vacuous one is weaker than a
missing one, because it reports safety. **Rule:** every invariance test needs a
paired assertion that the mutation *would* have moved the value (see
`test_future_invariance_covers_the_user_x_item_features`, which asserts the
pre-mutation values are non-NULL so the comparison isn't NULL == NULL).

### I9 — DuckDB: `unnest` in a grouped projection   [fixed]

`SELECT unnest(categories) AS c, count(*) ... GROUP BY c` raises
`Binder Error: UNNEST not supported here`. Wrap the unnest in a subquery and
aggregate outside it.

### I13 — Training-set row order encoded the label   [fixed → D22]

The assembly wrote the artifact `ORDER BY group_id, label DESC, business_id`, so
the positive was the physically first row of every group. LightGBM resolves *tied*
scores in file order, and an under-trained model ties nearly everything — so the
positive sat at rank 1 for free, inflating validation NDCG@10 for the *least*-trained
model and making early stopping select a single tree. Every step-3 "does not land"
verdict was measured on that stump. Fixed to `ORDER BY group_id, business_id`
(label-independent, still deterministic → idempotent). Full analysis, evidence, and
the corrected verdicts are in `DECISIONS.md` D22.

**Class of bug — this one aims at the spine:** the label reaching the model's
*selection* signal through a channel no one was watching — row order in a Parquet
file, not a feature value. It is the same family as leakage, and the future-invariance
test cannot catch it because that test mutates events, not file layout. Guarded now by
`test_row_order_within_a_group_does_not_encode_the_label`. **Rule:** nothing about the
physical order of training rows may depend on the label.

### I14 — DuckDB `executemany` is O(rows) for bulk insert   [fixed]

Inserting ~3M sampled-negative rows via `con.executemany("INSERT INTO neg VALUES
(?,?)", rows)` hung for 7+ minutes — per-row DML against a columnar engine. Streaming
the same rows to a temp CSV and `read_csv`-ing them (`training_set.py`) does it in
seconds.

**Class of bug:** never push millions of rows through per-row DML in an analytical DB;
use a bulk-load path (CSV / Parquet / Arrow). `executemany` exists but is not the fast
path, and nothing warns you — it just crawls.

### I15 — DuckDB `COPY (...) TO ?` mis-binds positional parameters   [fixed]

`COPY (SELECT ... read_json(?) ...) TO ?` bound with `[input_path, out_dir]` handed the
**out_dir** to `read_json`, which then failed with "No files found" on a directory that
had just been deleted. Positional `?` do not bind in source order across a `COPY ... TO`
target.

**Fixed** in `ingest.py` by interpolating the trusted internal paths directly (`sql_path`)
and keeping only value literals (city/state) as bound parameters. Same family as I9:
DuckDB's binder has sharp edges around table functions and COPY. **Rule:** bind *values*,
interpolate *trusted identifiers/paths*, and check what the plan actually received.

### I16 — DuckDB nullable INT → numpy masked int64 can't hold NaN   [fixed]

`fetchnumpy()` returns a **masked int64** array for a nullable `BIGINT` feature
(`u_days_since_last`); `.filled(np.nan)` on it raises "cannot convert float NaN to
integer". The ranker needs NULL → NaN so LightGBM treats it as its native missing.

**Fixed** in `train.py` by casting to float64 *before* filling
(`np.ma.asarray(a, dtype=float64).filled(nan)`). **Rule:** float-cast a nullable-int
column before you can represent its missings as NaN.

### I17 — Renaming the project directory broke the uv `.venv`   [fixed]

After renaming `~/Projects/albumen` → `sift` on disk (D16), `uv run pytest` failed with
`ModuleNotFoundError: sift` while `uv run python -c "import sift"` succeeded. Cause: every
`.venv/bin/*` console-script shebang and the `activate` scripts hardcode the venv's
absolute path (`/Users/.../albumen/.venv/bin/python`, now a dead path). `uv run python`
execs the interpreter directly and works; a console script follows its stale shebang into
a nonexistent interpreter and a different site config, so the editable `sift` isn't found.

**Fixed** by rebuilding the environment (`rm -rf .venv && uv sync`). **Class of bug:**
virtualenvs bake absolute paths — moving or renaming the project dir invalidates them.
Recreate the venv, don't try to edit it. The git remote had the same stale-`albumen`
problem (pointed at the old GitHub URL) and was repointed at the same time.

---

## Dataset traps found beyond what DATA.md names

### I10 — `RestaurantsPriceRange2` arrives as the *string* `'None'`   [handled]

Price is absent three ways, not two: the key missing, JSON null, and the literal
string `'None'` (a small number of metro rows). A plain `CAST` raises on the third.
`dim_business.py` uses `TRY_CAST` plus a 1–4 range check, so all three collapse to
NULL. Tested in `test_dim_business.py::test_price_tier_traps`.

### I11 — Python-2 unicode reprs inside `attributes`   [known, not yet hit]

Some attribute values are Python-2 style reprs — `u'free'`, `u'casual'` — rather
than plain strings, on top of the nested Python-literal dicts DATA.md already warns
about. Harmless today because only price is extracted, but it will bite whenever
another attribute is parsed. `ast.literal_eval` handles the nested dicts (zero
failures across the metro); the `u'...'` scalars need stripping separately.

### I12 — `is_open` will remove a substantial share of the catalog   [accepted]

Rerank's `is_open` filter drops roughly a quarter of the metro catalog (exact
figure in `data/PROFILE.md`). Not a bug — but it's a large, planned reduction that
will move final-stage metrics, and it interacts with I5. Expect it rather than
discover it.
