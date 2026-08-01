from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from boto3.s3.transfer import TransferConfig
from scripts.artifact_contract import (
    MANIFEST_SCHEMA_VERSION,
    expected_embedding_versions,
    expected_feature_versions,
    expected_selected_models,
    required_fixed_bundle_paths,
)
from scripts.artifact_entrypoint import (
    ARTIFACT_PREFIX,
    ArtifactContractError,
    _configured_generation,
    download_generation,
)

from sift.store.online import SCHEMA_VERSION

GENERATION = "2026-07-30-abcdef0"
BUCKET = "private-sift-artifacts"


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.downloaded: list[str] = []

    def download_file(
        self,
        Bucket: str,
        Key: str,
        Filename: str,
        Config: TransferConfig,
    ) -> None:
        assert Bucket == BUCKET
        assert isinstance(Config, TransferConfig)
        self.downloaded.append(Key)
        Path(Filename).write_bytes(self.objects[Key])


def _objects() -> dict[str, bytes]:
    payloads = {
        path.as_posix(): f"payload:{path.as_posix()}".encode()
        for path in required_fixed_bundle_paths()
    }
    event = "data/silver/events/year=2022/data_0.parquet"
    payloads[event] = b"event payload"
    files = [
        {
            "path": path,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for path, payload in payloads.items()
    ]
    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "generation_id": GENERATION,
        "redis_schema_version": SCHEMA_VERSION,
        "selected_models": expected_selected_models(),
        "feature_versions": expected_feature_versions(),
        "embedding_versions": expected_embedding_versions(),
        "file_count": len(files),
        "total_bytes": sum(len(payload) for payload in payloads.values()),
        "files": files,
    }
    prefix = f"{ARTIFACT_PREFIX}/{GENERATION}"
    return {
        f"{prefix}/manifest.json": json.dumps(manifest).encode(),
        **{f"{prefix}/{path}": payload for path, payload in payloads.items()},
    }


def test_download_generation_verifies_self_contained_bundle(tmp_path: Path) -> None:
    objects = _objects()
    client = FakeS3(objects)
    destination = tmp_path / "data"

    manifest = download_generation(
        client=client,
        bucket=BUCKET,
        generation_id=GENERATION,
        destination=destination,
    )

    assert manifest == destination / ".sift-artifact-manifest.json"
    assert manifest.is_file()
    expected_keys = set(objects)
    assert set(client.downloaded) == expected_keys
    for path in required_fixed_bundle_paths():
        assert (tmp_path / path).is_file()


def test_download_generation_rejects_checksum_mismatch(tmp_path: Path) -> None:
    objects = _objects()
    artifact_key = next(key for key in objects if key.endswith(".npy"))
    objects[artifact_key] = b"tampered"

    with pytest.raises(ArtifactContractError, match="mismatch"):
        download_generation(
            client=FakeS3(objects),
            bucket=BUCKET,
            generation_id=GENERATION,
            destination=tmp_path / "data",
        )


def test_download_generation_rejects_unsafe_manifest_path(tmp_path: Path) -> None:
    objects = _objects()
    manifest_key = f"{ARTIFACT_PREFIX}/{GENERATION}/manifest.json"
    manifest = json.loads(objects[manifest_key])
    manifest["files"][0]["path"] = "data/../escaped"
    objects[manifest_key] = json.dumps(manifest).encode()
    client = FakeS3(objects)

    with pytest.raises(ArtifactContractError, match="unsafe artifact path"):
        download_generation(
            client=client,
            bucket=BUCKET,
            generation_id=GENERATION,
            destination=tmp_path / "data",
        )
    assert client.downloaded == [manifest_key]


def test_download_generation_rejects_incompatible_schema(tmp_path: Path) -> None:
    objects = _objects()
    manifest_key = f"{ARTIFACT_PREFIX}/{GENERATION}/manifest.json"
    manifest = json.loads(objects[manifest_key])
    manifest["redis_schema_version"] = SCHEMA_VERSION - 1
    objects[manifest_key] = json.dumps(manifest).encode()

    with pytest.raises(ArtifactContractError, match="redis_schema_version"):
        download_generation(
            client=FakeS3(objects),
            bucket=BUCKET,
            generation_id=GENERATION,
            destination=tmp_path / "data",
        )


def test_artifact_environment_must_be_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIFT_ARTIFACT_BUCKET", BUCKET)
    monkeypatch.delenv("SIFT_ARTIFACT_GENERATION", raising=False)
    with pytest.raises(ArtifactContractError, match="must be set together"):
        _configured_generation()
