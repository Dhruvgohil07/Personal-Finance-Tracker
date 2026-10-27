"""The categorizer: decide a transaction's category (SPEC §8, ADR 016).

Pure, like `app/parsers/`: no database, no web framework, no Redis, no
logging. The merchant dictionary and the user's rules are passed in as plain
data by `app/services/categorization.py`, which is the only part that talks
to the database. A test enforces the purity (tests/unit/test_categorizer.py).

    base.py      the facts a decision is made from, the decision, the chain
    handlers.py  one handler per step of SPEC §8's chain
    learning.py  what a correction's rule matches on (SPEC §8.1)
"""
