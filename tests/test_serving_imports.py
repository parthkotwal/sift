"""The serving import surface: starting the API must not require training libraries.

A deployed container installs what the API imports. `implicit` (matrix factorisation)
and `torch` (the rejected two-tower, D26) are training-only: serving reads the factors
those produced, it never fits them. Before `retrieval/artifacts.py` existed the exact
index took its artifact paths from `als.py`, so importing the index imported
`implicit.als` — and a serving image had to carry a library it would never call, while
a training-side import error became an API startup failure.

**These assertions must run in a subprocess.** Inside the pytest session some other
module has already imported `implicit`, so an in-process `sys.modules` check can never
observe its absence — it would pass while proving nothing, the I8 failure mode. Each
test therefore starts a clean interpreter and asks it what it loaded.

`scipy` is deliberately *not* asserted against: LightGBM imports it, and the ranker is
a genuine serving dependency (D27). The line is drawn at what Sift's own code pulls in,
not at what its required libraries do internally.
"""

from __future__ import annotations

import subprocess
import sys

TRAINING_ONLY = ("implicit", "torch")


def _modules_after_importing(target: str) -> set[str]:
    """Import `target` in a clean interpreter; return which training libs it loaded."""
    probe = (
        f"import importlib, sys; importlib.import_module({target!r}); "
        f"print(' '.join(m for m in {TRAINING_ONLY!r} if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"importing {target} failed:\n{result.stderr}"
    return set(result.stdout.split())


def test_the_api_entrypoint_does_not_import_training_libraries() -> None:
    """`sift.api.main:app` is what the container runs, so this is the binding case."""
    loaded = _modules_after_importing("sift.api.main")
    assert loaded == set(), (
        f"serving entrypoint pulled in training-only libraries: {sorted(loaded)}. "
        "Artifact paths and shapes belong in sift.retrieval.artifacts, which imports "
        "neither."
    )


def test_the_exact_index_does_not_import_training_libraries() -> None:
    """The specific edge that caused this: the index needs four paths and a width, and
    took them from the module that fits the factors."""
    assert _modules_after_importing("sift.retrieval.index") == set()


def test_the_artifacts_module_is_importable_on_its_own() -> None:
    """It is the shared floor of the two lifecycles, so it must not depend on either."""
    assert _modules_after_importing("sift.retrieval.artifacts") == set()


def test_the_probe_would_notice_a_training_library() -> None:
    """Negative control. Every assertion above is an *absence*, and an absence passes
    just as happily when the probe is broken — so prove the probe can see a positive
    before trusting it to report a negative (I8)."""
    assert _modules_after_importing("sift.retrieval.als") == {"implicit"}


def test_training_paths_still_resolve_through_their_original_modules() -> None:
    """The move must be invisible to training code: `als` and `interactions` re-export
    the constants, so existing imports keep working and no artifact path changed."""
    from sift.retrieval import als, artifacts, index, interactions

    for name in ("FACTORS", "USER_FACTORS", "ITEM_FACTORS", "USER_IDS", "ITEM_IDS"):
        assert getattr(als, name) == getattr(artifacts, name), name
        assert getattr(index, name) == getattr(artifacts, name), name
    assert interactions.ALS_DIR == artifacts.ALS_DIR
    assert interactions.INTERACTIONS == artifacts.INTERACTIONS
    # The paths themselves are part of the contract: a deployment bundle is built from
    # these names, so a rename here silently breaks artifact packaging.
    assert artifacts.ITEM_FACTORS.name == "item_embedding_behavioral_v1.npy"
    assert artifacts.ITEM_IDS.name == "item_ids.json"
    assert artifacts.ALS_DIR.name == "als"
