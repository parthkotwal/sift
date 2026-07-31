"""The eval ledger: headline metrics as a versioned artifact that refuses to regress.

`DECISIONS.md` records every landing number in prose, and the rule is that nothing
lands without beating its predecessor. Nothing enforced it. The suite covers component
properties and the metric *functions*, but never the end-to-end number that decides
whether a model ships, so a refactor could move recall@500 and the only thing standing
between that and a wrong conclusion was somebody remembering to re-run eval (I24).

**Why this is not a test.** The numbers come from the real Yelp dump, which is
gitignored and must stay that way. A CI test asserting 0.2519 either cannot run or
reintroduces a hidden dependency on machine state (I1). A synthetic fixture large
enough to make ALS beat popularity by a stable margin would be asserting a property of
the fixture, not of the model — I8 with extra steps.

So the split is: the *mechanism* here is unit-tested with synthetic reports and runs
anywhere; the *numbers* live in a gitignored ledger beside the model artifacts, and the
eval entrypoints diff against it and refuse to record a regression without an explicit
flag. A fresh clone has no ledger and its first run establishes one — stated plainly
because "no baseline" and "no regression" must not look alike.

The ledger stays out of git for the same reason `data/RESULTS.md` does: these are
dataset-derived metrics and Yelp's terms restrict publishing them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sift.config import DERIVED_DIR

if TYPE_CHECKING:  # pragma: no cover - import cycle, see below
    from sift.eval.run import EvalReport

# `EvalReport` is imported for types only. `eval.run` calls into this module from its
# entrypoint, so a runtime import here would close the cycle; nothing below needs the
# class at runtime, only four of its attributes. The dependency that matters points one
# way — the ledger knows the shape of a report, a report knows nothing of the ledger.

LEDGER = DERIVED_DIR / "eval_ledger.json"

# Deterministic pipelines (seed 42, single-threaded ALS) reproduce these exactly on one
# machine; the tolerance absorbs last-bit float summation differences across platforms
# without hiding a real move. Anything a model change can do is orders of magnitude
# larger than this.
TOLERANCE = 1e-6


@dataclass(frozen=True)
class Finding:
    """One way this run disagrees with the recorded baseline."""

    metric: str
    baseline: float
    current: float
    fatal: bool = False

    @property
    def delta(self) -> float:
        return self.current - self.baseline

    def render(self) -> str:
        mark = "INVALID" if self.fatal else "REGRESSION"
        return (
            f"  {mark}  {self.metric:<16} {self.baseline:.4f} -> {self.current:.4f} "
            f"({self.delta:+.4f})"
        )


def _metrics(report: EvalReport) -> dict[str, float]:
    return {
        **{f"recall@{k}": value for k, value in sorted(report.recall.items())},
        "ndcg@10": report.ndcg_at_10,
    }


def load(path: Path = LEDGER) -> dict[str, dict[str, object]]:
    """Recorded baselines by run name; empty when no ledger exists yet."""
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a ledger object")
    return loaded


def compare(report: EvalReport, baseline: dict[str, object] | None) -> list[Finding]:
    """What this run says that the baseline does not. Empty means clean or first-ever.

    A changed eval-set size is reported as **fatal**, not as a regression. D18 froze the
    holdout, so if `n_users` moves, the two runs did not measure the same thing and
    comparing their metrics is meaningless in either direction — an improvement would
    be just as untrustworthy as a decline. That is a different failure from "the model
    got worse" and must not be silenced by the same flag.
    """
    if baseline is None:
        return []
    findings: list[Finding] = []

    recorded_users = baseline.get("n_users")
    if isinstance(recorded_users, int) and recorded_users != report.n_users:
        findings.append(
            Finding(
                metric="n_users",
                baseline=float(recorded_users),
                current=float(report.n_users),
                fatal=True,
            )
        )

    recorded = _recorded_metrics(baseline)
    for metric, current in _metrics(report).items():
        before = recorded.get(metric)
        if before is not None and current < before - TOLERANCE:
            findings.append(Finding(metric=metric, baseline=before, current=current))
    return findings


def _recorded_metrics(baseline: dict[str, object] | None) -> dict[str, float]:
    """The baseline's metrics as floats, tolerating a ledger written by an older shape."""
    recorded = (baseline or {}).get("metrics")
    if not isinstance(recorded, dict):
        return {}
    return {
        str(metric): float(value)
        for metric, value in recorded.items()
        if isinstance(value, int | float)
    }


def _improvements(report: EvalReport, baseline: dict[str, object] | None) -> list[str]:
    """Metrics that moved up, so a clean run still says what it gained."""
    recorded = _recorded_metrics(baseline)
    return [
        f"{metric} {recorded[metric]:.4f} -> {value:.4f}"
        for metric, value in _metrics(report).items()
        if metric in recorded and value > recorded[metric] + TOLERANCE
    ]


def entry(report: EvalReport) -> dict[str, object]:
    return {
        "n_users": report.n_users,
        "metrics": _metrics(report),
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def check(
    reports: list[EvalReport],
    *,
    path: Path = LEDGER,
    accept: bool = False,
) -> tuple[list[str], dict[str, list[Finding]]]:
    """Diff every report against the ledger and record the clean ones.

    Returns the lines to print and the findings by run name. Reports that regress are
    **not** written unless `accept` is set, so a bad run cannot quietly become the
    baseline the next run is judged against — the way a ratchet fails is by ratcheting
    the wrong way once.
    """
    ledger = load(path)
    lines: list[str] = []
    all_findings: dict[str, list[Finding]] = {}
    updated = dict(ledger)

    for report in reports:
        baseline = ledger.get(report.name)
        recorded = baseline if isinstance(baseline, dict) else None
        findings = compare(report, recorded)
        all_findings[report.name] = findings
        if recorded is None:
            lines.append(f"  NEW         {report.name}")
            updated[report.name] = entry(report)
            continue
        if findings:
            lines.append(f"  {report.name}")
            lines.extend(f"  {finding.render()}" for finding in findings)
            if accept:
                updated[report.name] = entry(report)
            continue
        improvements = _improvements(report, recorded)
        lines.append(
            f"  OK          {report.name}"
            + (f"  ({'; '.join(improvements)})" if improvements else "  (unchanged)")
        )
        updated[report.name] = entry(report)

    if updated != ledger:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n")
    return lines, all_findings


def report_and_exit_code(
    reports: list[EvalReport], *, path: Path = LEDGER, accept: bool = False
) -> int:
    """Print the ledger verdict; return the process exit code."""
    lines, findings = check(reports, path=path, accept=accept)
    print(f"\neval ledger ({path}):")
    for line in lines:
        print(line)
    fatal = [f for group in findings.values() for f in group if f.fatal]
    regressed = [f for group in findings.values() for f in group if not f.fatal]
    if fatal:
        print(
            "\n  The eval set itself changed, so these runs are not comparable. D18 froze"
            "\n  the holdout; rebuild it or find out what moved before reading any metric"
            "\n  above. --accept does not apply to this."
        )
        return 2
    if regressed and not accept:
        print(
            "\n  Not recorded. Re-run with --accept to make these the new baseline, and"
            "\n  say why in DECISIONS.md — 'nothing lands without beating its predecessor'"
            "\n  allows exceptions, but only argued ones (D29 is the standing example)."
        )
        return 1
    return 0
