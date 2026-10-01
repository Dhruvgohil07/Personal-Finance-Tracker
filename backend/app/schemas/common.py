"""Small building blocks shared by several request schemas."""

# For StringConstraints(pattern=...): no control characters, i.e. none of
# U+0000-U+001F (NUL, tab, newline, ...) or U+007F (DEL).
# A NUL byte can't be stored in a Postgres text column at all: the driver
# raises an error on INSERT, which would surface as a 500. Tabs and newlines
# have no place in a one-line label either. With this pattern they become a
# normal 422 instead.
# (Lone surrogates like "\ud800" are already rejected by Pydantic's JSON
# parser, so they never reach our schemas.)
NO_CONTROL_CHARS = r"^[^\x00-\x1f\x7f]*$"
