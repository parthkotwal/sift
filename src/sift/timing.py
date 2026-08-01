"""Opt-in nested timings, for locating a cost the five serving stages are too coarse to explain.

The funnel reports five numbers, and they were enough while the question was "which
stage regressed". They are not enough for the question the Fargate deployment raised:
`feature_lookup_ms` bundles a Redis round trip, JSON decoding, DuckDB relation creation
and a DuckDB query, and "70ms" says nothing about which of those grew. Neither does
`ranking_ms`, which is a matrix build, a model call and a sort.

**Off unless a request asks.** Collection is enabled per request by
:func:`collecting`, so the serving path pays one thread-local read per span otherwise.
That is deliberate: the numbers are diagnostic, not a contract, and making them
permanent response surface would invite them into a budget they were never calibrated
for — the failure D28 records, one level finer. `bench.py`'s tripwires stay on the five
stages; these exist to answer "why", once, and can change shape freely.

Spans *accumulate* by name rather than overwrite, so a span inside a loop reports the
loop's total rather than its last iteration. Nesting is allowed and the names carry the
hierarchy (`feature.redis_mget`), because a tree would imply the children sum to the
parent — they do not, and pretending otherwise is how the original 30/20/30/20 latency
budget encoded an arithmetic error (D28).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

_local = threading.local()


class RequestTimings:
    """Named durations in milliseconds, accumulated over one request."""

    def __init__(self) -> None:
        self.spans: dict[str, float] = {}

    def add(self, name: str, elapsed_ms: float) -> None:
        self.spans[name] = self.spans.get(name, 0.0) + elapsed_ms

    def as_dict(self) -> dict[str, float]:
        """Sorted by cost, because the reason to read this is to find the big one."""
        return dict(sorted(self.spans.items(), key=lambda item: -item[1]))


def current() -> RequestTimings | None:
    return getattr(_local, "timings", None)


@contextmanager
def collecting() -> Iterator[RequestTimings]:
    """Collect spans recorded on this thread for the duration of the block.

    The previous collector is restored rather than cleared, so a nested `collecting`
    (a profiled request inside a profiled batch) cannot silently discard the outer one.
    """
    previous = current()
    timings = RequestTimings()
    _local.timings = timings
    try:
        yield timings
    finally:
        _local.timings = previous


@contextmanager
def span(name: str) -> Iterator[None]:
    """Record how long the block took, when something is collecting."""
    timings = current()
    if timings is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        timings.add(name, (time.perf_counter() - started) * 1_000)
