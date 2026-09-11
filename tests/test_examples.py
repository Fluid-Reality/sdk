from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


SDK_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = tuple(sorted((SDK_ROOT / "examples").glob("[0-9][0-9]_*.py")))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda path: path.name)
def test_example_help_is_available(example: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SDK_ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(example), "--help"],
        cwd=SDK_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_every_numbered_example_is_documented() -> None:
    readme = (SDK_ROOT / "README.md").read_text(encoding="utf-8")
    for example in EXAMPLES:
        assert f"examples/{example.name}" in readme
