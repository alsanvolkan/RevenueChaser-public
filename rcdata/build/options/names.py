"""Which names the options datasets fetch.

- **The list set** (decision D2): SPX always, and the option watch list, read at each run through
  ``rcdata.read.watched("option")`` (``ops.sqlite`` ``watchlist_option``; there is no second accessor). A name added
  to the list is in the next run's set; a name taken off it is no longer fetched, and its rows stay.
"""
from __future__ import annotations

# Fetched whatever the list holds (newdata.OPTIONS_ALWAYS shows the same name on the Settings card).
ALWAYS = ("SPX",)


def list_names() -> list[str]:
    """SPX, then the option watch list in ticker order, each once."""
    from ... import read
    return [*ALWAYS, *sorted(read.watched("option") - set(ALWAYS))]
