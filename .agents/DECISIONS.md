# DECISIONS.md — decision log

Append-only. One entry per real choice: context, options, choice, why. Agents: never silently re-litigate an **accepted** decision or silently resolve an **open** one — raise it with the author, then record the outcome here. Reversing a decision gets a *new* entry that supersedes the old one, not an edit.

Format:

```
## D<N> — <title>   [accepted | open | superseded by D<M>]  (YYYY-MM-DD)
Context / Options / Choice / Why
```

---

## D1 — Replay, not fake streaming   [superseded by D10] (2026-07-21)

**Context:** Yelp ships a static, timestamped dump; the (then) batch-platform project needed the shape of a live pipeline.
**Choice was:** replay — sort by timestamp, partition by date, run incrementally under a simulated clock.
**Superseded:** the replay *clock* as organizing principle died with the batch-platform framing (D10). What survives: never pretend the data is live, and offline jobs stay partitioned and date-aware because point-in-time features require it.

## D2 — One canonical event schema in silver   [accepted] (2026-07-21)

**Context:** Reviews, tips, and check-ins arrive as three differently-shaped JSON files.
**Options:** (a) keep three typed event tables; (b) normalize all sources into one `user_id, entity_id, event_type, ts, payload` table.
**Choice:** (b) One canonical table.
**Why:** Feature computation shouldn't know Yelp's file formats; adding an event source becomes an ingest-only change; `event_type` keeps label definitions a modeling choice. Cost accepted: heterogeneous payloads in a semi-structured column; check-ins carry `user_id = null` (no attribution in source) and feed entity-side features only.

## D3 — Yelp Open Dataset, not Food.com   [accepted] (2026-07-21)

**Context:** eggly's existing study uses Food.com (1.13M interactions, one event type). The author's concerns: data scale, and owning the work rather than inheriting AI-assisted analysis.
**Choice:** Yelp (~7M reviews, multiple event types, several GB), in a standalone repo (albumen), sibling to eggly.
**Why:** Scale is honest; heterogeneous events make the system real; building both halves fresh means the author owns them. eggly's Food.com findings are hypotheses elsewhere, out of scope here (re-affirmed in D10).

## D4 — The model is a swappable consumer   [superseded by D10] (2026-07-21)

**Choice was:** one model behind `score()`, project unchanged if swapped.
**Superseded:** the two-stage system has two models (retrieval embeddings, ranker), so "swap the model, project unchanged" is no longer the framing tell. What survives, restated in D10: each model sits behind a stage interface, is swappable, and must beat its predecessor to land. What's dead: the idea that the model is peripheral — the funnel *is* the system; the models are just not the research subject.

## D5 — Partition grain and job cadence   [superseded by D10]

Was open (daily vs. weekly under the replay clock). Moot: offline jobs are date-partitioned for point-in-time correctness, and cadence is just "how often the batch job runs" — an operational knob, not an architecture decision.

## D6 — Orchestrator: Dagster vs. Airflow   [superseded by D10]

Was open. Resolved by D10: **no orchestrator.** The offline path is plain scheduled scripts; an orchestrator is a platform-project deliverable and this is no longer a platform project.

## D7 — Data-quality tooling   [superseded by D10]

Was open (Great Expectations vs. Soda vs. hand-rolled). Resolved by D10: hand-rolled assertions at ingest and store boundaries only — the correctness budget moved to the leakage/invariance/skew tests, which matter more here than distribution gates.

## D8 — Table format: plain Parquet first   [accepted] (2026-07-21)

Plain partitioned Parquet; no Iceberg/Delta. Unchanged by the reframe — even more clearly right now that the pipeline is supporting cast.

## D9 — Feature store: hand-built, not Feast   [accepted] (2026-07-21)

**Choice:** Build the feature store by hand — definitions module, materialization job, lookup client.
**Why:** Point-in-time machinery and train/serve consistency are the project's learning core; a framework would hide exactly the thing we're here to understand. The reframe (D10) *promoted* this decision: the store is now a headline component, not plumbing.

## D10 — Reframe: a two-stage retrieval/ranking service, not a batch platform   [accepted] (2026-07-22)

**Context:** The batch-platform framing had no one-sentence mechanism ("a pipeline is a stage, not a system") and its deliverable was hard to state as a machine doing something.
**Options:** (a) keep the platform framing; (b) reposition the same engineering under a serving system: given a user id, return 10 businesses from the full metro catalog in <100ms p99, via retrieval (150K→500, ANN ∪ heuristics) → ranking (500→50, LightGBM) → rerank (→10, filters+diversity), with staged metrics (recall@500, NDCG@10, per-stage latency).
**Choice:** (b).
**Why:** The staged funnel is the canonical industry architecture and a stateable mechanism; the engineering content (point-in-time correctness, canonical events, partitioned Parquet) survives *inside* it, chiefly in the feature store. **Costs, accepted knowingly:** orchestration/quality-gates/replay-clock deliverables cut (D5–D7); and the silhouette is closer to the author's prior projects — mitigated by the resume-shape rule in `AGENTS.md` (outward descriptions lead with the feature store, staged metrics, and latency budget, never "a recommendation API").

## D11 — ALS before two-tower for retrieval   [accepted] (2026-07-22)

**Context:** Retrieval needs user/item embeddings for the ANN index. A PyTorch two-tower is the fashionable choice — and exactly the kind of subtle ML (sampling bias, training instability) the author has been burned by inheriting rather than owning.
**Options:** (a) two-tower first; (b) ALS (`implicit`) first — factors are "a two-tower without the towers": vectors whose dot product predicts interaction — then two-tower as an upgrade gated on beating ALS at recall@500.
**Choice:** (b).
**Why:** The entire serving path (index build, source union, retrieval eval, latency) gets built and measured on a model the author already understands from eggly. Applies the project's own rule — nothing lands without beating its predecessor — to its riskiest component. If the two-tower never beats ALS, ALS ships and the system is unchanged.

## D12 — Embeddings are feature definitions   [accepted] (2026-07-22)

**Context:** Needed a dataset-agnostic seam where eggly's semantic representation can later plug in.
**Choice:** The feature store's definition abstraction covers embeddings: a definition = name, entity type, dtype/shape, as-of semantics, version, compute fn — where an embedding's compute is a versioned model artifact. Stores serve bytes; consumers bind by name+version via config.
**Why:** One abstraction instead of two (features + a separate `score()` seam); swapping behavioral ↔ semantic ↔ concatenated embeddings becomes configuration, not refactoring. This is the eggly coexistence architecture, finalized.

## D13 — `is_open` is a filter, never a feature   [accepted] (2026-07-22)

**Context:** `is_open` reflects dump-time state; historically it's unknowable (a business open in 2016 but closed by dump time carries 0 into 2016 rows) — using it in training is leakage, yet filtering on it at serving time is correct and necessary.
**Choice:** Rerank-stage hard filter only; blocklisted as a model feature.
**Why:** The general principle it instantiates: any signal that's legitimate online but unconstructible historically is train/serve skew by definition — it belongs in serving-time filters, not in the model.

## D14 — One language: Python (not Go)   [accepted] (2026-07-22)

**Context:** The stack sections assumed Python throughout; the question was whether the *online* serving path should instead be Go for latency headroom against the <100ms p99 budget.
**Options:** (a) Python everywhere; (b) Python offline (training) + Go online (serving).
**Choice:** (a) Python everywhere.
**Why:** (b) reintroduces training/serving skew — the exact hazard the feature store exists to prevent (D9, D12) — at the language boundary: the offline path is unavoidably Python (`implicit` ALS, LightGBM/PyTorch training, FAISS all Python-native), so Go could only serve by re-implementing feature compute and inference a second time, making one feature *definition* physically unable to back both paths. It also violates the collaboration rules ("explainable out loud", "no cleverness the author can't own", "no new tools without rent"). The 100ms budget is met in Python because the hot paths are native (FAISS/LightGBM are C++, store lookups are network-bound) and the Python is thin glue; if a specific stage's glue later measures as the bottleneck, that's a localized, instrumented optimization — not a language decision made up front.

## D15 — Repo tooling: uv + ruff + mypy(strict) + pytest   [accepted] (2026-07-22)

**Context:** Step-0 scaffolding needed an environment manager and quality tooling.
**Choice:** `uv` (env + deps + lockfile + Python 3.12 pin), `ruff` (lint+format), `mypy --strict` (AGENTS.md mandates type hints), `pytest` (the correctness properties — leak/invariance/idempotency — are pytest tests). `src/` layout. Pre-commit hooks deliberately skipped: the chokepoint tests belong in CI where they can't be bypassed, not in local hooks.
**Why:** Smallest ceremony that still enforces the docs' non-negotiables structurally. Dev tooling only; runtime ML deps are added at the build step that first needs them.

## D16 — Rename to Sift; decouple from eggly   [accepted] (2026-07-22)

**Context:** The project was named Albumen and framed as eggly's standalone behavioral engine (D3, D11, D12 carry eggly rationale). It has diverged enough that the eggly relationship is no longer a driving concern. Agent docs also moved to `.agents/`.
**Choice:** Rename the project and Python package `albumen` → `sift` everywhere (code, `pyproject`, docs). Drop eggly as the project's raison d'être: remove the "Relationship to eggly" sections and neutralize eggly attributions in the living docs (ARCHITECTURE/AGENTS/CONCEPTS). **The append-only D-log bodies of D3/D11/D12 are left intact as history** — this entry supersedes their *eggly framing*, not their decisions.
**Why:** The name and framing should match reality; carrying a sibling-project relationship the work no longer has is misleading in exactly the outward-facing descriptions the resume-shape rule governs.
**What survives, deliberately:** the D12 embedding-definition seam. Its original motivation was eggly's semantic vectors, but the decision stands on its own as *general extensibility* — any externally-produced embedding (a stronger behavioral model, a bought/content/semantic vector) registers as another definition and plugs into the index/ranker by config. It costs nothing now (it is just the definition abstraction being built anyway), so the seam is kept open and reframed rather than cut. Food.com hypotheses, already out of scope, are simply dropped from the scope list.

## D17 — Step-1 metro = Philadelphia; frozen temporal split T = 2019-01-01   [accepted] (2026-07-22)

**Context:** Build step 1 requires an initial metro and a temporal split date, both frozen forever (changing either invalidates the entire eval history). Chosen from a read-only profile of the Jan-2022 dump (`scratch/profile_raw.py`; exact metrics in gitignored `data/PROFILE.md`).
**Options — metro:** the highest-volume metros were Philadelphia, New Orleans, Tampa, Nashville. **Options — T:** (a) 2019-01-01, full post-T holdout; (b) 2019-01-01 with eval capped to 2019, excluding the COVID years; (c) 2020-01-01.
**Choice:** Metro = **Philadelphia, PA** (business `city` field). Split = **T = 2019-01-01**, train strictly before T, test on/after T, full post-T holdout (option a).
**Why:** Philadelphia has the largest review volume and the densest multi-year history — the cleanest signal for a temporal split, and the widest catalog to retrieve against. T = 2019-01-01 leaves a large, rich training history while opening the holdout on 2019, a dense pre-COVID year, giving a substantial (~1/5 of the metro's reviews) test set. The 2020 COVID volume drop is a real distribution shift; option (a) keeps it *inside* the holdout (realistic — production faces drift) rather than hiding it (b) or grading solely on it (c). Cost accepted: eval spans a regime change, so absolute post-T numbers blend rec quality with the pandemic shift — acceptable because every model is judged on the *same* holdout, so comparisons stay valid.
**Metro caveat:** "Philadelphia, PA" is the `city` string, not the Census CBSA (which would fold in PA/NJ suburbs). City-level is the unambiguous frozen unit for step 1; the metro definition may widen at build step 7 (scale), which would be a new decision, not an edit to this one.

## D18 — Frozen evaluation definitions   [accepted] (2026-07-22)

**Context:** Build step 2 needs the eval set pinned down before any number means anything. Three definitions were open, each changing every metric Sift will ever report. Measured from the holdout: ~71% of users with post-T activity have **no** pre-T metro history, and repeat businesses are ~1.6% of ground-truth pairs.
**Choices:**
1. **Eval users = users with >=1 metro review strictly before T.** Cold-start users are excluded from the headline number.
2. **Relevance = any post-T metro review, regardless of star rating.** Retrieval's target is engagement ("did we surface where they actually went").
3. **Repeats included:** a business reviewed both before and after T remains a valid target.
4. **Metrics:** recall@{10,50,100,500} (headline **recall@500**) and **NDCG@10**, both **macro-averaged** — every user counts equally regardless of activity level.
**Why:** (1) With no history there is nothing to personalize from, so every model is forced to behave identically on those users; leaving 71% of the eval set in that regime compresses the gap between a good model and the popularity floor and flatters the baseline. Cold-start is a real but *separate* problem, reportable on its own. (2) Star-filtering discards ~31% of signal; engagement is the honest retrieval target, and graded relevance by stars remains available to NDCG later, when ranking is judged on satisfaction rather than engagement. (3) Repeat visits are genuine recommendations (returning to a favorite), and retrieval is measured *before* rerank, so there is no conflict at this stage. (4) Macro-average prevents heavy users from dominating.
**Open, deferred to build step 6:** ARCHITECTURE's rerank stage plans an **already-reviewed hard filter**, which would make the ~1.6% of repeat targets structurally unreachable and put a ceiling on final-stage metrics. Choice (3) means that filter must be revisited when rerank is built — drop it, soften it to a demotion, or keep it and accept the documented ceiling. Do not resolve this silently.
**Consequence:** these definitions are frozen exactly as SPLIT_T is. Eval history lives in the gitignored `data/RESULTS.md` (Yelp ToS restricts publishing dataset-derived metrics).

## D19 — Ranker training set: engagement label, popularity-pool negatives   [accepted] (2026-07-22)

**Context:** Build step 3b assembles the LightGBM ranker's training rows: positives + sampled negatives + point-in-time features. Three sub-choices.
**Choices:**
1. **Label = engagement:** every pre-T review is a positive (label 1). Matches the D18 eval target; stars stay a feature (and available for graded NDCG later), not the label.
2. **Negatives from the popularity pool:** for each positive, sample `NEG_RATIO=4` presumed-negatives (label 0) from the top `POOL_SIZE=500` businesses by pre-T review count — the set retrieval actually serves — never a business the user reviewed (no false negatives).
3. **Point-in-time features** from `sift.features.pit` (right-exclusive as-of the row's ts); one positive + its negatives share a `group_id` for LightGBM query grouping.
**Why:** (1) Train and eval measure the same thing (engagement); using all reviews maximizes signal. (2) At serving the ranker only ever reorders popularity's top-500, so training negatives must come from that distribution — uniform-random negatives (mostly long-tail) would teach popularity-vs-obscure and leave the ranker unpracticed at the popular-vs-popular discrimination it faces live ("train on the serving distribution"). (3) Features are strictly as-of t, guaranteed by the invariance/leak tests.
**Simplifications accepted (v1):** the negative pool is popularity **as of T**, reused for every historical training row, rather than recomputing "what retrieval would have returned at t" per row. This is a *sampling-distribution* approximation, not feature leakage — the model's inputs remain strictly as-of-t (the pool is never an input). Negatives are drawn with a fixed seed (`SEED`) so the training set is reproducible/idempotent. Ratio and pool size are un-tuned starting points (no sweep, per the no-experimentation rule).
**Deferred:** user x item features (distance, category affinity, price match) need the business dimension, not yet ingested — the v1 event-stream features (user/item to-date counts and mean stars) cannot express this-user-likes-this-business, capping the ranker's reachable quality until they land.

## D20 — Ranker positives are restricted to the candidate pool   [accepted] (2026-07-25)   *(narrows D19 choice 2)*

**Context:** D19 paired every pre-T review (positive) with negatives drawn from the popularity top-500. Measuring the assembled artifact after the step-3 loss showed the two sides came from different populations: **only 39.1% of positives were inside the top-500, while 100% of negatives were, by construction.** Median `i_reviews_to_date` was 75 for positives vs 255 for negatives, and in **72.5% of groups the positive was less popular than its own negatives**. The cheapest way to separate the classes was therefore "less popular ⇒ positive" — the inverse of the serving signal — and a feature sweep of the trained model confirmed it had learned exactly that (score falling from +0.151 at 50 prior reviews to -0.183 at 400), with its entire dynamic range sitting below the serving pool's *minimum* of 289. That is the mechanism that put the step-3 ranker measurably *below* popularity rather than level with it.
**Options:** (a) keep all positives, sample negatives from the full catalog stratified to match; (b) restrict positives to businesses in the candidate pool; (c) keep D19 as-is.
**Choice:** (b). Positives are pre-T reviews **of pool businesses only**; negatives unchanged. Training set: 3.80M rows → 1.49M (297,665 positives).
**Why:** The ranker only ever reorders retrieval's output, so its serving conditional is "given these 500, which does this user pick." A row whose positive is unretrievable trains it on a question it is never asked, and — worse — makes pool membership a free shortcut that correlates *negatively* with the label. (a) was rejected because ranking the full catalog is retrieval's job, not the ranker's; training for it reintroduces the same mismatch in a different form.
**Measured consequence, recorded honestly:** the artifact was removed as intended — feature gain on `i_reviews_to_date` fell from 3.45M to 83.5K, in-group val NDCG@10 fell from 0.862 to 0.737 (the easy shortcut is gone), and early stopping now halts at **iteration 1**. But the ranker still does not land: recall@10 0.0169 → 0.0096 and NDCG@10 0.0122 → 0.0060 against popularity. See `data/RESULTS.md`. Both the D19 and D20 training sets lose; D19's slightly higher recall@10 came *from* the inverted-popularity artifact, so reverting to chase that number would be optimizing a metric through a known defect. D20 is the correct foundation to add features onto, not a win in itself.
**What it bought:** the diagnosis is now conclusive rather than inferred. With the sampling confound gone, the residual is measurable: across all 26,489 eval users the ranker emits **10 distinct orderings of the 500 and 2 distinct top-10s**. It is a user-independent reordering of popularity, so it cannot beat a user-independent baseline — only reproduce it lossily. "The features are too weak" is now demonstrated, not asserted.
**Cost accepted:** 61% of positives are discarded, and the ranker is trained only on visits that land in the popularity head. Those users' long-tail visits are unreachable at this stage anyway (recall@500 = 0.2131 caps it), so the loss is to a population the ranker cannot serve.

## D21 — Business dimension: quasi-static attributes are admissible features   [accepted] (2026-07-25)

**Context:** Build step 3's ranker needs the `user x item` features ARCHITECTURE.md specifies (centroid-to-business distance, category-affinity dot, price match). All three combine an as-of-t user aggregate with an attribute of the business — and every business attribute in the dump is a **January 2022 snapshot**, i.e. future state relative to any historical training row. The prime directive forbids attaching future values to training rows, so this needed resolving rather than assuming.
**Options:** (a) treat all dimension columns as leakage and abandon the planned features; (b) admit *quasi-static identity* attributes while continuing to blocklist *accumulating* ones; (c) reconstruct historical attribute values (the dump provides no history, so this is not actually available).
**Choice:** (b), with the line drawn at whether the attribute can encode the label event. Admitted: `latitude`, `longitude`, `categories`, `price_tier`. Blocklisted: `stars`, `review_count`. Filter-only: `is_open` (D13, unchanged).
**Why:** The distinction is causal, not chronological. `review_count` at dump time *counts the very review being predicted* — it is leakage by construction. A business's location, category, and price band do not change as a function of whether one user reviewed it in 2016; they are properties of the entity's identity. Using a 2022 reading of them on a 2016 row risks *staleness*, which costs accuracy, not *leakage*, which fabricates it. `price_tier` is the weakest of the three (a venue can move price band across years) and is accepted knowingly as the most stale of the admitted columns.
**Enforcement, and its limit — read this part:** the future-invariance test mutates *events*; dimension attributes are not events, so **that test does not and cannot cover them**. Chokepoint 4 (the snapshot blocklist) is what guards this table, and it is implemented two ways: `stars`/`review_count` are **never materialised by the ingest at all** (you cannot leak a column that does not exist — asserted by `test_snapshot_counters_are_not_materialized`), and `is_open`, which must exist for rerank, is guarded by an assertion that no feature module references it. The time-varying half of every `ui_*` feature still flows through the existing as-of chokepoint, so the invariance test does cover that half.
**Cost accepted:** the metro's `price_tier` coverage is 61%, so `ui_price_delta` is NULL for ~30% of rows (LightGBM consumes this as a native missing value). `categories` is 99.9% covered and lat/long is 100%.

## D22 — The training artifact's row order must not encode the label   [accepted] (2026-07-25)

**Context:** Found while investigating why LightGBM early-stopped at **iteration 1** across every ranker attempt. `training_set.py` wrote the artifact `ORDER BY group_id, label DESC, business_id`, placing the positive as the physically first row of every group. LightGBM reads groups as consecutive blocks in file order and resolves *tied scores* in that order. An under-trained model ties nearly everything, so the positive was scored at rank 1 for free — which inflated validation NDCG@10 precisely for the **least**-trained model. Early stopping then dutifully selected it.
**Evidence:** holding features and data fixed and varying only row order, validation NDCG@10 at iteration 1 was 0.7527 (positive first), 0.6669 (positive last), 0.7097 (ordered by `business_id`) — and all three converged to ~0.727 by iteration 120, which is the model's real skill. Only the label-ordered variant peaked at iteration 1; the other two improved monotonically and stopped near iteration 120+.
**Choice:** order rows within a group by `business_id` alone — label-independent, still fully deterministic so idempotency is preserved. Guarded by `test_row_order_within_a_group_does_not_encode_the_label`, which was verified to fail against the old ordering before being kept.
**Why it matters beyond the fix:** with the ordering corrected the ranker trains for 579 iterations instead of 1, and **lands** (`data/RESULTS.md`). This is the same class of defect as leakage — the label reaching the model's *selection* signal through a channel nobody was watching — except the channel was row order in a Parquet file rather than a feature value.
**Correction to the record:** the two prior "DOES NOT LAND" verdicts (D19 and D20 blocks in `data/RESULTS.md`) were both measured on single-tree models and are therefore **confounded**. D20's diagnosis specifically — "10 distinct orderings, so the ranker is user-independent, so the features are too weak" — was measured on a stump, and the stump was this bug, not the feature set. The feature set was *also* a real limitation (a global-only model cannot express this-user-likes-this-business, and adding `ui_*` features raised orderings from 10 to 21,757 while still on one tree). But the two causes were never cleanly separated, and D20's reasoning claimed more certainty than the measurement supported. Re-running the global-only feature set with the ordering fixed would settle the attribution; it has not been run.
**Addendum (2026-07-26) — the attribution has now been run; results in `data/RESULTS.md`.** Recorded here rather than as a new D-entry because it resolves a question this entry raised; it is a measurement, not a choice, and D22's reasoning above stands unedited. Outcome: both causes were real and neither sufficed alone. Fixing the ordering lifted global-only from 0.0096 to 0.0144 recall@10 but it still loses to popularity's 0.0169; adding `ui_*` under the bug reached only 0.0118. The gains are superadditive — the features are worth +0.0022 on a stump and +0.0080 on a trained model. **One specific claim in D20 is now falsified:** "the ranker is user-independent, so the features are too weak." With the ordering fixed the global-only model yields 691 distinct orderings, not 10 — it is *segment*-personalised, not user-independent. Its true ceiling is that 691 orderings collapse to **13 distinct top-10s** across 26,489 users: user-side features reshuffle the tail while the sharp end stays nearly global. The conclusion D20 drew was right; the mechanism it named was not.

## D23 — The feature store persists entity timelines; user x item features are derived at read time   [accepted] (2026-07-26)

**Context:** Build step 4. ARCHITECTURE specifies one definition materialized two ways (historical → Parquet, current → Redis), but the v1 feature set contains three `user x item` features (`ui_distance_km`, `ui_category_affinity`, `ui_price_delta`) that cannot be keyed by a single entity — there is no "user x item" row to materialize without writing users x items rows, which for this metro is 26k x 14.5k.
**Observation that resolves it:** `pit.py`'s cumulative CTEs (`user_tl`, `item_tl`, `user_cat_tl`) already *are* feature values as-of every timestamp where they change. Materialising them is persisting what is currently recomputed on every run — which is most of the eval's 60s cross product.
**Options:** (a) persist entity timelines, derive `ui_*` at read time from two stored vectors; (b) keep the monolithic query and make definitions a naming layer; (c) one compute function per definition.
**Choice:** (a).
**Why:** It is what production systems do — store the user vector and the item vector, combine at request time — so the `ui_*` features are *not* an exception to "one definition, two materialisations": their inputs are stored, and the combining expression is itself the single definition used by both paths. (b) yields a store that stores nothing and leaves the online path needing DuckDB. (c) re-scans the user timeline once per feature, losing the shared CTEs for no capability gain.
**Consequent design — everything is an expression over materialised state.** A definition declares `name, entity, dtype, version, reads, expr, leakage`, where `reads` names the state groups the expression consumes (`user`, `item`, `user_category`, `business`) and `expr` is SQL over their aliased columns. This unifies the two cases: `u_reviews_to_date` is an expression over one group's state, `ui_distance_km` an expression over two. The state groups are the unit of *compute and storage*; definitions are the unit of *reference and versioning*, which is how consumers bind (D12).
**The leakage argument becomes a required field.** AGENTS.md mandates a one-line "why can't this contain the future?" per feature; making it a non-defaulted dataclass field means a definition cannot be registered without one, and a test asserts none are empty. A documentation rule turned into a structural one.
**Cost accepted:** the read path is now an assembled query rather than a hand-written one, so a malformed `expr` fails at SQL-compile time rather than in Python. Mitigated by the registry test suite compiling every definition against a synthetic fixture.

## D24 — Redis publishes generation-scoped current state behind one pointer   [accepted] (2026-07-26)

**Context:** Build step 4's online materialisation writes hundreds of thousands of
entity records. Updating stable keys in place would let a request observe a mixed
snapshot — new user state with old item state — even though each individual Redis
command is atomic. The online representation also has to preserve D23: entity state
is stored; `user x item` values are derived rather than materialised as a cross product.
**Options:** (a) overwrite stable records in place; (b) write a fresh generation and
atomically switch an active-generation pointer; (c) use Redis logical databases and
`SWAPDB`.
**Choice:** (b). User and item latest rows are individual JSON records; a user's
current category vector is one record (category → cumulative count); business
identity state is one record. A lookup resolves the generation once, bulk-fetches
the required records with `MGET`, and projects them through the same registry SQL
expressions as the historical reader.
The old generation receives a one-hour TTL after publication so in-flight readers
can finish.
**Why:** (a) violates snapshot consistency. (c) makes Sift own whole Redis databases,
which is unsafe in a shared local instance and hides the key schema. The generation
pointer makes the publication boundary inspectable and atomic while keeping keys
namespaced. Redis earns its dependency by replacing the measured ~41-second Parquet
cross product with batched current-state lookups; Docker Compose captures the new
runtime requirement.
**Cost accepted:** online projection still invokes DuckDB so the registry's SQL text
remains the single feature definition. This may consume part of the 20ms lookup
budget; the new per-stage benchmark measures that cost before any optimization.

## D25 — ALS lands; exact search wins the current index gate   [accepted] (2026-07-26)

**Context:** Build step 5a trained the fixed D11 ALS configuration on 737,406
pre-T user/business interaction pairs (confidence = repeat count) over 213,961
users and the complete 14,568-business metro catalog. Retrieval must beat pre-T
popularity at recall@500. The architecture expected an ANN index, but dependencies
also have to pay rent.

**Measured choice:** ALS lands: recall@500 **0.2131 → 0.2519**; recall@10
0.0169 → 0.0350; NDCG@10 0.0122 → 0.0269. At @500 it uniquely recovers 13,928
held-out target pairs while popularity uniquely recovers 10,744. Use exact
full-catalog inner-product search now. The initial HNSW index reached only 0.889
mean exact overlap@500. A denser configuration still missed the 0.99 fidelity gate
and became slower than exact search; exact is ~1ms p99 at this catalog size.

**Ranker consequence:** D20 makes candidate source part of the ranker's training
contract. A separate ALS-conditioned artifact/model was built (3.389M rows,
677,864 groups; best validation NDCG@10 0.6977). It did not land: on the frozen
holdout it changed ALS recall@10 0.0350 → 0.0334 and NDCG@10 0.0269 → 0.0260.
The API therefore serves ALS's own order; popularity is the explicit cold-user
fallback. Both ranker variants remain runnable.

**Serving and definition seam:** `user_embedding_behavioral_v1` and
`item_embedding_behavioral_v1` are registered vector feature definitions (D12).
The online user vector is published inside Redis's generation switch and checked
coordinate-by-coordinate against its Parquet artifact. Warm online p99 is 2.690ms
(Redis vector lookup + exact retrieval); in-process FastAPI p99 is 3.634ms.

**Scale trigger:** revisit ANN only when exact retrieval materially approaches the
30ms stage budget or catalog/memory growth makes the full scan costly. At that
point the replacement must pass exact overlap@500 and end-to-end retrieval recall,
not merely return neighbors quickly.

## D26 — The fixed two-tower does not replace ALS   [accepted] (2026-07-27)

**Context:** D11 permits one two-tower attempt after ALS, gated strictly on beating
ALS's 0.2519 recall@500. The architecture already selected PyTorch, a small MLP per
tower, 64-D outputs, and sampled-softmax; the remaining choices had to resolve
temporal inputs and negative-sampling bias without becoming a tuning sprint.

**Fixed design, no sweep:** learned 32-D user/item ID embeddings; user tower adds
`u_reviews_to_date`, `u_mean_stars_to_date`, and `u_days_since_last`, all read
right-exclusively at the positive event timestamp; item tower adds category
multi-hot, location, and price from the D21 quasi-static dimension. Each side uses
a 128-unit ReLU MLP and emits an L2-normalized 64-D vector. Five deterministic CPU
epochs optimize sampled-softmax over in-batch items plus 128 uniform full-catalog
negatives. A mixture logQ correction addresses popularity-biased in-batch sampling;
the complete pre-T CSR history masks known positives from the denominator.

**Temporal choice:** item aggregates were deliberately excluded. An item's value
as-of its own row can be from the future relative to another row for which it serves
as an in-batch negative (I21). Quasi-static item inputs make the shared item vector
valid for every query timestamp. User aggregates remain point-in-time because each
user vector belongs only to its own row.

**Result:** two-tower recall@500 is **0.2399**, below ALS at **0.2519**. It also
loses at recall@10 (0.0192 vs 0.0350) and NDCG@10 (0.0141 vs 0.0269). At @500,
14,065 target pairs are found by both, 10,706 only by the two-tower, 10,944 only by
ALS, and 53,804 by neither. Exact retrieval p99 is comparable (0.861ms two-tower,
0.967ms ALS), so latency cannot rescue the quality loss.

**Choice:** do not land it. ALS remains the Redis/FastAPI embedding and no
two-tower-conditioned ranker is trained—the candidate source did not pass its own
gate. Keep the deterministic training/evaluation path and versioned rejected
artifacts runnable and inspectable; do not tune, mine hard negatives, or run
ablations. PyTorch remains isolated to the explicitly attempted 5b offline stage.

## D27 — Retrieval's score reaches the ranker as time-sliced ALS state   [accepted] (2026-07-27)

**Context:** The ranker's eight features contained no retrieval score, so the ALS-conditioned model was asked to reorder ALS's top-500 while blind to the signal that produced that order — it could only depart from a strong ranking using weaker information, which is why it *lowered* recall@10 (0.0350 → 0.0334) and did not land. `ARCHITECTURE.md`:84 already lists "retrieval source" among the planned user x item features; it had never been built.
**The trap:** the obvious implementation leaks catastrophically. The headline ALS artifact is fit on every pre-T interaction, so its score for a pair it was fit on *reports* that the pair was observed rather than predicting it. Measured: observed pairs mean 0.4555 vs 0.0047 unobserved, and an observed pair outranks an unobserved one **98.7%** of the time. Every training positive is an observed pair and every sampled negative is not, so the feature would be the label in disguise — spectacular offline numbers, and at serving the top-scoring items are businesses the user has *already reviewed*.
**Options:** (a) yearly ALS slices; (b) one earlier cutoff with training rows restricted after it; (c) skip the score and build the categorical "retrieval source" instead.
**Choice:** (a). One ALS per yearly boundary from 2010, each fit only on interactions strictly before it, stored as ordinary timestamped entity state. The store's existing right-exclusive ASOF join then selects the slice: a row is always scored by a model that never saw it. This is the same "snapshot at partition boundaries, read as-of" pattern the state groups already use — the vectors are just a heavier payload — so it needed no new machinery in the read path.
**Why not (b) or (c):** (b) cuts training rows to ~12% of the set, too little to judge the feature. (c) is near-constant with one live source and defers the problem rather than solving it.
**Costs, accepted:** a row is scored by a model up to a year old, and serving (as-of T) uses the 2018 slice while retrieval itself uses the full pre-T model. One uniform right-exclusive rule for every state group beats a special case that would buy a fresher score — staleness is safe, leakage is not. Rows before 2010 get NULL, which is honest: no model existed yet. Nine ALS fits, ~48s total.
**Result: the ranking stage pays rent for the first time on ALS candidates.** recall@10 0.0350 → **0.0386**, recall@100 0.1170 → **0.1264**, NDCG@10 0.0269 → **0.0294**; recall@500 unchanged at 0.2519 as it must be. `ui_als_score` is the second-strongest feature by gain (422K vs `i_reviews_to_date`'s 1.35M) — substantial but not dominant, which is what a non-leaky retrieval score should look like. In-group validation NDCG@10 is 0.7041, nowhere near the ~1.0 an oracle feature would produce.
**Servable as of 800c69d (was "not yet servable").** `user_als` and `item_als` joined `ONLINE_STATE_GROUPS`, so `online_features()` now returns all nine features and the skew check passes over the full set. The rule this paragraph originally asserted still holds and is worth keeping stated: training may use the full registry, but shipping a model that reads a feature serving cannot supply is skew by construction — that is what gated this decision, and it gated it correctly.

**Serving as of 4132a6f.** `retrieval/online.py` matches this decision's offline path candidate-for-candidate. Wiring it exposed a per-thread item-ALS cache that failed under concurrency; I31 replaced that with one shared database, immutable catalog relations per generation, cursor-local request state, and bounded intra-request parallelism. The ranker now serves, while the load benchmark keeps the latency result tied to an explicit concurrency and host.

## D28 — The latency budget is re-baselined against measurement, and gains a concurrency level   [accepted] (2026-07-30)

**Context:** `ARCHITECTURE.md` set the per-stage allocation in build step 1 — retrieval ≤ 30ms, online feature lookup ≤ 20ms, ranker inference ≤ 30ms, rerank + overhead ≤ 20ms — and labelled it "initial allocation, to be revised against measurement". It was written before ALS state was a feature, before any stage had been timed, and it sums to exactly 100ms: apportioned, not observed. With the ranker now actually serving (I30), every stage has been measured through uvicorn and the allocation is wrong in a specific way.

**What measurement showed.** Two stages were over-allocated by roughly an order of magnitude, and the one doing the work was starved:

| stage | allocated | measured (conc 1) | measured (conc 4) |
|---|---:|---:|---:|
| retrieval (Redis vector + exact search) | ≤ 30ms | ~2.6ms p50 / 5.7ms p99 | — |
| online feature lookup | ≤ 20ms | 18.7ms p50 / 31.0ms p99 | 34.5ms p50 / 55.0ms p99 |
| ranker inference | ≤ 30ms | ~2.6ms p50 / 3.8ms p99 | — |
| rerank + overhead | ≤ 20ms | not built | not built |
| **end-to-end** | **< 100ms p99** | **42.8ms p99** | **69.2ms p99** |

**The structural defect, which matters more than the numbers:** the old budget named no concurrency level, and a per-stage millisecond figure without one is not a contract. The same unchanged code measures 18ms p50 and 162ms p50 depending only on offered load (I31) — so "feature lookup ≤ 20ms" was satisfiable and violable simultaneously, and for months the only figures on record came from single-threaded loops that could not observe the difference.

**Options:** (a) re-baseline the allocation against measurement and state a concurrency envelope; (b) keep the original allocation and drive feature lookup under 20ms first; (c) drop per-stage lines and hold only the end-to-end p99.

**Choice: (a).** Revised, and this supersedes `ARCHITECTURE.md`'s initial allocation:

- retrieval, including the user-embedding lookup — **≤ 10ms**
- online feature lookup — **≤ 60ms**
- ranker inference — **≤ 10ms**
- rerank + overhead — **≤ 20ms** (unchanged; unbuilt, so unmeasured)
- **end-to-end p99 < 100ms at up to 4 concurrent requests per process**, the figure that was always the real contract, and now with the envelope it was always missing.

**Why not (b):** it optimises a stage that is not the bottleneck against the contract that binds — `AGENTS.md` explicitly forbids that — and it would have kept the API serving a displaced model while the work proceeded. **Why not (c):** the per-stage lines earned their keep. The 20ms allocation is the only reason I29's regression from 2ms to 49ms was ever noticed; an end-to-end number alone looked fine throughout. Deleting the tripwire because it fired is the wrong lesson.

**Costs, accepted.** Feature lookup now owns 60% of the budget, which looks lopsided for a stage that is mostly JSON marshalling rather than computation — ~1000 Redis records decoded in Python, re-encoded, and re-parsed by DuckDB per request. That is a known-addressable cost, not a fundamental one (I31 names the fix: item and business current state is catalog-wide and immutable within a generation, so only the three user-side records genuinely vary per request). The allocation reflects where the work is today, not where it should end up. Second cost: the concurrency envelope is modest, and it is hardware-specific — 4 performance cores. A single 0.5 vCPU Fargate task will be tighter, so the AWS validation run must report its own measured numbers rather than inheriting these.

**Not a weakening of the contract.** End-to-end p99 < 100ms is unchanged and met with 31ms of headroom at the stated envelope. The reallocation moves budget *from* two stages measured at ~15% of their allocation *to* the one that was starved, which is what "to be revised against measurement" asked for. What changed is that the numbers are now measured through the transport the service actually uses, at a stated load, instead of apportioned in advance.

**Corrected the same day, by the tool written to check it (`python -m sift.api.bench`).** The decision stands; three of its numbers did not, and the errors are worth keeping visible because each is a distinct measurement mistake.

*The ranker allocation came from the wrong instrument.* The ≤10ms above was derived from ~2.6ms measured **before** LightGBM was pinned to one thread. Pinning costs 4.3x per request — 1.39ms p50 all-cores vs 6.01ms pinned — and that is the price paid for the concurrency fix, which was worth it but never re-measured per stage. Through the server the stage is ~10ms p50. This is exactly the failure D28 was written about: after changing the code I re-measured only feature lookup and the total, and reused a stale per-stage number for the rest.

*Per-stage p99s are not additive, so a budget summing to 100ms was confused from the start.* At concurrency 4 the stage p99s total ~103ms while end-to-end p99 is ~79ms, because each stage's unluckiest 1% are mostly different requests. The original 30/20/30/20 = 100 encoded an arithmetic that does not hold; carrying that structure forward into a "corrected" allocation would have preserved the bug. **Stage lines and the end-to-end contract are now independent, and stated at different concurrencies because they do different jobs:** stage lines are regression tripwires, calibrated at concurrency 1 where numbers are reproducible and comparable across machines; the end-to-end contract is a serving promise, stated at the envelope. They are not required to sum.

**Revised, superseding the allocation above:**

- stage tripwires, at **concurrency 1** — retrieval ≤ 10ms, feature lookup ≤ 40ms, ranking ≤ 15ms, rerank + overhead ≤ 20ms
- the contract — **end-to-end p99 < 100ms at up to 4 concurrent requests per process**

*And the headroom claim was one lucky run.* "69.2ms p99, 31ms of headroom" came from 300 samples. At n=300 the p99 is the 3rd-worst request, so the same unchanged build measured 103ms (MISS) and 79ms (PASS) on consecutive runs. `bench` now defaults to 1,000 samples and **refuses to gate below that** rather than emit a verdict that flips on scheduling noise — the n=100 lesson from I31, re-learned one order of magnitude up.

**Validity limit, stated rather than buried.** These absolute numbers were taken on a developer desktop that is not a benchmark host: 1-minute load 8.6 against 4 performance cores, with the window server and an Electron app each taking ~40% CPU. On that machine the same build reported end-to-end p99 of 50ms and 99ms minutes apart — a spread wider than every optimisation in I31 combined. So: the *relative* results in I31 (3703ms -> 118ms) are safe, because effects that large survive the noise; the *absolute* comparison against 100ms is not settled here. `bench` now reads the host load average, labels a contended run, and refuses `--check` above half the logical cores. The binding validation is the AWS phase 7 run against the ALB, on hardware that does nothing else — which is the honest place to settle it, and one more reason that acceptance criterion exists.

**This tightens D25's ANN scale trigger, and D25 is not edited to say so** (this log is append-only). D25 says to revisit ANN "when exact retrieval materially approaches the 30ms stage budget"; retrieval's allocation is now 10ms, so the trigger moves with it. Exact search measures ~2.6ms p50 / 5.7ms p99 including the Redis vector lookup, so it still sits at roughly half the new allocation and ANN still pays no rent — the seam is unchanged, only the threshold that would open it.

**Consequence for the AWS deployment.** `AWS_DEPLOYMENT_PLAN.md` Phase 0 required that the intended online path be the one the API actually serves, that required online features be servable, that correctness/skew tests pass, and that the current latency result be understood. All four now hold: the ranker serves through `sift.api.main:app`, `ui_als_score` is servable (I25), skew passes 500 pairs / 36,500 values, and the latency result is understood including its concurrency and host dependence. Ground rule 9 and acceptance criterion 8 are clear. The remaining I31 work is a measured, documented optimisation; the binding contract verdict belongs to the uncontended deployment-hardware run, not the development desktop.

## D29 — Rerank lands, and it lowers the headline metric on purpose   [accepted] (2026-07-30)

**Context:** Build step 6, the last stage of the funnel and the only one whose inputs are deliberately absent from training. `is_open` is the project's cleanest example of a signal that is legitimate online and unconstructible historically (D13); "already reviewed" is the same shape. Two questions were deferred to this step and explicitly marked *do not resolve silently*: I5 (repeats are valid holdout targets, but the planned filter makes them unreachable) and I12 (`is_open` removes ~a quarter of the catalog).

**Choice:** hard filters for closed and already-reviewed, then a cap of 2 per primary category, applied to the ranker's top 50 to produce the final 10. The cold-start popularity path is reranked identically — a closed restaurant is no better a recommendation for a user we know nothing about. Diversity defers rather than discards: capped candidates backfill in ranker order if the list would otherwise come up short, because a pass that returns 7 results instead of 10 is a worse failure than a monotonous 10.

**Measured on the frozen holdout, 26,489 users, each mechanism isolated:**

| variant | recall@10 | recall@50 | NDCG@10 | vs ranker |
|---|---:|---:|---:|---:|
| ALS -> ranker (no rerank) | 0.0386 | 0.0880 | 0.0294 | — |
| diversity cap only | 0.0379 | 0.0880 | 0.0291 | −1.8% |
| closed filter only | 0.0379 | 0.0814 | 0.0292 | −1.7% |
| repeats filter only | 0.0224 | 0.0620 | 0.0157 | **−42.0%** |
| all three — what serving does | 0.0228 | 0.0580 | 0.0164 | −41.0% |

**This is the first stage in the project that does not beat the thing before it, and it lands anyway.** That is a real departure from "nothing lands without beating its predecessor", so it needs the stronger justification: the drop is not the stage performing badly, it is the stage revealing what the previous number contained.

**The finding.** **949 of the ranker's 2,579 top-10 hits (36.8%) are businesses the user had already reviewed** — drawn from ~1.3% of the candidate pool. A repeat converts at roughly 28x the rate of an average candidate, because a return visit is the easiest thing in this dataset to predict. So more than a third of D27's headline recall@10 was credit for predicting that people go back to their regular places. True, and not discovery. The filter removes the densest slice of the model's success, which is why it costs 42% rather than the ~1.6% that repeats' share of *targets* would suggest.

**The reasoning error this corrects, recorded because it was recommended and accepted before it was checked.** The pre-measurement argument was: repeats occupy 13.0% of the served top-10 but only ~1.6% of holdout targets, so filtering frees 13% of slots to forfeit 1.6% of reachable targets — an 8x favourable trade. That is wrong, and wrong in a way that generalises: it treats the freed slots as converting at the average rate. The slots a good model puts at the top are by construction not average slots. **Share-of-slots and share-of-hits are not interchangeable, and substituting one for the other inverted this trade by more than an order of magnitude.** Any argument of the form "this frees X% of positions to lose Y% of targets" is incomplete until the conversion rate of those positions is measured (ISSUES.md I5).

**I12 resolved the other way, and also not as predicted.** Closed businesses are 27.6% of the catalog and 8.64% of holdout target pairs (7,732 of 89,519), which looked like an 8.64% ceiling loss. It costs 1.7%. They are 27.9% of the candidate pool but only 10.9% of the ranker's top-10 — the model had already learned much of the signal indirectly, since a closed business stops accruing reviews. The gap between "8.64% of targets become unreachable" and "1.7% of recall is lost" is the difference between a target existing and a model finding it.

**Both numbers are reported, permanently.** `python -m sift.rerank.evaluate` prints the unfiltered ranker row beside the filtered ones. The closed filter's cost is not ranking quality: `is_open` records whether a business trades in 2022 while the ground truth is 2019 behaviour, so a business visited in 2019 that has since closed is simultaneously a correct suppression today and a permanently unreachable target. Publishing only the filtered figure would let that dataset-vintage artifact read as a regression; publishing only the unfiltered one would overstate what the product returns.

**The frozen holdout is untouched.** D18 governs the ground truth and still counts repeats as valid targets; this is a serving decision reported alongside it, not a redefinition. Changing the holdout to exclude repeats or closed businesses would invalidate every number Sift has produced, and would also hide the finding — the 42% gap *is* the result.

**Serving-only state, and why it is not a feature.** The filters read `is_open`, categories, and reviewed history through their own store read rather than `FeatureQuery`. Keeping them off the feature path is structural, not conventional: the moment `is_open` can arrive as a feature it is one registry entry away from being trained on, which is exactly the leak D13 forbids. Neither input has a training-side counterpart, so both sit outside the skew check by construction — a property of the stage, not a gap in the check.

**One asymmetry, stated because it will look like a bug later.** Redis publishes reviewed history as-of the generation's `as_of` (2022-01-19), matching every other online record. The offline harness reads pre-T history instead. That is not skew: at 2022 the online set contains the post-T reviews that *are* the eval targets, so evaluating through it would filter away the ground truth and report recall near zero. Serving and eval legitimately disagree about when "now" is — the same reason `is_open` cannot be a feature at all.

**Costs, accepted.** Redis schema 5 -> 6 for the new `user_reviewed` group. The bump is additive but deliberate: a rerank-capable reader against a schema-5 generation would find no reviewed records, filter nothing, and serve people places they have already been with nothing raised. Converting silent degradation into a refusal is the entire job of that version. Rerank costs 4.8ms p99 at concurrency 1 (a ~51-key Redis read plus in-process filtering); the end-to-end contract still holds at 77.7ms p99 with 0/1000 breaches at concurrency 4. Rerank and overhead were one 20ms budget line while rerank was unbuilt and are now separate and separately measured — a shared line would have let overhead regress a hundredfold and still pass (D28's lesson, one stage further along).

## D30 — The headline metrics become a ratchet, not prose   [accepted] (2026-07-30)

**Context:** "Nothing lands without beating the thing before it" is the project's central rule, quoted in `ARCHITECTURE.md`, the README, and half the decision log. Nothing enforced it. Every landing number — ALS's 0.2519 recall@500, the two-tower's 0.2399, the ranker's 0.0386 recall@10 — existed only as prose here. The suite covered component properties and the metric *functions* but never the end-to-end number that decides whether a model ships, so a refactor could move any of them and the only defence was somebody remembering to re-run eval (I24). This session moved every one of those numbers, which is the argument for closing it now rather than later.

**The constraint that made it hard, and why the obvious fix is wrong.** The numbers come from the real Yelp dump, which is gitignored and must stay that way. A test asserting 0.2519 either cannot run on a clean checkout or reintroduces I1's hidden dependency on machine state. A synthetic fixture large enough to make ALS beat popularity by a stable margin would be asserting a property of the fixture, not of the model — I8 with extra steps. **Both obvious forms of "add a test" are worse than nothing, because both would look like coverage.**

**Options:** (a) assert the numbers in CI against a committed fixture; (b) assert them against the real data, skipping when absent; (c) treat the eval run as an artifact — record the numbers to a local ledger and have the entrypoints refuse to record a regression.

**Choice: (c).** `sift/eval/ledger.py` keeps `data/derived/eval_ledger.json` beside the model artifacts, gitignored for the same reason `data/RESULTS.md` is: these are dataset-derived metrics and Yelp's terms restrict publishing them. Every eval entrypoint diffs its reports against the ledger, prints the verdict, and exits non-zero on a regression. `--accept` records the new value deliberately.

**Why not (a) or (b):** (a) is the I8 failure mode — a green check that measures the fixture. (b) is worse than (a), because a skipped test is invisible: the run that most needs the assertion (a clean checkout, a fresh machine, CI) is exactly the run that silently skips it.

**The split this makes.** The *mechanism* is unit-tested with synthetic reports and runs anywhere; the *numbers* live where the data does. That is the same separation the project already draws between `DECISIONS.md` (public reasoning) and `data/RESULTS.md` (local numbers), applied to enforcement.

**Three properties, each pinned by a test, because each is a way this could pass while doing nothing:** a first run *establishes* a baseline and says so, since "no baseline" and "no regression" must not look alike; a regression is **not** written, because a ratchet fails by ratcheting the wrong way once — record a bad run and the next run is judged against it and the loss vanishes; and a changed `n_users` is **fatal rather than a regression**, immune to `--accept`, because D18 froze the holdout and if the eval set moved then an improvement is exactly as untrustworthy as a decline. That last one is a different failure from "the model got worse" and must not share an escape hatch with it.

**This does not make the rule absolute, and deliberately keeps the exception legible.** D29 landed rerank while lowering recall@10 by 41%, with an argument. `--accept` is how that is expressed: the flag exists so an exception is a decision someone takes and records here, rather than a default nobody notices. The failure message says so.

**Costs, accepted.** The ledger is local, so a fresh clone starts with no baseline and its first run establishes one — correct, but it means the guard protects a *machine's* history rather than the project's, and two developers can hold different baselines. That is the honest consequence of numbers that cannot be committed; the shared record stays `DECISIONS.md` and `data/RESULTS.md`, and this catches the case those two cannot: a change nobody thought to re-measure.

## D31 — Catalog-wide state is one record per generation, not one per business   [accepted] (2026-07-30)

**Context:** I31's remaining lever. A 500-candidate request fetched **1,003 Redis records** — 3 for the user, and 2 (item state, business dimension) for every candidate. Measured directly: the `mget` plus Python decode of those 1,003 records was **16.1ms of a ~19ms feature-lookup stage**. But 1,000 of them are identical for every request in a generation: item state is catalog-wide, and the business dimension is quasi-static by construction (D21). Only the three user-side records genuinely vary.

**Choice:** publish item state and the business dimension the way item ALS vectors already were (D27/I29) — one Redis record each per generation, parsed once per process into an immutable per-generation DuckDB relation. Redis schema 6 -> 7.

**Result, single-threaded:** feature lookup **19.3 -> 9.3ms p50** (32 -> 15ms p99); end-to-end **33.0 -> 22.4ms p50**.

**The result that matters more: the concurrency envelope doubled.** D28 stated the contract at up to 4 concurrent requests because 8 breached it. Measured now at 1,000 requests per level: concurrency 4 is 42.4ms p99 (was 82.2), and **concurrency 8 is 67.9ms p99 with 0/1000 over the contract, where it was 118ms with 57/300 over**. `SUPPORTED_CONCURRENCY` moves 4 -> 8. That is the honest payoff — the stage got faster, but what the deployment gets is twice the load at the same promise.

**Why the schema bump is not optional.** This one is *not* additive, unlike 5 -> 6. A schema-6 generation stores item and business state per business, so a schema-7 reader would find nothing under the catalog key, project an empty relation, and return NULL for every item-side feature — a request that succeeds and is silently wrong. The version is what converts that into a refusal; a runtime check further downstream would be looking for an absence it cannot distinguish from a genuinely empty catalog.

**Two things measurement caught that the design did not predict**, both recorded because the pattern is the point:

- **Rerank got *slower* at first (2.9 -> 5.0ms p50).** Reading `is_open` and categories for ~50 candidates from the DuckDB relation costs a scan of all 14,568 rows per request — worse than the 51 Redis reads it replaced. Fixed by materialising the same data as a plain dict once per generation, which makes the stage ~50 hash lookups: **1.4ms p50, better than before the change**. Moving data closer is not automatically faster; the access pattern decides.
- **1.1ms appeared in `overhead`.** Pinning the request's generation (D-I33) is a real Redis round trip and sat before the first timer, so it landed in the bucket that had meant "routing and serialization". It is now charged to retrieval. An unattributed millisecond is exactly what per-stage instrumentation exists to prevent, and `overhead` reading 0.01ms again is the check that it worked.

**Stage tripwires re-tightened with the stage.** Feature lookup's line moves 40 -> 30ms and rerank's 10 -> 8ms, each about 2x the measured p99. A 40ms tripwire on a 15ms stage would let it regress to 39ms unnoticed — which is precisely how I29's 2ms -> 49ms was caught, so leaving the line where it was would have retired the alarm that justified the work.

**Costs, accepted.** The process now holds the business catalog twice — once as a DuckDB relation for the feature join, once as a Python dict for rerank. That is a few MB against a 19.6MB ALS payload already resident, and the alternative was a per-request scan. Publication is also slightly slower and more memory-hungry, since three catalog records are assembled in memory before being written; at 14,568 businesses this is not close to mattering, and it is the same trade D27 already accepted for the ALS vectors.
