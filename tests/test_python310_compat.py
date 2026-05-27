"""Regression tests guarding against Python 3.10 compatibility breakage.

pyMC Repeater supports Python 3.10+ (LuckFox Pico Ultra ships with 3.10).
These tests scan the source tree statically so regressions are caught in CI
without needing a 3.10 interpreter in the test environment.
"""

import ast
import sys
from pathlib import Path

_REPEATER_ROOT = Path(__file__).parent.parent / "repeater"


def _py_files():
    return [p for p in _REPEATER_ROOT.rglob("*.py") if ".pyc" not in str(p)]


def test_minimum_python_version():
    """Fail fast if the test environment itself is below the minimum supported version."""
    assert sys.version_info >= (3, 10), (
        f"Python 3.10+ required, running {sys.version_info.major}.{sys.version_info.minor}"
    )


def test_no_datetime_utc():
    """`datetime.UTC` was added in 3.11 — `timezone.utc` must be used instead."""
    violations = []
    for path in _py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # catch: from datetime import UTC
            if isinstance(node, ast.ImportFrom) and node.module == "datetime":
                if any(alias.name == "UTC" for alias in node.names):
                    rel = path.relative_to(_REPEATER_ROOT.parent)
                    violations.append(f"  {rel}:{node.lineno}")
            # catch: datetime.UTC
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "UTC"
                and isinstance(node.value, ast.Name)
                and node.value.id == "datetime"
            ):
                rel = path.relative_to(_REPEATER_ROOT.parent)
                violations.append(f"  {rel}:{node.lineno}")

    assert not violations, (
        "datetime.UTC (Python 3.11+) found in the following files — "
        "use timezone.utc instead:\n" + "\n".join(violations)
    )
