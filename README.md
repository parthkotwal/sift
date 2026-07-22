# Sift

A two-stage retrieval and ranking service over the Yelp Open Dataset: given a user, return ten businesses they'll like — chosen from every business in the metro — in under 100ms p99.

Scoring 150K businesses with a good model takes seconds, so the work is split into a funnel of stages with different jobs and different metrics. The interesting parts are the internals: a hand-built feature store with point-in-time correctness, staged evaluation, and per-stage latency budgets — not the endpoint.

## The funnel

```
request: user_id, location
   │
   ▼
retrieval   150K → ~500   ANN over embeddings ∪ heuristics   recall@500
   ▼
ranking     500 → 50      LightGBM on user×item features     NDCG@10
   ▼
rerank      50 → 10       hard filters + diversity
   ▼
response: 10 businesses   p99 < 100ms, budgeted per stage
```

Offline, a batch job builds point-in-time-correct training examples from the review log, trains the retrieval embeddings and the ranker on a frozen temporal split, rebuilds the ANN index, and materializes features to both stores.

## The two hard problems

- **Point-in-time correctness:** every feature on a training row is computed only from events strictly before that row's timestamp — enforced structurally (sandboxed feature compute, one as-of read path) and verified by a test that deliberately tries to leak and must fail the build.
- **Training/serving skew:** one feature *definition*, materialized two ways — historical as-of values to Parquet for training, current values to Redis for serving — so the two paths cannot silently disagree. The feature store is hand-built (not Feast) because owning that machinery is the point.

## Build order

Backwards, one stage at a time: dumb popularity baseline end-to-end first, then the eval harness, then ranking, then the feature store, then learned retrieval (ALS first; a two-tower lands only if it beats ALS at recall@500), then rerank, then scale. Nothing lands without beating the thing before it.
