from __future__ import annotations

import json
import logging.config
from pathlib import Path

import pytest
from scripts import runtime_probe


def test_runtime_record_captures_cpu_environment_and_numpy_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_probe, "_logical_cpu_count", lambda: 2)
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setattr(
        runtime_probe,
        "_numpy_runtime",
        lambda: {
            "numpy_version": "2.5.1",
            "show_runtime": "'num_threads': 1",
            "threadpools": [{"internal_api": "openblas", "num_threads": 1}],
        },
    )

    assert runtime_probe.runtime_record() == {
        "event": "sift_runtime",
        "logical_cpu_count": 2,
        "openblas_num_threads": "1",
        "omp_num_threads": None,
        "numpy": {
            "numpy_version": "2.5.1",
            "show_runtime": "'num_threads': 1",
            "threadpools": [{"internal_api": "openblas", "num_threads": 1}],
        },
    }


def test_uvicorn_access_log_includes_the_worker_process_id() -> None:
    config_path = Path("scripts/uvicorn_log_config.json")
    config = json.loads(config_path.read_text())
    assert "%(process)d" in config["formatters"]["access"]["fmt"]
    logging.config.dictConfig(config)
