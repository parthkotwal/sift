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
