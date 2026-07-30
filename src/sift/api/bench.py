"""Load benchmark against a running Sift endpoint, at a stated concurrency.

This exists because `retrieval.online --samples` cannot check the serving contract.
That loop is single-threaded and in-process, so it profiles one permanently warm
thread; the same code measured 18ms and 162ms p50 depending only on offered load, and
the difference was invisible for months because nothing measured it (ISSUES.md I31).
D28's budget therefore names a concurrency level, and this is what verifies it.

Two clocks are reported and they answer different questions. **Server** timings come
from the response's own per-stage breakdown — the funnel's cost, comparable to the
offline numbers. **Client wall** is measured around the request and includes queueing,
serialization, and network, so it is the only one that reflects what a caller
experiences through a load balancer. Through an ALB the gap between them *is* the
infrastructure's contribution.

Run against localhost:

    python -m sift.api.bench --concurrency 4

Or through a deployed endpoint (AWS_DEPLOYMENT_PLAN.md phase 7, criterion 10):

    python -m sift.api.bench --url http://<alb-dns> --concurrency 4 --samples 300

`--check` exits non-zero when the run misses the budget, so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from sift.eval.holdout import load_ground_truth
from sift.eval.metrics import percentile

DEFAULT_URL = "http://localhost:8000"

# D28's budget as code rather than prose, so a run can check it. Stage names match the
# API's `latency` fields.
#
# `rerank_ms` and `overhead_ms` were one 20ms line while rerank was unbuilt. Now that
# it exists (D29) they are separate and both measured: rerank is 4.8ms p99 (a ~51-key
# Redis read plus in-process filtering), overhead is 0.02ms p99. The old shared 20ms
# would have been vacuous for overhead — it could regress a hundredfold and still pass —
# which is the failure mode D28 was written about, one stage further along.
#
# Per-stage lines and the end-to-end contract are deliberately stated at *different*
# concurrencies, because they do different jobs:
#
#   - Stage tripwires exist to catch algorithmic regressions (I29's 2ms -> 49ms). They
#     are calibrated at concurrency 1, where the numbers are reproducible and
#     comparable across machines rather than a function of how many cores are free.
#   - The end-to-end contract is a serving promise, so it is stated at the concurrency
#     the service is expected to take.
#
# They are also not required to sum. Percentiles are not additive: at concurrency 4 the
# stage p99s total 103ms while end-to-end p99 is 77ms, because the unluckiest 1% of each
# stage are mostly different requests. The original 30/20/30/20 allocation summing to
# exactly 100ms encoded that confusion; a budget built by adding stage p99s
# over-provisions every stage and still does not bound the total.
STAGE_TRIPWIRE_MS: dict[str, float] = {
    "retrieval_ms": 10.0,
    "feature_lookup_ms": 40.0,
    "ranking_ms": 15.0,
    "rerank_ms": 10.0,
    "overhead_ms": 5.0,
}
TRIPWIRE_CONCURRENCY = 1
END_TO_END_P99_MS = 100.0
SUPPORTED_CONCURRENCY = 4

# The percentile the contract is written in. p50/p95 are reported for shape, but only
# p99 is asserted -- a budget stated at the median hides exactly the tail that hurts.
CONTRACT_PERCENTILE = 99.0

# Below this, a p99 verdict is noise. At n=300 the p99 is the 3rd-worst request, so two
# unlucky OS scheduling events decide it: the same unchanged build measured 103ms (MISS)
# and 79ms (PASS) on consecutive runs. n=1200 wants ~12 samples above the p99, which is
# enough for the verdict to be about the code. `--check` refuses to gate below this
# rather than emit a number that flips -- a gate that fails randomly gets ignored, and
# an ignored gate is worse than none (ISSUES.md I31 records the same lesson at n=100).
MIN_SAMPLES_FOR_CONTRACT = 1_000


def host_load() -> float | None:
    """One-minute load average, or None where the platform will not say."""
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None


def quiet_host_threshold() -> float:
    """Load above which absolute latency numbers stop being about the code.

    Half the logical cores, leaving room for the benchmark's own client threads and the
    server. This is a heuristic, not a law -- `--max-load` overrides it -- but the
    failure it prevents is real: measured on a developer desktop at load 8.6 on 4
    performance cores, the same unchanged build reported end-to-end p99 of 50ms and
    99ms minutes apart, which is a wider spread than every optimisation in I31 combined.
    """
    cores = os.cpu_count() or 2
    return max(1.0, cores / 2)

Sample = dict[str, float]
Fetch = Callable[[str], Sample]


@dataclass(frozen=True)
class StageStats:
    name: str
    p50: float
    p95: float
    p99: float
    budget_ms: float | None = None

    @property
    def within_budget(self) -> bool:
        return self.budget_ms is None or self.p99 <= self.budget_ms


@dataclass(frozen=True)
class BenchReport:
    url: str
    concurrency: int
    requests: int
    errors: int
    stages: tuple[StageStats, ...]
    over_contract: int
    load_before: float | None = None
    load_after: float | None = None
    field_names: tuple[str, ...] = field(default=(), repr=False)

    @property
    def host_was_quiet(self) -> bool:
        """Whether the host had spare capacity. Unknown platforms are assumed quiet:
        refusing to measure because we cannot read a load average would be worse."""
        if self.load_before is None:
            return True
        return self.load_before <= quiet_host_threshold()

    @property
    def checks_tripwires(self) -> bool:
        """Stage tripwires are only meaningful at the concurrency they were calibrated
        at; above it they are expected to be over and say nothing about a regression."""
        return self.concurrency <= TRIPWIRE_CONCURRENCY

    @property
    def ok(self) -> bool:
        """Every applicable line held, and nothing failed outright.

        Errors count against the run: a benchmark that reports percentiles over only the
        requests that succeeded describes a service that is not working as a fast one.
        """
        if self.errors:
            return False
        end_to_end = next((s for s in self.stages if s.name == "total_ms"), None)
        if end_to_end is not None and not end_to_end.within_budget:
            return False
        if not self.checks_tripwires:
            return True
        return all(stage.within_budget for stage in self.stages)

    def render(self) -> str:
        lines = [
            f"{self.requests} requests at concurrency {self.concurrency} -> {self.url}",
            f"  {'stage':<20} {'p50':>9} {'p95':>9} {'p99':>9} {'budget':>9}  ",
        ]
        for stage in self.stages:
            budget = f"{stage.budget_ms:.0f}ms" if stage.budget_ms is not None else "-"
            # Only flag what this run actually asserts. Marking a stage OVER when the run
            # is not checking it trains the reader to ignore the column.
            asserted = stage.name == "total_ms" or self.checks_tripwires
            mark = "" if stage.within_budget or not asserted else "  OVER"
            lines.append(
                f"  {stage.name:<20} {stage.p50:>7.2f}ms {stage.p95:>7.2f}ms "
                f"{stage.p99:>7.2f}ms {budget:>9}{mark}"
            )
        share = 100.0 * self.over_contract / self.requests if self.requests else 0.0
        lines.append(
            f"  requests over {END_TO_END_P99_MS:.0f}ms end-to-end: "
            f"{self.over_contract}/{self.requests} ({share:.1f}%)"
        )
        if self.errors:
            lines.append(f"  FAILED REQUESTS: {self.errors}")
        if 0 < self.requests < MIN_SAMPLES_FOR_CONTRACT:
            lines.append(
                f"  warning: {self.requests} samples is too few for a stable p99 "
                f"(want {MIN_SAMPLES_FOR_CONTRACT}); treat this as shape, not a verdict"
            )
        if self.load_before is not None:
            note = "" if self.host_was_quiet else "  <- NOT A QUIET HOST"
            lines.append(
                f"  host load 1m: {self.load_before:.2f} before, "
                f"{self.load_after:.2f} after (quiet <= {quiet_host_threshold():.1f})"
                f"{note}"
            )
        if not self.host_was_quiet:
            lines.append(
                "  these absolute numbers describe a contended machine, not the code; "
                "relative before/after on the same host is still meaningful"
            )
        if not self.checks_tripwires:
            lines.append(
                f"  stage budgets are calibrated at concurrency {TRIPWIRE_CONCURRENCY} and are "
                "not asserted here; only the end-to-end contract is"
            )
        if self.concurrency > SUPPORTED_CONCURRENCY:
            lines.append(
                f"  note: concurrency {self.concurrency} exceeds the {SUPPORTED_CONCURRENCY} "
                "the D28 contract is stated at; a miss here is the envelope, not a defect"
            )
        lines.append(f"  verdict: {'PASS' if self.ok else 'MISS'}")
        return "\n".join(lines)


def deterministic_users(count: int, users: Iterable[str] | None = None) -> list[str]:
    """A stable user sample, hash-ordered so every run measures the same work.

    Hash order rather than the file's order: the holdout is built in a scan order that
    correlates with activity, so taking a prefix of it would sample unusually busy
    users and quietly measure a different workload than the funnel sees.
    """
    pool = sorted(
        load_ground_truth() if users is None else users,
        key=lambda value: hashlib.sha256(value.encode()).digest(),
    )
    if not pool:
        raise ValueError("no holdout users; build the ground truth before benchmarking")
    # Cycle rather than truncate, so --samples above the pool size still runs.
    return [pool[index % len(pool)] for index in range(count)]


def http_fetch(url: str, k: int = 10, timeout: float = 30.0) -> Fetch:
    """A fetcher that records the client-side wall clock around each request."""
    base = url.rstrip("/")

    def fetch(user_id: str) -> Sample:
        query = urllib.parse.urlencode({"user_id": user_id, "k": k})
        started = time.perf_counter()
        with urllib.request.urlopen(f"{base}/recommend?{query}", timeout=timeout) as response:
            payload = json.load(response)
        wall_ms = (time.perf_counter() - started) * 1_000
        latency = dict(payload["latency"])
        latency["client_wall_ms"] = wall_ms
        return latency

    return fetch


def run_benchmark(
    fetch: Fetch,
    users: Sequence[str],
    concurrency: int,
    *,
    url: str = DEFAULT_URL,
    warmup: bool = True,
) -> BenchReport:
    """Drive `fetch` over `users` at `concurrency` and summarize per stage.

    One warm-up request is issued and discarded: a fresh process builds the catalog-wide
    ALS relation on first use (~215ms, once per process -- I31), and including it would
    put a one-time cost in every percentile.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    if not users:
        raise ValueError("no users to benchmark")
    if warmup:
        fetch(users[0])

    load_before = host_load()
    samples: list[Sample] = []
    errors = 0

    def attempt(user_id: str) -> Sample | None:
        try:
            return fetch(user_id)
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for result in pool.map(attempt, users):
            if result is None:
                errors += 1
            else:
                samples.append(result)

    # Report every field the endpoint returned, in the endpoint's own order, so a new
    # stage appears here without this module being edited.
    names = tuple(samples[0]) if samples else ()
    stages = tuple(
        StageStats(
            name=name,
            p50=percentile([s[name] for s in samples], 50),
            p95=percentile([s[name] for s in samples], 95),
            p99=percentile([s[name] for s in samples], CONTRACT_PERCENTILE),
            budget_ms=STAGE_TRIPWIRE_MS.get(
                name, END_TO_END_P99_MS if name == "total_ms" else None
            ),
        )
        for name in names
    )
    return BenchReport(
        url=url,
        concurrency=concurrency,
        requests=len(samples),
        errors=errors,
        stages=stages,
        over_contract=sum(1 for s in samples if s.get("total_ms", 0.0) > END_TO_END_P99_MS),
        load_before=load_before,
        load_after=host_load(),
        field_names=names,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default=DEFAULT_URL, help=f"endpoint base URL (default {DEFAULT_URL})"
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=MIN_SAMPLES_FOR_CONTRACT,
        help=f"requests to issue (default {MIN_SAMPLES_FOR_CONTRACT}, the floor for a stable p99)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=SUPPORTED_CONCURRENCY,
        help=f"simultaneous in-flight requests (default {SUPPORTED_CONCURRENCY}, the D28 envelope)",
    )
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any budgeted p99 is missed or any request failed",
    )
    parser.add_argument(
        "--max-load",
        type=float,
        default=None,
        help="refuse to gate above this 1m host load (default: half the logical cores)",
    )
    args = parser.parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    if args.check and args.samples < MIN_SAMPLES_FOR_CONTRACT:
        raise SystemExit(
            f"--check needs at least {MIN_SAMPLES_FOR_CONTRACT} samples to gate on p99; "
            f"got {args.samples}. Below that the verdict flips on OS scheduling noise "
            "rather than on the code. Drop --check to report shape instead."
        )

    report = run_benchmark(
        http_fetch(args.url, k=args.k),
        deterministic_users(args.samples),
        args.concurrency,
        url=args.url,
    )
    print(report.render())
    if not args.check:
        return
    max_load = args.max_load if args.max_load is not None else quiet_host_threshold()
    load = report.load_before
    if load is not None and load > max_load:
        raise SystemExit(
            f"refusing to gate: host 1m load was {load:.2f}, above {max_load:.2f}. "
            "Absolute p99 on a contended host measures the host. Re-run on a quiet "
            "machine (or the deployment target), or raise --max-load deliberately."
        )
    if not report.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
