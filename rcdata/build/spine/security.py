"""``spine/security``: the served spine, built from the sources' staging tables (no fetch, no network).

**Tables** (``spine.duckdb``, serving schema version 1):

- ``security``: one row per ticker of the newest SEC file: CIK, name, exchange, the tier (``tier.py``) and the type
  it came from, the Compustat link (gvkey, iid, and how it was made), the CUSIP and the ISIN derived from it, the
  FIGI fields, the current GICS codes and the sector and industry the old tables carry (bridge names, below), each
  with its source, and the SEC file's provenance (``source_as_of``, ``fetched_at``, ``run_id``);
- ``security_history``: one row per value and period: the SEC file's ``cik``, ``name`` and ``exchange`` per ticker
  over the stored snapshots, and Compustat's identifier history (``comp.sec_idhist``: ``tic``, ``cusip``, ``exchg``,
  ...) of the linked securities;
- ``gics_history``: Compustat's GICS periods (``comp.co_hgic``) of the linked companies;
- ``index_membership``: who is in each index and when, from the fund files (``funds/index_holdings``, voted) and
  from Compustat's constituents history (``wrds/idxcst_his``, the check).

Every period is ``valid_from`` inclusive and ``valid_to`` exclusive (the first day it no longer holds); ``valid_to``
is NULL while it holds. Compustat's inclusive end dates (``effthru``, ``indthru``, ``thru``) are moved one day on.

**The link.** A ticker's CIK finds its Compustat companies (``comp.company.cik``); among their securities the one
whose ticker (``tic``, ``BRK.B`` -> ``BRK-B``) is the SEC ticker is the link (``link_source`` ``tic``). With no ticker
match, a CIK with one SEC ticker and one active US security takes that one (``only_security``). Otherwise there is no
link, and no CUSIP.

**Bridge names.** ``sector`` is the GICS sector through ``core.gics_map.canonical_sector`` (one of the 11 Morningstar
names the boards use); ``industry`` is the GICS industry group's name (working choice: the spine holds no Unusual
Whales industry text). Both feed the views only.

**Build issues** (``ISSUE_KINDS``) are computed here and written to ``ops.sqlite`` once the new file is live
(``after_swap``, ``ops.sync_issues``): an issue found again stays open, one no longer found is resolved. A rule that
needs a source that has never run is not applied; that source is one ``source_never_run`` issue instead.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta

import duckdb

from ....core import calendar
from ....core.gics_map import UNCLASSIFIED, canonical_sector
from ....core.identity import isin as isin_mod
from ... import ops
from .. import base
from . import KIND, stage, tier as tier_rule, views
from .funds import NAME as FUNDS
from .openfigi_map import NAME as OPENFIGI, PRESENT
from .sec_tickers import NAME as SEC
from .wrds import CO_HGIC, IDXCST_HIS, INDEX_GVKEYX, SEC_IDHIST, norm_cik

NAME = "spine/security"
TABLES = ("security", "security_history", "gics_history", "index_membership")
# Compustat ``exchg`` codes of the US exchanges the SEC file's listed rows trade on: NYSE, AMEX, Nasdaq.
US_EXCHG = (11, 12, 14)
# How far ahead of a fund day's SEC snapshot (the newest on or before the day) a line is looked for (delta S1): a fund
# day is up to 6 days after its snapshot, the next weekly file 7 days, one missed week 14 more, so 21 days, the bound
# included. Two missed weeks of the SEC file is no lookahead. Only for a ticker the SEC file has not held yet, or held
# under the same CIK (delta-2 SF1, delta-3 SF-A).
LOOKAHEAD = timedelta(days=21)

SECURITY = [
    ("ticker", "TEXT PRIMARY KEY"), ("cik", "TEXT"), ("name", "TEXT"), ("exchange", "TEXT"),
    ("tier", "TEXT"), ("tier_rule", "INTEGER"), ("security_type", "TEXT"), ("type_source", "TEXT"),
    ("gvkey", "TEXT"), ("iid", "TEXT"), ("link_source", "TEXT"),
    ("cusip", "TEXT"), ("cusip_source", "TEXT"), ("isin", "TEXT"), ("isin_source", "TEXT"),
    ("figi", "TEXT"), ("composite_figi", "TEXT"), ("share_class_figi", "TEXT"), ("exch_code", "TEXT"),
    ("figi_ticker", "TEXT"), ("security_description", "TEXT"), ("figi_source", "TEXT"),
    ("gsector", "TEXT"), ("ggroup", "TEXT"), ("gind", "TEXT"), ("gsubind", "TEXT"), ("gics_from", "DATE"),
    ("gics_source", "TEXT"), ("sector", "TEXT"), ("industry", "TEXT"),
    ("source", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT"),
]
HISTORY = [("ticker", "TEXT"), ("cik", "TEXT"), ("gvkey", "TEXT"), ("iid", "TEXT"), ("field", "TEXT"),
           ("value", "TEXT"), ("valid_from", "DATE"), ("valid_to", "DATE"), ("source", "TEXT")]
GICS = [("gvkey", "TEXT"), ("cik", "TEXT"), ("gsector", "TEXT"), ("ggroup", "TEXT"), ("gind", "TEXT"),
        ("gsubind", "TEXT"), ("valid_from", "DATE"), ("valid_to", "DATE"), ("source", "TEXT"),
        ("source_as_of", "DATE")]
MEMBERSHIP = [("index_slug", "TEXT"), ("ticker", "TEXT"), ("cik", "TEXT"), ("gvkey", "TEXT"), ("iid", "TEXT"),
              ("valid_from", "DATE"), ("valid_to", "DATE"), ("source", "TEXT"), ("files_held", "INTEGER"),
              ("files_counted", "INTEGER"), ("source_as_of", "DATE")]
SCHEMAS = {"security": SECURITY, "security_history": HISTORY, "gics_history": GICS, "index_membership": MEMBERSHIP}

# The plan's named problems, one kind each; ``key`` is the ticker unless the kind says otherwise.
ISSUE_KINDS = {
    "source_never_run": "a source this build needs has no data yet (key: the dataset)",
    "tier_a_no_wrds_link": "a Tier A name whose CIK finds no Compustat security",
    "tier_a_no_cusip": "a Tier A name with a Compustat link but no CUSIP",
    "tier_a_no_figi": "a Tier A CUSIP OpenFIGI has no FIGI for (asked: absent; or not asked yet)",
    "tier_a_no_gics": "a Tier A name with a Compustat link but no current GICS",
    "figi_ticker_differs": "OpenFIGI's ticker for the CUSIP is not the SEC ticker",
    "cusip_shared": "one CUSIP linked to two SEC tickers (key: the CUSIP)",
    "cik_many_gvkeys": "a CIK with several Compustat companies and no ticker match among them (key: the CIK)",
    "index_key_empty": "an index key of INDEX_GVKEYX with no comp.idxcst_his rows (key: the index)",
    "fund_file_short": "a fund file under its index's floor today, so it did not count (key: index:provider-fund)",
    "member_not_in_sec": "a fund-file holding that matches no SEC ticker (key: index:ticker)",
    "member_not_tier_a": "an index member that is not Tier A (key: index:ticker)",
    "membership_files_disagree": "the fund files of an index disagree on a name today (key: index:ticker)",
    "membership_wrds_differs": ("fund-file members and Compustat's differ on the Compustat snapshot day "
                                "(key: index:ticker)"),
}

# GICS names (the 2023 structure) of the two levels the bridge names come from.
GICS_SECTORS = {"10": "Energy", "15": "Materials", "20": "Industrials", "25": "Consumer Discretionary",
                "30": "Consumer Staples", "35": "Health Care", "40": "Financials", "45": "Information Technology",
                "50": "Communication Services", "55": "Utilities", "60": "Real Estate"}
GICS_GROUPS = {
    "1010": "Energy", "1510": "Materials", "2010": "Capital Goods", "2020": "Commercial & Professional Services",
    "2030": "Transportation", "2510": "Automobiles & Components", "2520": "Consumer Durables & Apparel",
    "2530": "Consumer Services", "2550": "Consumer Discretionary Distribution & Retail",
    "3010": "Consumer Staples Distribution & Retail", "3020": "Food, Beverage & Tobacco",
    "3030": "Household & Personal Products", "3510": "Health Care Equipment & Services",
    "3520": "Pharmaceuticals, Biotechnology & Life Sciences", "4010": "Banks", "4020": "Financial Services",
    "4030": "Insurance", "4510": "Software & Services", "4520": "Technology Hardware & Equipment",
    "4530": "Semiconductors & Semiconductor Equipment", "5010": "Telecommunication Services",
    "5020": "Media & Entertainment", "5510": "Utilities", "6010": "Equity Real Estate Investment Trusts (REITs)",
    "6020": "Real Estate Management & Development"}


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _next_day(d: date | None) -> date | None:
    return None if d is None else d + timedelta(days=1)


def compustat_ticker(tic) -> str | None:
    """Compustat's ``tic`` as the SEC file writes it: ``BRK.B`` -> ``BRK-B``."""
    t = str(tic or "").strip().upper().replace(".", "-")
    return t or None


def sector_names(gsector, ggroup) -> tuple[str, str]:
    """The bridge ``(sector, industry)``: the Morningstar sector of the GICS sector, and the GICS group's name."""
    name = GICS_SECTORS.get(str(gsector or ""))
    return (canonical_sector(name) if name else UNCLASSIFIED), GICS_GROUPS.get(str(ggroup or ""), UNCLASSIFIED)


# ── The link ──────────────────────────────────────────────────────────────────────────────────────────────────

def link(ticker: str, cik: str, gvkeys_of_cik: dict[str, list[str]], securities: dict[str, list[dict]],
         tickers_of_cik: dict[str, int]) -> tuple[dict | None, str | None, str | None]:
    """``(security row, link_source, issue)`` for one SEC ticker; issue is ``cik_many_gvkeys`` or None."""
    gvkeys = gvkeys_of_cik.get(cik) or []
    candidates = [s for g in gvkeys for s in securities.get(g, [])]
    matched = [s for s in candidates if compustat_ticker(s.get("tic")) == ticker]
    if matched:
        matched.sort(key=lambda s: (s.get("secstat") != "A", str(s.get("iid"))))
        return matched[0], "tic", None
    active = [s for s in candidates if s.get("secstat") == "A" and s.get("exchg") in US_EXCHG and s.get("cusip")]
    if len(active) == 1 and tickers_of_cik.get(cik, 0) == 1:
        return active[0], "only_security", None
    return None, None, ("cik_many_gvkeys" if len(gvkeys) > 1 else None)


# ── Periods ───────────────────────────────────────────────────────────────────────────────────────────────────

def sec_history(snapshots: list[dict], fields=("cik", "name", "exchange")) -> list[dict]:
    """``security_history`` rows from the SEC-file snapshots: one per ticker, field and run of equal values.

    A ticker missing from a snapshot ends its periods on that snapshot's day; a later return starts new ones.
    """
    days = sorted({r["source_as_of"] for r in snapshots})
    by_ticker: dict[str, dict[date, dict]] = defaultdict(dict)
    for r in snapshots:
        by_ticker[r["ticker"]][r["source_as_of"]] = r
    out = []
    for ticker, seen in sorted(by_ticker.items()):
        for field in fields:
            cur, start = None, None
            for d in days:
                row = seen.get(d)
                value = None if row is None else row.get(field)
                present = row is not None
                if start is not None and (not present or value != cur):
                    out.append({"ticker": ticker, "cik": seen[start]["cik"], "field": field, "value": cur,
                                "valid_from": start, "valid_to": d, "source": SEC})
                    start = None
                if present and start is None:
                    cur, start = value, d
            if start is not None:
                out.append({"ticker": ticker, "cik": seen[start]["cik"], "field": field, "value": cur,
                            "valid_from": start, "valid_to": None, "source": SEC})
    return out


# ── The vote of the fund files ────────────────────────────────────────────────────────────────────────────────

def squash(tickers) -> dict[str, str | None]:
    """``{ticker without its dashes: the ticker}`` over ``tickers``; None where two tickers squash alike."""
    out: dict[str, str | None] = {}
    for t in sorted(tickers):
        k = t.replace("-", "")
        out[k] = None if k in out and out[k] != t else t
    return out


def match_holding(line: dict, by_cusip: dict[str, str], cusip_owners: dict[str, set[tuple[str, str]]],
                  link_of: dict[str, tuple[str, str]], tickers: set[str], squashed: dict[str, str | None]) -> str | None:
    """The SEC ticker of one holdings line: by CUSIP; else its own ticker when ``tickers`` (the SEC tickers of the
    snapshot) holds it, or its ticker without the class dash (iShares writes ``BRKB``) when exactly one of them
    squashes to it (``squash(tickers)``).

    A ticker match is refused when the line's CUSIP is known (``cusip_owners``: Compustat's current and historical
    CUSIPs, by security) to belong to another security than the one the ticker is linked to (``link_of``): a line
    written before the ticker was reused (should-fix 4a). The ticker's own earlier CUSIP, or a CUSIP nobody knows,
    does not break the match (delta S2)."""
    cusip = line.get("cusip")
    if cusip and cusip in by_cusip:
        return by_cusip[cusip]
    t = line.get("ticker")
    if not t:
        return None
    hit = t if t in tickers else squashed.get(t.replace("-", ""))
    owners = cusip_owners.get(cusip) if cusip else None
    if hit and owners and hit in link_of and link_of[hit] not in owners:
        return None
    return hit


# The vote, in SQL over the whole fund-file history (it grows by about 6,000 lines a day): ``_fund_snap`` gives each
# fund day the SEC snapshot of that day (the newest on or before it, else the first), and ``_fund_map`` names the SEC
# ticker of each distinct (snapshot, ticker, CUSIP) the files wrote, matched against that snapshot's tickers (then
# the next three weeks', for a ticker the weekly file has not held yet) and the snapshot it matched (``msnap``, whose
# row gives the CIK), so a member that later left the SEC file keeps its
# past (should-fix 4b); a name is a member of an index on a day when more than
# half of that day's counting files hold it; the periods are the runs of member days ("gaps and islands"), each
# ending on the index's next day with a counting file, so a day with no counting file is no absence.
VOTE_SQL = """
CREATE OR REPLACE TEMP TABLE _fund_days AS
    SELECT as_of, index_slug, count(*) AS files FROM stg_fund_files WHERE counted GROUP BY ALL;
CREATE OR REPLACE TEMP TABLE _fund_held AS
    SELECT l.as_of, l.index_slug, m.sec_ticker AS ticker, count(DISTINCT l.provider || '/' || l.fund) AS held,
           min(m.msnap) AS msnap
    FROM stg_fund_lines l
    JOIN _fund_snap s ON s.as_of = l.as_of
    JOIN _fund_map m ON m.snap = s.snap AND m.tk = coalesce(l.ticker, '') AND m.cu = coalesce(l.cusip, '')
    JOIN stg_fund_files f ON f.as_of = l.as_of AND f.index_slug = l.index_slug AND f.provider = l.provider
                         AND f.fund = l.fund AND f.counted
    WHERE m.sec_ticker IS NOT NULL GROUP BY ALL;
CREATE OR REPLACE TEMP TABLE _fund_dense AS
    SELECT d.as_of, d.index_slug, n.ticker, d.files, coalesce(h.held, 0) AS held,
           coalesce(h.held, 0) * 2 > d.files AS member
    FROM _fund_days d
    JOIN (SELECT DISTINCT index_slug, ticker FROM _fund_held) n USING (index_slug)
    LEFT JOIN _fund_held h ON h.as_of = d.as_of AND h.index_slug = d.index_slug AND h.ticker = n.ticker;
CREATE OR REPLACE TEMP TABLE _fund_periods AS
    WITH runs AS (
        SELECT *, row_number() OVER (PARTITION BY index_slug, ticker ORDER BY as_of)
                  - row_number() OVER (PARTITION BY index_slug, ticker, member ORDER BY as_of) AS grp
        FROM _fund_dense),
    islands AS (
        SELECT index_slug, ticker, min(as_of) AS valid_from, max(as_of) AS last_day
        FROM runs WHERE member GROUP BY index_slug, ticker, grp)
    SELECT i.index_slug, i.ticker, i.valid_from,
           (SELECT min(d.as_of) FROM _fund_days d WHERE d.index_slug = i.index_slug AND d.as_of > i.last_day)
               AS valid_to,
           x.held AS files_held, x.files AS files_counted,
           -- The member's CIK as of its period's last day (delta S3): that of the SEC snapshot its lines matched
           -- that day (``msnap``, the day's own or one ahead of it), so never a later owner of the ticker (delta-2).
           (SELECT s.cik FROM stg_sec_all s WHERE s.ticker = i.ticker AND s.source_as_of = h.msnap) AS cik,
           (SELECT max(d.as_of) FROM _fund_days d WHERE d.index_slug = i.index_slug) AS source_as_of
    FROM islands i
    JOIN _fund_dense x ON x.index_slug = i.index_slug AND x.ticker = i.ticker AND x.as_of = i.last_day
    JOIN _fund_held h ON h.index_slug = i.index_slug AND h.ticker = i.ticker AND h.as_of = i.last_day;
"""


# ── The build ─────────────────────────────────────────────────────────────────────────────────────────────────

def _create(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict]) -> int:
    schema = SCHEMAS[table]
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    con.execute(f"CREATE TABLE {table} ({cols})")
    if rows:
        plain = [(c, t.replace(" PRIMARY KEY", "")) for c, t in schema]
        base.rows_table(con, f"_load_{table}", plain, rows)
        con.execute(f"INSERT INTO {table} SELECT * FROM _load_{table}")
        con.execute(f"DROP TABLE _load_{table}")
    return stage.count(con, table)


class SpineSecurity:
    name = NAME
    kind = KIND
    providers: tuple[str, ...] = ()
    tables = TABLES
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True             # not due until a first CLI run or Fetch built it (review should-fix 2)

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list:
        return []                                   # built from the other datasets' Parquet only

    def backfill(self, ctx: base.RunContext, start, end: date) -> None:
        return None

    def to_parquet(self, ctx, raw) -> list:
        return []

    def to_parquet_all(self, ctx, raws) -> list:
        return []

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        found: list[tuple[str, str, str]] = []
        have = {SEC: stage.count(con, "stg_sec"), SEC_IDHIST.name: stage.count(con, "stg_wrds_company"),
                CO_HGIC.name: stage.count(con, "stg_wrds_hgic"), IDXCST_HIS.name: stage.count(con, "stg_wrds_idx"),
                OPENFIGI: stage.count(con, "stg_figi"), FUNDS: stage.count(con, "stg_fund_lines")}
        for ds, n in have.items():
            if not n:
                found.append(("source_never_run", ds, f"{ds} has no rows in this build"))
        wrds_on, figi_on = bool(have[SEC_IDHIST.name]), bool(have[OPENFIGI])

        sec = _rows(con, "SELECT * FROM stg_sec ORDER BY ticker")
        company = _rows(con, "SELECT gvkey, conm, cik FROM stg_wrds_company")
        gvkeys_of_cik: dict[str, list[str]] = defaultdict(list)
        cik_of_gvkey: dict[str, str] = {}
        for c in company:
            cik = norm_cik(c["cik"])
            if cik:
                gvkeys_of_cik[cik].append(c["gvkey"])
                cik_of_gvkey[c["gvkey"]] = cik
        securities: dict[str, list[dict]] = defaultdict(list)
        for s in _rows(con, "SELECT * FROM stg_wrds_security"):
            securities[s["gvkey"]].append(s)
        tickers_of_cik: dict[str, int] = defaultdict(int)
        for r in sec:
            tickers_of_cik[r["cik"]] += 1
        figi = {r["cusip"]: r for r in _rows(con, "SELECT * FROM stg_figi")}
        # The current GICS row of each company only; the periods are written in SQL below (should-fix 5).
        hgic: dict[str, list[dict]] = defaultdict(list)
        for g in _rows(con, "SELECT * FROM stg_wrds_hgic WHERE indthru IS NULL ORDER BY gvkey, indfrom"):
            hgic[g["gvkey"]].append(g)

        rows, linked = [], {}
        for r in sec:
            sec_row, how, problem = link(r["ticker"], r["cik"], gvkeys_of_cik, securities, tickers_of_cik)
            if problem:
                found.append((problem, r["cik"], f"CIK {r['cik']}: {len(gvkeys_of_cik[r['cik']])} companies"))
            cusip = (sec_row or {}).get("cusip")
            cusip = str(cusip).strip().upper() if cusip else None
            f = figi.get(cusip) if cusip else None
            f_ok = f if f and f.get("status") == PRESENT else None
            typ, typ_src = tier_rule.security_type(r["ticker"], r["name"], f_ok, (sec_row or {}).get("tpci"))
            current = [g for g in hgic.get((sec_row or {}).get("gvkey"), []) if g.get("indthru") is None]
            g = current[-1] if current else None
            sector, industry = sector_names(g and g.get("gsector"), g and g.get("ggroup"))
            isin = isin_mod.from_cusip(cusip)
            row = {
                "ticker": r["ticker"], "cik": r["cik"], "name": r["name"], "exchange": r["exchange"],
                "tier": tier_rule.tier(r["exchange"], typ), "tier_rule": tier_rule.VERSION,
                "security_type": typ, "type_source": typ_src,
                "gvkey": (sec_row or {}).get("gvkey"), "iid": (sec_row or {}).get("iid"), "link_source": how,
                "cusip": cusip, "cusip_source": SEC_IDHIST.name if cusip else None,
                "isin": isin, "isin_source": "derived from the CUSIP (ISO 6166)" if isin else None,
                "figi": (f_ok or {}).get("figi"), "composite_figi": (f_ok or {}).get("composite_figi"),
                "share_class_figi": (f_ok or {}).get("share_class_figi"), "exch_code": (f_ok or {}).get("exch_code"),
                "figi_ticker": (f_ok or {}).get("ticker"),
                "security_description": (f_ok or {}).get("security_description"),
                "figi_source": OPENFIGI if f_ok else None,
                "gsector": (g or {}).get("gsector"), "ggroup": (g or {}).get("ggroup"), "gind": (g or {}).get("gind"),
                "gsubind": (g or {}).get("gsubind"), "gics_from": (g or {}).get("indfrom"),
                "gics_source": CO_HGIC.name if g else None, "sector": sector, "industry": industry,
                "source": SEC, "source_as_of": r["source_as_of"], "fetched_at": r["fetched_at"], "run_id": r["run_id"],
            }
            rows.append(row)
            if sec_row:
                linked[(sec_row["gvkey"], sec_row["iid"])] = r["ticker"]
            found += self._row_issues(row, f, wrds_on, figi_on)
        by_cusip: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            if row["cusip"]:
                by_cusip[row["cusip"]].append(row["ticker"])
        for cusip, tickers in sorted(by_cusip.items()):
            if len(tickers) > 1:
                found.append(("cusip_shared", cusip, "linked to " + ", ".join(sorted(tickers))))
        counts = {"security": _create(con, "security", rows)}

        history = sec_history(_rows(con, "SELECT ticker, cik, name, exchange, source_as_of FROM stg_sec_all"))
        _create(con, "security_history", history)
        # Compustat's history of the linked companies, filtered and written in SQL: comp.sec_idhist holds millions
        # of rows and this runs on every spine build (should-fix 5). Exclusive ends: Compustat's inclusive end + 1.
        base.rows_table(con, "_linked", [("gvkey", "TEXT"), ("iid", "TEXT"), ("ticker", "TEXT")],
                        [{"gvkey": g, "iid": i, "ticker": t} for (g, i), t in linked.items()])
        base.rows_table(con, "_gvkey_cik", [("gvkey", "TEXT"), ("cik", "TEXT")],
                        [{"gvkey": g, "cik": c} for g, c in cik_of_gvkey.items()])
        con.execute(f"""INSERT INTO security_history
            SELECT l.ticker, c.cik, h.gvkey, h.iid, lower(h.item), h.itemvalue, h.efffrom, h.effthru + 1,
                   '{SEC_IDHIST.name}'
            FROM stg_wrds_idhist h
            JOIN (SELECT DISTINCT gvkey FROM _linked) g ON g.gvkey = h.gvkey
            LEFT JOIN _linked l ON l.gvkey = h.gvkey AND l.iid = h.iid
            LEFT JOIN _gvkey_cik c ON c.gvkey = h.gvkey
            ORDER BY h.gvkey, h.iid, h.item, h.efffrom""")
        counts["security_history"] = stage.count(con, "security_history")

        _create(con, "gics_history", [])
        con.execute(f"""INSERT INTO gics_history
            SELECT h.gvkey, c.cik, h.gsector, h.ggroup, h.gind, h.gsubind, h.indfrom, h.indthru + 1,
                   '{CO_HGIC.name}', h.source_as_of
            FROM stg_wrds_hgic h
            JOIN (SELECT DISTINCT gvkey FROM _linked) g ON g.gvkey = h.gvkey
            LEFT JOIN _gvkey_cik c ON c.gvkey = h.gvkey
            ORDER BY h.gvkey, h.indfrom""")
        counts["gics_history"] = stage.count(con, "gics_history")
        for tmp in ("_linked", "_gvkey_cik"):
            con.execute(f"DROP TABLE {tmp}")

        members, member_issues = self._membership(con, rows, linked)
        found += member_issues
        counts["index_membership"] = _create(con, "index_membership", members)
        views.create(con)
        ctx.pending[NAME] = found
        return counts

    @staticmethod
    def _row_issues(row: dict, figi_row: dict | None, wrds_on: bool, figi_on: bool) -> list[tuple[str, str, str]]:
        if row["tier"] != "A":
            return []
        t, out = row["ticker"], []
        if wrds_on and row["gvkey"] is None:
            out.append(("tier_a_no_wrds_link", t, f"CIK {row['cik']} finds no Compustat security"))
        if wrds_on and row["gvkey"] is not None and not row["cusip"]:
            out.append(("tier_a_no_cusip", t, f"gvkey {row['gvkey']} iid {row['iid']} has no CUSIP"))
        if wrds_on and row["gvkey"] is not None and not row["gsubind"]:
            out.append(("tier_a_no_gics", t, f"gvkey {row['gvkey']} has no current GICS"))
        if figi_on and row["cusip"] and not row["figi"]:
            state = (figi_row or {}).get("status") or "not asked yet"
            out.append(("tier_a_no_figi", t, f"CUSIP {row['cusip']}: {state}"))
        if row["figi_ticker"] and row["figi_ticker"] != t:
            out.append(("figi_ticker_differs", t, f"OpenFIGI says {row['figi_ticker']} for CUSIP {row['cusip']}"))
        return out

    @staticmethod
    def _membership(con, rows: list[dict], linked: dict) -> tuple[list[dict], list[tuple]]:
        found: list[tuple[str, str, str]] = []
        tier = {r["ticker"]: r["tier"] for r in rows}
        link_of = {t: gi for gi, t in linked.items()}
        # Every CUSIP Compustat knows, current or past, with the securities it named (delta S2).
        cusip_owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for g, i, c in con.execute(
                "SELECT gvkey, iid, upper(cusip) FROM stg_wrds_security WHERE cusip IS NOT NULL UNION "
                "SELECT gvkey, iid, upper(itemvalue) FROM stg_wrds_idhist WHERE upper(item) = 'CUSIP' "
                "AND itemvalue IS NOT NULL").fetchall():
            cusip_owners[c].add((g, i))
        files = _rows(con, "SELECT * FROM stg_fund_files")
        latest = max((f["as_of"] for f in files), default=None)
        for f in files:
            if f["as_of"] == latest and not f["counted"]:
                found.append(("fund_file_short", f"{f['index_slug']}:{f['provider']}-{f['fund']}",
                              f"{f['lines']} stock lines on {f['as_of']}, under the floor: not counted"))
        # Each fund day's SEC snapshot; each distinct (snapshot, ticker, CUSIP) is matched once, in Python, against
        # that snapshot's tickers; the vote runs in SQL (VOTE_SQL).
        con.execute("CREATE OR REPLACE TEMP TABLE _fund_snap AS SELECT d.as_of, coalesce("
                    "(SELECT max(source_as_of) FROM stg_sec_all WHERE source_as_of <= d.as_of), "
                    "(SELECT min(source_as_of) FROM stg_sec_all)) AS snap "
                    "FROM (SELECT DISTINCT as_of FROM stg_fund_lines) d")
        on_snap: dict[date, set[str]] = defaultdict(set)
        # Each ticker's (snapshot, CIK) rows, oldest first: who held it as of a snapshot (delta-3 SF-A).
        held_by: dict[str, list[tuple[date, str]]] = defaultdict(list)
        for t, d, cik in con.execute("SELECT ticker, source_as_of, cik FROM stg_sec_all "
                                     "ORDER BY source_as_of").fetchall():
            on_snap[d].add(t)
            held_by[t].append((d, cik))
        squashed = {d: squash(ts) for d, ts in on_snap.items()}
        by_cusip_all = {r["cusip"]: r["ticker"] for r in rows if r["cusip"]}
        # Each snapshot's CUSIP map, built once (not once per distinct line).
        by_cusip = {d: {c: t for c, t in by_cusip_all.items() if t in ts} for d, ts in on_snap.items()}
        snaps = sorted(on_snap)
        held_on = {t: [d for d, _ in rs] for t, rs in held_by.items()}

        def same_holder(ticker: str, snap: date, ahead_snap: date) -> bool:
            """Whether the ticker's row in ``ahead_snap`` is a ticker the SEC file had not held by ``snap``, or is held
            by the same CIK as its last row on or before ``snap`` (a name the file dropped and took back)."""
            rows_ = held_by[ticker]
            i = bisect_right(held_on[ticker], snap)
            if i == 0:
                return True
            prior = rows_[i - 1][1]
            return any(d == ahead_snap and cik == prior for d, cik in rows_[i:])
        mapping = []
        for snap, tk, cu in con.execute(
                "SELECT DISTINCT s.snap, coalesce(l.ticker, ''), coalesce(l.cusip, '') FROM stg_fund_lines l "
                "JOIN _fund_snap s USING (as_of) WHERE (l.ticker IS NOT NULL OR l.cusip IS NOT NULL) "
                "AND s.snap IS NOT NULL").fetchall():
            line = {"ticker": tk or None, "cusip": cu or None}
            hit, msnap = match_holding(line, by_cusip[snap], cusip_owners, link_of, on_snap[snap], squashed[snap]), snap
            # The SEC file is weekly, so a name the files already write under its new ticker (a new joiner, a rename)
            # is looked for in the snapshots up to LOOKAHEAD after the day's, oldest first (delta S1). Only a ticker
            # the SEC file had not held by the day's snapshot, or one it held under the same CIK (it dropped the name
            # for a while, delta-3 SF-A): a later row of a ticker under another CIK is its next owner (delta-2 SF1).
            if hit is None:
                for d in snaps[snaps.index(snap) + 1:]:
                    if d > snap + LOOKAHEAD:
                        break
                    ahead = match_holding(line, by_cusip[d], cusip_owners, link_of, on_snap[d], squashed[d])
                    if ahead and same_holder(ahead, snap, d):
                        hit, msnap = ahead, d
                        break
            mapping.append({"snap": snap, "tk": tk, "cu": cu, "sec_ticker": hit, "msnap": msnap if hit else None})
        base.rows_table(con, "_fund_map", [("snap", "DATE"), ("tk", "TEXT"), ("cu", "TEXT"), ("sec_ticker", "TEXT"),
                                           ("msnap", "DATE")], mapping)
        con.execute(VOTE_SQL)
        if latest is not None:
            for ln in _rows(con, "SELECT DISTINCT l.index_slug, l.provider, l.fund, l.ticker, l.cusip, l.name "
                                 "FROM stg_fund_lines l JOIN _fund_snap s ON s.as_of = l.as_of "
                                 "JOIN _fund_map m ON m.snap = s.snap AND m.tk = coalesce(l.ticker, '') "
                                 "AND m.cu = coalesce(l.cusip, '') JOIN stg_fund_files f ON f.as_of = l.as_of "
                                 "AND f.index_slug = l.index_slug AND f.provider = l.provider AND f.fund = l.fund "
                                 f"AND f.counted WHERE m.sec_ticker IS NULL AND l.as_of = DATE '{latest}'"):
                found.append(("member_not_in_sec", f"{ln['index_slug']}:{ln.get('ticker') or ln.get('cusip')}",
                              f"{ln['provider']} {ln['fund']} holds {ln.get('name') or '?'}"))
            for d in _rows(con, f"SELECT index_slug, ticker, held, files, member FROM _fund_dense "
                                f"WHERE as_of = DATE '{latest}'"):
                key = f"{d['index_slug']}:{d['ticker']}"
                if d["member"] and tier.get(d["ticker"]) != "A":
                    found.append(("member_not_tier_a", key, f"tier {tier.get(d['ticker'])}"))
                if 0 < d["held"] < d["files"]:
                    found.append(("membership_files_disagree", key, f"held by {d['held']} of {d['files']} files"))
        out = [{**m, "source": FUNDS}
               for m in _rows(con, "SELECT * FROM _fund_periods ORDER BY index_slug, ticker, valid_from")]
        con.execute("DROP TABLE _fund_map")              # base.rows_table makes a real table: not for the file
        # A Compustat constituent's ticker comes only from the link, so it is a current SEC row: its CIK is that row's.
        cik_now = {r["ticker"]: r["cik"] for r in rows}
        slug_of = {v: k for k, v in INDEX_GVKEYX.items()}
        wrds = _rows(con, "SELECT * FROM stg_wrds_idx ORDER BY gvkeyx, gvkey, iid, valid_from")
        for w in wrds:
            # Only a security the link step tied to an SEC ticker gets one. Compustat's current tic of an unlinked
            # security is not used: a delisted member's tic may belong to another company today (should-fix 3).
            ticker = linked.get((w["gvkey"], w["iid"]))
            out.append({"index_slug": slug_of.get(w["gvkeyx"], w["gvkeyx"]), "ticker": ticker, "cik": cik_now.get(ticker),
                        "gvkey": w["gvkey"], "iid": w["iid"], "valid_from": w["valid_from"],
                        "valid_to": _next_day(w["thru"]), "source": IDXCST_HIS.name,
                        "source_as_of": w["source_as_of"]})
        found += _wrds_differs(out, wrds)
        if wrds:
            # One wrong index key among right ones answers no rows for it: say so once, not per member.
            seen = {w["gvkeyx"] for w in wrds}
            found += [("index_key_empty", slug, f"comp.idxcst_his answered no rows for gvkeyx {key}")
                      for slug, key in sorted(INDEX_GVKEYX.items()) if key not in seen]
        return out, found

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["ticker", "cik", "name", "exchange", "tier", "security_type", "gvkey", "iid", "cusip", "figi",
                "gsubind"]
        added, removed = stage.compare_live(con, f"SELECT {', '.join(cols)} FROM security", "security", cols)
        tier_a = stage.count(con, "security", "tier = 'A'")
        with_cusip = stage.count(con, "security", "tier = 'A' AND cusip IS NOT NULL")
        as_of = con.execute("SELECT max(source_as_of) FROM security").fetchone()[0]
        n_issues = len(ctx.pending.get(NAME, []))
        ctx.notes.append(f"{tier_a} Tier A, {with_cusip} with a CUSIP; {n_issues} build issues found")
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": stage.count(con, "security"), "added": added, "removed": removed,
                "coverage": round(with_cusip / tier_a, 4) if tier_a else None}

    def after_swap(self, ctx: base.RunContext) -> None:
        found = ctx.pending.get(NAME)
        if found is None:
            return
        ops.sync_issues(NAME, ISSUE_KINDS, found, run_id=ctx.run_id)


def _wrds_differs(members: list[dict], wrds_rows: list[dict]) -> list[tuple[str, str, str]]:
    """Fund-file members against Compustat's on the Compustat snapshot day, for the indexes both cover."""
    if not wrds_rows:
        return []
    day = max(w["source_as_of"] for w in wrds_rows)

    def on(source: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for m in members:
            if m["source"] == source and m["ticker"] and m["valid_from"] <= day and \
                    (m["valid_to"] is None or m["valid_to"] > day):
                out[m["index_slug"]].add(m["ticker"])
        return out

    fund_days = {m["valid_from"] for m in members if m["source"] == FUNDS}
    if not fund_days or min(fund_days) > day:
        return []                                       # the fund files start after the snapshot: nothing to compare
    funds, wrds = on(FUNDS), on(IDXCST_HIS.name)
    out = []
    for idx in sorted(INDEX_GVKEYX):
        if idx not in funds:
            continue
        for t in sorted(funds[idx] - wrds.get(idx, set())):
            out.append(("membership_wrds_differs", f"{idx}:{t}", f"in the fund files, not in Compustat on {day}"))
        for t in sorted(wrds.get(idx, set()) - funds[idx]):
            out.append(("membership_wrds_differs", f"{idx}:{t}", f"in Compustat on {day}, not in the fund files"))
    return out


DATASET = SpineSecurity()
