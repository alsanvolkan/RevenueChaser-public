"""``prices`` built on a copy of the live file (#446 steps 3 and 4): only what changed is reloaded.

A whole ``prices`` build reads forty years of ``comp.secd`` and thirty of CRSP into TEMP tables before it writes a
row: on homesrv (9 Nov 2026) that was 6 h 12 min and 92 GB of spill on the HDD, most of it before the file grew at
all. A build on the live copy (``serving.build(kind, base="live")``) stages only the slice it rebuilds.

**What is rebuilt.** Two axes, as ``dirty.Dirty`` has them:

- **a day**, ``since`` (the SQL variable ``rc_dirty_since``): every served row on or after it is rebuilt, for every
  security. It is the earliest of: the first day of each changed WRDS year partition (``comp.secd``, ``crsp.dsf``,
  ``comp.sec_divid``: a monthly refresh rewrites its years whole, the daily fetch its newest one), each changed Alpaca
  bar session, and the day after the live file's newest session (an action announced ahead goes ex, a priced row the build date used to cut becomes servable);
- **a security from a day**, the TEMP table ``_dirty_sec (security_id, since)``: that security's rows on or after
  its own ``since`` are rebuilt. ``since`` is ``0001-01-01`` -- the whole history -- for a security that joined or
  left the universe, whose issuer CIK changed, whose CRSP PERMNO link changed, or whose Compustat alias
  (``_gvkey_alias``) formed, moved or went (the fund and the series both). A cover share count that changed restates
  the caps from its ``as_of``; an Alpaca action that moved or changed its ratio or amount restates from its ex-date.
  The set is closed over the issuer: the issuer cap of every class of the issuer moves with one class's cap.

Both come from the live copy itself (its served ids, issuers, Alpaca actions, covers, and the identity the last build
recorded in :data:`IDENTITY_TABLE`: universe, PERMNO link, aliases) against what this build computes, and from the
Parquet files ``dirty.plan`` found changed. The first build on a live file built before the identity was recorded
reads the served ids instead, once: every alias security and series whole, and no PERMNO link change seen.

**What is rebuilt whole every time**, because it is small or needs the whole store: ``shares_class``,
``adj_factor``, ``security_symbol``, ``price_revision``, every Alpaca action, the S&P 500 index bars, the views, and
the build issues other than the three :data:`WINDOWED_ISSUES` (``adjustment_differs`` and the rest read the served
tables, which are whole).

**What is kept** (:data:`KEPT`): ``wrds_price_diff``, the WRDS difference report, which compares the two sources over
their whole history and is the one table that would need it staged; it is rebuilt by the next whole build. The
monthly WRDS refresh (a new ``comp.sec_split`` snapshot), a change of the spine's exchange history
(``wrds/sec_idhist``), a change of the build code, a schema bump and ``RC_BUILD_WHOLE=1`` still build whole
(``full_rebuild_reason``, ``dirty.plan``).

**The issues of a window** (:data:`WINDOWED_ISSUES`) are found only among the rebuilt rows, and a few days before
them where a CRSP split can decide one (``Scope.margin``); an open one outside that stays open (``keep``), because
nothing it was found on has changed.

The proof is ``rcdata/tests/test_prices_incremental.py``: for each kind of change, the build on the live copy serves
exactly what a whole build of the same tree serves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

#: The served tables a build on the live copy keeps as they are.
KEPT = ("wrds_price_diff",)
#: The issue kinds found only among the rebuilt rows (each needs the WRDS history staged).
WINDOWED_ISSUES = ("alpaca_close_differs", "split_only_crsp", "price_gap_no_action")
#: The suffix of the whole-history view of a WRDS staging table on a build on the live copy.
WHOLE = "_whole"
#: The day that means "the whole history" in ``_dirty_sec``.
EVER = date(1, 1, 1)
#: The security id prefixes that are not a Compustat ``gvkey:iid``.
NOT_GVKEY = ("figi", "cik", "sym", "class", "index", "permno")
#: The index bars are always rebuilt whole (one series, one row a session).
INDEX_ID = "index:SPX"
#: The build file's record of the identity it was built with (the universe, the PERMNO link, the Compustat aliases),
#: so the next build on the live copy sees what moved: bookkeeping, not served (``serving.META_TABLES``).
IDENTITY_TABLE = "build_identity"
#: An issue key: ``<security id>:<day>`` or ``<security id>:<day>:<source>``.
_ISSUE_KEY = re.compile(r"^(?P<sid>.+?):(?P<day>\d{4}-\d{2}-\d{2})(?::[^:]*)?$")


@dataclass
class Scope:
    """What a build on the live copy rebuilds: every row on or after ``since``, and each ``by_security`` row on or
    after its own day. Held in the build connection as ``rc_dirty_since`` and ``_dirty_sec``."""

    since: date
    by_security: dict[str, date] = field(default_factory=dict)
    words: list[str] = field(default_factory=list)
    #: Days before what is rebuilt that an issue kind is still looked for (``split_only_crsp``: a CRSP split that
    #: far before a rebuilt gap or Alpaca split decides it, ``serving.NEAR_SPLIT_DAYS``; ``price_gap_no_action``: a
    #: gap that a split from up to ``SPLIT_SAME_DAYS`` later can explain or stop explaining).
    margin: dict[str, int] = field(default_factory=dict)

    def keep(self, kind: str, key: str) -> bool:
        """Whether an open issue of a windowed kind lies outside what was looked at (so it stays open)."""
        if kind not in WINDOWED_ISSUES:
            return False
        m = _ISSUE_KEY.match(key or "")
        if not m:
            return False
        day = date.fromisoformat(m["day"]) + timedelta(days=self.margin.get(kind, 0))
        if day >= self.since:
            return False
        own = self.by_security.get(m["sid"])
        return own is None or day < own


def where(day: str, sid: str = "security_id") -> str:
    """The SQL condition "this row is rebuilt", over a row's day and security id columns."""
    return (f"({day} >= getvariable('rc_dirty_since') OR {sid} IN "
            f"(SELECT d.security_id FROM _dirty_sec d WHERE d.since <= {day}))")


def active(ctx: Any) -> bool:
    return getattr(ctx, "incremental", None) is not None


# ── The dirty-set declaration of the prices datasets (``dirty.plan`` builds a kind on the live copy only when
# every one of its datasets declares one) ──────────────────────────────────────────────────────────────────────

def declared(ctx: Any):
    """Each prices dataset's ``dirty``: the kind words its own set (``scope``), so nothing is narrowed per dataset."""
    from .. import dirty
    return dirty.CLEAN


def whole_reason(ctx: Any) -> str | None:
    """``wrds/prices``' ``full_rebuild_reason``: a change the build on the live copy cannot place on a day."""
    from .. import base
    changed = getattr(ctx, "changed", None) or ()
    root = base.parquet_root()
    idhist = root / "wrds" / "sec_idhist"
    for p in changed:
        if _under(p, idhist):
            return "the spine's exchange history (wrds/sec_idhist) changed"
    # A new snapshot is the monthly WRDS refresh (``wrds_history.SNAPSHOT_DAYS``): the owner's rule is a whole build
    # then (#446 item 6), which also brings the difference report up to date (``KEPT``).
    splits = root / "wrds" / "prices" / "sec_split"
    if any(_under(p, splits) for p in changed):
        return "a new comp.sec_split snapshot (the monthly WRDS refresh)"
    return None


def enable(datasets) -> None:
    from . import wrds_history
    for ds in datasets:
        ds.dirty = declared
        if ds.name == wrds_history.NAME:
            ds.full_rebuild_reason = whole_reason


# ── The scope ────────────────────────────────────────────────────────────────────────────────────────────────

def _under(p: Path, folder: Path) -> bool:
    try:
        Path(p).relative_to(folder)
        return True
    except ValueError:
        return False


def _first_day(path: Path, column: str) -> date | None:
    """The first day of a changed file's partition (``year=`` or ``date=``): a rewritten year may have lost a row
    before its new earliest one (review S4 of #564). The file's earliest ``column`` when it has no such partition."""
    for part in path.parts:
        if part.startswith("year="):
            return date(int(part[5:]), 1, 1)
        if part.startswith("date="):
            return date.fromisoformat(part[5:])
    if path.is_file():
        con = duckdb.connect()
        try:
            from .. import base
            got = con.execute(f"SELECT min({column}) FROM read_parquet({base.sql_path(path)})").fetchone()[0]
        finally:
            con.close()
        if got is not None:
            return got
    return None


def _since_of_files(changed) -> list[tuple[date, str]]:
    """The first day each changed file of a dated source touches, with what it is."""
    from .. import base
    root = base.parquet_root()
    dated = {("wrds", "prices", "secd"): "datadate", ("wrds", "prices", "dsf"): "dlycaldt",
             ("wrds", "prices", "sec_divid"): "datadate", ("alpaca", "bars"): "session_date"}
    out = []
    for p in changed:
        for parts, column in dated.items():
            if _under(p, root.joinpath(*parts)):
                day = _first_day(Path(p), column)
                if day is not None:
                    out.append((day, "/".join(parts[-2:])))
    return out


def scope(ctx: Any, con: duckdb.DuckDBPyConnection, rebuild_shares) -> Scope:
    """Work out what this build on the live copy rebuilds, write ``rc_dirty_since`` and ``_dirty_sec``, rebuild
    ``shares_class`` (``rebuild_shares``: the dataset's own step, run here because the covers are an input to the
    scope), and narrow the WRDS staging to the slice the rebuilt rows need (:func:`restrict`).

    Called after the identity (``_universe``, ``_permno_link``, ``_gvkey_alias``, Alpaca's ``_act``) is built over
    the whole history and before anything served is replaced: the served tables are still the live file's."""
    from . import serving as s
    plan = ctx.incremental
    changed = tuple(getattr(plan, "changed", None) or ())
    words: list[str] = []

    newest = con.execute("SELECT max(session_date) FROM price_bar_daily WHERE security_id NOT LIKE 'index:%'"
                         ).fetchone()[0]
    candidates = _since_of_files(changed)
    if newest is not None:
        candidates.append((newest + timedelta(days=1), "the day after the newest session"))
    since = min((d for d, _ in candidates), default=date(9999, 12, 31))
    why = sorted({w for d, w in candidates if d == since})

    prefixes = ", ".join(f"'{p}'" for p in NOT_GVKEY)
    by: dict[str, date] = {}

    def add(rows, day: date | None = EVER) -> None:
        for r in rows:
            sid, when = (r[0], r[1]) if len(r) > 1 else (r[0], day)
            if sid is None or when is None:
                continue
            if sid not in by or when < by[sid]:
                by[sid] = when

    served = "(SELECT DISTINCT security_id FROM price_bar_daily WHERE security_id NOT LIKE 'index:%')"
    old = _old_identity(con)
    if old is not None:
        # Against the identity the live file was built with: a name that left loses its rows, one that joined gets
        # its whole history, a series whose Compustat alias formed, moved or went is reloaded under both ids, and a
        # security whose PERMNO link changed has its CRSP cross-check (which splits count, ``LINKED_BY_ID``) found
        # again over its whole history.
        con.execute(f"CREATE OR REPLACE TEMP TABLE _identity AS {IDENTITY_SQL}")
        diff = ("SELECT what, key, security_id, how FROM ((SELECT * FROM _old_identity EXCEPT SELECT * FROM _identity) "
                "UNION ALL (SELECT * FROM _identity EXCEPT SELECT * FROM _old_identity))")
        universe = con.execute(f"(SELECT DISTINCT security_id FROM ({diff}) WHERE what = 'universe') "
                               f"UNION (SELECT * FROM {served} EXCEPT SELECT security_id FROM _universe)").fetchall()
        alias = con.execute(f"SELECT security_id FROM ({diff}) WHERE what = 'alias' "
                            f"UNION SELECT key FROM ({diff}) WHERE what = 'alias'").fetchall()
        link = con.execute(f"SELECT DISTINCT security_id FROM ({diff}) WHERE what = 'link'").fetchall()
    else:
        # A live file built before its identity was recorded: what it serves stands in for it, once. A name that
        # joined is one the live file serves nothing of though Compustat prices it (a member with no Compustat
        # series is no joiner, or it would be one on every build); every security served under an alias, and every
        # series an alias takes, is reloaded whole; a changed PERMNO link cannot be seen.
        priced = f"""(SELECT DISTINCT coalesce(a.security_id, w.gvkey || ':' || w.iid) AS security_id
                      FROM {s.wrds_history.STAGING['secd']}{WHOLE} w LEFT JOIN _gvkey_alias a ON a.gid = w.gvkey || ':' || w.iid
                      WHERE w.prccd > 0 AND coalesce(upper(w.curcdd), 'USD') = 'USD'
                        AND w.datadate <= coalesce(getvariable('rc_build_date'), DATE '9999-12-31'))"""
        universe = con.execute(f"(SELECT * FROM {served} EXCEPT SELECT security_id FROM _universe) UNION "
                               f"(SELECT security_id FROM _universe INTERSECT SELECT * FROM {priced} "
                               f"EXCEPT SELECT * FROM {served})").fetchall()
        alias = con.execute(f"SELECT DISTINCT security_id FROM price_bar_daily WHERE source = '{s.wrds_history.COMPUSTAT}' "
                            f"AND split_part(security_id, ':', 1) IN ({prefixes}) "
                            "UNION SELECT DISTINCT security_id FROM _gvkey_alias "
                            "WHERE security_id IN (SELECT security_id FROM _universe) "
                            "UNION SELECT DISTINCT gid FROM _gvkey_alias").fetchall()
        link = []
        words.append("no identity recorded in the live file (a PERMNO link change is not seen this once)")
    add(universe)
    add(alias)
    add(link)
    # The issuer behind each served cap; a security the issuer rule cannot place has none, before and now.
    con.execute(f"CREATE OR REPLACE TEMP TABLE _issuer AS SELECT DISTINCT security_id, cik FROM ({s.ISSUER_SQL})")
    issuer = con.execute("""
        WITH old AS (SELECT DISTINCT security_id, cik FROM market_cap_daily),
             new AS (SELECT o.security_id, i.cik FROM (SELECT DISTINCT security_id FROM old) o
                     LEFT JOIN _issuer i USING (security_id))
        SELECT DISTINCT security_id FROM ((SELECT * FROM old EXCEPT SELECT * FROM new)
                                          UNION ALL (SELECT * FROM new EXCEPT SELECT * FROM old))""").fetchall()
    add(issuer)
    # Alpaca's actions: which one a Compustat action of that kind and day gives way to, and its ratio or amount
    # (a corrected split ratio moves every cover cap after it, review S3 of #564).
    def alpaca(a: str) -> str:
        return (f"{s.ACTION_KIND.format(a=a)} AS k, {s.ACTION_RATIO.format(a=a)} AS ratio, "
                f"{s.ACTION_AMOUNT.format(a=a)} AS amount")
    moved = con.execute(f"""
        WITH old AS (SELECT security_id, ex_date, {s.ACTION_KIND.format(a="c")} AS k, ratio, amount
                     FROM corporate_action c WHERE source = '{s.ALPACA_ACTIONS}' AND ex_date IS NOT NULL),
             new AS (SELECT security_id, ex_date, {alpaca("a")} FROM _act a WHERE ex_date IS NOT NULL)
        SELECT security_id, min(ex_date) FROM ((SELECT * FROM old EXCEPT SELECT * FROM new)
                                               UNION ALL (SELECT * FROM new EXCEPT SELECT * FROM old))
        GROUP BY ALL""").fetchall()
    add(moved)
    # The covers: a changed count restates its security's caps from its as-of.
    cover = "SELECT security_id, as_of, value, source, fetched_at FROM shares_class WHERE measure = 'outstanding'"
    con.execute(f"CREATE OR REPLACE TEMP TABLE _old_cover AS {cover}")
    rebuild_shares()
    covers = con.execute(f"""
        SELECT security_id, min(as_of) FROM ((SELECT * FROM _old_cover EXCEPT ALL ({cover}))
                                             UNION ALL (({cover}) EXCEPT ALL SELECT * FROM _old_cover))
        WHERE as_of IS NOT NULL GROUP BY ALL""").fetchall()
    add(covers)
    add([(INDEX_ID,)])
    # Closed over the issuer, old and new: one class's cap is in every class's issuer cap.
    con.execute("""CREATE OR REPLACE TEMP TABLE _iss AS
        SELECT security_id, cik FROM _issuer WHERE cik IS NOT NULL
        UNION SELECT DISTINCT security_id, cik FROM market_cap_daily WHERE cik IS NOT NULL""")
    while True:
        con.execute("CREATE OR REPLACE TEMP TABLE _dirty_sec (security_id TEXT PRIMARY KEY, since DATE)")
        con.execute("INSERT INTO _dirty_sec SELECT unnest(?), unnest(?)", (list(by), list(by.values())))
        peers = con.execute("""
            SELECT p.security_id, min(d.since) FROM _dirty_sec d JOIN _iss i USING (security_id)
            JOIN _iss p ON p.cik = i.cik
            GROUP BY ALL""").fetchall()
        grew = [(sid, day) for sid, day in peers if sid not in by or day < by[sid]]
        if not grew:
            break
        add(grew)
    con.execute(f"SET VARIABLE rc_dirty_since = DATE '{since.isoformat()}'")

    whole = sum(1 for d in by.values() if d == EVER) - 1                 # the index is no security
    words.append(f"rebuilt from {since} ({', '.join(why) or 'nothing older'})")
    words.append(f"{whole} securities whole ({len(universe)} universe, {len(alias)} alias, {len(link)} PERMNO link, "
                 f"{len(issuer)} issuer)")
    later = sum(1 for d in by.values() if EVER < d < since)
    if later:
        words.append(f"{later} more from their own day (covers, Alpaca actions)")
    result = Scope(since=since, by_security=dict(by), words=words, margin={"split_only_crsp": s.NEAR_SPLIT_DAYS,
                                                                     "price_gap_no_action": s.SPLIT_SAME_DAYS})
    restrict(con, result)
    plan.memo["note"] = "; ".join(words) + f"; {', '.join(KEPT)} kept from the last whole build"
    return result


def restrict(con: duckdb.DuckDBPyConnection, sc: Scope) -> None:
    """Replace the whole-history WRDS staging views by the slice the rebuilt rows need: every row from the January
    of the year before ``since`` (the lag of the first rebuilt row), the last row before that of each series (a
    series silent for over a year), and every row of a security rebuilt from an older day."""
    from . import wrds_history as w
    cut = date(sc.since.year - 1, 1, 1) if sc.since.year > 1 else EVER
    con.execute(f"SET VARIABLE rc_stage_cut = DATE '{cut.isoformat()}'")
    # The Compustat series and the CRSP PERMNOs of the securities rebuilt from before the cut, or from so soon
    # after it that a CRSP split looked for before their own day (``Scope.margin``) lies before it (delta review 1
    # of #564: the split's previous row was not staged, so the split was not seen).
    early = f"d.since - INTERVAL {max(sc.margin.values(), default=0) + 1} DAY < getvariable('rc_stage_cut')"
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _dirty_gid AS
        SELECT security_id AS gid FROM _dirty_sec d WHERE {early}
        UNION SELECT a.gid FROM _gvkey_alias a JOIN _dirty_sec d USING (security_id) WHERE {early}""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _dirty_permno AS
        SELECT l.permno FROM _permno_link l JOIN _dirty_sec d USING (security_id) WHERE {early}""")
    for table, key, day, ids in (("secd", "gvkey, iid", "datadate", "gvkey || ':' || iid IN (SELECT gid FROM _dirty_gid)"),
                                 ("dsf", "permno", "dlycaldt", "permno IN (SELECT permno FROM _dirty_permno)"),
                                 ("sec_divid", None, "datadate", "gvkey || ':' || iid IN (SELECT gid FROM _dirty_gid)")):
        name = w.STAGING[table]
        src = name + WHOLE
        body = f"SELECT * FROM {src} WHERE {day} >= getvariable('rc_stage_cut') OR {ids}"
        if key is not None:
            body += (f" UNION ALL SELECT * FROM {src} WHERE ({key}, {day}) IN (SELECT ({key}, max({day})) FROM {src} "
                     f"WHERE {day} < getvariable('rc_stage_cut') GROUP BY {key}) AND NOT ({ids})")
        con.execute(f"DROP VIEW IF EXISTS {name}")
        con.execute(f"CREATE TEMP TABLE {name} AS {body}")


# ── The identity a build was made with ──────────────────────────────────────────────────────────────────────────

IDENTITY_SQL = """
    SELECT 'universe' AS what, CAST(NULL AS TEXT) AS key, security_id, CAST(NULL AS TEXT) AS how FROM _universe
    UNION ALL SELECT 'link', CAST(permno AS TEXT), security_id, how FROM _permno_link
    UNION ALL SELECT 'alias', gid, security_id, NULL FROM _gvkey_alias"""


def _old_identity(con: duckdb.DuckDBPyConnection) -> bool | None:
    """Copy the live file's recorded identity to ``_old_identity`` (True), or None when it has none."""
    has = con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = ? AND NOT temporary",
                      (IDENTITY_TABLE,)).fetchone()[0]
    if not has:
        return None
    con.execute(f"CREATE OR REPLACE TEMP TABLE _old_identity AS SELECT what, key, security_id, how FROM main.{IDENTITY_TABLE}")
    return True


def record_identity(con: duckdb.DuckDBPyConnection) -> None:
    """Write the identity this build is made with into the build file, whole or on the live copy."""
    con.execute(f"CREATE OR REPLACE TABLE main.{IDENTITY_TABLE} AS {IDENTITY_SQL}")
