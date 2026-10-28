"""``sec/form4``: Form 4 and 4/A, the insiders' transactions, through the EDGAR daily index (``edgar.Feed``).

The filing's XML document is an ``ownershipDocument``: the issuer (CIK, name, trading symbol), one or more reporting
owners (CIK, name, and whether director, officer with title, ten-percent owner or other), whether the trade was under
a Rule 10b5-1 plan (``aff10b5One``, since 2023), and two tables of transactions: ``nonDerivativeTransaction`` (the
shares) and ``derivativeTransaction`` (options and the like: exercise price, expiry, the underlying shares). Each
value sits in a ``<value>`` child; a value given only as a footnote is NULL here. Holdings reported without a
transaction are not rows.

One row per transaction (``tx_table`` ``non_derivative`` or ``derivative``, ``line`` its place), with the filing's
first reporting owner and ``owners``, their number (a joint filing reports one transaction once). A filing whose
document does not parse is one row ``status`` ``unparsed`` (issuer from the SGML header) and a filing with no
transaction one row ``no_transaction``: kept, never dropped. The issuer is named by the spine
(``common.issuer_tickers``: its symbol when the spine's CIK for that ticker is the issuer's, else the issuer CIK's one
ticker).

Parquet: ``parquet/sec/form4/rows/day=<index day>/``. Served: ``insider_transactions``.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import duckdb

from .. import base
from ..spine.stage import partitions
from . import common, edgar

NAME = "sec/form4"
TABLE = "insider_transactions"
FORMS = frozenset({"4", "4/A"})
BUDGET_S = 10 * 60.0
ROWS = [("accession", "TEXT"), ("form", "TEXT"), ("filed", "DATE"), ("index_day", "DATE"), ("issuer_cik", "TEXT"),
        ("issuer_name", "TEXT"), ("issuer_symbol", "TEXT"), ("symbol", "TEXT"), ("owner_cik", "TEXT"),
        ("owner_name", "TEXT"), ("owners", "INTEGER"), ("is_director", "BOOLEAN"), ("is_officer", "BOOLEAN"),
        ("officer_title", "TEXT"), ("is_ten_pct", "BOOLEAN"), ("is_other", "BOOLEAN"), ("aff10b5one", "BOOLEAN"),
        ("tx_table", "TEXT"), ("line", "INTEGER"), ("security_title", "TEXT"), ("transaction_date", "DATE"),
        ("code", "TEXT"), ("equity_swap", "BOOLEAN"), ("shares", "DOUBLE"), ("price", "DOUBLE"),
        ("acquired_disposed", "TEXT"), ("shares_after", "DOUBLE"), ("direct_indirect", "TEXT"),
        ("exercise_price", "DOUBLE"), ("underlying_title", "TEXT"), ("underlying_shares", "DOUBLE"),
        ("expiration_date", "DATE"), ("status", "TEXT"), ("detail", "TEXT"), ("fetched_at", "DOUBLE"),
        ("run_id", "TEXT")]
SERVED = ROWS[:8] + [("ticker", "TEXT"), ("ticker_source", "TEXT")] + ROWS[8:] + [("source", "TEXT")]
TABLES = (("non_derivative", "nonderivativetable/nonderivativetransaction"),
          ("derivative", "derivativetable/derivativetransaction"))


def _flag(raw) -> bool | None:
    text = str(raw or "").strip().lower()
    return True if text in ("1", "true", "y", "yes") else False if text in ("0", "false", "n", "no") else None


def _cik(raw) -> str | None:
    text = str(raw or "").strip()
    return str(int(text)) if text.isdigit() else (text or None)


def _value(el, path: str) -> str | None:
    return edgar.text(el, f"{path}/value")


def _day(raw) -> date | None:
    return edgar.ymd(str(raw or "")[:10])


def parse(text: str, entry: dict, index_day: date) -> list[dict]:
    """One Form 4 submission as its transaction rows (at least one row: ``unparsed`` or ``no_transaction``)."""
    sub = edgar.parse_submission(text)
    f = sub["fields"]
    issuer = (sub["roles"].get("ISSUER") or [{}])[0]
    head = {"accession": f.get("ACCESSION NUMBER") or entry["accession"],
            "form": (f.get("CONFORMED SUBMISSION TYPE") or entry.get("form") or "").upper(),
            "filed": edgar.ymd(f.get("FILED AS OF DATE")) or edgar.ymd(entry.get("filed")) or index_day,
            "index_day": index_day, "issuer_cik": issuer.get("cik"), "issuer_name": issuer.get("name"),
            "status": "unparsed", "detail": None, "fetched_at": entry.get("fetched_at"), "run_id": entry.get("run_id")}
    doc = next((d for d in sub["docs"] if d["xml"] and "ownershipdocument" in d["xml"][:3000].lower()), None)
    if doc is None:
        return [{**head, "detail": "no ownershipDocument XML"}]
    try:
        root = edgar.tree(doc["xml"])
    except ET.ParseError as e:
        return [{**head, "detail": f"ownershipDocument does not parse: {e}"}]
    symbol_raw = edgar.text(root, "issuer/issuertradingsymbol")
    owners = root.findall("reportingowner")
    first = owners[0] if owners else None
    rel = first.find("reportingownerrelationship") if first is not None else None
    head.update({
        "issuer_cik": _cik(edgar.text(root, "issuer/issuercik")) or head["issuer_cik"],
        "issuer_name": edgar.text(root, "issuer/issuername") or head["issuer_name"],
        "issuer_symbol": symbol_raw,
        "symbol": (common.norm_symbol(symbol_raw) or None) if (symbol_raw or "").upper() not in ("", "NONE") else None,
        "owner_cik": _cik(edgar.text(first, "reportingownerid/rptownercik")),
        "owner_name": edgar.text(first, "reportingownerid/rptownername"), "owners": len(owners),
        "is_director": _flag(edgar.text(rel, "isdirector")), "is_officer": _flag(edgar.text(rel, "isofficer")),
        "officer_title": edgar.text(rel, "officertitle"), "is_ten_pct": _flag(edgar.text(rel, "istenpercentowner")),
        "is_other": _flag(edgar.text(rel, "isother")), "aff10b5one": _flag(edgar.text(root, "aff10b5one"))})
    rows = []
    for name, path in TABLES:
        for n, tx in enumerate(root.findall(path)):
            rows.append({**head, "tx_table": name, "line": n, "status": "ok",
                         "security_title": _value(tx, "securitytitle"),
                         "transaction_date": _day(_value(tx, "transactiondate")),
                         "code": edgar.text(tx, "transactioncoding/transactioncode"),
                         "equity_swap": _flag(edgar.text(tx, "transactioncoding/equityswapinvolved")),
                         "shares": edgar.number(_value(tx, "transactionamounts/transactionshares")),
                         "price": edgar.number(_value(tx, "transactionamounts/transactionpricepershare")),
                         "acquired_disposed": _value(tx, "transactionamounts/transactionacquireddisposedcode"),
                         "shares_after": edgar.number(_value(tx, "posttransactionamounts/"
                                                                 "sharesownedfollowingtransaction")),
                         "direct_indirect": _value(tx, "ownershipnature/directorindirectownership"),
                         "exercise_price": edgar.number(_value(tx, "conversionorexerciseprice")),
                         "underlying_title": _value(tx, "underlyingsecurity/underlyingsecuritytitle"),
                         "underlying_shares": edgar.number(_value(tx, "underlyingsecurity/underlyingsecurityshares")),
                         "expiration_date": _day(_value(tx, "expirationdate"))})
    return rows or [{**head, "status": "no_transaction"}]


class SecForm4(edgar.Feed):
    name = NAME
    forms = FORMS
    sub = "form4"
    tables = (TABLE,)
    budget_s = BUDGET_S

    def type_day(self, ctx: base.RunContext, day: date) -> list[Path]:
        con = duckdb.connect()
        try:
            rows = edgar.Appender(con, "rows", ROWS)
            for _acc, entry, text in self.filings_of(day):
                rows.add(parse(text, entry, day))
            if not rows.total:
                return []
            rows.flush()
            return [self.write_day(con, "rows", "rows", day, "accession, tx_table, line")]
        finally:
            con.close()

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        files = [f for _, f in partitions(self.parquet_dir() / "rows", "day")]
        if not files:
            return {TABLE: common.create_table(con, TABLE, SERVED)}
        src = common.files_sql(files)
        common.issuer_map(con, f"SELECT issuer_cik, symbol FROM {src}", ctx.notes, NAME)
        select = (f"SELECT p.*, m.ticker, m.ticker_source, '{NAME}' AS source FROM {src} p LEFT JOIN _issuer_map m "
                  "ON m.issuer_cik IS NOT DISTINCT FROM p.issuer_cik AND m.symbol IS NOT DISTINCT FROM p.symbol "
                  "ORDER BY p.filed, p.accession, p.tx_table, p.line")
        return {TABLE: common.create_table(con, TABLE, SERVED, select)}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = base.compare_with_live(con, self.kind, TABLE, ["accession", "tx_table", "line", "status"])
        newest = con.execute(f"SELECT max(filed) FROM {TABLE}").fetchone()[0]
        out = {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
               "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(),
               "rows": common.count(con, TABLE), "added": added, "removed": removed}
        if self.more_left(ctx) and ctx.as_of is not None:
            out["next_check_due"] = ctx.as_of.isoformat()
        return out


DATASET = SecForm4()
