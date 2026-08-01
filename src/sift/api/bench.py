"""Load benchmark against a running Sift endpoint, at a stated concurrency.

This exists because `retrieval.online --samples` cannot check the serving contract.
That loop is single-threaded and in-process, so it profiles one permanently warm
thread; the same code measured 18ms and 162ms p50 depending only on offered load, and
the difference was invisible for months because nothing measured it (ISSUES.md I31).
D28's budget therefore names a concurrency level, and this is what verifies it.

**Three clocks, because two was one too few.** The funnel's `total_ms` starts on the
first line of `recommend()`, so it excludes routing, dependency resolution, waiting for
a threadpool slot, and response serialization. This benchmark asserted that number for
its whole life, which meant a change could have "improved" the contract by moving delay
*out of the timer* rather than out of the request. Measured against the Fargate
deployment, the gap is 1.3ms p50 at concurrency 1 and 16.7ms p99 at concurrency 4 — small
at the median, real in the tail, and previously invisible. So:

  - ``total_ms``       the funnel: retrieval through rerank, comparable to offline runs.
  - ``server_ms``      the middleware's ``app;dur`` — funnel **plus** framework and
                       queueing. This is what the server actually spent, and it is the
                       clock the contract is asserted on.
  - ``client_wall_ms`` server plus connection setup and network. Through an ALB from a
                       developer machine this is dominated by geography (~150ms round
                       trip to us-west-2 on a 1.6ms request), so it is reported for
                       shape and never asserted.

Their differences are named too: ``framework_ms`` (server − funnel) and ``transport_ms``
(client − server). A regression that hides in either is now visible in its own line.

**Two load models, because closed-loop throughput is not capacity.** By default the
client keeps `--concurrency` requests in flight and issues the next only when one
returns, so achieved throughput is bounded by *concurrency ÷ latency* and reveals
nothing about the server's ceiling. That trap is easy to fall into: a run at concurrency
4 through an ALB produced 15.2 req/s, which was read as the server serializing, when
4 threads ÷ 260ms round trip is 15.4 req/s — the number described the load generator.
`--rate` issues requests on a fixed schedule instead, so offered load is independent of
service time and the server saturating shows up as achieved rate falling behind offered.

Run against localhost:

    python -m sift.api.bench --concurrency 4

Fixed arrival rate rather than closed loop, which is what shows saturation:

    python -m sift.api.bench --rate 40 --samples 2000

Or through a deployed endpoint (AWS_DEPLOYMENT_PLAN.md phase 7, criterion 10):

    python -m sift.api.bench --url http://<alb-dns> --concurrency 4 --samples 1000

`--check` exits non-zero when the run misses the budget, so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import threading
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
#
# Re-tightened after I31 moved feature lookup from 32ms p99 to 15ms: a 40ms line on a
# 15ms stage would let it regress back to 39ms unnoticed, which is precisely how I29
# (2ms -> 49ms) was caught in the first place. A tripwire has to follow its stage down
# or it stops being one. Each is set at roughly 2x the measured p99 — loose enough not
# to flap on scheduling noise, tight enough that a real regression trips it.
STAGE_TRIPWIRE_MS: dict[str, float] = {
    "retrieval_ms": 10.0,  # p99 4.5
    "feature_lookup_ms": 30.0,  # p99 15.3
    "ranking_ms": 15.0,  # p99 11.0
    "rerank_ms": 8.0,  # p99 3.3
    "overhead_ms": 5.0,  # p99 0.03
}
TRIPWIRE_CONCURRENCY = 1
END_TO_END_P99_MS = 100.0

# The funnel's own total, and the whole server-side request. `server_ms` is the one the
# contract is asserted on: D28 promises an *end-to-end* p99, and the funnel timer starts
# after the framework has already done work. Asserting the narrower number would let a
# change pass by relocating delay rather than removing it.
FUNNEL_CLOCK = "total_ms"
SERVER_CLOCK = "server_ms"
CONTRACT_CLOCK = SERVER_CLOCK
# Reported in this order, after the funnel stages. Each is a difference between two
# clocks, so a cost that appears in none of the stages still lands on a named line.
REQUEST_CLOCKS: tuple[str, ...] = (
    FUNNEL_CLOCK,
    SERVER_CLOCK,
    "framework_ms",
    "client_wall_ms",
    "transport_ms",
)
# Raised from 4 after I31 (D31). Concurrency 8 measures 67.9ms p99 with 0/1000 over the
# contract, where it was 118ms with 57/300 over before the catalog records landed.
SUPPORTED_CONCURRENCY = 8

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
    duration_s: float = 0.0
    offered_rate: float | None = None
    schedule_lag_ms: float | None = None
    # Which clock the verdict used. Falls back to the funnel when the endpoint did not
    # report `app;dur`, and `--check` refuses to gate on the fallback: a build that
    # cannot show its framework cost cannot be shown to meet an end-to-end promise.
    contract_clock: str = CONTRACT_CLOCK
    keep_alive: bool = False

    @property
    def achieved_rate(self) -> float | None:
        if self.duration_s <= 0:
            return None
        return (self.requests + self.errors) / self.duration_s

    @property
    def measures_server_clock(self) -> bool:
        return self.contract_clock == SERVER_CLOCK

    @property
    def client_kept_up(self) -> bool:
        """Open-loop only: whether offered load was really the rate that was asked for.

        A client that fell behind its own schedule was the bottleneck, so the run
        describes the load generator rather than the server. One interval of slack is
        allowed; beyond that the number is not an offered rate any more.
        """
        if self.offered_rate is None or self.schedule_lag_ms is None:
            return True
        return self.schedule_lag_ms <= 1_000.0 / self.offered_rate

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
        end_to_end = next((s for s in self.stages if s.name == self.contract_clock), None)
        if end_to_end is not None and not end_to_end.within_budget:
            return False
        if not self.checks_tripwires:
            return True
        # The clocks are reported, not budgeted: `client_wall_ms` is mostly geography and
        # `framework_ms` has no calibrated line yet. Only the funnel stages and the
        # contract clock decide the verdict.
        return all(
            stage.within_budget
            for stage in self.stages
            if stage.name not in REQUEST_CLOCKS or stage.name == self.contract_clock
        )

    def render(self) -> str:
        model = (
            f"fixed rate {self.offered_rate:.1f}/s"
            if self.offered_rate is not None
            else f"closed loop, concurrency {self.concurrency}"
        )
        reuse = "connection reuse" if self.keep_alive else "new connection per request"
        lines = [
            f"{self.requests} requests, {model}, {reuse} -> {self.url}",
            f"  {'measure':<20} {'p50':>9} {'p95':>9} {'p99':>9} {'budget':>9}  ",
        ]
        for stage in self.stages:
            if stage.name == REQUEST_CLOCKS[0]:
                lines.append("  -- request clocks --")
            budget = f"{stage.budget_ms:.0f}ms" if stage.budget_ms is not None else "-"
            # Only flag what this run actually asserts. Marking a stage OVER when the run
            # is not checking it trains the reader to ignore the column.
            asserted = stage.name == self.contract_clock or (
                self.checks_tripwires and stage.name not in REQUEST_CLOCKS
            )
            mark = "" if stage.within_budget or not asserted else "  OVER"
            note = "  <- contract" if stage.name == self.contract_clock else ""
            lines.append(
                f"  {stage.name:<20} {stage.p50:>7.2f}ms {stage.p95:>7.2f}ms "
                f"{stage.p99:>7.2f}ms {budget:>9}{mark}{note}"
            )
        share = 100.0 * self.over_contract / self.requests if self.requests else 0.0
        lines.append(
            f"  requests over {END_TO_END_P99_MS:.0f}ms on {self.contract_clock}: "
            f"{self.over_contract}/{self.requests} ({share:.1f}%)"
        )
        if not self.measures_server_clock:
            lines.append(
                "  the endpoint did not report app;dur, so the verdict used the funnel "
                "clock; framework and queueing time is unmeasured in this run"
            )
        if self.achieved_rate is not None:
            achieved = f"  throughput: {self.achieved_rate:.1f} req/s over {self.duration_s:.1f}s"
            if self.offered_rate is not None:
                achieved += f" (offered {self.offered_rate:.1f}/s)"
            lines.append(achieved)
        if self.offered_rate is None:
            lines.append(
                "  closed loop: throughput is concurrency / latency by construction and "
                "is not the server's capacity — use --rate to measure that"
            )
        elif not self.client_kept_up:
            lines.append(
                f"  the client fell {self.schedule_lag_ms:.0f}ms behind its own schedule: "
                "offered load was not the requested rate, so this run measures the load "
                "generator. Raise --max-inflight or lower --rate"
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


def server_timing_ms(header: str | None, metric: str) -> float | None:
    """Pull one `dur` out of a Server-Timing header, or None if it is not there.

    Absent is reported as None rather than 0.0 so the caller can tell "the server did
    not say" from "the server said zero" — the first means this build cannot verify the
    contract, the second would be a suspiciously fast request.
    """
    if not header:
        return None
    for part in header.split(","):
        name, _, rest = part.strip().partition(";")
        if name != metric:
            continue
        for attribute in rest.split(";"):
            key, _, value = attribute.partition("=")
            if key.strip() == "dur":
                try:
                    return float(value)
                except ValueError:
                    return None
    return None


def http_fetch(url: str, k: int = 10, timeout: float = 30.0, *, keep_alive: bool = False) -> Fetch:
    """A fetcher recording the client wall clock and the server's own `app;dur`.

    `keep_alive` reuses one connection per client thread. It is off by default because
    the deployed benchmark and ordinary `urllib` both send `Connection: close`, and that
    is the path that once exposed a response-truncation bug in Uvicorn's httptools
    parser. The two modes measure different things and the report says which ran: with
    reuse off, `transport_ms` includes a fresh TCP handshake every request, which through
    an ALB is most of it.
    """
    base = url.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    local = threading.local()

    def connection() -> http.client.HTTPConnection:
        existing: http.client.HTTPConnection | None = getattr(local, "connection", None)
        if existing is not None:
            return existing
        created: http.client.HTTPConnection = (
            http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
            if parsed.scheme == "https"
            else http.client.HTTPConnection(parsed.netloc, timeout=timeout)
        )
        local.connection = created
        return created

    def fetch(user_id: str) -> Sample:
        query = urllib.parse.urlencode({"user_id": user_id, "k": k})
        path = f"{parsed.path}/recommend?{query}"
        started = time.perf_counter()
        if keep_alive:
            client = connection()
            try:
                client.request("GET", path)
                response = client.getresponse()
                body = response.read()
                header = response.getheader("Server-Timing")
            except (http.client.HTTPException, OSError):
                # A reused connection the server has since closed is a transport fault,
                # not a slow request. Drop it so the next attempt reconnects rather than
                # failing every remaining request on this thread.
                local.connection = None
                raise
            payload = json.loads(body)
        else:
            with urllib.request.urlopen(f"{base}/recommend?{query}", timeout=timeout) as opened:
                payload = json.load(opened)
                header = opened.headers.get("Server-Timing")
        wall_ms = (time.perf_counter() - started) * 1_000
        sample = dict(payload["latency"])
        sample["client_wall_ms"] = wall_ms
        app_ms = server_timing_ms(header, "app")
        if app_ms is not None:
            sample[SERVER_CLOCK] = app_ms
        return sample

    return fetch


def derive_clocks(sample: Sample) -> Sample:
    """Name the gaps between clocks, so a cost outside every stage still has a line.

    Only what the sample supports: a fetcher that reported no `server_ms` gets no
    `framework_ms`, rather than a zero that would read as "no framework cost".
    """
    enriched = dict(sample)
    funnel = sample.get(FUNNEL_CLOCK)
    server = sample.get(SERVER_CLOCK)
    client = sample.get("client_wall_ms")
    if server is not None and funnel is not None:
        enriched["framework_ms"] = max(0.0, server - funnel)
    if client is not None and server is not None:
        enriched["transport_ms"] = max(0.0, client - server)
    return enriched


def _ordered_fields(sample: Sample) -> tuple[str, ...]:
    """Funnel stages in the endpoint's own order, then the request clocks.

    Stage names are not hardcoded, so a new stage in the endpoint appears here without
    this module being edited; the clocks are pinned last because they are derived.
    """
    stages = tuple(name for name in sample if name not in REQUEST_CLOCKS)
    return stages + tuple(name for name in REQUEST_CLOCKS if name in sample)


def _closed_loop(
    attempt: Callable[[str], Sample | None], users: Sequence[str], concurrency: int
) -> list[Sample | None]:
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(attempt, users))


def _open_loop(
    attempt: Callable[[str], Sample | None],
    users: Sequence[str],
    rate: float,
    max_inflight: int,
) -> tuple[list[Sample | None], float]:
    """Issue requests on a fixed schedule, independent of how long they take.

    Returns the results and the worst schedule lag: how far behind its slot the client
    fell before it could submit. Lag near zero means offered load really was `rate`, so
    an achieved rate below it is the *server* saturating. Lag growing means the client
    ran out of threads and the run describes the load generator instead — the failure
    this mode exists to make visible rather than silently absorb.
    """
    interval = 1.0 / rate
    # A semaphore, not just the pool's worker count: `submit` never blocks, so a pool
    # alone would queue an unbounded backlog and the schedule lag below would stay near
    # zero however far behind the client actually was — the run would look like a clean
    # offered rate while measuring a growing queue. Bounding in-flight work makes the
    # client block when it cannot keep up, which is the signal this mode exists for.
    slots = threading.Semaphore(max_inflight)

    def release_after(user_id: str) -> Sample | None:
        try:
            return attempt(user_id)
        finally:
            slots.release()

    started = time.perf_counter()
    worst_lag = 0.0
    with ThreadPoolExecutor(max_workers=max_inflight) as pool:
        futures = []
        for index, user_id in enumerate(users):
            slot = started + index * interval
            now = time.perf_counter()
            if now < slot:
                time.sleep(slot - now)
            slots.acquire()
            futures.append(pool.submit(release_after, user_id))
            worst_lag = max(worst_lag, time.perf_counter() - slot)
        return [future.result() for future in futures], worst_lag * 1_000


def run_benchmark(
    fetch: Fetch,
    users: Sequence[str],
    concurrency: int,
    *,
    url: str = DEFAULT_URL,
    warmup: bool = True,
    rate: float | None = None,
    max_inflight: int | None = None,
    keep_alive: bool = False,
) -> BenchReport:
    """Drive `fetch` over `users` and summarize per stage and per clock.

    One warm-up request is issued and discarded: a fresh process builds the catalog-wide
    ALS relation on first use (~215ms, once per process -- I31), and including it would
    put a one-time cost in every percentile.

    `rate` switches from closed-loop (keep `concurrency` in flight) to a fixed arrival
    schedule. The two answer different questions and the report says which ran.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    if rate is not None and rate <= 0:
        raise ValueError("rate must be positive")
    if not users:
        raise ValueError("no users to benchmark")
    if warmup:
        fetch(users[0])

    load_before = host_load()

    def attempt(user_id: str) -> Sample | None:
        try:
            return derive_clocks(fetch(user_id))
        except (urllib.error.URLError, OSError, KeyError, ValueError, http.client.HTTPException):
            return None

    started = time.perf_counter()
    schedule_lag_ms: float | None = None
    if rate is None:
        results = _closed_loop(attempt, users, concurrency)
    else:
        inflight = max_inflight if max_inflight is not None else max(concurrency, 64)
        results, schedule_lag_ms = _open_loop(attempt, users, rate, inflight)
    duration_s = time.perf_counter() - started

    samples = [result for result in results if result is not None]
    errors = sum(1 for result in results if result is None)

    names = _ordered_fields(samples[0]) if samples else ()
    stages = tuple(
        StageStats(
            name=name,
            p50=percentile([s[name] for s in samples if name in s], 50),
            p95=percentile([s[name] for s in samples if name in s], 95),
            p99=percentile([s[name] for s in samples if name in s], CONTRACT_PERCENTILE),
            budget_ms=STAGE_TRIPWIRE_MS.get(
                name, END_TO_END_P99_MS if name in (FUNNEL_CLOCK, SERVER_CLOCK) else None
            ),
        )
        for name in names
    )
    contract_clock = CONTRACT_CLOCK if any(SERVER_CLOCK in s for s in samples) else FUNNEL_CLOCK
    return BenchReport(
        url=url,
        concurrency=concurrency,
        requests=len(samples),
        errors=errors,
        stages=stages,
        over_contract=sum(1 for s in samples if s.get(contract_clock, 0.0) > END_TO_END_P99_MS),
        load_before=load_before,
        load_after=host_load(),
        field_names=names,
        duration_s=duration_s,
        offered_rate=rate,
        schedule_lag_ms=schedule_lag_ms,
        contract_clock=contract_clock,
        keep_alive=keep_alive,
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
    parser.add_argument(
        "--rate",
        type=float,
        default=None,
        help=(
            "offered requests/second on a fixed schedule (open loop). Without this the "
            "run is closed-loop, where throughput is concurrency/latency and says "
            "nothing about server capacity"
        ),
    )
    parser.add_argument(
        "--max-inflight",
        type=int,
        default=None,
        help="client threads available to --rate (default: max(concurrency, 64))",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help=(
            "reuse one connection per client thread. Off by default, matching urllib and "
            "the deployed benchmark, which send Connection: close"
        ),
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
        http_fetch(args.url, k=args.k, keep_alive=args.keep_alive),
        deterministic_users(args.samples),
        args.concurrency,
        url=args.url,
        rate=args.rate,
        max_inflight=args.max_inflight,
        keep_alive=args.keep_alive,
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
    if not report.measures_server_clock:
        raise SystemExit(
            f"refusing to gate: the endpoint reported no app;dur, so only "
            f"{FUNNEL_CLOCK} is available. That clock starts after routing, dependency "
            "resolution and threadpool queueing, so passing on it would not show the "
            "end-to-end promise D28 actually makes."
        )
    if not report.client_kept_up:
        raise SystemExit(
            f"refusing to gate: the client fell {report.schedule_lag_ms:.0f}ms behind "
            f"its own {args.rate:.1f}/s schedule, so offered load was not what was asked "
            "for and the run measures the load generator. Raise --max-inflight."
        )
    if not report.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
