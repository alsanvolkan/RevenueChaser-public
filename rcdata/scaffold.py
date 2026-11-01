"""Create a serving file with its tables and no rows, for a kind that has never been built.

Four of the seven serving files have never been built on the server. To a board, and to the Settings / Data screen,
"never created" and "created and empty" look identical: both answer no rows. They are not the same thing — one
means the build has never run, the other that it ran and found nothing — and only the first also makes every reader
of that kind raise on open. An empty file with its tables closes that gap: the screen can say which of the two it
is, and a reader gets no rows instead of a missing file.

The tables come from the build modules' own schemas, never from a list of their own here, so a table added to a
build appears in the empty file without a second edit. Creating a file is a write to the live store, so
``create_empty`` refuses a kind that already has one: it only ever fills a gap.
"""
from __future__ import annotations

from pathlib import Path

from . import serving


class AlreadyBuilt(FileExistsError):
    """This kind already has a live serving file; it is never replaced from here."""


def _options_schemas() -> dict[str, list[tuple[str, str]]]:
    from .build.options import tables
    return {name: list(t.schema) for name, t in tables.TABLES.items()}


def _ownership_schemas() -> dict[str, list[tuple[str, str]]]:
    """The ownership kind's tables: the 13F serve module's four, plus one per provider module.

    Each provider module names its served columns (``SERVED``: the raw schema with ``ticker`` and ``source`` added),
    and only those modules know them, so they are read from there rather than restated.
    """
    from .build.ownership import finra, form4, ftd, ibkr, regsho, sched13dg, serve
    out = {t: list(s) for t, s in serve.SCHEMAS.items()}
    for mod in (finra, ibkr, regsho, ftd, form4, sched13dg):
        out[mod.TABLE] = list(mod.SERVED)
    return out


def _schema_of_kind() -> dict[str, dict[str, list[tuple[str, str]]]]:
    """``{kind: {table: [(column, type), ...]}}``, each kind's from the module that builds it."""
    from .build.fundamentals import tables as fundamentals_tables
    from .build.market_series import SCHEMA as MARKET_SERIES_SCHEMA, TABLE as MARKET_SERIES_TABLE
    from .build.prices import serving as prices_serving
    from .build.spine import sec_tickers_mf as spine_fund
    from .build.spine import security as spine_security
    from .build.uw_spot_expiry_strike import SCHEMA as SPX_SCHEMA, TABLE as SPX_TABLE

    return {
        "market": {MARKET_SERIES_TABLE: list(MARKET_SERIES_SCHEMA), SPX_TABLE: list(SPX_SCHEMA)},
        # ``spine/security`` builds four of the five; ``sec/company_tickers_mf`` serves ``fund`` itself.
        "spine": {**{t: list(s) for t, s in spine_security.SCHEMAS.items()},
                  spine_fund.TABLE: list(spine_fund.FUND)},
        "prices": {t: list(s) for t, s in prices_serving.SCHEMAS.items()},
        "fundamentals": {t: list(s) for t, s in fundamentals_tables.SCHEMAS.items()},
        "ownership": _ownership_schemas(),
        "options": _options_schemas(),
    }


#: Resolved once at import, so a test can read it without building anything.
SCHEMA_OF_KIND = _schema_of_kind()


def create_empty(kind: str) -> Path:
    """Build ``kind``'s serving file with every table of its schema and no rows; the live path it was swapped to.

    :class:`AlreadyBuilt` when the kind already has a live file, so this never replaces data.
    """
    serving._kind(kind)
    if serving.live_path(kind).exists():
        raise AlreadyBuilt(f"{kind} already has a serving file at {serving.live_path(kind)}")
    schemas = SCHEMA_OF_KIND[kind]
    with serving.build(kind) as b:
        for table, columns in schemas.items():
            cols = ", ".join(f"{c} {t}" for c, t in columns)
            b.con.execute(f"CREATE TABLE {table} ({cols})")
        return b.swap({table: 0 for table in schemas})


def create_missing() -> dict[str, Path]:
    """Create an empty serving file for every kind that has none; ``{kind: path}`` of what was created."""
    made: dict[str, Path] = {}
    for kind in serving.KINDS:
        if not serving.live_path(kind).exists():
            made[kind] = create_empty(kind)
    return made
