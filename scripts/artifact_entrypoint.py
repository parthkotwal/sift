"""Download and verify one S3 artifact generation before running a task command."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from scripts.artifact_contract import (
    EVENTS_PREFIX,
    MANIFEST_SCHEMA_VERSION,
    expected_embedding_versions,
    expected_feature_versions,
    expected_selected_models,
    required_fixed_bundle_paths,
)
from sift.config import DATA_DIR
from sift.store.online import SCHEMA_VERSION

ARTIFACT_PREFIX = "sift/artifacts"
MANIFEST_NAME = ".sift-artifact-manifest.json"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
S3_CONFIG = Config(
    connect_timeout=5,
    read_timeout=60,
    retries={"total_max_attempts": 4, "mode": "adaptive"},
)
TRANSFER_CONFIG = TransferConfig(max_concurrency=4)


class ArtifactContractError(RuntimeError):
    """The selected generation is incomplete, unsafe, or incompatible."""


class S3Downloader(Protocol):
    def download_file(
        self,
        Bucket: str,
        Key: str,
        Filename: str,
        Config: TransferConfig,
    ) -> None: ...


@dataclass(frozen=True)
class ArtifactFile:
    path: PurePosixPath
    size_bytes: int
    sha256: str


def _manifest_files(raw: object, generation_id: str) -> tuple[ArtifactFile, ...]:
    if not isinstance(raw, dict):
        raise ArtifactContractError("artifact manifest must be a JSON object")
    manifest = cast(dict[str, object], raw)
    expected = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "generation_id": generation_id,
        "redis_schema_version": SCHEMA_VERSION,
        "selected_models": expected_selected_models(),
        "feature_versions": expected_feature_versions(),
        "embedding_versions": expected_embedding_versions(),
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise ArtifactContractError(
                f"artifact manifest has incompatible {field}: "
                f"expected {value!r}, got {manifest.get(field)!r}"
            )

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ArtifactContractError("artifact manifest files must be a non-empty list")

    files = []
    seen: set[PurePosixPath] = set()
    for raw_file in cast(list[object], raw_files):
        if not isinstance(raw_file, dict):
            raise ArtifactContractError("artifact manifest file entry must be an object")
        entry = cast(dict[str, object], raw_file)
        raw_path = entry.get("path")
        size = entry.get("size_bytes")
        sha256 = entry.get("sha256")
        if not isinstance(raw_path, str):
            raise ArtifactContractError("artifact file path must be a string")
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("data",):
            raise ArtifactContractError(f"unsafe artifact path: {raw_path!r}")
        if path in seen:
            raise ArtifactContractError(f"duplicate artifact path: {raw_path!r}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ArtifactContractError(f"invalid artifact size for {raw_path!r}")
        if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
            raise ArtifactContractError(f"invalid artifact digest for {raw_path!r}")
        seen.add(path)
        files.append(ArtifactFile(path, size, sha256))

    required = {PurePosixPath(path.as_posix()) for path in required_fixed_bundle_paths()}
    missing = sorted(str(path) for path in required - seen)
    if missing:
        raise ArtifactContractError(f"artifact manifest omits required file: {missing[0]}")
    events_prefix = PurePosixPath(EVENTS_PREFIX.as_posix())
    if not any(path.is_relative_to(events_prefix) and path.suffix == ".parquet" for path in seen):
        raise ArtifactContractError("artifact manifest contains no canonical event partition")
    if manifest.get("file_count") != len(files):
        raise ArtifactContractError("artifact manifest file_count does not match files")
    if manifest.get("total_bytes") != sum(artifact.size_bytes for artifact in files):
        raise ArtifactContractError("artifact manifest total_bytes does not match files")
    return tuple(files)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        while chunk := artifact.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download_generation(
    *,
    client: S3Downloader,
    bucket: str,
    generation_id: str,
    destination: Path = DATA_DIR,
) -> Path:
    """Download and verify a generation into an initially empty data directory."""
    if (
        not bucket
        or not generation_id
        or "/" in generation_id
        or generation_id in {".", ".."}
    ):
        raise ArtifactContractError("bucket and generation must be non-empty")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ArtifactContractError(f"artifact destination is not empty: {destination}")

    prefix = f"{ARTIFACT_PREFIX}/{generation_id}"
    temporary_manifest = destination / ".manifest.download"
    client.download_file(
        bucket,
        f"{prefix}/manifest.json",
        str(temporary_manifest),
        Config=TRANSFER_CONFIG,
    )
    try:
        manifest_raw = json.loads(temporary_manifest.read_text())
        files = _manifest_files(manifest_raw, generation_id)
        for artifact in files:
            relative = Path(*artifact.path.parts[1:])
            local_path = destination / relative
            local_path.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(
                bucket,
                f"{prefix}/{artifact.path.as_posix()}",
                str(local_path),
                Config=TRANSFER_CONFIG,
            )
            if local_path.stat().st_size != artifact.size_bytes:
                raise ArtifactContractError(f"size mismatch for {artifact.path}")
            if _sha256(local_path) != artifact.sha256:
                raise ArtifactContractError(f"checksum mismatch for {artifact.path}")
        final_manifest = destination / MANIFEST_NAME
        temporary_manifest.replace(final_manifest)
        return final_manifest
    except Exception:
        temporary_manifest.unlink(missing_ok=True)
        raise


def _configured_generation() -> tuple[str, str] | None:
    bucket = os.environ.get("SIFT_ARTIFACT_BUCKET")
    generation = os.environ.get("SIFT_ARTIFACT_GENERATION")
    if bucket is None and generation is None:
        return None
    if not bucket or not generation:
        raise ArtifactContractError(
            "SIFT_ARTIFACT_BUCKET and SIFT_ARTIFACT_GENERATION must be set together"
        )
    return bucket, generation


def main(argv: Sequence[str] | None = None) -> int:
    command = list(argv if argv is not None else sys.argv[1:])
    if not command:
        print("artifact entrypoint requires a task command", file=sys.stderr)
        return 2
    try:
        configured = _configured_generation()
        if configured is not None:
            bucket, generation = configured
            client = cast(S3Downloader, boto3.client("s3", config=S3_CONFIG))
            manifest = download_generation(
                client=client,
                bucket=bucket,
                generation_id=generation,
            )
            print(f"verified artifact generation {generation} at {manifest.parent}")
        os.execvp(command[0], command)
    except (ArtifactContractError, BotoCoreError, ClientError, OSError, ValueError) as error:
        print(f"artifact startup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
