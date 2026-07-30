# ARCHITECTURE.md — what Sift is and how it's built

## One line

A two-stage retrieval and ranking service over the Yelp Open Dataset: given a user id, return ten businesses they'll like, chosen from every business in the metro, in under 100ms p99.

## Why it's a system and not a model

Scoring 150K businesses with a good model takes seconds. The work is therefore split into stages with different jobs, different models, and different metrics — a funnel that spends compute where it matters. That staged architecture is the mechanism; everything else (the feature store, the offline path, the eval harness) exists to serve it.

**The tells that the framing is right:**

- Every stage is inspectable — a candidate list you can eyeball, a feature row you can print, a latency histogram per stage. Any component that can't be explained out loud doesn't belong in the build.
- Models pay rent. Nothing lands without beating the thing it replaces on the frozen eval. The retrieval model, the ranker — each is swappable behind its interface, and the dumb baseline it displaced remains runnable.

What it is *not*: a model-experimentation project (no tuning sprints, no ablations), a batch-platform project (that framing was superseded — see `DECISIONS.md` D10), or a product (UI stays trivial).

## Serving path (the funnel)

```
request: user_id, location
    │
    ▼
 RETRIEVAL    14.6K → ~500   exact search over ALS embeddings;   metric: recall@500
                              ANN is the measured scale-up seam
    │                        unioned with cheap heuristics
    │                        (popular nearby, category match)
    ▼
 RANKING      500 → 50       LightGBM over rich user×item         metric: NDCG@10
    │                        features from the online store
    ▼
 RERANK       50 → 10        hard filters (open now, already      metric: recall@10,
    │                        reviewed) + category diversity       reported both ways
    ▼
response: 10 businesses      end-to-end p99 < 100ms
```

**Retrieval** narrows the full catalog to ~500 candidates by dot product over ALS
user/item embeddings. The current Philadelphia catalog is 14,568 items, where
exact NumPy scoring is ~1ms p99 and lossless; HNSW failed its 0.99 exact-overlap
gate before it could earn the dependency (D25). ANN remains the same index
interface's scale-up option. Heuristic sources may be unioned later, with marginal
hit contribution measured so no source rides for free.

**Ranking** scores those ~500 with a gradient-boosted model over features the retrieval stage can't afford: distance, category affinity, review velocity, rating trend, price match, plus which retrieval source produced the candidate. Cuts to 50.

**Rerank** applies hard filters — `is_open` *now* (a serving-time filter, deliberately never a training feature — see the skew section), already-reviewed — and a category-diversity cap to reach the final 10. Built in D29; both of its inputs are serving-only state with no training-side counterpart, which is why they reach the stage through their own store read rather than the feature path.

It is the one stage that *lowers* the headline metric, and measuring why produced the sharpest result in the project so far: **949 of the ranker's 2,579 top-10 hits (36.8%) were businesses the user had already reviewed**, drawn from ~1.3% of the candidate pool. Filtering them costs 42% of recall@10 — not because the filter is wrong, but because more than a third of the pre-rerank number was predicting return visits rather than discovery. The closed-business filter, which looked like the expensive one (8.64% of holdout targets are now-closed businesses), costs 1.7%; the diversity cap costs 1.8%. Both the filtered and unfiltered figures are reported side by side so a dataset-vintage artifact cannot read as a ranking regression (`ISSUES.md` I5, I12).

**Latency budget** (re-baselined against measurement, `DECISIONS.md` D28). Two different things, deliberately not one:

- **Regression tripwires**, at concurrency 1 where numbers are reproducible: retrieval ≤ 10ms, online feature lookup ≤ 40ms, ranker inference ≤ 15ms, rerank ≤ 10ms, overhead ≤ 5ms. (Rerank and overhead shared one 20ms line while rerank was unbuilt; now both are measured — 4.8ms and 0.02ms p99 — a shared budget would have let overhead regress a hundredfold and still pass.)
- **The contract**: end-to-end **p99 < 100ms at up to 4 concurrent requests per process**.

They are not required to sum, because per-stage p99s are not additive — at concurrency 4 the stage p99s total ~103ms while end-to-end p99 is ~79ms, since each stage's unluckiest 1% are mostly different requests. The original 30/20/30/20 = 100ms allocation encoded that arithmetic error, and was apportioned before anything was timed.

Instrumented per stage from day one: an end-to-end number with no breakdown is a footgun (`AGENTS.md`), a per-stage number with no concurrency level is not a contract (the same code measured 18ms and 162ms p50 on offered load alone), and a p99 over too few samples on a loaded host is not a measurement (the same build gave 50ms and 99ms minutes apart at load 8.6 on 4 cores). `python -m sift.api.bench --check` is what verifies all three — it gates on ≥1,000 samples and refuses to gate on a contended host. See `ISSUES.md` I31.

## Offline path

A batch job (plain scheduled scripts — no orchestrator, a deliberate cut, `DECISIONS.md` D10) that:

1. Builds training examples from the review log with **point-in-time-correct** features (positives from real events, negatives sampled — see below).
2. Trains the retrieval embedding model and the LightGBM ranker on a temporal split (train before date T, evaluate after) that is **frozen in build step 1 and never touched again**.
3. Exports item embeddings and validates the retrieval index against exact search;
   the current index is exact, with ANN deferred until catalog scale makes it pay.
4. Materializes features to both stores (historical → Parquet, current → Redis).

Raw-data hygiene from the earlier framing survives underneath: raw JSON lands immutably as date-partitioned Parquet, and one canonical event table (`user_id, entity_id, event_type, ts, payload` — `DECISIONS.md` D2) feeds all feature computation. Offline jobs are partition-wise and idempotent (re-run = identical output).

## The feature store

One feature **definition**, materialized two ways:

- **Historical:** values as-of every needed past timestamp → Parquet, for training.
- **Online:** current values → Redis, keyed by entity id, for serving.

Hand-built (definitions module, materialization job, lookup client), not Feast — ownership is the point (`DECISIONS.md` D9). The problem it exists to solve is **training/serving skew**: the failure mode where the offline and online computations of "the same" feature silently disagree, and model quality degrades with no error surfaced. One definition, two materializations, is the cure — plus a recurring check that compares Redis values against Parquet as-of-now for sampled entities and alerts on mismatch.

**Definitions are the seam.** A definition declares name, entity type, dtype/shape, as-of semantics, version, and a compute function. A windowed count is a definition whose compute is an aggregation; **an embedding is a definition whose compute is a versioned model artifact** (`item_embedding_behavioral_v1`). The stores serve bytes and don't care. This is the extension seam: a new embedding — a stronger behavioral model, or a content/semantic vector produced elsewhere — registers as another definition (`item_embedding_semantic_v1`) alongside the first, consumed by the index or ranker via config, no refactor (`DECISIONS.md` D12). Consumers reference definitions by name+version only; nothing computes features inline.

## The spine: point-in-time correctness (and its sibling, skew)

Every feature attached to a training row is computed only from events **strictly before** the row's timestamp. The canonical failure: attach a business's average rating to a 2019 training row, computed over all of history — it contains the very review being predicted. Nothing crashes; offline metrics inflate; the model is garbage.

Enforcement is structural — four chokepoints, not discipline:

1. **Sandboxed compute.** A definition's compute function never touches raw tables; the framework hands it a pre-filtered view of events `< cutoff`. A definition physically cannot see the future.
2. **One read path.** Training assembly obtains features only via `store.get_asof(entity, name, ts)` (right-exclusive); a test asserts training modules import no raw readers. The as-of join exists in exactly one place.
3. **Future-invariance property test.** Compute features as-of `t` on synthetic events; mutate only events after `t`; recompute — every value must be identical. **The leak test:** CI registers a deliberately leaking definition (full-history aggregate) and asserts this test fails it. It is never weakened to make a build pass.
4. **Snapshot blocklist.** No feature may source from the dump-time snapshot columns (`business.stars`, `review_count`, user lifetime stats, vote counts — full list in `DATA.md`); asserted by a schema test. And `is_open` is a rerank filter, never a model feature — the cleanest example of a signal that's legitimate online and unconstructible historically (`DECISIONS.md` D13).

## Ranker features (v1 plan)

As-of `t`, right-exclusive windows. Hazard taxonomy lives in `DATA.md`.

- **User:** reviews in 30/90d and to-date; days since last review; mean stars given to-date; category-affinity shares; price-tier mix; activity centroid.
- **Item:** reviews in 7/30/90d; review velocity (30d vs prior 30d); mean stars received to-date; rating trend (30d − lifetime, both to-date); check-ins and tips 90d; catalog age; category; price tier.
- **User×item:** centroid-to-business distance; affinity·category dot; price match; reviewed-before; retrieval source.

## Retrieval training and eval

- **Step one is ALS, not a neural net** (`DECISIONS.md` D11): ALS factors are a two-tower without the towers — user/item vectors whose dot product predicts interaction — so the whole serving path (index, union, eval) gets built on a model the author fully owns. The learned two-tower is an *upgrade* that must beat ALS at recall@500 to land; if it doesn't, ALS ships and the system is unchanged.
- **Two-tower result:** the fixed v1 uses 32-D learned IDs, point-in-time user features, D21-approved static item attributes, 128-unit MLPs, and 64-D normalized outputs. Its sampled-softmax candidates combine in-batch and uniform full-catalog negatives, apply logQ, and mask known positives. It scored 0.2399 recall@500 against ALS's 0.2519, so it does not land (D26). No tuning or hard-negative follow-up: that would turn the project into model experimentation.
- **Retrieval is evaluated against the full catalog** (did the ~500 contain the user's actual future interaction?), never with sampled negatives — retrieval's job is the catalog, and eval choice flips conclusions (a lesson learned the hard way). Report popularity vs. ALS vs. two-tower, plus per-source marginal contribution and index latency. ALS landed in D25; after time-sliced ALS state made its retrieval score point-in-time safe, the candidate-conditioned LightGBM ranker also landed in D27 and now reorders ALS's 500 candidates.

## Build order — backwards, one stage at a time

Each step ends with something that runs end to end; nothing lands without beating the thing before it.

1. **Dumb path:** FastAPI endpoint; retrieval = most-reviewed businesses in the city; no ML, no store. The temporal split (train < T, eval ≥ T) is defined here and frozen forever.
2. **Eval harness:** recall@k and NDCG@10 against the holdout + per-stage latency measurement. Every later change is judged against this.
3. **Ranking stage:** hand-computed features + LightGBM, reading features straight from Parquet at request time — deliberately wrong, to isolate whether ranking helps before adding storage.
4. **Feature store:** pull features behind definitions; materialize to Parquet and Redis; swap the service to Redis reads; land the leak test and the future-invariance test.
5. **Retrieval stage:** 5a — ALS embeddings → measured index, replace popularity retrieval, measure recall@500. Exact search won at current scale (D25). 5b — the fixed two-tower was measured and did not beat ALS (D26); ALS remains selected.
6. **Rerank:** filters + diversity. Built (D29). The only stage that lowers the headline metric, and the reason is the finding: a third of the ranker's top-10 hits were repeat visits, so the pre-rerank number was measuring return-visit prediction as much as discovery.
7. **Scale:** all metros; Spark for training-example generation **if and only if** candidate-pair volume warrants it (measured, not assumed).

## Stack

Python · PyTorch (two-tower, step 5b only) · LightGBM · `implicit` ALS · NumPy exact vector search (ANN only when scale earns it) · Redis (online features) · Postgres (metadata) · FastAPI · Parquet + DuckDB (offline, inspection) · Docker Compose. Deliberately small — every tool must be explainable out loud.

## Scope

**In:** staged retrieval/ranking, hand-built feature store with point-in-time correctness and a leakage test, staged offline eval, per-stage latency instrumentation, reproducible local deploy (`docker compose up`).

**Out:** UI beyond trivial, model experimentation/ablations, A/B infrastructure, streaming features, orchestrators, the social/people-matching layer.

## Extensibility: the embedding seam

Sift is standalone. Its one deliberate extension point is the feature-definition registry (D12): because embeddings are just definitions, any externally-produced vector — a stronger behavioral model, or a content/semantic embedding — registers as `item_embedding_semantic_v1` alongside the behavioral one and plugs into the same index/ranker sockets by config, no refactor. Nothing is built for that today; the seam simply costs nothing to keep open.
