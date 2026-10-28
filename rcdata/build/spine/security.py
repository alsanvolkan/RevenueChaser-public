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


def periods(days_present: dict[date, bool]) -> list[tuple[date, date | None]]:
    """``[(valid_from, valid_to)]`` from ``{day: present}`` over the days a source was read (valid_to exclusive)."""
    out, start = [], None
    for d in sorted(days_present):
        if days_present[d] and start is None:
            start = d
        elif not days_present[d] and start is not None:
            out.append((start, d))
            start = None
    if start is not None:
        out.append((start, None))
    return out


# ── The vote of the fund files ────────────────────────────────────────────────────────────────────────────────

def match_holding(line: dict, by_cusip: dict[str, str], sec_tickers: set[str],
                  squashed: dict[str, str | None]) -> str | None:
    """The SEC ticker of one holdings line: by CUSIP, else its own ticker, else its ticker without the class dash
    (iShares writes ``BRKB``) when exactly one SEC ticker squashes to it."""
    cusip = line.get("cusip")
    if cusip and cusip in by_cusip:
        return by_cusip[cusip]
    t = line.get("ticker")
    if not t:
        return None
    if t in sec_tickers:
        return t
    return squashed.get(t.replace("-", ""))


def vote(lines: list[dict], counted: dict[tuple[date, str], set[tuple[str, str]]]) -> dict:
    """``{(index, ticker): {day: (held, counted)}}`` over the days each index had a counting file.

    ``lines`` carry ``as_of``, ``index_slug``, ``provider``, ``fund`` and the matched ``sec_ticker``; ``counted`` maps
    ``(day, index)`` to the ``(provider, fund)`` files that count that day.
    """
    held: dict[tuple[date, str, str], set[tuple[str, str]]] = defaultdict(set)
    for ln in lines:
        f = (ln["provider"], ln["fund"])
        if ln.get("sec_ticker") and f in counted.get((ln["as_of"], ln["index_slug"]), set()):
            held[(ln["as_of"], ln["index_slug"], ln["sec_ticker"])].add(f)
    names_of: dict[str, set[str]] = defaultdict(set)
    for (_, idx, t) in held:
        names_of[idx].add(t)
    out: dict[tuple[str, str], dict[date, tuple[int, int]]] = {}
    for (day, idx), files in counted.items():
        if not files:
            continue
        for t in names_of[idx]:
            out.setdefault((idx, t), {})[day] = (len(held.get((day, idx, t), ())), len(files))
    return out


def is_member(held: int, files: int) -> bool:
    return files > 0 and held * 2 > files


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
        hgic: dict[str, list[dict]] = defaultdict(list)
        for g in _rows(con, "SELECT * FROM stg_wrds_hgic ORDER BY gvkey, indfrom"):
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
            typ, typ_src = tier_rule.security_type(r["ticker"], r["name"], f_ok)
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
        gvkeys = {g for g, _ in linked}
        for h in _rows(con, "SELECT * FROM stg_wrds_idhist ORDER BY gvkey, iid, item, efffrom"):
            if h["gvkey"] not in gvkeys:
                continue
            history.append({"ticker": linked.get((h["gvkey"], h["iid"])), "cik": cik_of_gvkey.get(h["gvkey"]),
                            "gvkey": h["gvkey"], "iid": h["iid"], "field": str(h["item"] or "").lower(),
                            "value": h["itemvalue"], "valid_from": h["efffrom"], "valid_to": _next_day(h["effthru"]),
                            "source": SEC_IDHIST.name})
        counts["security_history"] = _create(con, "security_history", history)

        gics = [{"gvkey": g["gvkey"], "cik": cik_of_gvkey.get(g["gvkey"]), "gsector": g["gsector"],
                 "ggroup": g["ggroup"], "gind": g["gind"], "gsubind": g["gsubind"], "valid_from": g["indfrom"],
                 "valid_to": _next_day(g["indthru"]), "source": CO_HGIC.name, "source_as_of": g["source_as_of"]}
                for gv in sorted(gvkeys) for g in hgic.get(gv, [])]
        counts["gics_history"] = _create(con, "gics_history", gics)

        members, member_issues = self._membership(con, rows, linked, securities)
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
    def _membership(con, rows: list[dict], linked: dict, securities: dict) -> tuple[list[dict], list[tuple]]:
        found: list[tuple[str, str, str]] = []
        tier = {r["ticker"]: r["tier"] for r in rows}
        cik = {r["ticker"]: r["cik"] for r in rows}
        sec_tickers = set(tier)
        by_cusip = {r["cusip"]: r["ticker"] for r in rows if r["cusip"]}
        squashed: dict[str, str | None] = {}
        for t in sec_tickers:
            k = t.replace("-", "")
            squashed[k] = None if k in squashed and squashed[k] != t else t
        lines = _rows(con, "SELECT * FROM stg_fund_lines WHERE ticker IS NOT NULL OR cusip IS NOT NULL")
        counted: dict[tuple[date, str], set[tuple[str, str]]] = defaultdict(set)
        files = _rows(con, "SELECT * FROM stg_fund_files")
        for f in files:
            if f["counted"]:
                counted[(f["as_of"], f["index_slug"])].add((f["provider"], f["fund"]))
        latest = max((f["as_of"] for f in files), default=None)
        for f in files:
            if f["as_of"] == latest and not f["counted"]:
                found.append(("fund_file_short", f"{f['index_slug']}:{f['provider']}-{f['fund']}",
                              f"{f['lines']} stock lines on {f['as_of']}, under the floor: not counted"))
        for ln in lines:
            ln["sec_ticker"] = match_holding(ln, by_cusip, sec_tickers, squashed)
            if ln["as_of"] == latest and ln["sec_ticker"] is None and \
                    (ln["provider"], ln["fund"]) in counted.get((latest, ln["index_slug"]), set()):
                found.append(("member_not_in_sec", f"{ln['index_slug']}:{ln.get('ticker') or ln.get('cusip')}",
                              f"{ln['provider']} {ln['fund']} holds {ln.get('name') or '?'}"))
        out = []
        voted = vote(lines, counted)
        for (idx, t), days in sorted(voted.items()):
            for start, end in periods({d: is_member(*days[d]) for d in days}):
                held, files = days[max(d for d in days if d >= start and (end is None or d < end))]
                out.append({"index_slug": idx, "ticker": t, "cik": cik.get(t), "valid_from": start, "valid_to": end,
                            "source": FUNDS, "files_held": held, "files_counted": files,
                            "source_as_of": max(days)})
            if latest in days:
                held, files = days[latest]
                if is_member(held, files) and tier.get(t) != "A":
                    found.append(("member_not_tier_a", f"{idx}:{t}", f"tier {tier.get(t)}"))
                if 0 < held < files:
                    found.append(("membership_files_disagree", f"{idx}:{t}", f"held by {held} of {files} files"))
        slug_of = {v: k for k, v in INDEX_GVKEYX.items()}
        wrds = _rows(con, "SELECT * FROM stg_wrds_idx ORDER BY gvkeyx, gvkey, iid, valid_from")
        for w in wrds:
            ticker = linked.get((w["gvkey"], w["iid"]))
            if ticker is None:
                cands = [compustat_ticker(s.get("tic")) for s in securities.get(w["gvkey"], []) if s["iid"] == w["iid"]]
                ticker = cands[0] if cands and cands[0] in sec_tickers else None
            out.append({"index_slug": slug_of.get(w["gvkeyx"], w["gvkeyx"]), "ticker": ticker, "cik": cik.get(ticker),
                        "gvkey": w["gvkey"], "iid": w["iid"], "valid_from": w["valid_from"],
                        "valid_to": _next_day(w["thru"]), "source": IDXCST_HIS.name,
                        "source_as_of": w["source_as_of"]})
        found += _wrds_differs(out, wrds)
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
