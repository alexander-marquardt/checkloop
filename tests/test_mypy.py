"""Verify that the source tree passes mypy --strict with no errors."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SRC = str(Path(__file__).parent.parent / "src" / "checkloop")


def test_mypy_strict() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mypy", _SRC],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"mypy reported type errors:\n{result.stdout}{result.stderr}"
