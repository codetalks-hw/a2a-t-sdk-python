"""Run the JavaScript canonical-form harness against the shared golden vectors.

The SDK's ``a2at-canonical/1`` profile claims another implementation can recompute the digest.
The harness under ``tests/js`` is that other implementation; this test only proves it reproduces
every vector the Python writer produced and rejects every vector the Python writer rejects.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _node_binary() -> str:
    """Return the Node.js executable, skipping the test cleanly when it is absent."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    return node


def test_javascript_implementation_agrees_with_every_golden_vector() -> None:
    node = _node_binary()
    harness = HERE / "js" / "canonical.test.mjs"
    assert harness.is_file()

    result = subprocess.run(
        [node, str(harness)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "canonical conformance ok:" in result.stdout
