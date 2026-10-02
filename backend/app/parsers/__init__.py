"""Statement parsers and normalization (SPEC §6).

This package is PURE: it must not import FastAPI, SQLAlchemy, the database
session, Redis or anything that talks to the network (SPEC §3 layering
rule). Everything here is plain Python working on strings, dates and
integers, so it can be unit tested without Postgres, Redis or a web
request. `tests/unit/test_parsers_base.py` enforces that with a test.
"""
