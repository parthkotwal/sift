from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scripts.package_artifacts import package_generation, required_artifacts


def _source_fixture(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    event = source / "silver" / "events" / "year=2022" / "data_0.parquet"
    event.parent.mkdir(parents=True)
    event.write_bytes(b"events")

    for index, (path, _relative) in enumerate(required_artifacts(source)):
        if path == event:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"artifact-{index}".encode())
    return source


def test_package_is_self_contained_and_hashed(tmp_path: Path) -> None:
    source = _source_fixture(tmp_path)
    created = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    manifest_path = package_generation(
        source_data=source,
        output_root=tmp_path / "bundles",
        generation_id="2026-07-30-abcdef0",
        git_sha="abcdef0123456789",
        created_at=created,
    )

    manifest = json.loads(manifest_path.read_text())
    assert manifest["generation_id"] == "2026-07-30-abcdef0"
    assert manifest["git_commit"] == "abcdef0123456789"
    assert manifest["created_at"] == "2026-07-30T12:00:00+00:00"
    assert manifest["file_count"] == len(manifest["files"])
    assert manifest["total_bytes"] == sum(item["size_bytes"] for item in manifest["files"])
    assert not any("/raw/" in item["path"] for item in manifest["files"])
    assert not any("training_set" in item["path"] for item in manifest["files"])
    assert not any("two_tower" in item["path"] for item in manifest["files"])

    for item in manifest["files"]:
        artifact = manifest_path.parent / item["path"]
        assert artifact.is_file()
        assert artifact.stat().st_size == item["size_bytes"]
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == item["sha256"]


def test_generation_directory_is_immutable(tmp_path: Path) -> None:
    source = _source_fixture(tmp_path)
    output_root = tmp_path / "bundles"
    package_generation(
        source_data=source,
        output_root=output_root,
        generation_id="generation",
        git_sha="abc",
    )
    with pytest.raises(FileExistsError):
        package_generation(
            source_data=source,
            output_root=output_root,
            generation_id="generation",
            git_sha="abc",
        )


def test_missing_required_artifact_fails(tmp_path: Path) -> None:
    source = _source_fixture(tmp_path)
    output_root = tmp_path / "bundles"
    required, _relative = required_artifacts(source)[0]
    required.unlink()

    with pytest.raises(FileNotFoundError, match="required deployment artifact"):
        package_generation(
            source_data=source,
            output_root=output_root,
            generation_id="generation",
            git_sha="abc",
        )
    assert not (output_root / "generation").exists()
