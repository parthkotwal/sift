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

One entry, and it is a deliberate deferral rather than a backlog. Resolved entries used
to accumulate here marked `[fixed]`, which made the section unreadable as a to-do list —
they now move to *Fixed — kept because the failure mode recurs* as soon as they close,
so anything appearing under this heading is genuinely outstanding.

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

**The deferral's stated condition has now been met, and the entry stays open anyway
(2026-07-30).** "Revisit once the ranker has real score resolution" was written when the
ranker had 18 distinct scores over 500 candidates and no personalization signal. D27 gave
it `ui_als_score` and it now beats ALS's own ordering outright, so the condition is
satisfied — the reason to leave this open is no longer the original one.

What remains is narrower. The path that serves is already stable (`retrieval/online.py`
and `reranked_candidate_lists`), so **serving is not affected**; the open question is
whether `reranked_lists`, the popularity-pool path, should be unified with it. That path
exists only as the build-step-4 baseline, and D20 makes candidate source part of the
ranker's training contract, so changing its tie-break means remeasuring a baseline whose
whole job is to be a fixed comparison point. Low value, real cost, and the ledger (D30)
would now catch it moving — which is a better reason to leave it alone than the one this
entry was opened with.

---

## Fixed — kept because the failure mode recurs

### I33 — One request read the active generation three times   [fixed]

**I logged this as "twice" and as "narrow". Cross-review found it is three times, and
the third one is what makes it not narrow.** `recommend` called
`lookup_user_embedding` (retrieval's input), `lookup` (the ranker's features), and
`rerank_inputs` (the filters) — each resolving the active generation independently. A
publication landing mid-request could therefore retrieve with one snapshot, rank with
the next, and filter with a third.

Missing the embedding read is what made my severity assessment wrong. I reasoned about
two catalog-scale reads that drift slowly between generations and concluded the
consequence was mild. But the embedding is the *user's* vector: mixing it with another
generation's item features is precisely the user-state/item-vector mixture the
per-generation item-ALS relation fix had just closed one layer down — the same bug, one
call site up, which I had just finished writing about and still under-counted.

**Fixed.** `OnlineFeatureStore.snapshot()` captures the manifest once; all three reads
take a `snapshot=` argument and use it. `recommend` pins one at the top. Two tests
cover it: a unit test asserting one resolution and three reads sharing it, and a store
test that flips the active pointer between reads and proves the pinned snapshot still
returns the old generation's values while an unpinned read follows the pointer.

**Rule:** when counting the call sites of a bug, enumerate them from the code rather
than from memory of the code. The one I forgot was the one that mattered.

This is the same class the cross-review fix for the item-ALS relation closed one layer
down — an in-flight request straddling a generation switch — and it survives here
because the generation is resolved per *call* rather than captured per *request*.

**Consequences are mild and bounded**, which is why it is logged rather than rushed:
both reads are catalog-scale state that changes slowly between generations, so the
realistic outcome is filtering against a slightly fresher catalog than the features
were scored on, not the user-state/item-vector mixture the other bug produced. The
window is also narrow — the manifest is cached per generation per thread, so it only
opens when the active pointer flips between the two calls.

**The fix is a small refactor, not a patch:** capture the snapshot once at the top of
`recommend` and pass the generation into both reads, which means `lookup` has to return
or accept a generation rather than resolving one. Worth doing deliberately, together
with whether the store should expose an explicit `snapshot()` handle that a request
holds for its whole life — which would make the property structural instead of
remembered, the same way the per-generation relation names did.

### I31 — The online feature path collapses under concurrency   [fixed]

Wiring the ranker into the API (I30) made the API call `store.lookup()` for the
first time, which exposed a latent defect in the I29 fix: **it was validated on a
workload that structurally cannot exhibit the bug.**

`_ensure_item_als` caches the item-ALS relation in `threading.local()`. Both
benchmarks that blessed it — `retrieval.online --samples` and `ranking.online
--samples` — are single-threaded loops, so they build the relation once and measure
a permanently warm thread. But `/recommend` is a **sync** FastAPI endpoint, so
Starlette runs it in the AnyIO worker threadpool: the cache is per worker thread,
and every cold thread pays the full build.

Measured through uvicorn on a 10-core / 4-performance-core machine, 200 requests:

| | feature_lookup p50 | p99 | total p99 | requests > 100ms |
|---|---:|---:|---:|---:|
| concurrency 1 | 19.21ms | 35.13ms | 39.92ms | 0/200 |
| concurrency 4 | 36.46ms | 54.86ms | 62.77ms | 0/200 |
| concurrency 8 | 79.47ms | 3676.52ms | 3702.96ms | **78/200** |

At concurrency 8 the end-to-end p99 contract (<100ms) fails on 39% of requests.
Sequentially it never fails, which is exactly why every benchmark to date missed it.

**The cold build, decomposed** (19.6MB Redis payload, 14,568 businesses):

| step | cost |
|---|---:|
| Redis `GET` | 128.2ms |
| `json.loads` | 119.9ms |
| **re-serialise via `_rows_json`** | **240.2ms** |
| DuckDB `json_each` | 149.5ms |
| total per cold thread | **637.8ms** |

Two distinct problems, and the second is the one that bites:

1. **240ms is pure waste.** The payload is parsed from JSON, dumped straight back to
   JSON, then parsed a third time by `json_each` — three passes over 19.6MB to move
   data that was already in memory as Python objects.
2. **The cache is per-thread for data that is per-process.** Item ALS vectors are
   catalog-wide and immutable within a generation — the exact justification for
   making them one Redis record in the first place. Caching them per thread
   re-pays 637ms per worker and duplicates ~19.6MB of parsed state per worker.

Contention compounds it: each thread holds its own DuckDB connection, and DuckDB
and LightGBM both default to using all cores, so 8 concurrent requests oversubscribe
4 performance cores. That is why concurrency 8 degrades super-linearly (p50 19 -> 36
-> 79ms) rather than merely queueing.

**Fixed, in three parts — and not fully closed.**

*One DuckDB database per store, one cursor per thread.* Cursors share the catalog and
buffer pool, so each immutable item-ALS generation relation is built once and read
by every thread. The generation must be part of the relation identity: Redis keeps
the previous snapshot alive for in-flight requests, and replacing one global table
would mix old request state with new vectors during activation. Verified before
relying on it: regular tables are visible across cursors, TEMP tables are
cursor-scoped, same-named TEMP tables stay independent, concurrent same-generation
lookups do not cross-talk, and old/new generation relations remain independently
addressable across a pointer swap.

*Per-request relations became TEMP.* This is a **correctness** requirement, not a
performance one, and it is the part worth remembering: all six per-request relations
(`queries`, `user_current`, `item_current`, `user_category_current`,
`user_als_current`, `business_current`) were plain `CREATE OR REPLACE TABLE`. That was
safe only because every thread had its own private database. Sharing a database
without scoping them would have let concurrent requests overwrite each other's rows —
one user served another user's features, silently, with no error. Consolidating state
turned a wasteful-but-isolated design into a shared one; the isolation had to be
re-established explicitly rather than inherited.

*Bounded intra-request parallelism.* DuckDB defaulted to 10 threads and LightGBM to
every core, so each request tried to fan out across the whole machine and multiplied
against request concurrency instead of adding to it. Both are pinned to 1
(`LOOKUP_THREADS`): a lookup projects a few hundred rows and scoring 500 candidates
is ~2.6ms single-threaded, so there was nothing to win per request and a lot to lose
under load.

The cold build also dropped 637.8ms -> ~215ms by handing the Redis blob straight to
`json_each`. It is already a JSON object of `business_id -> vector`, so the Python
parse and the `_rows_json` re-encode were two passes over 19.6MB that bought nothing.

**Measured through uvicorn, 300 requests per level** (total p99 / requests over 100ms):

| concurrency | before | shared db | + bounded threads |
|---|---:|---:|---:|
| 1 | 39.92ms · 0 | 38.11ms · 0 | 42.77ms · 0 |
| 4 | 62.77ms · 0 | 129.28ms · 8 | **69.21ms · 0** |
| 8 | 3702.96ms · 78/200 | 147.02ms · 52 | **118.37ms · 57** |
| 16 | — | 368.67ms · 259 | 316.07ms · 291 |

The tail at concurrency 8 improved ~31x and concurrency 4 is now fully inside the
100ms contract. **Still open above that:** p50 scales roughly linearly past
concurrency 2 (18 -> 23 -> 34 -> 70 -> 162ms), which is CPU saturation on 4
performance cores, not cold start.

**The remaining lever, now taken (D31).** Per request the store fetched ~1000 Redis
records (500 `item` + 500 `business`) — measured at **16.1ms of the ~19ms stage** — and
pushed them through the identical three-pass JSON route `item_als` had escaped. But
`item` and `business` current state is *also* catalog-wide and immutable within a
generation; only the three user-side records genuinely vary. Both are now one record
per generation, parsed once per process, and the per-request fetch is 3 records.

Feature lookup **19.3 -> 9.3ms p50**; end-to-end **33.0 -> 22.4ms p50**. The payoff that
matters is the envelope: concurrency 8 went from 118ms p99 with 57/300 requests over the
contract to **67.9ms p99 with 0/1000 over**, so the supported concurrency doubled.

Two surprises worth keeping, both in D31: rerank got *slower* first, because reading 50
candidates out of a 14,568-row DuckDB relation scans the whole thing — moving data
closer is not automatically faster, the access pattern decides. And 1.1ms silently
appeared in `overhead`, which turned out to be the generation-pinning Redis round trip
sitting before the first timer.

**Rule this earns:** a per-stage latency number measured single-threaded does not
describe a threaded server. Benchmark the transport the service actually uses, at
the concurrency it actually sees — `--samples` in a loop is a profile of one warm
thread, not a serving measurement.

**Also caught here:** a p99 over 100 samples is the 99th of 100 points — effectively
the maximum, with no statistical content. Every `--samples 100` p99 in this log is
under-powered, including I29's headline 26.874ms; at n=600 the same single-threaded
path measures p99 35.39ms.

### I29 — Online feature lookup regressed on ALS state   [fixed → D31]

Publishing ALS state (D27) took online feature lookup from ~2ms to **49ms p50** —
over its 20ms allocation, though end-to-end p99 still fit under 100ms. The
per-stage budget is what surfaced this; an end-to-end number alone looked fine.

**Cause:** item ALS vectors were published per business, so a 500-candidate request
fetched 500 keys from Redis and rebuilt a DuckDB relation from 500 x 64 floats of
JSON — every request, for data identical across requests within a generation.

**Fixed** two ways, in the order that mattered. The catalog's vectors are now one
Redis record rather than 14,568, and `_ensure_item_als` builds `item_als_current`
once per (connection, generation) instead of per request. Only the user side, which
genuinely varies, is still per-request.

| | feature lookup p50 | p99 | total p99 |
|---|---:|---:|---:|
| per-item keys | 49.192ms | 60.986ms | 62.752ms |
| + in-process cache | 39.139ms | 48.198ms | 49.961ms |
| **one record, built once** | **19.354ms** | **26.874ms** | **28.393ms** |

**Honest remainder:** p50 now sits just under the 20ms allocation but p95/p99
(25.3/26.9ms) still exceed it. Total p99 is 28.4ms against a 100ms end-to-end
budget, so there is headroom overall, but this stage is not yet inside its own
line. The next lever is the user-side relation and the per-request JSON round trip
into DuckDB, which is the same class of cost one layer down. Left open rather than
declared done.

**Also caught here:** `materialize_online`'s printed report showed
`user ALS vectors 0` while Redis held 180,528. The counts were added to the Redis
hash and the read path but not to the `OnlineManifest` the function *returns*,
which is what `main()` prints. The data was correct and the report was not — worth
recording because a report that under-states is more dangerous than one that
errors: it sends the next reader to debug something that works.

### I30 — The API does not serve the ranker that now lands   [fixed]

`retrieval/online.py` serves ALS order directly, and its docstring still states the
reason: "The ALS-conditioned ranker did not beat ALS's own ordering, so it does not
land." D27 falsified that — the ranker now beats ALS on recall@10 (+10.3%) and
NDCG@10 (+9.3%). The docstring is stale and the API is serving the displaced model.

Wiring it needs the ALS retriever to score its 500 candidates through
`ALS_RANKER_MODEL` the way `OnlineRanker` already does for popularity, plus stage
latency instrumentation. Gated behind I29: shipping a ranker whose feature lookup is
2.5x over budget would trade measured quality for measured latency without deciding
which matters more here.

**Code written, not shippable.** `OnlineALSRetriever` now retrieves `SERVING_POOL`
candidates, reads their features through the store, and orders them with
`ALS_RANKER_MODEL` using the same stable tie-break as `reranked_candidate_lists`, so
serving cannot order candidates differently from the offline run that decided the
ranker lands. Tests pin the properties that matter: the ranker overrides the ALS
order it was given, tied scores fall back to retrieval's order (I6), the *whole* pool
is scored rather than a pre-truncated top-k, and a cold user is served popularity
without a feature lookup at all.

**Now blocked by I31, which wiring this is what exposed.** Calling `store.lookup()`
from the API for the first time surfaced a per-thread cache that collapses under
concurrency. The original I29 gate turned out to be the less important one: the 2.5x
figure was single-threaded, and single-threaded was never the serving condition.

**Fixed (4132a6f).** I31's shared catalog cache, cursor-local request relations, and
bounded DuckDB/LightGBM threads removed the cold-thread collapse that blocked the
intended path. The transport-level benchmark added in `affa429` is now the contract
check; the single-threaded loop remains only an in-process profile, and the binding
verdict belongs to uncontended deployment hardware.

**On the stale docstring:** it claimed the ranker does not land, which D27 falsified
three commits earlier. The same falsified fact was live in four places at once — this
entry, `DECISIONS.md` D27's closing paragraph, `README.md`, and the module docstring —
while `I25` sat marked open after being fixed. A project whose deliverable is "the
author can explain every stage cold" cannot afford docs that describe a superseded
system: this is the set of files someone reads to prepare, and all four disagreed
with the code. When a decision reverses a result, grep for the old claim.


### I25 — The online store cannot serve `ui_als_score`   [fixed]

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

**Fixed (800c69d).** `user_als` and `item_als` joined `ONLINE_STATE_GROUPS`, so
`online_features()` now returns all nine features and the skew check passes over the
full set (500 pairs / 36,500 values). The derived-not-listed design paid off exactly
as intended: closing this needed no edit to `online_features()` itself.

**This entry stayed marked open for three commits after it was fixed**, and
`DECISIONS.md` D27 still read "Not yet servable" — see the note under I30 on stale
claims outliving the code.

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

### I24 — No regression test pins the headline retrieval metrics   [fixed → D30]

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

**Fixed, built to that shape (D30).** `sift/eval/ledger.py` keeps
`data/derived/eval_ledger.json` — gitignored with the rest of the dataset-derived
numbers — and every eval entrypoint diffs against it, prints the verdict, and exits
non-zero on a regression. `--accept` records the new value deliberately.

The deferral's constraint is respected rather than worked around: the mechanism is
unit-tested with synthetic reports and runs anywhere, while the numbers live in a
local artifact. No test asserts 0.2519, so nothing reintroduces I1's hidden dependency
on machine state, and no synthetic fixture is asked to reproduce a property of the
real distribution (I8).

Three behaviours the tests pin, because each is a way this could pass while doing
nothing:

- **A first run establishes rather than passes.** A fresh clone has no ledger, and
  "no baseline" must not look like "no regression" — otherwise the first run of a
  broken model reads clean.
- **A regression is not written.** The way a ratchet fails is by ratcheting the wrong
  way once: record a bad run and the *next* run is judged against it, so the loss
  disappears silently.
- **A changed eval set is fatal, not a regression, and `--accept` cannot silence it.**
  D18 froze the holdout; if `n_users` moves, the runs did not measure the same thing
  and an *improvement* is exactly as untrustworthy as a decline. That is a different
  failure from "the model got worse" and must not share an escape hatch with it.

### I5 — Repeats vs. the already-reviewed rerank filter   [resolved at step 6 → D29]

D18 choice 3 counts a business reviewed both before and after T as a valid target;
ARCHITECTURE's rerank stage plans an already-reviewed hard filter that would make
those structurally unreachable, capping final-stage metrics. Recorded in D18 as
open. Resolve at step 6 — drop the filter, soften to a demotion, or keep it and
document the ceiling. Do not resolve silently.

**Resolved: keep the hard filter, and report both numbers (D29).** But the cost is an
order of magnitude larger than this entry implied, and the way it was got wrong is
worth more than the decision.

**The wrong argument, made first.** Repeats are ~1.6% of holdout targets but occupy
**13.0%** of the ranker's served top-10 — so the filter looked like it freed 13% of
the final slots to forfeit 1.6% of the reachable targets, an 8x favourable trade. That
reasoning was recommended, accepted, and is wrong. It treats the freed slots as
converting at the *average* rate.

**What measurement showed.** Isolating each mechanism on the full holdout (26,489
users):

| variant | recall@10 | vs ranker |
|---|---:|---:|
| ALS -> ranker, no rerank | 0.0386 | — |
| closed filter only | 0.0379 | −1.7% |
| repeats filter only | 0.0224 | **−42.0%** |
| all three (serving) | 0.0228 | −41.0% |

The mechanism, measured directly: **949 of the ranker's 2,579 top-10 hits (36.8%) are
businesses the user had already reviewed** — from ~1.3% of the candidate pool. A
repeat converts at roughly 28x the rate of an average candidate, because a return
visit is the easiest thing in this dataset to predict. The filter therefore removes
the *densest* slice of the model's success, not a representative one.

**The rule this earns:** a share-of-slots figure and a share-of-hits figure are not
interchangeable, and swapping them silently inverts a trade-off by more than an order
of magnitude. Any argument of the form "this frees X% of positions to lose Y% of
targets" is incomplete until the conversion rate of those positions is measured. The
slots a good model puts at the top are, by construction, not average slots.

**Why keep the filter anyway.** The 42% is real but it is not all discovery: a third
of the pre-rerank number was credit for predicting return visits to places the user
already knows. Serving filters them; the unfiltered number stays published beside the
filtered one so the gap is legible rather than hidden. D18 and the frozen holdout are
untouched — this is a serving decision reported alongside them, not a redefinition of
ground truth.

### I34 — "Already reviewed" meant any event type   [fixed]

Both reviewed-history queries — the Redis publisher and the offline rerank harness —
selected user/business pairs from the canonical event table with no `event_type`
filter. Correct today by accident: ingest emits only `'review'`.

The trap is that it is designed not to stay that way. D2 chose one canonical event
table precisely so tips and check-ins can land as *pure ingest additions* — same
schema, new `event_type` — and `ingest.py`'s own docstring says so. The day one lands,
both queries silently widen: a business the user merely tipped becomes "reviewed" and
is suppressed from every recommendation they ever see. Nothing raises, no test fails,
and the symptom is a slightly worse recall that looks like model drift.

**Fixed** by filtering both on `event_type = REVIEW_EVENT`, with the constant defined
in `ingest.py` next to the writer that emits it so the literal cannot drift from the
producer. A test writes a mixed-event partition and asserts a tipped business is not
treated as reviewed.

**Rule:** a query against the canonical event table that omits `event_type` is making a
claim about *all future event types*, not just today's. The schema was designed to grow;
consumers have to say what they mean now, while there is only one answer and the
omission is invisible.

### I32 — An ablation row measured a disabled variant   [fixed]

`rerank.evaluate` reports each filter in isolation so the recall change can be
attributed. The "diversity cap only" row passed `no_cap` — the sentinel that *disables*
diversity — instead of `CATEGORY_CAP`, so it reran the baseline and reported it as a
measurement of the cap.

It printed **+0.0%**, identical to the baseline at four decimals across recall@10,
recall@50 and NDCG@10. That is the signature: an ablation that exactly reproduces its
control is almost never a finding about the mechanism, it is a report that the
mechanism was not switched on. A sampled run had shown the cap moving recall@10 by
−3.2%, which is what made the zero suspicious enough to check.

**Fixed** by passing the real cap, with the sentinel's role commented at the call site.
**Rule:** in an ablation table, a difference of exactly zero is a bug report until
proven otherwise — verify the knob moved before believing the row. Same family as I8
(a test that passes vacuously) and I27 (a check that cannot fire): the failure is
always a mechanism that never engaged while its output looked legitimate.

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

### I12 — `is_open` will remove a substantial share of the catalog   [resolved at step 6 → D29]

Rerank's `is_open` filter drops roughly a quarter of the metro catalog (exact
figure in `data/PROFILE.md`). Not a bug — but it's a large, planned reduction that
will move final-stage metrics, and it interacts with I5. Expect it rather than
discover it.

**Resolved: filter at serving, report both numbers (D29). It costs far less than the
catalog share suggests — and the reason is the interesting part.**

Measured: closed businesses are 27.6% of the catalog, 27.9% of the ranker's
500-candidate pool, but only **10.9%** of its top-10 — the ranker had already learned
much of the signal indirectly through review recency, since a closed business stops
accruing reviews. And **7,732 of 89,519 holdout target pairs (8.64%) are businesses
that are now closed**, which looked like an 8.64% ceiling loss.

It is not. Filtering them costs **1.7%** of recall@10 (0.0386 → 0.0379): those targets
were mostly never being surfaced anyway. The gap between "8.64% of targets become
unreachable" and "1.7% of recall is lost" is the difference between a target existing
and a model finding it — worth stating, because the pessimistic figure was the one on
record here and it overstated the damage fivefold.

**Why both numbers are still reported.** The loss is small but it is *not* ranking
quality: `is_open` records whether a business trades in 2022 while the ground truth is
2019 behaviour, so a business the user visited in 2019 and that has since closed is
simultaneously a correct suppression today and a permanently unreachable target. That
is the same dump-vintage hazard D13 cites for keeping `is_open` out of training
entirely, surfacing one stage later in the eval instead of in the model.
`python -m sift.rerank.evaluate` prints the filtered and unfiltered rows together so
the artifact cannot be mistaken for a regression.
