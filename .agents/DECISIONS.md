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
