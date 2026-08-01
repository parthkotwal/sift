# Sift

A two-stage retrieval and ranking service over the Yelp Open Dataset: given a user, return businesses they'll like — chosen from every business in the metro — under a per-stage latency budget.

The contract, stated with the envelope it was measured at, because a bare p99 is not a claim: **warm server-side p99 under 100 ms at 20 requests/second with persistent connections**, measured on the AWS deployment (since destroyed). Fresh TLS connections and concurrency 4 did *not* meet it, and a cold process's first request was ~673 ms. Those are documented boundaries of a one-task showcase, not open defects.

Scoring 150K businesses with a good model takes seconds, so the work is split into a funnel of stages with different jobs and different metrics. The interesting parts are the internals: a hand-built feature store with point-in-time correctness, staged evaluation, and per-stage latency budgets — not the endpoint.

## The funnel

```
request: user_id, location
   │
   ▼
retrieval   14.6K → 500   exact ALS dot product (ANN at scale) recall@500
   ▼
ranking     500 → 500     LightGBM on user×item features     NDCG@10
   ▼          reordered, not cut
rerank      500 → k       hard filters + diversity           recall@10, both ways
   ▼
response: exactly k       server p99 < 100ms at the measured envelope
```

**Ranking narrows nothing.** It orders all 500 and hands the whole pool to rerank, which
is the only stage that removes anything. That is deliberate: ranking used to cut to a
fixed 50 first, which made the hard filters unable to fill a large `k` — a legal `k=50`
came back with 33–40 results and said nothing. Rerank now filters the full ranked pool and
returns **exactly k whenever k eligible candidates exist**, which the retrieved pool
always affords (D33).

Offline, a batch job builds point-in-time-correct training examples from the review log, trains retrieval embeddings and candidate-conditioned rankers on a frozen temporal split, and materializes features to both stores. Exact vector search won the current index gate: at 14,568 metro businesses it is ~1ms p99 and lossless, while HNSW missed the required overlap and paid no useful rent. ANN remains the scale-up seam, not a dependency carried for appearances.

## The two hard problems

- **Point-in-time correctness:** every feature on a training row is computed only from events strictly before that row's timestamp — enforced structurally (sandboxed feature compute, one as-of read path) and verified by a test that deliberately tries to leak and must fail the build.
- **Training/serving skew:** one feature *definition*, materialized two ways — historical as-of values to Parquet for training, current values to Redis for serving — so the two paths cannot silently disagree. The feature store is hand-built (not Feast) because owning that machinery is the point.

## Build order

Backwards, one stage at a time: dumb popularity baseline end-to-end first, then the eval harness, then ranking, then the feature store, then learned retrieval (ALS first; a two-tower lands only if it beats ALS at recall@500), then rerank, then scale. Nothing lands without beating the thing before it — with one deliberate, documented exception.

**The exception is rerank (D29)**, which lowers recall@10 by 41% and lands anyway. The rule exists to stop a change being kept because it feels better; it is not a rule that the final number must always rise. Rerank's drop is almost entirely the already-reviewed filter removing credit the ranker was earning for predicting return visits, and knowing that is worth more than the number it cost. An exception that has to be argued in the decision log is the rule working, not the rule being broken.

The rule is enforced, not just stated: every eval entrypoint diffs its numbers against a local ledger and exits non-zero on a regression rather than recording it, so an exception has to be taken deliberately with `--accept` (D30). The numbers stay out of git — they're dataset-derived and Yelp's terms restrict publishing them — so a fresh clone's first run establishes its own baseline and says so.

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
all of them with the ALS-conditioned LightGBM ranker, and reranks that whole ranked pool
down to k with hard filters and a category-diversity cap. Users absent from the ALS artifact fall back
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

That feature also closed the project's longest-running deferral. Every stage orders with
a stable sort, so candidates it scores identically keep the order the previous stage gave
them — a stage with no opinion between two candidates must not overwrite one that had
one. That was deliberately *not* done while the ranker produced 18 distinct scores over
500 candidates, because falling back to the incumbent would have reported the baseline's
ordering as the ranker's. Adding retrieval's score dissolved the ties: the same model now
produces a median of 500 distinct scores over 500, three users in 26,489 have a tie at
the top-10 boundary at all, and unifying the tie-break moved no metric. The lesson is in
D32 — a deferral justified by a number needs the number attached to something that
re-runs, or it goes stale without anything failing.

Everything catalog-wide — item state, the business dimension, and the ALS vectors — is
one Redis record per generation, parsed once per process into an immutable relation,
while request-local DuckDB relations stay isolated per worker. A 500-candidate request
therefore reads **3 Redis records rather than 1,003**, which was 16ms of a 19ms stage;
it is also what doubled the supported concurrency from four to eight (D31). The load
benchmark checks that envelope and refuses a verdict on a contended host; the binding
result comes from deployment hardware rather than inheriting a noisy desktop
measurement (`.agents/ISSUES.md` I31).

**Concurrency 8 is a desktop number, and the cloud disagreed** — the 2-vCPU Fargate task
met the contract through concurrency 2, not 8. That is not a regression; it is four
performance cores versus two vCPUs, which is exactly why the contract names a concurrency
instead of a bare p99. The desktop figure is kept as the regression profile, deliberately
not weakened until the cloud passed, because a tripwire retuned to whatever hardware last
ran it stops catching regressions.

To check the latency contract, drive the running endpoint at a stated concurrency:

```bash
uv run python -m sift.api.bench --concurrency 8 --check
```

It reports per-stage p50/p95/p99 and gates on the budget, and it separates three clocks
that answer different questions: the funnel's own `total_ms`, the whole server request
(`server_ms`, from the middleware — this is what the contract is asserted on), and client
wall time, which is reported but never asserted because through a load balancer it is
mostly geography. Asserting the funnel clock would have let a change "meet" the contract
by moving delay outside the timer (D34).

`--rate` offers a fixed arrival schedule instead of the default closed loop, where
throughput is `concurrency / latency` by construction and says nothing about capacity.
`?detail=true` returns opt-in sub-stage timings for locating a cost the five stages are
too coarse to explain.

It also refuses to produce a verdict it cannot support — under 1,000 samples, on a host
busy enough that the numbers describe the machine rather than the code, or against an
endpoint too old to report `server_ms`. Point `--url` at any deployment to measure it the
same way.

For the in-process per-stage profile of a single warm thread:

```bash
uv run python -m sift.retrieval.online --samples 500
```

That loop is single-threaded, so it profiles one thread rather than the server — I31
is the record of what that hides, and why the benchmark above exists.

Set `SIFT_REDIS_URL` to use a non-default Redis endpoint.

## Deployed to AWS, then torn down

**Nothing is running.** The showcase was deployed to AWS, measured, and destroyed on
2026-08-01 to control cost. `infra/terraform/` and the `Dockerfile` are kept so the
deployment is reproducible, not because it is live — every URL in the deployment history
is dead. Spend was a **modeled conservative upper bound of $5.62**, not a settled bill —
teardown-day charges post after billing-system delay, so the deterministic bound is the
honest figure rather than a Cost Explorer total that had not yet arrived. It deliberately
overstates, charging the full topology through the end of verification even though
services were deleted progressively. Ceiling was $10.

What ran: one 2-vCPU/4-GiB Fargate task behind an ALB, a private Valkey node, S3 for
immutable artifact generations, ECR, and CloudFront as an IP-restricted HTTPS front door,
with GitHub Actions deploying through OIDC rather than stored AWS keys.

**What the cloud measured, stated at the concurrency and connection model it was measured
at**, because those change the answer more than the code does:

| | server p99 | notes |
|---|---:|---|
| 20 req/s offered, connection reuse, via CloudFront | **95.41 ms** | 8/1000 over budget — passes |
| 20 req/s offered, fresh TLS per request, via CloudFront | 120.55 ms | 32/1000 over — the edge, not the app |
| 20 req/s offered, direct in-VPC control | **42.14 ms** | 0/1000 over, same image and users |
| closed loop, concurrency 4 | ~172 ms | over budget; 45 req/s achieved |
| first request in a fresh process | ~673 ms | vs ~40 ms warm |

The gap between 95 ms and 42 ms at the *same* offered rate is the public edge path, not
the service — the direct control used the identical image, users, and rate. And 20 req/s
is not a measured ceiling: it is the highest fixed rate that passed end-to-end. At 30
req/s queueing grew into seconds, so the real ceiling is somewhere between and was never
pinned down.

The most useful result was a falsified hypothesis. A second Uvicorn worker looked obvious
on a 2-vCPU task and made things **worse** (110 ms vs 72 ms p99 at concurrency 2). What
actually mattered was a native thread pool nobody had counted: NumPy's OpenBLAS was
choosing 2 threads underneath an already-pinned DuckDB and LightGBM. Setting
`OPENBLAS_NUM_THREADS=1` on a single worker cut concurrency-4 p99 from 317 ms to 174 ms —
a bigger win than adding a process, and in the opposite direction (D35).

**What this deployment is not:** one task with no high availability, an ephemeral
IP-restricted security posture rather than production hardening, HTTP on the internal
CloudFront-to-ALB hop, and accepted base-image CVEs in Debian's `perl-base`. Those were
deliberate choices for a one-day showcase and are recorded as accepted limitations in
`.agents/AWS_ISSUES.md`, not as unfinished work.
