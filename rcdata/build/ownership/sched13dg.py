"""``sec/13dg``: Schedules 13D and 13G (and their amendments), who owns more than 5% of a class, through the EDGAR
daily index (``edgar.Feed``).

Forms: ``SC 13D``, ``SC 13D/A``, ``SC 13G``, ``SC 13G/A`` (text or HTML filings) and, since the SEC's structured
form of 2024-12-18, ``SCHEDULE 13D``, ``SCHEDULE 13D/A``, ``SCHEDULE 13G``, ``SCHEDULE 13G/A`` with an XML document.

- **XML** (``status`` ``ok``): the issuer (CIK, name, CUSIP), the class title and the event date, and one row per
  reporting person: name, type, the shares beneficially owned in aggregate, the percent of the class, sole and
  shared voting power. The tags are read by lower-case local name from short lists (``ISSUER_TAGS``,
  ``PERSON_TAGS``), checked on 2026-10-28 against the real filings of one day (248 filings): a 13G's persons sit in
  ``coverPageHeaderReportingPersonDetails`` and its signature block names them again (not persons), a 13D's in
  ``reportingPersons/reportingPersonInfo``, the issuer's CUSIP in ``issuerCusips/issuerCusipNumber``. A document in
  which no reporting person is found is one ``unparsed`` row, kept.
- **Text** (``status`` ``header_only``): the issuer (``SUBJECT COMPANY``) and the filer (``FILED BY``) from the SGML
  header, one row; no amount or percent is read from free text, so none is stored.

The issuer is named by its CUSIP when the spine holds it as one ticker's current CUSIP (share classes apart), else by
its CIK's one ticker (``common.cusip_issuer_map``).

Parquet: ``parquet/sec/13dg/rows/day=<index day>/``. Served: ``beneficial_ownership``.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import duckdb

from .. import base
from ..spine.stage import partitions
from . import common, edgar

NAME = "sec/13dg"
TABLE = "beneficial_ownership"
FORMS = frozenset({"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G",
                   "SCHEDULE 13G/A"})
BUDGET_S = 5 * 60.0
ISSUER_TAGS = {"issuer_cik": ("issuercik",), "issuer_name": ("issuername",),
               "cusip": ("issuercusipnumber", "issuercusip", "cusipnumber", "cusip"),
               "class_title": ("securitiesclasstitle", "titleofclass", "classtitle"),
               "event_date": ("dateofevent", "eventdaterequiresfilingthisstatement")}
PERSON_NAME = ("reportingpersonname", "nameofreportingperson", "nameofreportingpersons")
PERSON_TAGS = {"person_type": ("typeofreportingperson",),
               "amount": ("reportingpersonbeneficiallyownedaggregatenumberofshares", "aggregateamountowned",
                          "aggregateamountbeneficiallyowned", "amountbeneficiallyowned"),
               "percent": ("classpercent", "percentofclass", "percentofclassrepresented"),
               "sole_voting": ("solevotingpower",), "shared_voting": ("sharedvotingpower",)}
ROWS = [("accession", "TEXT"), ("form", "TEXT"), ("filed", "DATE"), ("index_day", "DATE"), ("issuer_cik", "TEXT"),
        ("issuer_name", "TEXT"), ("cusip", "TEXT"), ("class_title", "TEXT"), ("event_date", "DATE"),
        ("filer_cik", "TEXT"), ("filer_name", "TEXT"), ("person_line", "INTEGER"), ("person_name", "TEXT"),
        ("person_type", "TEXT"), ("amount", "DOUBLE"), ("percent", "DOUBLE"), ("sole_voting", "DOUBLE"),
        ("shared_voting", "DOUBLE"), ("status", "TEXT"), ("detail", "TEXT"), ("fetched_at", "DOUBLE"),
        ("run_id", "TEXT")]
SERVED = ROWS[:7] + [("ticker", "TEXT"), ("ticker_source", "TEXT")] + ROWS[7:] + [("source", "TEXT")]


def _first(el: ET.Element, names: tuple[str, ...]) -> str | None:
    for node in el.iter():
        if node.tag in names and node.text and node.text.strip():
            return node.text.strip()
    return None


def _date(raw) -> date | None:
    text = str(raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _cik(raw) -> str | None:
    text = str(raw or "").strip()
    return str(int(text)) if text.isdigit() else (text or None)


def parse(text: str, entry: dict, index_day: date) -> list[dict]:
    """One Schedule 13D/G submission as rows: one per reporting person (XML), else one ``header_only`` or
    ``unparsed`` row."""
    sub = edgar.parse_submission(text)
    f = sub["fields"]
    issuer = (sub["roles"].get("SUBJECT COMPANY") or [{}])[0]
    filer = (sub["roles"].get("FILED BY") or sub["roles"].get("FILER") or [{}])[0]
    head = {"accession": f.get("ACCESSION NUMBER") or entry["accession"],
            "form": (f.get("CONFORMED SUBMISSION TYPE") or entry.get("form") or "").upper(),
            "filed": edgar.ymd(f.get("FILED AS OF DATE")) or edgar.ymd(entry.get("filed")) or index_day,
            "index_day": index_day, "issuer_cik": issuer.get("cik"), "issuer_name": issuer.get("name"),
            "filer_cik": filer.get("cik"), "filer_name": filer.get("name"), "status": "header_only",
            "detail": None, "fetched_at": entry.get("fetched_at"), "run_id": entry.get("run_id")}
    doc = next((d for d in sub["docs"] if d["xml"] and "edgarsubmission" in d["xml"][:3000].lower()), None)
    if doc is None:
        return [head]
    try:
        root = edgar.tree(doc["xml"])
    except ET.ParseError as e:
        return [{**head, "status": "unparsed", "detail": f"the XML does not parse: {e}"}]
    found = {col: _first(root, names) for col, names in ISSUER_TAGS.items()}
    head.update({"issuer_cik": _cik(found["issuer_cik"]) or head["issuer_cik"],
                 "issuer_name": found["issuer_name"] or head["issuer_name"],
                 "cusip": (found["cusip"] or "").upper() or None, "class_title": found["class_title"],
                 "event_date": _date(found["event_date"])})
    # A 13G's signature block names each person again (``signatureinformation``): not a reporting person.
    persons = [el for el in root.iter() if "signature" not in el.tag
               and any(child.tag in PERSON_NAME for child in el)]
    rows = []
    for n, el in enumerate(persons):
        name = next(child.text.strip() for child in el if child.tag in PERSON_NAME and child.text)
        vals = {col: _first(el, names) for col, names in PERSON_TAGS.items()}
        rows.append({**head, "status": "ok", "person_line": n, "person_name": name,
                     "person_type": vals["person_type"], "amount": edgar.number(vals["amount"]),
                     "percent": edgar.number(vals["percent"]), "sole_voting": edgar.number(vals["sole_voting"]),
                     "shared_voting": edgar.number(vals["shared_voting"])})
    return rows or [{**head, "status": "unparsed", "detail": "no reporting person found in the XML"}]


class Sched13dg(edgar.Feed):
    name = NAME
    forms = FORMS
    sub = "13dg"
    tables = (TABLE,)
    budget_s = BUDGET_S

    def type_day(self, ctx: base.RunContext, day: date) -> list[Path]:
        filings = self.filings_of(day)
        if not filings:
            return []
        con = duckdb.connect()
        try:
            rows = edgar.Appender(con, "rows", ROWS)
            for _acc, entry, text in filings:
                rows.add(parse(text, entry, day))
            rows.flush()
            return [self.write_day(con, "rows", "rows", day, "accession, person_line")]
        finally:
            con.close()

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        files = [f for _, f in partitions(self.parquet_dir() / "rows", "day")]
        if not files:
            return {TABLE: common.create_table(con, TABLE, SERVED)}
        src = common.files_sql(files)
        common.cusip_issuer_map(con, f"SELECT issuer_cik, cusip FROM {src}", ctx.notes, NAME)
        select = (f"SELECT p.*, m.ticker, m.ticker_source, '{NAME}' AS source FROM {src} p "
                  "LEFT JOIN _cusip_issuer_map m ON m.issuer_cik IS NOT DISTINCT FROM p.issuer_cik "
                  "AND m.cusip IS NOT DISTINCT FROM p.cusip ORDER BY p.filed, p.accession, p.person_line")
        return {TABLE: common.create_table(con, TABLE, SERVED, select)}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = base.compare_with_live(con, self.kind, TABLE, ["accession", "person_line", "status"])
        newest = con.execute(f"SELECT max(filed) FROM {TABLE}").fetchone()[0]
        out = {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
               "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(),
               "rows": common.count(con, TABLE), "added": added, "removed": removed}
        if self.more_left(ctx) and ctx.as_of is not None:
            out["next_check_due"] = ctx.as_of.isoformat()
        return out


DATASET = Sched13dg()
