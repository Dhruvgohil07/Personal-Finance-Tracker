"""Kharcha backend."""

# The app version, defined ONCE here. The code reads it from this variable
# (FastAPI docs title bar, GET /health), never from a copied string.
# It must match `version` in pyproject.toml; tests/unit/test_version.py fails
# if the two ever drift apart, so bump both together.
__version__ = "0.1.0"
