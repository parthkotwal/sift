"""Artifact paths and shapes shared by ALS training and ALS serving.

These constants describe *where the artifacts live and what shape they are* — facts
both the trainer that writes them and the index that reads them need, and which depend
on no library either of them uses.

**This module must never import a training-only dependency** (`implicit`, `torch`,
`scipy`). It exists precisely to break that edge: the exact index needs four paths and
a width, and it used to get them from `als.py`, so importing the index pulled in
`implicit.als` — and through it `scipy` — before the API could answer `/health`. A
serving container was obliged to install the matrix-factorisation library it would
never call, and a training import error became an API startup failure.

The split is by *lifecycle*, not by convenience: a path is data about the artifact and
survives any change of trainer, while the fitting code is one implementation of how the
artifact came to exist. `als.py` and `interactions.py` re-export these names, so
existing training imports keep working unchanged.

`tests/test_serving_imports.py` asserts the property in a subprocess, which is the only
way to check it — inside the test session some other module has already imported
`implicit`, so `sys.modules` in-process can never show its absence.
"""

from __future__ import annotations

from sift.config import DERIVED_DIR
from sift.features.definitions import BEHAVIORAL_EMBEDDING_DIM

ALS_DIR = DERIVED_DIR / "als"

# The ALS factor width. Defined by the embedding registry rather than repeated here, so
# the index's expected vector length and the registered definition's shape cannot drift
# apart — the mismatch that would otherwise surface as a silent dimension error.
FACTORS = BEHAVIORAL_EMBEDDING_DIM

INTERACTIONS = ALS_DIR / "interactions.parquet"

USER_FACTORS = ALS_DIR / "user_embedding_behavioral_v1.npy"
ITEM_FACTORS = ALS_DIR / "item_embedding_behavioral_v1.npy"
USER_IDS = ALS_DIR / "user_ids.json"
ITEM_IDS = ALS_DIR / "item_ids.json"
USER_FACTOR_PARQUET = ALS_DIR / "user_embedding_behavioral_v1.parquet"
ITEM_FACTOR_PARQUET = ALS_DIR / "item_embedding_behavioral_v1.parquet"
ALS_MANIFEST = ALS_DIR / "manifest.json"
