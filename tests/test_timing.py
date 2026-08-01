"""Opt-in nested timings: off by default, accumulating, and thread-isolated.

These exist because the five serving stages are too coarse to explain the Fargate
result — `feature_lookup_ms` bundles a Redis round trip, JSON decoding and two DuckDB
steps. The properties below are what make the numbers trustworthy enough to act on.
"""

from __future__ import annotations

import threading
import time

from sift.timing import collecting, current, span


def test_a_span_records_nothing_when_nobody_is_collecting() -> None:
    """The serving default. A span outside `collecting` must be inert, not buffered
    somewhere that grows for the life of the process."""
    assert current() is None
    with span("ranking.predict"):
        pass
    assert current() is None


def test_spans_are_recorded_while_collecting() -> None:
    with collecting() as timings, span("feature.redis_mget"):
        time.sleep(0.005)
    recorded = timings.as_dict()
    assert set(recorded) == {"feature.redis_mget"}
    assert recorded["feature.redis_mget"] >= 4.0


def test_repeated_spans_accumulate_rather_than_overwrite() -> None:
    """A span inside a loop must report the loop's cost. Overwriting would report the
    last iteration and understate a per-candidate cost by the iteration count — exactly
    the kind of number that would send an investigation the wrong way."""
    with collecting() as timings:
        for _ in range(3):
            with span("feature.decode_records"):
                time.sleep(0.002)
    assert timings.as_dict()["feature.decode_records"] >= 5.0


def test_nested_spans_are_both_recorded_and_do_not_have_to_sum() -> None:
    """Names carry the hierarchy instead of a tree, because children summing to their
    parent is exactly the false invariant D28's original latency budget encoded."""
    with collecting() as timings, span("feature"):
        with span("feature.redis_mget"):
            time.sleep(0.002)
        time.sleep(0.002)
    recorded = timings.as_dict()
    assert set(recorded) == {"feature", "feature.redis_mget"}
    assert recorded["feature"] > recorded["feature.redis_mget"]


def test_the_report_is_ordered_by_cost() -> None:
    """The reason to read this is to find the expensive one."""
    with collecting() as timings:
        with span("cheap"):
            pass
        with span("expensive"):
            time.sleep(0.005)
    assert list(timings.as_dict()) == ["expensive", "cheap"]


def test_a_span_records_even_when_the_block_raises() -> None:
    """A stage that blew up is exactly when its timing matters."""
    with collecting() as timings:
        try:
            with span("feature.duckdb_query"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass  # noqa: SIM117
    assert "feature.duckdb_query" in timings.as_dict()


def test_collection_is_per_thread() -> None:
    """FastAPI runs sync endpoints in a threadpool, so one profiled request must not
    collect spans from the concurrent requests running beside it."""
    other_saw: list[object] = []
    done = threading.Event()

    def elsewhere() -> None:
        other_saw.append(current())
        with span("other.work"):
            pass
        done.set()

    with collecting() as timings:
        thread = threading.Thread(target=elsewhere)
        thread.start()
        done.wait(timeout=5)
        thread.join()
        with span("mine.work"):
            pass

    assert other_saw == [None], "the collector leaked into another thread"
    assert set(timings.as_dict()) == {"mine.work"}


def test_a_nested_collection_restores_the_outer_one() -> None:
    """Restoring rather than clearing, so a profiled call inside a profiled batch cannot
    silently discard the outer collector's spans."""
    with collecting() as outer:
        with span("outer.before"):
            pass
        with collecting() as inner, span("inner.only"):
            pass
        with span("outer.after"):
            pass
    assert set(inner.as_dict()) == {"inner.only"}
    assert set(outer.as_dict()) == {"outer.before", "outer.after"}
