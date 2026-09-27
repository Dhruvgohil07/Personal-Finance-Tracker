"""The version in app/__init__.py must match the one in pyproject.toml."""

import tomllib
from pathlib import Path

from app import __version__

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_app_version_matches_pyproject() -> None:
    # tomllib (standard library since Python 3.11) parses TOML files.
    # It needs the file opened in binary mode ("rb").
    with PYPROJECT.open("rb") as file:
        pyproject = tomllib.load(file)

    assert __version__ == pyproject["project"]["version"]
