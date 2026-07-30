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

`GET /recommend?user_id=<id>&k=10` reads the versioned user embedding from Redis,
performs exact ALS retrieval over the full metro catalog to get 500 candidates, and
orders them with the ALS-conditioned LightGBM ranker. Users absent from the ALS
artifact fall back explicitly to pre-T popularity, unranked.

The ranker earns that position only because retrieval's own score now reaches it as
time-sliced ALS state: with that feature it beats ALS's raw ordering (NDCG@10
0.0269 → 0.0294); without it, it lost. The naive version of the same feature leaks
the label outright, so the slices are what make it usable — see `.agents/DECISIONS.md`
D27.

**This path is not yet within its latency contract.** Single-threaded it is
comfortable, but the online store caches catalog-wide ALS vectors per *thread* while
the endpoint runs on a threadpool, so p99 degrades badly under concurrency
(`.agents/ISSUES.md` I31). The measurement is the deliverable here, including when it
says no.

For a latency distribution rather than one request:

```bash
uv run python -m sift.retrieval.online --samples 500
```

Note that this is a single-threaded loop against a warm process, so it profiles one
thread rather than the server — I31 is the record of what that hides.

Set `SIFT_REDIS_URL` to use a non-default Redis endpoint.

## Agent workflow

Claude Code and Codex review each other's commits automatically; see
[scripts/README.md](scripts/README.md).
