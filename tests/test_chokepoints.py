"""Chokepoint 2, asserted structurally: the as-of join exists in exactly one place.

ARCHITECTURE ("The spine") requires that training assembly and serving obtain
features only through the store's read path. That is easy to state and easy to
erode — someone in a hurry writes one more ASOF join next to the code that needs it,
and point-in-time correctness quietly becomes a convention several call sites are
trusted to follow rather than a property of one query.

This is worth asserting *structurally* rather than trusting review, and it is the
one chokepoint an equivalence test cannot protect: a second read path would have
nothing to be compared against. The other three chokepoints are covered elsewhere —
sandboxed compute by `state.py` holding the only aggregation SQL, future-invariance
and the leak tests in `test_spine.py`, and the snapshot blocklist in
`test_dim_business.py` and `test_definitions.py`.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

import sift.offline.training_set
import sift.ranking.online
import sift.ranking.rank
import sift.ranking.train
import sift.store.read

# Modules that consume features. Anything added here must read through the store.
CONSUMERS: tuple[ModuleType, ...] = (
    sift.offline.training_set,
    sift.ranking.rank,
    sift.ranking.online,
    sift.ranking.train,
)


def _source(module: ModuleType) -> str:
    assert module.__file__ is not None
    return Path(module.__file__).read_text()


@pytest.mark.parametrize("module", CONSUMERS, ids=lambda m: m.__name__)
def test_consumers_do_not_write_their_own_as_of_join(module: ModuleType) -> None:
    source = _source(module).upper()
    assert "ASOF JOIN" not in source, f"{module.__name__} builds its own as-of join"
    assert "ASOF LEFT JOIN" not in source, (
        f"{module.__name__} builds its own as-of join"
    )


@pytest.mark.parametrize("module", CONSUMERS, ids=lambda m: m.__name__)
def test_consumers_do_not_import_the_state_sql(module: ModuleType) -> None:
    """State SQL is the store's business. A consumer importing `state_query` would be
    materialising its own features, which is the same defect wearing a helper."""
    source = _source(module)
    assert "state_query" not in source, f"{module.__name__} builds state itself"
    assert "features.state" not in source, f"{module.__name__} imports state SQL"


def test_the_as_of_join_exists_in_exactly_one_module() -> None:
    """Not 'few' — one. Counted across the whole package so the claim stays true as
    the codebase grows, rather than only for today's consumer list.

    Matched case-sensitively on the SQL keyword form: the emitted query uses
    `ASOF LEFT JOIN`, while every prose mention of the concept writes "ASOF join"
    with a lowercase verb. That is a text heuristic, not a parser — it would miss a
    join assembled from fragments, and it would false-positive on a docstring that
    shouted the keyword. It is cheap and it fails loudly on the realistic mistake,
    which is someone pasting a working join into a second module."""
    package = Path(sift.store.read.__file__).parents[1]
    owners = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "ASOF LEFT JOIN" in path.read_text() or "ASOF JOIN" in path.read_text()
    )
    assert owners == ["store/read.py"], f"as-of joins found in {owners}"


def test_feature_reads_go_through_the_store() -> None:
    """Every consumer that uses features must name the read path it uses."""
    for module in CONSUMERS:
        source = _source(module)
        if "feature" not in source.lower():
            continue
        assert (
            "sift.store.read" in source
            or "sift.store.online" in source
            or "sift.features.definitions" in source
        ), f"{module.__name__} touches features without going through the store"
