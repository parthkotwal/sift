# CONCEPTS.md — the ideas Sift is built on

A living study guide. Each entry: what the idea is, why Sift needs it, and the question you should be able to answer about it cold. Agents: when a task touches a concept, link here; if the concept is missing, add a short entry as part of the task. Keep entries tight — this is for understanding, not exhaustiveness.

---

## Two-stage retrieval and ranking (the funnel)

**What:** The canonical industry recsys architecture. A cheap, fast model narrows the full catalog to hundreds of candidates (retrieval); an expensive, accurate model scores only those (ranking); business logic finishes (rerank). Exists because scoring the whole catalog with the good model is seconds-slow, and because the stages have different jobs: retrieval must not *miss* (recall), ranking must *order* (NDCG).

**Here:** 150K → ~500 (ANN ∪ heuristics) → 50 (LightGBM) → 10 (filters + diversity), each stage with its own metric and latency budget.

**Own it:** *Why can't one model do this, and why do the stages get different metrics?*

## Approximate nearest neighbor (ANN) search

**What:** Index structures (HNSW graphs, IVF cells) that find the ~k closest vectors to a query in sub-millisecond time by searching a tiny fraction of the space, trading a little recall for a lot of speed. Exact search over 150K×64 floats is a big dot-product; over 150M it's impossible — ANN is what makes embedding retrieval servable.

**Here:** the fidelity/latency comparison rejected ANN for the current 14,568-item metro catalog. Raw-inner-product HNSW returned only 0.889 mean overlap@500; a denser graph approached the 0.99 gate only by becoming slower than exact NumPy scoring. Exact search is therefore the current index, at ~1ms p99. ANN is retained as a scale-up concept and interface, not as an unused dependency.

**Own it:** *What does HNSW give up relative to exact search, and how would you measure whether it's hurting you?* (Compare ANN recall@500 against brute-force on a sample.)

The subtlety here is maximum inner product search: raw dot product is not cosine,
and it is not a metric. Normalizing vectors would change ALS's ranking, while a
graph built directly in inner-product space can have poor reachability. Always
validate the exact ranking the model means, rather than assuming the index's
distance label makes it equivalent.

## Embedding-based retrieval; ALS; two-tower models

**What:** Represent users and items as vectors such that dot product ≈ affinity; retrieval = nearest-neighbor search in that space. **ALS** (alternating least squares matrix factorization) learns these vectors from the interaction matrix alone. A **two-tower** network learns them from features (user tower, item tower), which lets unseen items get vectors — the main thing ALS can't do. Structurally they're the same interface: two vector families and a dot product.

**Here:** ALS first (D11) — the whole serving path built on a model the author owns; the two-tower is an upgrade gated on beating ALS at recall@500.

**The 5b implementation:** user tower = learned ID plus right-exclusive user state;
item tower = learned ID plus quasi-static category/location/price attributes. The
asymmetry is a temporal correctness choice. If an in-batch item's time-varying
aggregate were computed at that item's own event timestamp, it could be a future
value for an earlier query elsewhere in the batch. Keeping item inputs static makes
one encoded item valid for every query timestamp in that sampled-softmax matrix.

**Own it:** *In what precise sense is ALS "a two-tower without the towers," and what's the one capability the towers add?*

## Negative sampling (and in-batch negatives)

**What:** Ranking/retrieval models need examples of what users *didn't* choose. Since that's almost everything, you sample. Uniform random negatives are easy but mostly teach "popular beats obscure." **In-batch negatives** (each positive's item serves as a negative for the other users in the batch) are nearly free — but sample items proportional to popularity, over-penalizing popular items; the **logQ correction** (subtract log sampling probability from the logit) compensates.

**Here:** Ranker negatives are sampled at training-set assembly. The two-tower uses
in-batch items plus uniform full-catalog negatives, with logQ computed from that
mixture. Uniform candidates ensure businesses with no positive event are trained
against rather than retaining arbitrary random vectors. A separate mask removes
every known user-positive from the denominator except the row's target; otherwise
efficient negative sampling quietly manufactures false negatives (D26).

**Where logQ goes, exactly.** The correction comes from importance-weighting the
sampled denominator: `sum_{j in S} exp(s_j)/q_j = sum_{j in S} exp(s_j - log q_j)`.
`s` is the *whole* score function — for a temperature-scaled model that is
cosine/T — so `log q` is subtracted from the already-scaled logit, in nats. Doing
it before the division silently corrects for `q^(1/T)` instead of `q`; at T=0.07
that is a ~14x over-correction that lets the sampler dominate the objective. `q`
must also be the distribution the sampler *actually* draws from: here a
size-weighted mixture of popularity-proportional in-batch draws and uniform
catalog draws, whose uniform component is what keeps `log q` finite for an item
that never appears as a positive (I23).

**Own it:** *Why do uniform negatives make the model popularity-biased, and why do in-batch negatives have the opposite bias?* And: *your logits are `cos/T` and you
subtract `log q` — what changes if you subtract before dividing by T?*

## Candidate-conditioned training (train on the serving distribution)

**What:** A ranker in a funnel never sees the catalog — it sees whatever retrieval handed it. Its real job is the *conditional* question "given these k candidates, which does the user pick," so its training rows must be drawn from that same conditional. The classic failure is subtler than "different data": if positives and negatives come from different populations, the model can separate them by a property of the *sampling scheme* instead of by user preference, and that property may point the opposite way at serving time. The tell is a feature whose learned response is confident in a range the serving inputs never occupy.

**Here:** D19 paired positives (any pre-T review) with negatives from the popularity top-500 — 39% vs 100% pool membership, so in 72.5% of groups the positive was the *less* popular item and the model learned popularity inverted. D20 restricts positives to the pool, making the training question identical to the serving question. Note the contrast with the negative *pool* being frozen as-of T (D19), which is fine: a sampling distribution is not a model input, so it cannot leak. Which side of that line a choice falls on is the thing to get right.

**Own it:** *Your ranker scores below the baseline it reorders. Name two distinct causes — one in the sampled data, one in the feature set — and the measurement that separates them.*

## Point-in-time correctness / leakage

**What:** Features attached to a training row must reflect only what was knowable before that row's timestamp. Violating this — leakage — lets the model peek at the future, often at the very label it predicts. Nothing errors; offline metrics inflate; production collapses.

**Here:** The spine. Canonical failure: business average rating attached to a 2019 row, computed over all history — it contains the review being predicted. Enforced structurally via four chokepoints (`ARCHITECTURE.md` → "The spine"): sandboxed compute views, one `get_asof` read path, the future-invariance test, the snapshot blocklist.

**Own it:** *Tell the average-rating story from scratch, then explain why the enforcement is structural rather than "being careful."*

## As-of join

**What:** For each left row at time `t`, join the single right-side snapshot with the greatest timestamp `< t` — the temporal analogue of "latest version before the cutoff." Right-exclusive, because same-instant data may already contain the event being predicted.

**Here:** The only legal way training rows meet features: `store.get_asof(entity, name, t)`, implemented once.

**Own it:** *Why `< t` and not `≤ t`?*

## Training/serving skew

**What:** The offline computation of a feature (for training) and the online computation (for serving) drift apart — different code paths, different data freshness, different edge-case handling. The model was trained on values it never sees in production; quality degrades with no error surfaced. One of the most common real-world ML failures.

**Here:** The feature store's reason to exist: one *definition*, materialized to Parquet (historical, as-of) and Redis (current). Plus a recurring check comparing the two for sampled entities. Corollary rule: a signal that's legitimate online but unconstructible historically (`is_open`) is skew by definition → serving-time filter, never a feature (D13).

The Redis refresh is a snapshot publication, not a stream of independently visible
record updates: a complete generation is written under new keys, then one active-
generation pointer is changed atomically. A request resolves that pointer once, so
it cannot combine half-old user state with half-new item state. Old generations live
for a grace period so an in-flight reader that resolved the previous pointer can
finish safely.

**Own it:** *Give one concrete way skew arises even when both paths are "correct," and how one-definition-two-materializations prevents it.*

## Feature store

**What:** A system that computes, versions, and serves features consistently between training and serving, keyed by entity and time. Its two jobs are exactly Sift's two named hazards: point-in-time correctness (training side) and skew prevention (serving side).

**Here:** Hand-built (D9): a definitions module (name, entity, dtype/shape, as-of semantics, version, compute fn), a materialization job, a lookup client. **Embeddings are definitions too** (D12) — compute fn is a versioned model artifact — which is what makes the store an extension seam: any externally-produced embedding plugs in as another definition (D12).

**Own it:** *What does a feature store solve that "a table of features" doesn't?* (Time, and train/serve consistency.)

## Temporal splits; recall@k; NDCG

**What:** Evaluate by training strictly on the past and testing on the future — random splits leak temporal information and flatter every model. **recall@k**: of the user's actual future interactions, what fraction appeared in the top-k? Measures *not missing* — retrieval's metric, with large k. **NDCG@k**: rewards putting the true items near the top with logarithmic position discounting. Measures *ordering* — ranking's metric, with small k.

**Here:** The split (train < T, eval ≥ T) is frozen in build step 1 and never touched — changing it invalidates the entire eval history. Retrieval is evaluated **against the full catalog**, never sampled negatives: eval choice flips conclusions (learned the hard way), and the catalog is retrieval's actual job.

**Own it:** *Why is a random split a leak in disguise, and why does retrieval get recall@500 while ranking gets NDCG@10?*

## Ties, and why row order is part of your metric

**What:** Every ranking metric needs a total order, but models produce tied scores — massively so when under-trained, since a shallow tree ensemble maps many inputs to the same leaf. The evaluator must therefore break ties *somehow*, and the usual answer is "whatever order the rows arrived in." That makes physical data layout a silent input to your metric. If the layout correlates with the label, the metric is measuring your sort order.

**Here:** The training artifact was written `ORDER BY group_id, label DESC, ...`, putting the positive first in every group. Ties resolved in its favour, so validation NDCG was *highest* for the least-trained model, and early stopping selected a single tree — three ranker evaluations were run on a stump before anyone looked (D22). The fix is a label-independent but still deterministic order. The same hazard has a serving-side twin: `argsort` on tied scores returns an arbitrary permutation, so a tied ranker can score below the baseline it reorders purely through tie-breaking.

**Own it:** *Your validation metric peaks at iteration 1 and then falls, on the training set too. Why is "it's overfitting" the wrong answer, and what would you check first?*

## Covariate shift (and why scale-free features resist it)

**What:** The input distribution moves between training and serving even though the relationship being learned is stable. Distinct from concept drift (where the relationship itself changes) and from leakage (which is a correctness bug, not a distribution one). Trees are especially exposed: they cannot extrapolate past their highest split, so serving inputs beyond the training range collapse into one terminal bin.

**Here:** Cumulative count features drift by construction — history only accumulates, so `i_reviews_to_date` runs p10/p50/p90 = 0/221/596 on pre-2018 training rows but 313/452/983 at serving. The `ui_*` features barely move (distance p50 1.855 → 1.967), because a ratio or a distance is scale-free in a way a running total is not. The general lever: define features as ranks, ratios, or distances rather than raw accumulating counts.

**Own it:** *Which of Sift's features drift with calendar time and which don't — and what does that imply about preferring "rank within the candidate pool" over "review count"?*

## Latency percentiles and budgets

**What:** Latency is a distribution; the mean lies. p99 is the experience of your unluckiest 1% — and under fan-out, of far more users than 1%. A **budget** allocates the end-to-end target across stages so a regression is attributable to a component instead of "the service got slow."

**Here:** <100ms p99 end-to-end, budgeted per stage (`ARCHITECTURE.md`), instrumented from build step 2 onward. Rule: never optimize a stage that isn't the measured bottleneck.

**Own it:** *Why budget and measure per stage instead of one end-to-end number?*

## Idempotency

**What:** Running the same job on the same input twice yields the same result — no duplicates, no drift. Achieved by deterministic transforms + overwrite-by-partition (never blind append).

**Here:** Required of every offline job (ingest, materialization, training-set build). Crashes and re-runs are normal; if re-running corrupts state, the job isn't done.

**Own it:** *How does overwrite-by-partition make a job idempotent, and what append pattern breaks it?*

## Partitions (and what remains of the pipeline framing)

**What:** Splitting data into independent chunks by key — here, event date. The unit of processing, re-running, and backfilling. Sift's offline layout keeps a thin slice of warehouse hygiene from its earlier platform framing: immutable raw Parquet partitioned by date, one canonical event table (D2) feeding all feature computation.

**Here:** Date partitions exist because as-of feature computation needs cheap "events before cutoff" access — the layout serves point-in-time correctness, not a platform ambition.

**Own it:** *Why partition events by date rather than by business_id, given what gold-style feature compute has to do?*

## Columnar storage (Parquet) and DuckDB

**What:** Parquet stores data column-wise with compression and statistics, so analytical reads touch only needed columns and skip irrelevant row groups. DuckDB queries Parquet in place with plain SQL.

**Here:** Every offline artifact is Parquet; DuckDB is the inspection tool that makes every stage printable — the property the whole collaboration model leans on.

**Own it:** *Mechanically, why is `SELECT avg(stars) GROUP BY business_id` fast on Parquet and slow on JSON lines?*

## Implicit vs. explicit feedback

**What:** Explicit = the user told you (stars). Implicit = observed behavior (tip, check-in). Implicit is more abundant and more honest about attention, but has no true negatives — absence of interaction ≠ dislike (→ negative sampling).

**Here:** Yelp provides both; the canonical event table's `event_type` keeps the label definition a modeling choice, not a schema change.

**Own it:** *Why is "no interaction" not a negative label?*

## Candidate source union (multi-source retrieval)

**What:** Production retrieval is rarely one model — it's a union of sources (embedding ANN, popularity, category/history heuristics), each catching what the others miss, deduplicated before ranking. Each source must justify itself by *marginal* contribution: hits that only it produced.

**Here:** ANN ∪ popular-nearby ∪ category-match. Marginal contribution per source is a first-class eval output — a source with ~zero marginal hits gets removed.

**Own it:** *Why measure marginal rather than standalone recall per source?*

## When Spark (and when not)

**What:** Spark distributes dataframe work across cores/machines; its cost is the shuffle plus operational complexity. Below memory scale, pandas/DuckDB is simpler and often faster.

**Here:** Deferred to build step 7, and only if measured candidate-pair volume (users × sampled candidates × features at training-set assembly) actually warrants it. Adding Spark to in-memory-sized data is the project's named anti-pattern.

**Own it:** *Which single step in Sift could genuinely explode, with rough numbers — and what measurement would trigger adopting Spark?*
