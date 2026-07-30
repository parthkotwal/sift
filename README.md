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
rerank      50 → 10       hard filters + diversity           recall@10, both ways
   ▼
response: 10 businesses   p99 < 100ms, budgeted per stage
```

Offline, a batch job builds point-in-time-correct training examples from the review log, trains retrieval embeddings and candidate-conditioned rankers on a frozen temporal split, and materializes features to both stores. Exact vector search won the current index gate: at 14,568 metro businesses it is ~1ms p99 and lossless, while HNSW missed the required overlap and paid no useful rent. ANN remains the scale-up seam, not a dependency carried for appearances.

## The two hard problems

- **Point-in-time correctness:** every feature on a training row is computed only from events strictly before that row's timestamp — enforced structurally (sandboxed feature compute, one as-of read path) and verified by a test that deliberately tries to leak and must fail the build.
- **Training/serving skew:** one feature *definition*, materialized two ways — historical as-of values to Parquet for training, current values to Redis for serving — so the two paths cannot silently disagree. The feature store is hand-built (not Feast) because owning that machinery is the point.

## Build order

Backwards, one stage at a time: dumb popularity baseline end-to-end first, then the eval harness, then ranking, then the feature store, then learned retrieval (ALS first; a two-tower lands only if it beats ALS at recall@500), then rerank, then scale. Nothing lands without beating the thing before it — with one deliberate, documented exception.

**The exception is rerank (D29)**, which lowers recall@10 by 41% and lands anyway. The rule exists to stop a change being kept because it feels better; it is not a rule that the final number must always rise. Rerank's drop is almost entirely the already-reviewed filter removing credit the ranker was earning for predicting return visits, and knowing that is worth more than the number it cost. An exception that has to be argued in the decision log is the rule working, not the rule being broken.

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
uv run python -m sift.retrieval.als_slices
uv run python -m sift.retrieval.train_ranker
uv run python -m sift.store.online        # current state, ALS vectors, reviewed history -> Redis
uv run python -m sift.store.skew          # Redis == Parquet as-of-now
uv run uvicorn sift.api.main:app --reload
```

Redis carries a schema version, and the client refuses a generation written under an
older one rather than degrading quietly — a rerank-capable reader against a pre-rerank
generation would find no reviewed history, filter nothing, and serve people places they
have already been with no error raised. Re-run `sift.store.online` after pulling.

`GET /recommend?user_id=<id>&k=10` reads the versioned user embedding from Redis,
performs exact ALS retrieval over the full metro catalog to get 500 candidates, orders
them with the ALS-conditioned LightGBM ranker, and reranks the top 50 down to 10 with
hard filters and a category-diversity cap. Users absent from the ALS artifact fall back
to pre-T popularity — reranked the same way, since a closed restaurant is no better a
recommendation for a user we know nothing about.

The rerank stage is the one that *lowers* the headline metric, and finding out why was
the point of building it: **949 of the ranker's 2,579 top-10 hits (36.8%) were
businesses the user had already reviewed**, from ~1.3% of the candidate pool. Filtering
them costs 42% of recall@10 — more than a third of the pre-rerank number was predicting
return visits rather than discovery. The filter that looked expensive (closed businesses
are 8.64% of holdout targets) costs 1.7%. Both numbers are reported side by side;
`.agents/DECISIONS.md` D29 has the reasoning.

The ranker earns that position only because retrieval's own score now reaches it as
time-sliced ALS state: with that feature it beats ALS's raw ordering (NDCG@10
0.0269 → 0.0294); without it, it lost. The naive version of the same feature leaks
the label outright, so the slices are what make it usable — see `.agents/DECISIONS.md`
D27.

The catalog-wide ALS state is shared once per process, while request-local DuckDB
relations remain isolated per worker. The load benchmark checks the stated
four-request concurrency envelope and refuses a verdict on a contended host; the
binding result comes from deployment hardware rather than inheriting a noisy desktop
measurement (`.agents/ISSUES.md` I31).

To check the latency contract, drive the running endpoint at a stated concurrency:

```bash
uv run python -m sift.api.bench --concurrency 4 --check
```

It reports per-stage p50/p95/p99 from the response's own breakdown plus client-side
wall time, and gates on the budget. It also refuses to produce a verdict it cannot
support — under 1,000 samples, or on a host busy enough that the numbers describe the
machine rather than the code. Point `--url` at any deployment to measure it the same way.

For the in-process per-stage profile of a single warm thread:

```bash
uv run python -m sift.retrieval.online --samples 500
```

That loop is single-threaded, so it profiles one thread rather than the server — I31
is the record of what that hides, and why the benchmark above exists.

Set `SIFT_REDIS_URL` to use a non-default Redis endpoint.

## Agent workflow

Claude Code and Codex review each other's commits automatically; see
[scripts/README.md](scripts/README.md).
