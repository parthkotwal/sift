# AGENTS.md — how to work in this repo

This file is the entry point for every agent (Claude, Codex, anything else) and a reminder to the author. Read it before doing anything. Then read, in order:

1. `ARCHITECTURE.md` — what Sift is: the funnel, the feature store, the build order.
2. `DATA.md` — the dataset, its schemas, and its traps. Mandatory before writing any ingestion, feature, or transform code.
3. `CONCEPTS.md` — the ideas behind the design. Skim so you know what's in it; link to it instead of re-explaining.
4. `DECISIONS.md` — what's been decided and what's still open. Never re-litigate an accepted decision silently; never quietly resolve an open one.

## The collaboration model (read this twice)

The author is a CS student learning ML systems engineering **by building this**. The goal is that the author can explain every stage, every table, every metric, and every failure mode cold — in an interview, without notes. That goal beats shipping speed every time.

- **Advise and explain, don't just build.** Before implementing anything non-trivial, state what you're about to do, why, and what the alternative was. Small increments the author can read and question beat large finished systems.
- **Teach at the point of contact.** When a task touches a concept (as-of joins, ANN indexes, negative sampling, skew…), point to its `CONCEPTS.md` entry — and if there isn't one, add a short entry as part of the task. `CONCEPTS.md` is a living study guide.
- **The author decides.** Present tradeoffs and a recommendation; don't pick silently. Record real choices in `DECISIONS.md`.
- **Prefer inspectability.** Every stage must be printable — a candidate list you can eyeball, a feature row you can query with DuckDB, a latency histogram per stage. If a change makes a stage harder to inspect, that's a cost — say so.
- **No cleverness the author can't own.** If a solution needs a trick the author is unlikely to understand from reading it, simplify it or explain it until it's ownable. "It works" is not sufficient.

## The prime directive: time-correct features, twice

The project's two named hazards are the same disease in two tenses:

1. **Point-in-time correctness (training).** Every feature on a training row comes only from events strictly before the row's timestamp. Enforced structurally — sandboxed feature compute, one `get_asof` read path, the future-invariance property test, the snapshot blocklist (`ARCHITECTURE.md` → "The spine"). Every new feature needs a one-line leakage argument: *why can't this value contain the future?* The leak test is never weakened to make a build pass.
2. **Training/serving skew (serving).** The online and offline values of a feature must come from the *same definition*. Never compute a feature inline in the service or in a training script — register a definition and read it from the store. If offline and online must differ for a signal (e.g., `is_open`), it is not a feature; it's a rerank filter (`DECISIONS.md` D13).

## Engineering rules

- **Models pay rent.** Nothing lands without beating its predecessor on the frozen eval (recall@500 for retrieval, NDCG@10 for ranking). The displaced baseline stays runnable. Build order goes one stage at a time (`ARCHITECTURE.md` → Build order); don't start a stage before the previous one is measured.
- **The temporal split is frozen.** Defined in build step 1, never touched again. Any change to it invalidates every number in the eval history — treat a proposed change as a major decision, not a tweak.
- **Retrieval is evaluated against the full catalog,** never sampled negatives. Eval choice flips conclusions; this one is settled.
- **Measure latency per stage.** An end-to-end number with no breakdown hides the problem. The budget lives in `ARCHITECTURE.md`; instrument before optimizing, and never optimize a stage that isn't the bottleneck.
- **Idempotent, partition-wise offline jobs.** Re-running a job on the same partition produces identical output (overwrite-by-partition, deterministic transforms). If you can't re-run it safely, it isn't done.
- **No new tools without rent.** The stack is deliberately small. Spark enters at step 7 only if measured candidate-pair volume warrants it; an orchestrator was deliberately cut (D10); Feast was deliberately rejected (D9). A new dependency needs a `DECISIONS.md` entry.
- **No model experimentation.** One retrieval model, one ranker, zero tuning sprints, zero ablations. If a task's output is "a slightly better model," it's out of scope; if it's "a stage that didn't exist" or "a correctness property enforced," it's in.
- **Never commit data.** The Yelp license prohibits redistribution; `data/` is gitignored forever. No raw records, real user names, or review text in committed code, docs, or test fixtures — fixtures are synthetic. Be conservative about publishing dataset-derived metrics in public docs (`DATA.md` → License).

## The resume-shape rule

The author's prior projects all read as "web service + ML layer + async job" — and Sift's silhouette is dangerously close. Therefore, in every outward-facing description (README, resume bullets, commit messages that might be read cold): **lead with the internals** — the staged funnel and its per-stage metrics, the feature store and its two materializations, the leakage test, the latency budget. Never describe Sift as "a recommendation API." If a sentence could describe a generic CRUD-plus-model app, rewrite it.

## Footguns

- **Dump-time snapshot columns** (`business.stars`, `review_count`, user lifetime stats, vote counts) are values from the future. Blocklisted as features; see `DATA.md`.
- **`is_open` is the sneaky one:** legitimate as a serving-time filter, leakage as a training feature (a business open in 2016 but closed by dump time carries `is_open=0` into 2016 rows). Filter, never feature.
- **Self-leaks:** to-date aggregates (user mean stars, rating trend, days-since-last-review) computed with a window that includes the label event itself. All windows right-exclusive `[t-w, t)`.
- **In-batch negatives oversample popular items** — the two-tower's known bias. Correct (logQ) or knowingly accept and document; don't discover it in an interview.
- **`checkin.json` has no `user_id`** — check-ins are user-less, entity-side signal only. Don't invent a user.
- **Whole-history aggregates** attached to historical rows: always available, always wrong. The chokepoints exist so this can't happen silently — don't route around them.
- **Timestamp hygiene:** one timezone convention, fixed at ingest, never revisited. Off-by-one-day bugs are leakage bugs.

## Working style

- Python, type hints, small modules with single responsibilities. Tests assert correctness *properties* (future-invariance, idempotency, schema conformance, no-leak), not line coverage.
- Comments state constraints the code can't show; no narration.
- Commits are named for what changed in the system, not the code ("store: right-exclusive as-of join + invariance test" beats "fix bug").

## Extensibility

Sift is standalone. Its one designed extension point is the **feature-definition registry**: embeddings are feature definitions (D12), so any externally-produced vector later registers as `item_embedding_semantic_v1` and plugs into the same index/ranker sockets by config. Build nothing for it now — just don't close the seam.
