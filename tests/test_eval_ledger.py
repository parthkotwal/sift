"""The eval ledger: a ratchet on the headline metrics.

The numbers themselves come from gitignored Yelp data, so they cannot be asserted here
(I24). What *can* be asserted — and is the part that would silently fail — is the
mechanism: that a regression is caught, that it is not quietly recorded, that a moved
eval set is treated as a different failure from a worse model, and that the first run
on a fresh clone establishes a baseline rather than looking like a pass.
"""

from __future__ import annotations

import json
from pathlib import Path

from sift.eval.ledger import check, load, report_and_exit_code
from sift.eval.run import EvalReport


def _report(name: str = "ALS", recall_500: float = 0.2519, ndcg: float = 0.0294) -> EvalReport:
    return EvalReport(
        name=name,
        n_users=26_489,
        recall={10: 0.0386, 500: recall_500},
        ndcg_at_10=ndcg,
    )


def test_the_first_run_establishes_a_baseline_rather_than_passing_silently(
    tmp_path: Path,
) -> None:
    """A fresh clone has no ledger. "No baseline" and "no regression" must not look
    alike, or the very first run of a broken model reads as a clean one."""
    path = tmp_path / "ledger.json"
    lines, findings = check([_report()], path=path)
    assert findings == {"ALS": []}
    assert any("NEW" in line for line in lines)
    assert load(path)["ALS"]["metrics"]["recall@500"] == 0.2519  # type: ignore[index]


def test_a_regression_is_caught(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    check([_report(recall_500=0.2519)], path=path)
    _, findings = check([_report(recall_500=0.2400)], path=path)
    regressed = findings["ALS"]
    assert [f.metric for f in regressed] == ["recall@500"]
    assert regressed[0].baseline == 0.2519
    assert regressed[0].current == 0.2400
    assert not regressed[0].fatal


def test_a_regression_is_not_recorded_as_the_new_baseline(tmp_path: Path) -> None:
    """The way a ratchet fails is by ratcheting the wrong way once: if a bad run were
    written, the *next* run would be judged against it and the loss would vanish."""
    path = tmp_path / "ledger.json"
    check([_report(recall_500=0.2519)], path=path)
    check([_report(recall_500=0.2400)], path=path)
    assert load(path)["ALS"]["metrics"]["recall@500"] == 0.2519  # type: ignore[index]

    # A third run at the original value must therefore still be clean.
    _, findings = check([_report(recall_500=0.2519)], path=path)
    assert findings["ALS"] == []


def test_accept_records_the_regression_deliberately(tmp_path: Path) -> None:
    """D29 is the standing example: a stage can land while lowering the metric, but
    only with an argument. The flag exists so that is a decision, not a default."""
    path = tmp_path / "ledger.json"
    check([_report(recall_500=0.2519)], path=path)
    check([_report(recall_500=0.2400)], path=path, accept=True)
    assert load(path)["ALS"]["metrics"]["recall@500"] == 0.2400  # type: ignore[index]


def test_an_improvement_is_recorded_and_named(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    check([_report(recall_500=0.2131)], path=path)
    lines, findings = check([_report(recall_500=0.2519)], path=path)
    assert findings["ALS"] == []
    assert any("0.2131 -> 0.2519" in line for line in lines)
    assert load(path)["ALS"]["metrics"]["recall@500"] == 0.2519  # type: ignore[index]


def test_a_changed_eval_set_is_fatal_not_a_regression(tmp_path: Path) -> None:
    """D18 froze the holdout. If n_users moves, the two runs did not measure the same
    thing, so an *improvement* is exactly as untrustworthy as a decline — and --accept
    must not silence it, because it is not a claim about the model at all."""
    path = tmp_path / "ledger.json"
    check([_report()], path=path)

    moved = EvalReport(
        name="ALS", n_users=20_000, recall={10: 0.0386, 500: 0.9999}, ndcg_at_10=0.9
    )
    _, findings = check([moved], path=path)
    fatal = [f for f in findings["ALS"] if f.fatal]
    assert [f.metric for f in fatal] == ["n_users"]
    assert report_and_exit_code([moved], path=path, accept=True) == 2, (
        "--accept must not silence a changed eval set"
    )


def test_exit_codes_separate_the_three_outcomes(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    assert report_and_exit_code([_report()], path=path) == 0
    assert report_and_exit_code([_report(recall_500=0.24)], path=path) == 1
    assert report_and_exit_code([_report(recall_500=0.24)], path=path, accept=True) == 0


def test_each_run_is_tracked_under_its_own_name(tmp_path: Path) -> None:
    """The entrypoints emit several reports per run — ALS, the ranker, each rerank
    variant — and a regression in one must not be masked by another holding steady."""
    path = tmp_path / "ledger.json"
    check([_report("ALS", 0.2519), _report("popularity", 0.2131)], path=path)
    _, findings = check(
        [_report("ALS", 0.2519), _report("popularity", 0.1000)], path=path
    )
    assert findings["ALS"] == []
    assert [f.metric for f in findings["popularity"]] == ["recall@500"]
    assert load(path)["ALS"]["metrics"]["recall@500"] == 0.2519  # type: ignore[index]
    assert load(path)["popularity"]["metrics"]["recall@500"] == 0.2131  # type: ignore[index]


def test_a_ledger_written_by_an_older_shape_does_not_crash(tmp_path: Path) -> None:
    """The ledger is a local artifact that outlives refactors of this module, so a
    missing or differently-shaped entry must degrade to 'no baseline', not to a
    traceback in the middle of a 10-minute eval run."""
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"ALS": {"recall@500": 0.2519}}))
    _, findings = check([_report()], path=path)
    assert findings["ALS"] == []
    assert load(path)["ALS"]["metrics"]["recall@500"] == 0.2519  # type: ignore[index]
