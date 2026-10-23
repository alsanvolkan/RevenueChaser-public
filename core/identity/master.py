"""The ``security_master`` row builder (issue #85, Part 2; classification reworked in #265, order extended in #272):
pure dicts in, dicts out.

``build(inputs_by_ticker, previous_master_by_ticker, manual=None, now=None)`` returns
``(master_rows, conflict_rows)``. No store, no network, no clock (``now`` is a parameter).

``inputs_by_ticker[ticker]`` is one dict gathered from the source caches::

    name, market_cap, name_source      stored name / cap (the equity universe)
    cik, cik_source, exchange          from the SEC exchange map
    sic_store, sic_store_description   the store's SIC (``sec_company_sic``), a fallback for the SEC one
    sec       {status, name, sic, sic_description, forms[], shell_closed}     SEC submissions summary
    sp1500    {sector, sub_industry, index}
    openfigi  {security_type, security_type2, name, figi, composite_figi, share_class_figi, exch_code,
               security_description}
    nasdaq    {is_etf, security_name}
    uw        {sector, industry, security_type, market_cap}
    yahoo     {sector, industry}
    fdic      {match: {...} | None}

A source key that is ``None`` (or absent) means **that source gave no answer this run**
(failed, skipped, not consulted). ``{}`` means it answered and has nothing for the ticker.

Never blank: when the source a previous value came from is missing this run, the previous
value **with its old source and asof** is kept -- unless a source of equal or higher
precedence produced a new value from what is present. ``*_asof`` moves only when a present
source (re)derived the value this run. ``gics_asof`` is one column for the two levels: it moves
when either level was freshly derived from a real source, otherwise it is kept.

The classification is the sector and the industry (see ``gics.py``: manual, the S&P 1500 list, UW, Yahoo, the explicit
SIC table, the type rule). ``industry_group`` and ``sub_industry`` (and their ``group_source`` / ``sub_source``) stay
as columns but nothing fills them: they are written ``None``. A previous value whose source is no longer a source
(``provider_uw+correction``) is never kept: no present source is needed to confirm it, so it is replaced by whatever
is built now.

The raw SIC group (``sic`` four digits, ``sic_description`` the SEC text, ``sic_group2`` the two-digit major group,
``sic_source`` the cache it came from) is written from the same two caches the sector rule reads. It is a raw copy:
it does not depend on the sector that was chosen, and a run that gives no code keeps the previous values.
"""
from __future__ import annotations

from . import conflicts as conflicts_mod
from . import gics as gics_mod
from . import sic_sector
from . import types as types_mod
from .sources import now_iso

LEVEL_COLS = {"sector": ("sector", "sector_source"), "industry": ("industry", "industry_source")}
# OpenFIGI identifiers (#267): one column each, plus ``figi_source``; nothing reads them yet (phase 2 decides).
FIGI_COLS = ("figi", "composite_figi", "share_class_figi", "exch_code", "security_description")
# Raw SIC group (#272), copied from the SEC submissions / ``sec_company_sic`` caches.
SIC_COLS = ("sic", "sic_description", "sic_group2", "sic_source")
# CUSIP / ISIN (#127). The apply step owns the values; the builder only carries the previous ones so a
# master rewrite does not NULL them before that step runs.
ID_COLS = ("cusip", "isin", "cusip_source", "isin_source")


# ── source label -> (input key it needs, precedence rank; lower = stronger) ──────────────
def _type_requires(src: str | None) -> str | None:
    s = src or ""
    if s.startswith("openfigi"):
        return "openfigi"
    if s.startswith("sec_") or s.startswith("name_rule(Acquisition"):
        return "sec"
    if s.startswith("nasdaq_"):
        return "nasdaq"
    if s.startswith("fdic_"):
        return "fdic"
    if s.startswith("uw_"):
        return "uw"
    return None


def _type_rank(src: str | None) -> int:
    s = src or "none"
    if s == "manual":
        return 0
    if s.startswith(("sec_", "nasdaq_", "fdic_", "name_rule(Acquisition")):
        return 1
    if s.startswith("openfigi"):
        return 3
    if s.startswith("name_rule"):
        return 4
    if s.startswith("uw_"):
        return 5
    return 6


def _gics_requires(src: str | None) -> str | None:
    return {"sp1500": "sp1500", "provider_uw": "uw", "provider_yf": "yahoo", "sic_explicit": "sec"}.get(src or "")


def _gics_rank(src: str | None) -> int:
    s = src or "none"
    if s.startswith("type_rule"):
        return 5
    return {"manual": 0, "sp1500": 1, "provider_uw": 2, "provider_yf": 3, "sic_explicit": 4}.get(s, 6)


def _has(inputs: dict, key: str | None) -> bool:
    return key is None or inputs.get(key) is not None


def _keep_previous(prev_src, new_src, inputs, requires, rank) -> bool:
    """True when the previous value should stay: its source is missing and nothing as strong replaced it."""
    return bool(prev_src) and not _has(inputs, requires(prev_src)) and rank(new_src) > rank(prev_src)


def _field(inputs: dict, prev: dict, key: str, src_key: str | None = None, default_src: str | None = None):
    """A passive field (cik, exchange, name): this run's value, else the previous one."""
    new = inputs.get(key)
    if new not in (None, ""):
        return new, (inputs.get(src_key) if src_key else None) or default_src
    return prev.get(key), prev.get(src_key) if src_key else None


def _text(value) -> str | None:
    value = value.strip() if isinstance(value, str) else None
    return value or None


def _sic(inputs: dict, prev: dict) -> dict:
    """The four raw SIC columns: this run's code with its text and source, else the previous values (never blank).

    The code is the SEC submissions row's, else the store's ``sec_company_sic``. The text is that cache's own
    ``sic_description``; when the submissions row has a code but no text, the store's text fills it only if the store
    holds the same code, and the previous text only if the previous code is the same.
    """
    found = sic_sector.code_from(inputs)
    if found is None:
        return {c: prev.get(c) for c in SIC_COLS}
    code, source = found
    four, group2 = sic_sector.parts(code)
    desc = _text((inputs.get("sec") or {}).get("sic_description")) if source == sic_sector.SUBMISSIONS \
        else _text(inputs.get("sic_store_description"))
    if desc is None and source == sic_sector.SUBMISSIONS and sic_sector.code_of(inputs.get("sic_store")) == code:
        desc = _text(inputs.get("sic_store_description"))
    if desc is None and prev.get("sic") == four:
        desc = _text(prev.get("sic_description"))
    return {"sic": four, "sic_description": desc, "sic_group2": group2, "sic_source": source}


def build(inputs_by_ticker: dict, previous_master_by_ticker: dict | None = None, manual: dict | None = None,
          now: str | None = None) -> tuple[list[dict], list[dict]]:
    """``(master_rows, conflict_rows)`` for every ticker in ``inputs_by_ticker`` (see module docstring)."""
    now = now or now_iso()
    previous = previous_master_by_ticker or {}
    manual = manual or {}
    rows: list[dict] = []
    conflict_rows: list[dict] = []
    for ticker in sorted(inputs_by_ticker):
        inputs = inputs_by_ticker[ticker] or {}
        prev = previous.get(ticker) or {}
        man = manual.get(ticker) or {}
        row = {"ticker": ticker}

        row["cik"], row["cik_source"] = _field(inputs, prev, "cik", "cik_source", "sec_exchange")
        row["exchange"], _ = _field(inputs, prev, "exchange")
        row["name"], row["name_source"] = _field(inputs, prev, "name", "name_source", "equity_universe")

        # ── type ──
        if man.get("security_type"):
            typ, tsrc = man["security_type"], "manual"
        else:
            typ, tsrc = types_mod.classify_type(ticker, inputs)
        if _keep_previous(prev.get("type_source"), tsrc, inputs, _type_requires, _type_rank) \
                and prev.get("security_type"):
            typ, tsrc, tasof = prev["security_type"], prev["type_source"], prev.get("type_asof")
        else:
            tasof = now
        row.update(security_type=typ, type_source=tsrc, type_asof=tasof)

        # ── FIGI identifiers: this run's OpenFIGI row, else the previous values (never blank) ──
        figi = inputs.get("openfigi") or {}
        for col in FIGI_COLS:
            row[col] = figi[col] if figi.get(col) not in (None, "") else prev.get(col)
        row["figi_source"] = "openfigi" if any(row[col] for col in FIGI_COLS) else None

        # ── raw SIC group: the code, the SEC's text, the major group and the cache they came from ──
        row.update(_sic(inputs, prev))

        # ── CUSIP / ISIN: kept from the previous row; ``apply.apply_identifiers`` fills them (#127) ──
        for col in ID_COLS:
            row[col] = prev.get(col)

        # ── sector and industry ──
        new = gics_mod.classify(ticker, inputs, typ, manual=man)
        vals = {lv: new[lv][0] for lv in gics_mod.LEVELS}
        srcs = {lv: new[lv][1] for lv in gics_mod.LEVELS}
        fresh = {lv: True for lv in gics_mod.LEVELS}
        for lv in gics_mod.LEVELS:
            vcol, scol = LEVEL_COLS[lv]
            if prev.get(vcol) and _keep_previous(prev.get(scol), srcs[lv], inputs, _gics_requires, _gics_rank):
                vals[lv], srcs[lv], fresh[lv] = prev[vcol], prev[scol], False
        gasof = now
        if not any(fresh[lv] and srcs[lv] != "none" for lv in gics_mod.LEVELS) and prev.get("gics_asof"):
            gasof = prev["gics_asof"]
        for lv in gics_mod.LEVELS:
            vcol, scol = LEVEL_COLS[lv]
            row[vcol], row[scol] = vals[lv], srcs[lv]
        row.update(industry_group=None, group_source=None, sub_industry=None, sub_source=None)
        row["gics_asof"] = gasof

        changed = any(row.get(k) != prev.get(k) for k in row if k not in ("type_asof", "gics_asof"))
        row["updated_at"] = now if changed or not prev.get("updated_at") else prev["updated_at"]
        rows.append(row)
        conflict_rows.extend(conflicts_mod.detect(ticker, inputs, typ, tsrc))
    return rows, conflict_rows
