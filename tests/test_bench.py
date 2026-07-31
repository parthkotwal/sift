"""The load benchmark's aggregation and contract verdict.

The HTTP transport is injected, so these tests pin the part that decides PASS/MISS
without needing a running server. A benchmark that reports the wrong verdict is worse
than no benchmark: it launders a broken deployment into a green check.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from sift.api.bench import (
    END_TO_END_P99_MS,
    STAGE_TRIPWIRE_MS,
    SUPPORTED_CONCURRENCY,
    BenchReport,
    deterministic_users,
    quiet_host_threshold,
    run_benchmark,
)


def _sample(total: float, **stages: float) -> dict[str, float]:
    row = {
        "retrieval_ms": 2.0,
        "feature_lookup_ms": 20.0,
        "ranking_ms": 3.0,
        "overhead_ms": 0.1,
        "total_ms": total,
        "client_wall_ms": total + 1.0,
    }
    row.update(stages)
    return row


def _fetch(
    samples: list[dict[str, float]],
) -> tuple[Callable[[str], dict[str, float]], list[str]]:
    calls: list[str] = []

    def fetch(user_id: str) -> dict[str, float]:
        calls.append(user_id)
        # Warm-up consumes the first sample; index into the list so each request differs.
        return samples[min(len(calls) - 1, len(samples) - 1)]

    return fetch, calls


def test_a_healthy_run_passes_and_reports_every_returned_stage() -> None:
    # concurrency 1, so the stage tripwires are actually asserted rather than skipped.
    fetch, _ = _fetch([_sample(30.0)])
    report = run_benchmark(fetch, ["u1", "u2", "u3"], concurrency=1)
    assert report.checks_tripwires
    assert report.ok
    assert report.requests == 3
    assert report.errors == 0
    assert report.over_contract == 0
    # Stage names come from the payload, not a hardcoded list, so a new stage in the
    # endpoint shows up without editing the benchmark.
    assert [s.name for s in report.stages] == [
        "retrieval_ms",
        "feature_lookup_ms",
        "ranking_ms",
        "overhead_ms",
        "total_ms",
        "client_wall_ms",
    ]
    assert "PASS" in report.render()


def test_a_stage_over_its_budget_fails_the_run() -> None:
    over = STAGE_TRIPWIRE_MS["feature_lookup_ms"] + 1.0
    fetch, _ = _fetch([_sample(90.0, feature_lookup_ms=over)])
    report = run_benchmark(fetch, ["u1", "u2"], concurrency=1)
    assert not report.ok
    stage = next(s for s in report.stages if s.name == "feature_lookup_ms")
    assert not stage.within_budget
    assert "OVER" in report.render()
    assert "MISS" in report.render()


def test_end_to_end_breaches_are_counted_and_fail_the_run() -> None:
    fetch, _ = _fetch([_sample(END_TO_END_P99_MS + 50.0)])
    report = run_benchmark(fetch, ["u1", "u2", "u3", "u4"], concurrency=2)
    assert report.over_contract == 4
    assert not report.ok
    assert "4/4" in report.render()


def test_failed_requests_fail_the_run_rather_than_being_dropped() -> None:
    """Averaging only the successes reports a profile for a service that is not working."""

    def fetch(user_id: str) -> dict[str, float]:
        if user_id == "bad":
            raise OSError("connection reset")
        return _sample(20.0)

    report = run_benchmark(fetch, ["u1", "bad", "u2"], concurrency=1)
    assert report.errors == 1
    assert report.requests == 2
    assert not report.ok
    assert "FAILED REQUESTS: 1" in report.render()


def test_warmup_request_is_excluded_from_the_percentiles() -> None:
    """A cold process builds the ALS catalog relation once (~215ms, I31); counting it
    would put a one-time cost in every percentile."""
    cold = _sample(500.0)
    warm = _sample(25.0)
    calls: list[str] = []

    def fetch(user_id: str) -> dict[str, float]:
        calls.append(user_id)
        return cold if len(calls) == 1 else warm

    report = run_benchmark(fetch, ["u1", "u2"], concurrency=1, warmup=True)
    total = next(s for s in report.stages if s.name == "total_ms")
    assert total.p99 == 25.0, "the 500ms cold request leaked into the distribution"
    assert report.requests == 2


def test_concurrency_beyond_the_stated_envelope_is_flagged_not_silently_reported() -> None:
    fetch, _ = _fetch([_sample(30.0)])
    rendered = run_benchmark(fetch, ["u1"], concurrency=SUPPORTED_CONCURRENCY * 2).render()
    assert f"exceeds the {SUPPORTED_CONCURRENCY}" in rendered
    assert "envelope, not a defect" in rendered


def test_rejects_a_run_it_cannot_measure() -> None:
    fetch, _ = _fetch([_sample(30.0)])
    with pytest.raises(ValueError, match="concurrency"):
        run_benchmark(fetch, ["u1"], concurrency=0)
    with pytest.raises(ValueError, match="no users"):
        run_benchmark(fetch, [], concurrency=1)


def test_user_sample_is_deterministic_and_hash_ordered() -> None:
    pool = [f"u{index}" for index in range(20)]
    first = deterministic_users(5, pool)
    assert first == deterministic_users(5, pool), "same inputs must measure the same work"
    assert first != pool[:5], "file order correlates with activity; must not be a prefix"
    # Asking for more than the pool holds cycles rather than silently shrinking the run.
    assert len(deterministic_users(45, pool)) == 45


def test_empty_run_renders_without_dividing_by_zero() -> None:
    report = BenchReport(
        url="http://x", concurrency=1, requests=0, errors=0, stages=(), over_contract=0
    )
    assert "0/0" in report.render()


def test_stage_tripwires_are_not_asserted_above_their_calibration_concurrency() -> None:
    """A stage over budget under load is the envelope, not a regression. Asserting
    tripwires there would make every honest high-concurrency run look like a failure,
    which is how a verdict column stops being read."""
    over = STAGE_TRIPWIRE_MS["feature_lookup_ms"] + 100.0
    fetch, _ = _fetch([_sample(90.0, feature_lookup_ms=over)])
    loaded = run_benchmark(fetch, ["u1", "u2"], concurrency=8)
    assert not loaded.checks_tripwires
    assert loaded.ok, "tripwires must not be asserted above the calibration concurrency"
    assert "OVER" not in loaded.render(), "must not flag a line it is not asserting"
    assert "not asserted here" in loaded.render()

    # The identical samples at the calibrated concurrency must fail.
    fetch, _ = _fetch([_sample(90.0, feature_lookup_ms=over)])
    calibrated = run_benchmark(fetch, ["u1", "u2"], concurrency=1)
    assert not calibrated.ok


def test_the_end_to_end_contract_is_asserted_at_every_concurrency() -> None:
    """The tripwires relax under load; the serving promise does not."""
    for concurrency in (1, 4, 16):
        fetch, _ = _fetch([_sample(END_TO_END_P99_MS + 1.0)])
        report = run_benchmark(fetch, ["u1", "u2"], concurrency=concurrency)
        assert not report.ok, f"end-to-end breach passed at concurrency {concurrency}"


def test_a_contended_host_is_reported_rather_than_quietly_measured() -> None:
    """Absolute latency on a loaded machine measures the machine. The report has to say
    so, because the numbers otherwise look like ordinary results."""
    busy = BenchReport(
        url="http://x",
        concurrency=1,
        requests=1000,
        errors=0,
        stages=(),
        over_contract=0,
        load_before=quiet_host_threshold() * 4,
        load_after=quiet_host_threshold() * 4,
    )
    assert not busy.host_was_quiet
    assert "NOT A QUIET HOST" in busy.render()
    assert "describe a contended machine" in busy.render()

    calm = BenchReport(
        url="http://x",
        concurrency=1,
        requests=1000,
        errors=0,
        stages=(),
        over_contract=0,
        load_before=0.0,
        load_after=0.0,
    )
    assert calm.host_was_quiet
    assert "NOT A QUIET HOST" not in calm.render()


def test_an_unreadable_load_average_does_not_block_measurement() -> None:
    """Refusing to measure because the platform will not report load would be worse
    than measuring without the caveat."""
    report = BenchReport(
        url="http://x", concurrency=1, requests=10, errors=0, stages=(), over_contract=0
    )
    assert report.host_was_quiet
    assert "host load" not in report.render()
