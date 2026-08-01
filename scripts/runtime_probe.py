"""Record task CPU/BLAS settings before replacing this process with the API server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence

NUMPY_PROBE = """
import contextlib
import io
import json
import numpy
import threadpoolctl

output = io.StringIO()
with contextlib.redirect_stdout(output):
    numpy.show_runtime()
pools = [
    {
        key: pool.get(key)
        for key in (
            "architecture",
            "internal_api",
            "num_threads",
            "prefix",
            "threading_layer",
            "version",
        )
    }
    for pool in threadpoolctl.threadpool_info()
]
print(
    json.dumps(
        {
            "numpy_version": numpy.__version__,
            "show_runtime": output.getvalue(),
            "threadpools": pools,
        }
    )
)
"""


def _numpy_runtime() -> object:
    probe = subprocess.run(
        [sys.executable, "-c", NUMPY_PROBE],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(probe.stdout)


def _logical_cpu_count() -> int | None:
    return os.cpu_count()


def runtime_record() -> dict[str, object]:
    """Return one low-cardinality startup record with NumPy's real runtime report."""
    return {
        "event": "sift_runtime",
        "logical_cpu_count": _logical_cpu_count(),
        "openblas_num_threads": os.environ.get("OPENBLAS_NUM_THREADS"),
        "omp_num_threads": os.environ.get("OMP_NUM_THREADS"),
        "numpy": _numpy_runtime(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    command = list(argv if argv is not None else sys.argv[1:])
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print("runtime probe requires a command", file=sys.stderr)
        return 2
    try:
        print(json.dumps(runtime_record(), sort_keys=True), flush=True)
        os.execvp(command[0], command)
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
        print(f"runtime probe failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
