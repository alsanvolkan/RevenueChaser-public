"""``sec/13f``: Form 13F-HR and 13F-HR/A as they arrive, through the EDGAR daily index (``edgar.Feed``).

Each filing's full submission holds its cover page (``primary_doc.xml``: the report period, whether it amends and
how, ``RESTATEMENT`` or ``NEW HOLDINGS``, the report type, the summary's entry and value totals and whether it left
holdings out under confidential treatment, ``confidential_omitted``) and, unless it is a notice, its information
table (one ``infoTable`` per holding: issuer, class, CUSIP, FIGI when the filer gives one, value, shares or principal
and its type, put or call, discretion, voting authority). Tags are read by their local names (filers' namespace
prefixes vary).

A filing is ``ok`` (its holdings typed), ``notice`` (a 13F notice: another manager reports its holdings) or
``unparsed`` (no cover page XML, no period, or a document that does not parse; ``detail`` says which). Every holding
is kept as the filer wrote it, options and principal amounts included; which rows count (long equity) and which
filing is active for a period (the amendment walk) are decided where the totals are built, not here.

**Value.** Filings made before 2023-01-03 report ``value`` in thousands of dollars, later ones in dollars (the SEC's
rule change of that day); ``value_usd`` is the value in dollars (``value_scale`` 1,000 or 1). A filer that still
reports thousands after the change is not detected here but where the totals are built (``ownership/serve``,
alsanvolkan/RevenueChaser#208).

Keys: a filing by ``accession``; its rows by ``(cik, period)`` (the report period, quarter end) and ``filed`` (the
filed date), so a quarter's holdings and its amendments stay apart. Parquet: ``parquet/sec/13f/filings/day=<index
day>/`` and ``parquet/sec/13f/holdings/day=<index day>/``, each day written whole from its raw files. Served:
``sec13f_filings``; the holdings stay in Parquet (millions of rows a quarter) and feed ``ownership/serve``.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from ..spine.stage import partitions
from . import common, edgar, sec13f_sets

NAME = "sec/13f"
TABLE = "sec13f_filings"
FORMS = frozenset({"13F-HR", "13F-HR/A"})
BUDGET_S = 15 * 60.0
DOLLARS_FROM = date(2023, 1, 3)          # the SEC's rule: value in dollars, not thousands, from this filing day
FILINGS = [("accession", "TEXT"), ("cik", "TEXT"), ("filer_name", "TEXT"), ("form", "TEXT"), ("period", "DATE"),
           ("filed", "DATE"), ("index_day", "DATE"), ("is_amendment", "BOOLEAN"), ("amendment_no", "INTEGER"),
           ("amendment_type", "TEXT"), ("report_type", "TEXT"), ("other_managers", "INTEGER"),
           ("entry_total", "INTEGER"), ("value_total", "DOUBLE"), ("holdings", "INTEGER"),
           ("value_scale", "INTEGER"), ("confidential_omitted", "BOOLEAN"), ("status", "TEXT"), ("detail", "TEXT"),
           ("origin", "TEXT"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
HOLDINGS = [("accession", "TEXT"), ("cik", "TEXT"), ("period", "DATE"), ("filed", "DATE"), ("line", "INTEGER"),
            ("name_of_issuer", "TEXT"), ("title_of_class", "TEXT"), ("cusip", "TEXT"), ("figi", "TEXT"),
            ("value", "DOUBLE"), ("value_usd", "DOUBLE"), ("shares", "DOUBLE"), ("sh_type", "TEXT"),
            ("put_call", "TEXT"), ("discretion", "TEXT"), ("other_manager", "TEXT"), ("vote_sole", "BIGINT"),
            ("vote_shared", "BIGINT"), ("vote_none", "BIGINT")]
SERVED = FILINGS + [("source", "TEXT")]
ORIGIN = "edgar"
SETS_SCHEMA = {"forms": FORMS, "filings": FILINGS, "holdings": HOLDINGS, "dollars_from": DOLLARS_FROM}


def _mdy(value) -> date | None:
    try:
        return datetime.strptime(str(value).strip(), "%m-%d-%Y").date()
    except (TypeError, ValueError):
        return None


def amendment_type(raw) -> str | None:
    """Whitespace collapsed and upper-cased (``RESTATEMENT``, ``NEW HOLDINGS``); None for none."""
    return " ".join(str(raw or "").upper().split()) or None


def status_of(report_type: str | None, holdings: int, entry_total: int | None) -> tuple[str, str | None]:
    """``(status, detail)`` of a filing with a report period, the one rule of both paths (``sec13f_sets`` writes it in
    SQL; review S3): a notice is a notice; a report with no holdings read is unparsed ("no information table") unless
    its summary says 0 entries (all left out in confidence: still the filer's report for the quarter)."""
    if "NOTICE" in (report_type or ""):
        return "notice", None
    if not holdings and entry_total != 0:
        return "unparsed", "no information table"
    if entry_total is not None and entry_total != holdings:
        return "ok", f"{holdings} holdings read, the summary says {entry_total}"
    return "ok", None


def parse(text: str, entry: dict, index_day: date) -> tuple[dict, list[dict]]:
    """One 13F submission: its filing row and its holdings."""
    sub = edgar.parse_submission(text)
    f = sub["fields"]
    form = (f.get("CONFORMED SUBMISSION TYPE") or entry.get("form") or "").upper()
    filer = (sub["roles"].get("FILER") or [{}])[0]
    filed = edgar.ymd(f.get("FILED AS OF DATE")) or edgar.ymd(entry.get("filed")) or index_day
    row = {"accession": f.get("ACCESSION NUMBER") or entry["accession"], "cik": filer.get("cik") or entry.get("cik"),
           "filer_name": filer.get("name"), "form": form, "period": edgar.ymd(f.get("CONFORMED PERIOD OF REPORT")),
           "filed": filed, "index_day": index_day, "is_amendment": form.endswith("/A"), "amendment_no": None,
           "amendment_type": None, "report_type": None, "other_managers": None, "entry_total": None,
           "value_total": None, "holdings": 0, "value_scale": 1000 if filed < DOLLARS_FROM else 1,
           "confidential_omitted": None, "status": "unparsed", "detail": None, "origin": ORIGIN,
           "fetched_at": entry.get("fetched_at"), "run_id": entry.get("run_id")}
    docs = sub["docs"]
    primary = next((d for d in docs if d["xml"] and "edgarSubmission" in d["xml"][:2000]), None)
    table = next((d for d in docs if d["xml"] and d["type"] == "INFORMATION TABLE"), None)
    if primary is None:
        row["detail"] = "no cover page XML"
        return row, []
    try:
        root = edgar.tree(primary["xml"])
    except ET.ParseError as e:
        row["detail"] = f"cover page XML does not parse: {e}"
        return row, []
    cover = root.find(".//coverpage")
    row["period"] = _mdy(edgar.text(cover, "reportcalendarorquarter")) or row["period"]
    flag = (edgar.text(cover, "isamendment") or "").lower()
    row["is_amendment"] = row["is_amendment"] or flag in ("true", "y", "yes", "1")
    row["amendment_no"] = edgar.integer(edgar.text(cover, "amendmentno"))
    row["amendment_type"] = amendment_type(edgar.text(cover, "amendmentinfo/amendmenttype"))
    row["report_type"] = " ".join((edgar.text(cover, "reporttype") or "").upper().split()) or None
    row["filer_name"] = edgar.text(cover, "filingmanager/name") or row["filer_name"]
    summary = root.find(".//summarypage")
    row["other_managers"] = edgar.integer(edgar.text(summary, "otherincludedmanagerscount"))
    row["entry_total"] = edgar.integer(edgar.text(summary, "tableentrytotal"))
    row["value_total"] = edgar.number(edgar.text(summary, "tablevaluetotal"))
    omitted = (edgar.text(summary, "isconfidentialomitted") or "").lower()
    row["confidential_omitted"] = {"true": True, "false": False}.get(omitted)
    if row["period"] is None:
        row["detail"] = "no report period"
        return row, []
    try:
        info = edgar.tree(table["xml"]) if table is not None else None
    except ET.ParseError as e:
        row["detail"] = f"information table does not parse: {e}"
        return row, []
    holdings = []
    for n, it in enumerate(info.iter("infotable") if info is not None else ()):
        value = edgar.number(edgar.text(it, "value"))
        cusip = (edgar.text(it, "cusip") or "").upper() or None
        holdings.append({
            "accession": row["accession"], "cik": row["cik"], "period": row["period"], "filed": filed, "line": n,
            "name_of_issuer": edgar.text(it, "nameofissuer"), "title_of_class": edgar.text(it, "titleofclass"),
            "cusip": cusip, "figi": edgar.text(it, "figi"), "value": value,
            "value_usd": value * row["value_scale"] if value is not None else None,
            "shares": edgar.number(edgar.text(it, "shrsorprnamt/sshprnamt")),
            "sh_type": (edgar.text(it, "shrsorprnamt/sshprnamttype") or "").upper() or None,
            "put_call": edgar.text(it, "putcall"), "discretion": edgar.text(it, "investmentdiscretion"),
            "other_manager": edgar.text(it, "othermanager"),
            "vote_sole": edgar.integer(edgar.text(it, "votingauthority/sole")),
            "vote_shared": edgar.integer(edgar.text(it, "votingauthority/shared")),
            "vote_none": edgar.integer(edgar.text(it, "votingauthority/none"))})
    row["holdings"] = len(holdings)
    row["status"], row["detail"] = status_of(row["report_type"], len(holdings), row["entry_total"])
    return row, holdings


def recent_cusips(periods: int = 2) -> set[str]:
    """The long-equity CUSIPs (``SH``, no put or call) the 13F holdings hold in the newest ``periods`` report periods,
    from Parquet: what the spine's OpenFIGI map asks besides its own (``openfigi_map.wanted_cusips``), so the
    OpenFIGI fallback of ``ownership/serve`` has an answer for a CUSIP no other source names. The periods come from
    the filings (one row a filing), and only the holdings partitions filed from the older one's end day on are read:
    a report is filed once its period ends (review S5; the weekly run no longer scans every partition)."""
    filings = DATASET.filing_files()
    if not filings:
        return set()
    con = duckdb.connect()
    try:
        newest = [r[0] for r in con.execute(
            f"SELECT DISTINCT period FROM {common.files_sql(filings)} WHERE period IS NOT NULL AND holdings > 0 "
            f"ORDER BY period DESC LIMIT {int(periods)}").fetchall()]
        files = [f for day, f in partitions(DATASET.parquet_dir() / "holdings", "day")
                 if newest and date.fromisoformat(str(day)) >= min(newest)]
        if not files:
            return set()
        rows = con.execute(f"""
            SELECT DISTINCT upper(cusip) FROM {common.files_sql(files)}
            WHERE upper(sh_type) = 'SH' AND coalesce(trim(put_call), '') = '' AND cusip IS NOT NULL
              AND period IN ({", ".join(f"DATE '{p}'" for p in newest)})""").fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


class Sec13f(edgar.Feed):
    name = NAME
    forms = FORMS
    sub = "13f"
    tables = (TABLE,)
    budget_s = BUDGET_S

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        """The SEC's data sets fill the days they cover (``sec13f_sets``: the first fill), then the forms walk takes
        the range's days no set covers (after the newest set)."""
        first = start or end
        self.move_start(first)
        sec13f_sets.fill(ctx, self, first, min(end, calendar.today_ny() - timedelta(days=1)), SETS_SCHEMA)
        super().backfill(ctx, first, end)

    def type_day(self, ctx: base.RunContext, day: date) -> list[Path]:
        con = duckdb.connect()
        try:
            rows = edgar.Appender(con, "filings", FILINGS)
            held = edgar.Appender(con, "holdings", HOLDINGS)
            for _acc, entry, text in self.filings_of(day):
                filing, holdings = parse(text, entry, day)
                rows.add([filing])
                held.add(holdings)
            if not rows.total:
                return []
            rows.flush()
            held.flush()
            return [self.write_day(con, "filings", "filings", day, "accession"),
                    self.write_day(con, "holdings", "holdings", day, "accession, line")]
        finally:
            con.close()

    def filing_files(self) -> list[Path]:
        return [f for _, f in partitions(self.parquet_dir() / "filings", "day")]

    def holding_files(self) -> list[Path]:
        return [f for _, f in partitions(self.parquet_dir() / "holdings", "day")]

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        files = self.filing_files()
        if not files:
            return {TABLE: common.create_table(con, TABLE, SERVED)}
        select = f"SELECT *, '{NAME}' AS source FROM {common.files_sql(files)} ORDER BY filed, accession"
        return {TABLE: common.create_table(con, TABLE, SERVED, select)}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = base.compare_with_live(con, self.kind, TABLE, ["accession", "status", "holdings"])
        newest = con.execute(f"SELECT max(filed) FROM {TABLE}").fetchone()[0]
        unparsed = common.count(con, TABLE, "status = 'unparsed'")
        if unparsed:
            ctx.notes.append(f"{unparsed} filings unparsed")
        out = {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
               "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(),
               "rows": common.count(con, TABLE), "added": added, "removed": removed}
        if self.more_left(ctx) and ctx.as_of is not None:
            out["next_check_due"] = ctx.as_of.isoformat()
        return out


DATASET = Sec13f()
