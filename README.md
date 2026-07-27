# Sift

A two-stage retrieval and ranking service over the Yelp Open Dataset: given a user, return ten businesses they'll like — chosen from every business in the metro — in under 100ms p99.

Scoring 150K businesses with a good model takes seconds, so the work is split into a funnel of stages with different jobs and different metrics. The interesting parts are the internals: a hand-built feature store with point-in-time correctness, staged evaluation, and per-stage latency budgets — not the endpoint.

## The funnel

```
request: user_id, location
   │
   ▼
retrieval   14.6K → ~500  exact ALS dot product (ANN at scale) recall@500
   ▼
ranking     500 → 50      LightGBM on user×item features     NDCG@10
   ▼
rerank      50 → 10       hard filters + diversity
   ▼
response: 10 businesses   p99 < 100ms, budgeted per stage
```

Offline, a batch job builds point-in-time-correct training examples from the review log, trains retrieval embeddings and candidate-conditioned rankers on a frozen temporal split, and materializes features to both stores. Exact vector search won the current index gate: at 14,568 metro businesses it is ~1ms p99 and lossless, while HNSW missed the required overlap and paid no useful rent. ANN remains the scale-up seam, not a dependency carried for appearances.

## The two hard problems

- **Point-in-time correctness:** every feature on a training row is computed only from events strictly before that row's timestamp — enforced structurally (sandboxed feature compute, one as-of read path) and verified by a test that deliberately tries to leak and must fail the build.
- **Training/serving skew:** one feature *definition*, materialized two ways — historical as-of values to Parquet for training, current values to Redis for serving — so the two paths cannot silently disagree. The feature store is hand-built (not Feast) because owning that machinery is the point.

## Build order

Backwards, one stage at a time: dumb popularity baseline end-to-end first, then the eval harness, then ranking, then the feature store, then learned retrieval (ALS first; a two-tower lands only if it beats ALS at recall@500), then rerank, then scale. Nothing lands without beating the thing before it.

The fixed two-tower has now been run through that gate and did not replace ALS.
Its code and versioned artifacts remain reproducible for inspection; the selected
online path is unchanged.

## Run locally

Python dependencies are locked with `uv`. LightGBM also requires OpenMP on macOS
(`brew install libomp`). Redis is deliberately ephemeral: Parquet is the source of
truth, and the online store is a rebuildable serving materialization.

```bash
uv sync
docker compose up -d redis
uv run python -m sift.store.materialize   # historical timelines -> Parquet
uv run python -m sift.retrieval.interactions
uv run python -m sift.retrieval.als
uv run python -m sift.store.online        # current state + ALS user vectors -> Redis
uv run python -m sift.store.skew          # Redis == Parquet as-of-now
uv run uvicorn sift.api.main:app --reload
```

`GET /recommend?user_id=<id>&k=10` reads the versioned user embedding from Redis
and performs exact ALS retrieval over the full metro catalog. Users absent from the
ALS artifact fall back explicitly to pre-T popularity. The ALS-conditioned
LightGBM ranker remains runnable but does not serve: it lowered NDCG@10 relative to
ALS's own order, so it failed the model gate.

For a latency distribution rather than one request:

```bash
uv run python -m sift.retrieval.online --samples 100
```

Set `SIFT_REDIS_URL` to use a non-default Redis endpoint.
