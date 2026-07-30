"""Every ordering of model scores in the funnel breaks ties the same way.

A funnel stage's job is to *refine* the ordering the previous stage handed it. When two
candidates score identically the stage has expressed no opinion between them, so the
incumbent order is the only defensible answer — anything else scrambles a ranking the
previous stage had a reason for, and does it invisibly, because the output still looks
sorted.

`np.argsort` defaults to an unstable sort, so getting this right is one keyword per call
site and getting it wrong is silent. I6 is the record of that divergence living in the
codebase for four days across two paths that looked identical; D32 is why it closed.
Asserting it across the package rather than per call site is the point: the failure mode
is a *new* ordering added next to the existing ones, which no test of today's five would
catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

import sift.store.read

PACKAGE = Path(sift.store.read.__file__).parents[1]


def _argsort_calls() -> list[tuple[str, int, ast.Call]]:
    """Every `argsort` call in the package, with the file and line to name in a failure."""
    found: list[tuple[str, int, ast.Call]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "argsort"
            ):
                found.append((path.relative_to(PACKAGE).as_posix(), node.lineno, node))
    return found


def test_the_package_still_has_orderings_to_check() -> None:
    """Guards the guard: a scan that silently matches nothing passes forever."""
    assert len(_argsort_calls()) >= 5


@pytest.mark.parametrize(
    ("where", "call"),
    [((f"{file}:{line}"), call) for file, line, call in _argsort_calls()],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_score_ordering_is_stable(where: str, call: ast.Call) -> None:
    kinds = [kw.value for kw in call.keywords if kw.arg == "kind"]
    assert kinds, f"{where}: argsort without kind='stable' scrambles tied candidates"
    assert isinstance(kinds[0], ast.Constant) and kinds[0].value == "stable", (
        f"{where}: argsort kind must be 'stable', got {ast.unparse(kinds[0])}"
    )


def test_a_stable_descending_argsort_actually_preserves_input_order() -> None:
    """The property the scan above is a proxy for, pinned against numpy itself.

    Negating scores to sort descending is the idiom every call site uses; this asserts
    the negation does not quietly reverse tied runs along with the ordering.
    """
    scores = np.array([1.0, 3.0, 3.0, 2.0, 3.0])
    order = np.argsort(-scores, kind="stable")
    assert order.tolist() == [1, 2, 4, 3, 0]
