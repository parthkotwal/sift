"""Build one immutable, self-verifying Sift serving artifact generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from sift.config import DATA_DIR
from sift.features.definitions import (
    USER_BEHAVIORAL_EMBEDDING,
    get,
    get_embedding,
    online_features,
)
from sift.offline.dim_business import DIM_BUSINESS
from sift.offline.ingest import EVENTS_DIR
from sift.offline.popularity import POPULARITY_ARTIFACT
from sift.ranking.train import ALS_RANKER_MODEL
from sift.retrieval.artifacts import FACTORS, ITEM_FACTORS, ITEM_IDS
from sift.store.materialize import HISTORICAL_DIR, state_path
from sift.store.online import SCHEMA_VERSION

MANIFEST_SCHEMA_VERSION = 1
STATE_GROUPS = ("user", "item", "user_category", "user_als", "item_als")


def _relative_to_data(path: Path) -> Path:
    return path.relative_to(DATA_DIR)


def required_artifacts(source_data: Path) -> tuple[tuple[Path, Path], ...]:
    """Return ``(source, bundle-relative path)`` for the serving generation."""
    configured = (
        ITEM_FACTORS,
        ITEM_IDS,
        get_embedding(USER_BEHAVIORAL_EMBEDDING).artifact,
        POPULARITY_ARTIFACT,
        ALS_RANKER_MODEL,
        DIM_BUSINESS,
        *(state_path(group, HISTORICAL_DIR) for group in STATE_GROUPS),
    )
    relative = tuple(_relative_to_data(path) for path in configured)
    events_relative = _relative_to_data(EVENTS_DIR)
    event_files = tuple(sorted((source_data / events_relative).glob("year=*/*.parquet")))
    if not event_files:
        event_root = source_data / events_relative
        raise FileNotFoundError(f"no canonical event partitions under {event_root}")
    event_pairs = tuple(
        (path, Path("data") / path.relative_to(source_data)) for path in event_files
    )
    configured_pairs = tuple(
        (source_data / path, Path("data") / path) for path in relative
    )
    return configured_pairs + event_pairs


def _copy_and_hash(source: Path, destination: Path) -> tuple[int, str]:
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError(f"required deployment artifact is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        while chunk := input_file.read(1024 * 1024):
            output_file.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    shutil.copymode(source, destination)
    return size, digest.hexdigest()


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def package_generation(
    *,
    source_data: Path,
    output_root: Path,
    generation_id: str,
    git_sha: str,
    created_at: datetime | None = None,
) -> Path:
    """Copy the exact serving inputs into a new immutable generation directory."""
    if not generation_id or "/" in generation_id or generation_id in {".", ".."}:
        raise ValueError("generation ID must be one non-empty path component")
    generation_dir = output_root / generation_id
    output_root.mkdir(parents=True, exist_ok=True)
    if generation_dir.exists():
        raise FileExistsError(generation_dir)

    sources = required_artifacts(source_data)
    missing = [
        source
        for source, _relative in sources
        if source.is_symlink() or not source.is_file()
    ]
    if missing:
        raise FileNotFoundError(
            f"required deployment artifact is not a regular file: {missing[0]}"
        )

    embedding = get_embedding(USER_BEHAVIORAL_EMBEDDING)
    with tempfile.TemporaryDirectory(prefix=f".{generation_id}-", dir=output_root) as staging:
        staging_dir = Path(staging)
        files = []
        total_bytes = 0
        for source, relative in sources:
            size, sha256 = _copy_and_hash(source, staging_dir / relative)
            total_bytes += size
            files.append(
                {
                    "path": relative.as_posix(),
                    "size_bytes": size,
                    "sha256": sha256,
                }
            )

        manifest = {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "generation_id": generation_id,
            "git_commit": git_sha,
            "created_at": (created_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
            "redis_schema_version": SCHEMA_VERSION,
            "selected_models": {
                "retrieval": {
                    "kind": "exact_als",
                    "item_embedding": "item_embedding_behavioral_v1",
                    "dimensions": FACTORS,
                },
                "ranker": {
                    "kind": "lightgbm",
                    "artifact": (
                        Path("data") / _relative_to_data(ALS_RANKER_MODEL)
                    ).as_posix(),
                },
                "rerank": {"decision": "D29"},
            },
            "feature_versions": {
                name: get(name).version for name in online_features()
            },
            "embedding_versions": {embedding.name: embedding.version},
            "file_count": len(files),
            "total_bytes": total_bytes,
            "files": files,
        }
        (staging_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        staging_dir.rename(generation_dir)
        return generation_dir / "manifest.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", type=Path, default=DATA_DIR)
    parser.add_argument("--output-root", type=Path, default=DATA_DIR / "deployment")
    parser.add_argument("--generation", required=True)
    parser.add_argument("--git-sha", default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    manifest = package_generation(
        source_data=args.source_data,
        output_root=args.output_root,
        generation_id=args.generation,
        git_sha=args.git_sha or _git_sha(),
    )
    data = json.loads(manifest.read_text())
    print(
        f"packaged generation {data['generation_id']}: "
        f"{data['file_count']} files / {data['total_bytes']:,} bytes -> {manifest.parent}"
    )


if __name__ == "__main__":
    main()
