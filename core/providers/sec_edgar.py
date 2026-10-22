"""SEC EDGAR data layer — companyfacts (fundamentals) + local cache.

Fills in the small/micro-cap universe yfinance leaves incomplete; the primary source
is SEC EDGAR XBRL companyfacts (10-K annual data, free, no key).

Notes:
- SEC fair use: <=10 requests per second; a User-Agent is required.
- Cache: ``edgar_financials/{ticker}.json`` (the extracted annual data, not the raw XBRL).
"""
from __future__ import annotations

import json
import logging
import threading
import os
import time
from pathlib import Path

import httpx

from .. import rawstore
from ..config import to_float

logger = logging.getLogger(__name__)

SEC_USER_AGENT_DEFAULT = "RevenueChaser research (contact: local@example.com)"


_ua_warned = False


def _headers() -> dict:
    """SEC requires a descriptive ``Name email`` User-Agent on every request.

    The placeholder default is logged once per process: SEC may block it.
    """
    global _ua_warned
    ua = (os.getenv("RC_SEC_USER_AGENT") or "").strip()
    if not ua:
        if not _ua_warned:
            _ua_warned = True
            logger.warning("RC_SEC_USER_AGENT is not set: SEC requests use the placeholder "
                           "User-Agent and may be blocked. Set it to 'Your Name your@email'.")
        ua = SEC_USER_AGENT_DEFAULT
    return {"User-Agent": ua}

# Base URLs as constants so the end-to-end harness can point them at a local
# mock server. Previously inlined at each call site, which left no seam at all.
SEC_WWW_BASE = os.getenv("RC_SEC_WWW_BASE") or "https://www.sec.gov"
SEC_DATA_BASE = os.getenv("RC_SEC_DATA_BASE") or "https://data.sec.gov"

# SEC fair-use throttle: <=10 req/s. One process-wide bucket for BOTH hosts
# (www.sec.gov and data.sec.gov share the limit), used by every caller through
# ``get_json`` / ``get_text`` / ``get_bytes`` below.
_throttle_lock = threading.Lock()
_throttle_last = [0.0]
_THROTTLE_INTERVAL = 0.10
_MAX_RETRIES = 4
_RETRY_BASE = 2.0
_RETRY_AFTER_CAP = 60.0  # a server-sent Retry-After never holds a worker longer than this

# Process-wide circuit breaker (#168). Opens after ``_BREAKER_THRESHOLD`` consecutive calls that
# failed for good (retries exhausted), or at once on a 403 (SEC's rate-limit / block page). While
# open, ``_request`` raises ``SECUnavailable`` without any request, so a sync stops early instead
# of grinding through thousands of filers. It closes by itself after ``_BREAKER_COOLDOWN`` seconds,
# and a job closes it at the start of its run with ``reset_breaker`` (every run tries again).
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN = 900.0
_clock = time.monotonic
_breaker_lock = threading.Lock()
_breaker = {"fails": 0, "open_until": 0.0}


class SECUnavailable(RuntimeError):
    """SEC is treated as down for this process; no request was made."""


def breaker_open() -> bool:
    with _breaker_lock:
        return _clock() < _breaker["open_until"]


def reset_breaker() -> None:
    with _breaker_lock:
        _breaker["fails"] = 0
        _breaker["open_until"] = 0.0


def _breaker_ok() -> None:
    with _breaker_lock:
        _breaker["fails"] = 0


def _breaker_fail(immediate: bool = False) -> None:
    with _breaker_lock:
        _breaker["fails"] += 1
        if immediate or _breaker["fails"] >= _BREAKER_THRESHOLD:
            _breaker["open_until"] = _clock() + _BREAKER_COOLDOWN


def _throttle() -> None:
    with _throttle_lock:
        wait = _THROTTLE_INTERVAL - (time.monotonic() - _throttle_last[0])
        if wait > 0:
            time.sleep(wait)
        _throttle_last[0] = time.monotonic()


def _retry_delay(resp, attempt: int) -> float:
    """Honor ``Retry-After`` when present, else exponential backoff."""
    raw = (resp.headers.get("Retry-After") or "").strip() if resp is not None else ""
    if raw:
        try:
            return min(max(0.0, float(raw)), _RETRY_AFTER_CAP)
        except ValueError:
            pass
    return _RETRY_BASE ** attempt


def _request(url: str, client=None, attempts: int | None = None) -> bytes:
    """GET one EDGAR URL through the shared throttle, 429/5xx/transport backoff and the breaker.

    The single retry point for every EDGAR caller. A 404 is raised (callers that
    treat it as "absent" catch ``httpx.HTTPStatusError``). With no ``client`` it
    uses ``httpx.get`` so the test suite's ``mock_httpx`` boundary still applies.
    ``attempts`` (default ``_MAX_RETRIES``) caps the requests this call may make: a caller
    with a per-night request budget passes 1 (#301), and a 429/5xx then fails at once.
    """
    if breaker_open():
        raise SECUnavailable(f"SEC unavailable (circuit breaker open): {url}")
    attempts = _MAX_RETRIES if attempts is None else max(1, int(attempts))
    for attempt in range(attempts):
        _throttle()
        try:
            if client is None:
                r = httpx.get(url, headers=_headers(), timeout=60, follow_redirects=True)
            else:
                r = client.get(url)
        except httpx.TransportError:
            if attempt < attempts - 1:
                time.sleep(_RETRY_BASE ** attempt)
                continue
            _breaker_fail()
            raise
        if r.status_code == 429 or 500 <= r.status_code < 600:
            if attempt < attempts - 1:
                time.sleep(_retry_delay(r, attempt))
                continue
            _breaker_fail()
            r.raise_for_status()
        if r.status_code == 403:
            _breaker_fail(immediate=True)
        else:
            _breaker_ok()
        r.raise_for_status()
        return r.content
    raise RuntimeError(f"EDGAR request failed: {url}")  # pragma: no cover


def get_bytes(url: str, client=None) -> bytes:
    """Raw bytes for an EDGAR URL (throttled, backoff on 429/5xx)."""
    return _request(url, client)


def get_json(url: str, client=None, *, attempts: int | None = None) -> dict:
    """JSON for an EDGAR URL (throttled, backoff on 429/5xx; ``attempts`` as in ``_request``)."""
    return json.loads(_request(url, client, attempts).decode("utf-8"))


def get_text(url: str, client=None) -> str:
    """Decoded text for an EDGAR URL (throttled, backoff on 429/5xx)."""
    return _request(url, client).decode("utf-8", errors="replace")

# Short-term investments (#187): balance-sheet instants resolved PER PERIOD by
# ``_short_term_investments``. A TOTAL tag wins (the first present, never summed with another
# total); with no total, the AFS part (debt first, never both AFS tags) and the HTM part (the
# pre-CECL tag first, never both) are summed. Each counted fact must end on the date of that
# record's ``cash`` fact, so the figure can only understate liquidity.
_STI_TOTAL_TAGS = ["ShortTermInvestments", "MarketableSecuritiesCurrent"]
_STI_AFS_TAGS = ["AvailableForSaleSecuritiesDebtSecuritiesCurrent", "AvailableForSaleSecuritiesCurrent"]
_STI_HTM_TAGS = ["HeldToMaturitySecuritiesCurrent",
                 "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent"]

# us-gaap konsept adayları -> normalize alan (ilk eşleşen en güncel yıl serisi seçilir)
#
# A multi-tag field on the annual path keeps today's pick (the newest, then the longest series) and
# only EXTENDS it (#246): years a filer reported under an older tag are added when they are a hole
# inside that series or a contiguous run below it, each ending one fiscal year from its neighbour
# (``_backfill_history``). That function never bridges a gap, and a year the pick holds is never
# replaced. The add-only revenue concept below (#435) is the one exception to adjacency: its merge
# adds every year it reports that the pick lacks, so a revenue series can hold a non-adjacent pair.
# It never lands in the newest pair (measured: 5 of 152 filers gain a non-adjacent pair, 0 at the
# newest), so year-on-year growth — the last two non-null years — still reads the pick's own years.
# Quarterly series are not merged.
#
# ``revenue`` reads the ASC 606 including-assessed-tax sibling
# (``RevenueFromContractWithCustomerIncludingAssessedTax``) LAST (#435): 341 US filers have a fresh
# record and no revenue, and 25 of 39 measured tag only that one in the newest annual period. It
# NEVER competes for the pick, though: the pick is exactly the one over the three older tags
# (``Revenues``, ``...ExcludingAssessedTax``, ``SalesRevenueNet``), and this tag then only ADDS the
# years that pick lacks (``_ADD_ONLY_CONCEPTS`` below). When those three hold no series at all, the
# including-tax series IS the primary, so the filers it is here for keep gaining revenue.
#
# Letting it win the pick was measured to regress filers that already had revenue: it ends later
# than the other tags often enough to be the newest series, and the merge then replaced years the
# older tags already reported (Regal Rexnord FY2022 3.81bn became 5.22bn, AST SpaceMobile FY2024
# moved by -68%, one filer lost eleven years). The premise that the two tax tags differ only by the
# assessed tax is false, so neither is "the narrower one" and neither may take a year from the
# other. Bank top lines are a different definition of the top line and are never revenue: since #450
# ``RevenuesNetOfInterestExpense`` and ``InterestAndDividendIncomeOperating`` are read into their own
# field (``interest_income_top_line``), which no reader of ``revenue`` looks at, so no filer's revenue
# value moves. No other tag that merely contains the word "Revenue" is a top line either
# (``ContractWithCustomerLiabilityRevenueRecognized``, ``IncreaseDecreaseInDeferredRevenue``,
# ``CostOfRevenue`` and pro-forma acquisition revenue).
#
# ``cost_of_revenue`` is the one exception: it is resolved PER PERIOD in strict
# priority order (``_PRIORITY_FIELDS`` below), never by the longest series, and it
# feeds ``gross_profit`` when a filer tags no ``GrossProfit`` (#234). Its list is
# deliberately only total-cost concepts: ``CostsAndExpenses``,
# ``OperatingCostsAndExpenses`` and ``CostOfGoodsAndServicesSoldAndOperatingExpenses``
# include operating expenses and must never be added, or the margin is understated.
#
# The investment fields (#187) are resolved per period too. ``short_term_investments`` is the
# only one that is ever counted (``core/liquidity.py``); ``marketable_securities_noncurrent``
# and ``long_term_investments`` (equity-method and strategic stakes, not cash-like) are stored
# for visibility and never enter a gate. None of the fields in ``_NEVER_A_RECORD`` adds a record.
#
# A concept listed here may be the primary only when no other tag of its field reports anything,
# and otherwise only adds the years the primary lacks (any missing year, not just a contiguous run).
_ADD_ONLY_CONCEPTS = frozenset({"RevenueFromContractWithCustomerIncludingAssessedTax"})


CONCEPTS = {
    "revenue": (["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                 "RevenueFromContractWithCustomerIncludingAssessedTax"], "USD"),
    # #450: a bank's income-statement top line, never revenue (see the comment above). The order is a
    # tie-break only: net of interest expense is the closer analogue of a top line, so it is preferred
    # when a filer reports both; the pick rule above then decides as usual (newest, then longest).
    "interest_income_top_line": (["RevenuesNetOfInterestExpense",
                                  "InterestAndDividendIncomeOperating"], "USD"),
    "gross_profit": (["GrossProfit"], "USD"),
    "cost_of_revenue": (["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
                         "CostOfServices"], "USD"),
    "ebit": (["OperatingIncomeLoss"], "USD"),
    "net_income": (["NetIncomeLoss", "ProfitLoss"], "USD"),
    "operating_cash_flow": (["NetCashProvidedByUsedInOperatingActivities",
                             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"], "USD"),
    "capex": (["PaymentsToAcquirePropertyPlantAndEquipment",
               "PaymentsToAcquireProductiveAssets"], "USD"),
    "depreciation": (["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet"], "USD"),
    "total_assets": (["Assets"], "USD"),
    "total_equity": (["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], "USD"),
    "cash": (["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], "USD"),
    "short_term_investments": (_STI_TOTAL_TAGS + _STI_AFS_TAGS + _STI_HTM_TAGS, "USD"),
    "marketable_securities_noncurrent": (["MarketableSecuritiesNoncurrent",
                                          "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent"], "USD"),
    "long_term_investments": (["LongTermInvestments"], "USD"),
    "lt_debt": (["LongTermDebtNoncurrent"], "USD"),
    "lt_debt_current": (["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"], "USD"),
    # Debt components (#239). Stored next to the two fields above, read by nobody yet.
    "debt_and_lease_total": (["DebtAndCapitalLeaseObligations"], "USD"),
    "lt_debt_total": (["LongTermDebt"], "USD"),
    "debt_current": (["DebtCurrent"], "USD"),
    "short_term_borrowings": (["ShortTermBorrowings"], "USD"),
    "lt_debt_and_lease_noncurrent": (["LongTermDebtAndCapitalLeaseObligations"], "USD"),
    "convertible_debt_noncurrent": (["ConvertibleDebtNoncurrent", "ConvertibleLongTermNotesPayable"], "USD"),
    "convertible_debt_current": (["ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent"], "USD"),
    "notes_payable_noncurrent": (["LongTermNotesPayable"], "USD"),
    "notes_payable_current": (["NotesPayableCurrent"], "USD"),
    "line_of_credit": (["LineOfCredit"], "USD"),
    "line_of_credit_noncurrent": (["LongTermLineOfCredit"], "USD"),
    "finance_lease_noncurrent": (["FinanceLeaseLiabilityNoncurrent"], "USD"),
    "finance_lease_current": (["FinanceLeaseLiabilityCurrent"], "USD"),
    "operating_lease_noncurrent": (["OperatingLeaseLiabilityNoncurrent"], "USD"),
    "operating_lease_current": (["OperatingLeaseLiabilityCurrent"], "USD"),
    "receivables": (["AccountsReceivableNetCurrent"], "USD"),
    "inventory": (["InventoryNet"], "USD"),
    "payables": (["AccountsPayableCurrent"], "USD"),
    "rd_expense": (["ResearchAndDevelopmentExpense"], "USD"),
    # #158 (125c): buyback and SBC are NEW fields, never alternates of an existing one. Both are
    # cash-flow items reported as 3 / 6 / 9 month YTD in a 10-Q, so their quarters are derived.
    # SBC is the cash-flow add-back (``ShareBasedCompensation``, the figure next to
    # ``operating_cash_flow``), not the income-statement expense (``AllocatedShareBasedCompensationExpense``):
    # the add-back is the line that sits in the same statement as the buyback it is compared with
    # (#13, #76) and the two tags are never mixed in one field.
    "share_repurchases": (["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"], "USD"),
    "share_based_compensation": (["ShareBasedCompensation"], "USD"),
    "diluted_shares": (["WeightedAverageNumberOfDilutedSharesOutstanding"], "shares"),
    "basic_shares": (["WeightedAverageNumberOfSharesOutstandingBasic"], "shares"),
}

# The debt component fields (#239). Collected wide, defined late: ``total_debt`` below is still
# ``lt_debt + lt_debt_current`` and no reader looks at these. Several of them overlap (a total
# next to its parts, a line of credit next to its noncurrent part), so they must never be
# summed here: the definition of "debt" is a later, measured decision. Operating leases are
# stored for visibility only: EBITDA is already after rent, so they are not borrowings.
_DEBT_COMPONENT_FIELDS = frozenset({
    "debt_and_lease_total", "lt_debt_total", "debt_current", "short_term_borrowings",
    "lt_debt_and_lease_noncurrent", "convertible_debt_noncurrent", "convertible_debt_current",
    "notes_payable_noncurrent", "notes_payable_current", "line_of_credit", "line_of_credit_noncurrent",
    "finance_lease_noncurrent", "finance_lease_current",
    "operating_lease_noncurrent", "operating_lease_current"})

# Fields whose concept list is a strict priority order, resolved period by period.
_PRIORITY_FIELDS = frozenset({"cost_of_revenue", "marketable_securities_noncurrent",
                              "long_term_investments", "share_repurchases",
                              "share_based_compensation"}) | _DEBT_COMPONENT_FIELDS

# Fields that never add a record: the readers take the last record as the latest period, so a
# period holding only these fields is no period. ``short_term_investments`` has its own
# resolution (``_short_term_investments``) and so is not in ``_PRIORITY_FIELDS``.
_NEVER_A_RECORD = frozenset({"cost_of_revenue", "short_term_investments",
                             "marketable_securities_noncurrent", "long_term_investments",
                             "share_repurchases", "share_based_compensation",
                             "interest_income_top_line"}) | _DEBT_COMPONENT_FIELDS

# Flows that cannot be negative. A YTD difference that comes out negative for one of them (a Q4 whose
# FY was restated for discontinued operations while the 9M was not) is dropped and flagged
# ``<field>_suspect``, never ``abs()``. Operating cash flow, net income and EBIT may be negative.
_NON_NEGATIVE_FLOWS = frozenset({"revenue", "cost_of_revenue", "capex", "depreciation", "rd_expense",
                                 "share_repurchases", "share_based_compensation"})

# Fields written only when they hold a value: a record has no such key when the filer reports none.
# Every older field is present on every record with ``None``; only a new field can take this shape, and
# it is the right one for the bank top line (#450), which no reader consumes yet: "this filer reports
# no bank top line" must not look like a stored zero.
_OMIT_WHEN_ABSENT = frozenset({"interest_income_top_line"})

# Version of the extraction rules, stored in ``meta`` of every row this module writes. A row without
# it was extracted before #158 (no YTD-derived quarters, no per-field filed/accn). Bump it whenever
# a change here alters what a re-extraction would store.
EXTRACT_VERSION = 2


def _cik_map() -> dict:
    rows = rawstore.get_all("sec_company_tickers")
    if rows:
        return {r["ticker"]: {"cik": r["cik"], "name": r["name"]} for r in rows}
    payload = get_json(f"{SEC_WWW_BASE}/files/company_tickers.json")
    m = {row["ticker"]: {"cik": row["cik_str"], "name": row["title"]} for row in payload.values()}
    rawstore.write_rows("sec_company_tickers", [
        {"ticker": t, "cik": v["cik"], "name": v["name"]} for t, v in m.items()
    ])
    return m


def _fetch_facts(cik: int) -> dict | None:
    url = f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{cik:010d}.json"
    try:
        return get_json(url)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def _gaap(facts: dict) -> dict:
    return facts.get("facts", {}).get("us-gaap", {})


# One fiscal year in days, 52/53-week filers included (364 and 371 day years, a few days of drift).
_YEAR_DAYS = (350, 380)


def _one_year_apart(older_end: str, newer_end: str) -> bool:
    from datetime import date as _date
    try:
        days = (_date.fromisoformat(newer_end) - _date.fromisoformat(older_end)).days
    except ValueError:
        return False
    return _YEAR_DAYS[0] <= days <= _YEAR_DAYS[1]


def _backfill_history(primary: dict[str, tuple[float, str]],
                      all_series: list[dict[str, tuple[float, str]]]) -> dict[str, tuple[float, str]]:
    """Extend today's pick (``primary``) with years a filer reported under an older tag (#246).

    The primary keeps every year it holds, value and end date. A year it lacks is added only when
    it is (a) a hole inside the primary's span that a whole run of other-tag years closes, or
    (b) one of a contiguous run directly below the primary's earliest year, and in both cases only
    if its period end is one fiscal year (``_YEAR_DAYS``) from the year next to it. A calendar-year
    key one apart is not always an adjacent fiscal year: a 52/53-week filer's FY2014 can end on
    2015-01-03. The chain stops at the first year that is missing or fails, and nothing is added
    above the newest year. So THIS function never bridges a gap: the readers take the last two
    NON-NULL revenues as year-on-year growth, and 2026 against 2024 must stay "unknown". Of several
    tags reporting one added year, the first in concept-list order whose end fits wins.

    That adjacency is what this function adds. The add-only revenue merge that runs after it
    (``_annual_facts``, #435) adds any year the pick lacks, adjacent or not, so the result of the
    whole extraction can hold a non-adjacent pair of years; it is never at the newest pair, which
    stays adjacent-or-primary (the ``_ADD_ONLY_CONCEPTS`` note above has the measurement).
    """
    candidates: dict[str, list[tuple[float, str]]] = {}
    for series in all_series:
        for year, val_end in series.items():
            if year not in primary:
                candidates.setdefault(year, []).append(val_end)
    merged = dict(primary)

    def pick(year: int, fits) -> tuple[float, str] | None:
        return next((c for c in candidates.get(str(year), []) if fits(c[1])), None)

    years = sorted(int(y) for y in primary if y.isdigit())
    for lower, upper in zip(years, years[1:]):  # holes inside the primary's span: all or nothing
        run: dict[str, tuple[float, str]] = {}
        previous_end = primary[str(lower)][1]
        for year in range(lower + 1, upper):
            hit = pick(year, lambda end, prev=previous_end: _one_year_apart(prev, end))
            if hit is None:
                break
            run[str(year)] = hit
            previous_end = hit[1]
        else:
            if _one_year_apart(previous_end, primary[str(upper)][1]):
                merged.update(run)
    if years:  # a contiguous run below the earliest year
        year, newer_end = years[0] - 1, primary[str(years[0])][1]
        while True:
            hit = pick(year, lambda end, newer=newer_end: _one_year_apart(end, newer))
            if hit is None:
                break
            merged[str(year)] = hit
            year, newer_end = year - 1, hit[1]
    return merged


class _Fact(tuple):
    """``(value, period_end)`` that also carries ``.info``, the provenance of the chosen fact.

    It unpacks as the plain pair every existing reader expects. ``info`` holds ``filed`` and ``accn``
    of the fact (a derived value: the later fact's accn, the later filed date), ``start`` (flows),
    ``derived`` (True for a YTD difference) and ``ytd`` (the cumulative fact ending on the same date).
    """
    def __new__(cls, val, end, info=None):
        obj = super().__new__(cls, (val, end))
        obj.info = info or {}
        return obj


def _annual_facts(facts: dict, concepts: list[str], unit: str) -> dict[str, tuple[float, str]]:
    """{fiscal_year: (value, period_end)} of the best concept series (see ``_series_by_year``)."""
    from datetime import date as _date
    best: dict[str, tuple[float, str]] = {}

    def _one(concept: str) -> dict[str, tuple[float, str]]:
        units = _gaap(facts).get(concept, {}).get("units", {}).get(unit, [])
        by_year: dict[str, tuple[str, float, str, str, str]] = {}
        for f in units:
            if f.get("fp") != "FY":
                continue
            end = f.get("end") or ""
            start = f.get("start") or ""
            val = to_float(f.get("val"))
            if not end or val is None:
                continue
            # Yıllık gerçek = ~1 yıllık dönem (fp restated filing'lerde güvenilir değil)
            if start:
                try:
                    if (_date.fromisoformat(end) - _date.fromisoformat(start)).days <= 330:
                        continue
                except ValueError:
                    pass
            year = end[:4]
            filed = f.get("filed") or ""
            cur = by_year.get(year)
            if cur is None or filed > cur[0]:
                by_year[year] = (filed, val, end, f.get("accn") or "", start)
        return {y: _Fact(v, e, {"filed": fl, "accn": ac, "start": st or None})
                for y, (fl, v, e, ac, st) in by_year.items()}

    # En güncel (maks yıl) ve en kapsamlı konsept serisini seç; diğer konseptler yalnızca onun
    # eksik bıraktığı bitişik yılları ekler (#246). An add-only concept (#435) does not compete:
    # it is left out of ``best`` here and adds its years after the merge, so it can never take a
    # year an older tag already holds.
    found: dict[str, dict[str, tuple[float, str]]] = {}
    for concept in concepts:
        if concept in _ADD_ONLY_CONCEPTS:
            continue
        series = _one(concept)
        if not series:
            continue
        found[concept] = series
        if not best or max(series) > max(best) or (max(series) == max(best) and len(series) > len(best)):
            best = series
    merged = _backfill_history(best, list(found.values())) if best else {}
    for concept in concepts:  # the years the pick lacks, older, newer or inside a hole — adjacent or not (#435)
        if concept not in _ADD_ONLY_CONCEPTS:
            continue
        for year, val_end in _one(concept).items():
            merged.setdefault(year, val_end)
    return merged


def _series_by_year(facts: dict, concepts: list[str], unit: str) -> dict[str, float]:
    return {y: v for y, (v, _end) in _annual_facts(facts, concepts, unit).items()}


# Day spans of the periods a 10-Q / 10-K reports. A discrete quarter is 60-150 days (as before); the
# cumulative (YTD) facts of one fiscal year all start on the fiscal year's first day: 6 months
# (~182 days), 9 months (~273) and the full year (364-371 days for 52/53-week filers).
_QUARTER_DAYS = (60, 150)
_YTD_DAYS = {6: (160, 200), 9: (250, 290), 12: (350, 380)}


def _span_days(start: str, end: str) -> int | None:
    from datetime import date as _date
    try:
        return (_date.fromisoformat(end) - _date.fromisoformat(start)).days
    except ValueError:
        return None


def _months_of(days: int) -> int | None:
    """3 / 6 / 9 / 12 for a discrete quarter or a YTD span, else None (stub periods are skipped)."""
    if _QUARTER_DAYS[0] <= days <= _QUARTER_DAYS[1]:
        return 3
    for months, (lo, hi) in _YTD_DAYS.items():
        if lo <= days <= hi:
            return months
    return None


def _flow_quarters(units: list[dict]) -> dict[str, _Fact]:
    """Discrete quarters of one additive USD flow concept, from its 3 / 6 / 9 / 12 month facts (#158).

    Facts are keyed by ``(start, end)``, never by ``end`` alone (a 6M fact ends on the same day as the
    3M Q2 fact), the latest ``filed`` wins (a restated 10-Q), and ``fp`` is not used: a 10-Q lists the
    prior year's comparatives under the same ``fp``. A reported 3M fact is the quarter. Where there is
    none, the quarter is the difference of two cumulative facts that SHARE a ``start`` (the fiscal year's
    first day, so 52/53-week and non-December years match without a calendar): Q2 = 6M − 3M,
    Q3 = 9M − 6M, Q4 = FY − 9M. Both facts come from this one concept. A missing link is a gap.
    Each quarter's ``info["ytd"]`` is the cumulative fact ending on the same day, when there is one.
    """
    from datetime import date as _date, timedelta
    latest: dict[tuple[str, str], tuple] = {}
    for f in units:
        start, end = f.get("start") or "", f.get("end") or ""
        val = to_float(f.get("val"))
        if not start or not end or val is None:
            continue
        days = _span_days(start, end)
        if days is None or _months_of(days) is None:
            continue
        months = _months_of(days)
        if months == 12 and (f.get("form") or "").startswith("10-Q"):
            continue  # a 10-Q's trailing-twelve-month column is no fiscal year and no YTD fact
        # A 3M fact filed under fp Q1-Q4 is what the pre-#158 extraction counted; it outranks a 3M fact
        # of a 10-K (fp FY, a quarterly note) for the same period, so a v1 value is never replaced.
        is_q = months == 3 and f.get("fp") in ("Q1", "Q2", "Q3", "Q4")
        rank = (is_q, f.get("filed") or "", f.get("accn") or "", val)
        cur = latest.get((start, end))
        if cur is None or rank > cur[0]:
            latest[(start, end)] = (rank, val, days)
    facts = [{"start": s, "end": e, "val": v, "q": r[0], "filed": r[1], "accn": r[2], "months": _months_of(d)}
             for (s, e), (r, v, d) in latest.items()]

    # A start is a fiscal year's first day when a 6M / 9M / 12M fact begins there, or when a 3M fact
    # begins right after a 12M fact ended (a Q1 whose year has no longer fact yet).
    year_ends = [f["end"] for f in facts if f["months"] == 12]
    fiscal_starts = {f["start"] for f in facts if f["months"] > 3}
    for f in facts:
        if f["months"] == 3 and any(0 < (_date.fromisoformat(f["start"]) - _date.fromisoformat(e)).days <= 7
                                    for e in year_ends):
            fiscal_starts.add(f["start"])
    chains: dict[str, dict[int, dict]] = {}
    ytd_at: dict[str, dict] = {}
    for f in facts:
        if f["start"] not in fiscal_starts:
            continue
        chains.setdefault(f["start"], {})[f["months"]] = f
        cur = ytd_at.get(f["end"])
        key = lambda x: (x["months"], x["filed"], x["accn"], x["start"])  # the longest span wins a tie
        if cur is None or key(f) > key(cur):
            ytd_at[f["end"]] = f

    out: dict[str, _Fact] = {}
    discrete: dict[str, dict] = {}
    for f in facts:
        if f["months"] == 3:
            cur = discrete.get(f["end"])
            if cur is None or (f["q"], f["filed"], f["accn"], f["start"]) > (cur["q"], cur["filed"], cur["accn"], cur["start"]):
                discrete[f["end"]] = f
    picked: dict[str, dict] = {end: {**f, "derived": False} for end, f in discrete.items()}
    for chain in chains.values():
        for lo, hi in ((3, 6), (6, 9), (9, 12)):
            a, b = chain.get(lo), chain.get(hi)
            if not a or not b:
                continue
            gap = _span_days(a["end"], b["end"])
            if gap is None or not _QUARTER_DAYS[0] <= gap <= _QUARTER_DAYS[1]:
                continue
            later = a if a["filed"] > b["filed"] else b  # filed and accn both come from the later-filed fact
            cand = {"start": (_date.fromisoformat(a["end"]) + timedelta(days=1)).isoformat(),
                    "end": b["end"], "val": b["val"] - a["val"], "filed": later["filed"],
                    "accn": later["accn"], "derived": True, "q": False}
            cur = picked.get(b["end"])
            if cur is not None and not cur["derived"]:
                continue  # a reported 3M quarter always wins over a derived one
            if cur is None or (cand["filed"], cand["accn"], cand["start"]) > (cur["filed"], cur["accn"], cur["start"]):
                picked[b["end"]] = cand
    for end, f in picked.items():
        y = ytd_at.get(end)
        ytd = {"months": y["months"], "start": y["start"], "val": y["val"]} if y else None
        out[end] = _Fact(f["val"], end, {"filed": f["filed"], "accn": f["accn"], "start": f["start"],
                                         "derived": f["derived"], "ytd": ytd, "v1": f["q"]})
    return out


def _quarterly_facts(facts: dict, concepts: list[str], unit: str) -> dict[str, tuple[float, str]]:
    """Çeyreklik seri: {dönem_sonu_iso: (değer, dönem_sonu_iso)}.

    Additive USD flows (a fact with a ``start``) go through ``_flow_quarters``: the reported 3M quarter,
    or the quarter derived from YTD facts (#158). Instants (no ``start``) and share counts (weighted
    averages, unit ``shares``) keep the old rule: fp Q1..Q4, a reported ~90 day period only.
    """
    from datetime import date as _date
    best: dict[str, tuple[float, str]] = {}
    best_key: tuple[str, int] = ("", 0)

    def _one(concept: str) -> dict[str, tuple[float, str]]:
        units = _gaap(facts).get(concept, {}).get("units", {}).get(unit, [])
        by_end: dict[str, tuple[str, float, str, str]] = {}
        flow = []
        for f in units:
            if unit == "USD" and f.get("start"):
                flow.append(f)
                continue
            if f.get("fp") not in ("Q1", "Q2", "Q3", "Q4"):
                continue
            end = f.get("end") or ""
            start = f.get("start") or ""
            val = to_float(f.get("val"))
            if not end or val is None:
                continue
            if start:
                try:
                    d = (_date.fromisoformat(end) - _date.fromisoformat(start)).days
                    if d < 60 or d > 150:
                        continue
                except ValueError:
                    pass
            filed = f.get("filed") or ""
            cur = by_end.get(end)
            if cur is None or filed > cur[0]:
                by_end[end] = (filed, val, f.get("accn") or "", start)
        out = {e: _Fact(v, e, {"filed": fl, "accn": ac, "start": st or None, "derived": False, "ytd": None})
               for e, (fl, v, ac, st) in by_end.items()}
        out.update(_flow_quarters(flow))
        return out

    # The series is picked on the periods the pre-#158 extraction counted (``v1``: a reported fp Q1-Q4
    # quarter), never on derived ones, so a filer with two tags keeps the tag it had and v2 only ADDS
    # quarters. A field with no reported quarter at all (a cash flow filed as YTD only) is ranked on
    # everything it has.
    def _core(series, only_reported):
        return [e for e, fact in series.items() if not only_reported or fact.info.get("v1", True)]

    all_series = [s_ for s_ in (_one(c) for c in concepts) if s_]
    for only_reported in (True, False):
        for series in all_series:
            core = _core(series, only_reported)
            if not core:
                continue
            if not best or max(core) > best_key[0] or (max(core) == best_key[0] and len(core) > best_key[1]):
                best, best_key = series, (max(core), len(core))
        if best:
            break
    return best


def _series_by_quarter(facts: dict, concepts: list[str], unit: str) -> dict[str, float]:
    return {e: v for e, (v, _end) in _quarterly_facts(facts, concepts, unit).items()}


def _series_by_priority(facts_fn, facts: dict, concepts: list[str], unit: str) -> dict[str, tuple[float, str]]:
    """Per period, the (value, period end) of the first concept in ``concepts`` that has one.

    ``facts_fn`` is ``_annual_facts`` or ``_quarterly_facts``. Unlike those, a
    higher-priority concept is never displaced by a lower-priority one that has a
    longer or newer series: each period is settled on its own.
    """
    out: dict[str, tuple[float, str]] = {}
    for concept in concepts:
        for period, val_end in facts_fn(facts, [concept], unit).items():
            out.setdefault(period, val_end)
    return out


def _short_term_investments(facts_fn, facts: dict, cash_ends: dict[str, str]):
    """Per period: short-term investments paired with that period's ``cash`` (#187).

    Returns ``(values, basis, suspect)`` keyed by period. Only periods that have a ``cash`` fact
    get a value (no cash, no end date to pair with), and every counted fact must end on the
    cash end: a total on another date is dropped, a part on another date is left out. A
    negative fact is a sign error: the period gets no value and is flagged, never ``abs()``.
    """
    totals = _series_by_priority(facts_fn, facts, _STI_TOTAL_TAGS, "USD")
    afs = _series_by_priority(facts_fn, facts, _STI_AFS_TAGS, "USD")
    htm = _series_by_priority(facts_fn, facts, _STI_HTM_TAGS, "USD")
    values: dict[str, float] = {}
    basis: dict[str, str] = {}
    suspect: set[str] = set()
    for period, cash_end in cash_ends.items():
        if period in totals:
            candidates, how = [totals[period]], "total"
        else:
            candidates, how = [part[period] for part in (afs, htm) if period in part], "parts"
        counted = [val for val, end in candidates if end == cash_end]
        if not counted:
            continue
        if any(val < 0 for val in counted):
            suspect.add(period)
            continue
        values[period] = sum(counted)
        basis[period] = how
    return values, basis, suspect


def _field_series(facts_fn, facts: dict):
    """Per field, ``{period: value}`` and ``{period: end date of the fact the value came from}``.

    A third mapping carries the per-period flags of the derived fields
    (``short_term_investments_basis`` / ``_suspect``), a fourth the per-period provenance (``_Fact.info``)
    of the fact each field's value came from. ``short_term_investments`` has none: it joins several facts.
    """
    series: dict[str, dict[str, float]] = {}
    ends: dict[str, dict[str, str]] = {}
    infos: dict[str, dict[str, dict]] = {}
    negative: dict[str, set[str]] = {}
    for field, (concepts, unit) in CONCEPTS.items():
        if field == "short_term_investments":
            continue  # needs the cash ends: resolved below
        if field in _PRIORITY_FIELDS:
            picked = _series_by_priority(facts_fn, facts, concepts, unit)
        else:
            picked = facts_fn(facts, concepts, unit)
        if field in _NON_NEGATIVE_FLOWS:
            bad = {p for p, fact in picked.items() if fact[0] < 0 and getattr(fact, "info", {}).get("derived")}
            if bad:
                negative[field] = bad
                picked = {p: fact for p, fact in picked.items() if p not in bad}
        series[field] = {p: v for p, (v, _end) in picked.items()}
        ends[field] = {p: e for p, (_v, e) in picked.items()}
        infos[field] = {p: info for p, fact in picked.items() if (info := getattr(fact, "info", None))}
    sti, basis, suspect = _short_term_investments(facts_fn, facts, ends["cash"])
    series["short_term_investments"] = sti
    ends["short_term_investments"] = {p: ends["cash"][p] for p in sti}
    flags = {"short_term_investments_basis": basis, "short_term_investments_suspect": suspect,
             "negative_derived": negative}
    return series, ends, flags, infos


def _put_field_values(rec: dict, period: str, series: dict[str, dict[str, float]]) -> None:
    """Set every field's value for one period; a field in ``_OMIT_WHEN_ABSENT`` gets no key at all."""
    for field in CONCEPTS:
        value = series[field].get(period)
        if value is None and field in _OMIT_WHEN_ABSENT:
            continue
        rec[field] = value


def _record_periods(series: dict[str, dict[str, float]]) -> list[str]:
    """The periods that get a record. A field in ``_NEVER_A_RECORD`` alone never adds one: the
    readers take the last record as the latest period, and e.g. a cost with no other figure is
    no period."""
    return sorted({p for field, s in series.items() if field not in _NEVER_A_RECORD for p in s})


def _apply_flags(rec: dict, period: str, flags: dict) -> None:
    """Set the per-period flags of the derived fields (only when they apply)."""
    if period in flags["short_term_investments_basis"]:
        rec["short_term_investments_basis"] = flags["short_term_investments_basis"][period]
    if period in flags["short_term_investments_suspect"]:
        rec["short_term_investments_suspect"] = True
    for field, periods in flags["negative_derived"].items():
        if period in periods:
            rec[f"{field}_suspect"] = True


def _period_ends(rec: dict, period: str, ends: dict[str, dict[str, str]]) -> dict[str, str]:
    """``{field: end date of the fact that supplied it}`` for the fields of one annual record.

    An annual record is keyed by the calendar year of the period end, so a 52/53-week filer's
    two fiscal years ending in one calendar year share a record. A later adoption slice that
    pairs figures from one record can then check they ended on the same date. Only fields
    that hold a value are listed. A derived ``gross_profit`` ends with the revenue it came from.
    """
    out = {f: ends[f][period] for f in CONCEPTS if period in ends[f]}
    if rec.get("gross_profit_derived") and "revenue" in out:
        out["gross_profit"] = out["revenue"]
    return out


def _provenance(rec: dict, period: str, infos: dict[str, dict[str, dict]], quarterly: bool) -> None:
    """Add the per-field provenance of one record (#158, D11); a key is only set when it has content.

    ``filed`` / ``accn``: the filing date and accession of the fact each field came from (a value
    derived from two YTD facts takes the later accession and the later date). ``derived``: the fields
    whose value is a YTD difference. Both kinds get ``period_starts`` (the first day of the period each
    flow covers; an annual one is the fiscal year's first day). Quarterly records also get ``ytd`` (the
    cumulative fact ending on the record's date). A derived
    ``gross_profit`` (revenue − cost) has no entry: it joins two facts.
    """
    filed, accn, derived, starts, ytd = {}, {}, [], {}, {}
    for field in CONCEPTS:
        info = infos.get(field, {}).get(period)
        if not info:
            continue
        if info.get("filed"):
            filed[field] = info["filed"]
        if info.get("accn"):
            accn[field] = info["accn"]
        if info.get("derived"):
            derived.append(field)
        if info.get("start"):
            starts[field] = info["start"]
        if quarterly and info.get("ytd"):
            ytd[field] = info["ytd"]
    for key, value in (("filed", filed), ("accn", accn), ("derived", derived),
                       ("period_starts", starts), ("ytd", ytd)):
        if value:
            rec[key] = value


def _fill_gross_profit(rec: dict, revenue_end: str | None, cost_end: str | None) -> None:
    """Derive ``gross_profit`` = revenue − cost_of_revenue when the filer reports none (#234).

    A reported ``GrossProfit`` is never replaced. Revenue must be positive and cost
    present, and both facts must END ON THE SAME DATE: an annual record is keyed by
    the calendar year of the period end, so a 52/53-week filer's two fiscal years
    ending in one calendar year share a record, and a cost from the other one must
    not be paired. A negative cost is a sign error, so it yields no value and sets
    ``gross_profit_suspect`` (never abs()). A derived value sets ``gross_profit_derived``.
    """
    if rec.get("gross_profit") is not None:
        return
    revenue = rec.get("revenue")
    cost = rec.get("cost_of_revenue")
    if revenue is None or cost is None or revenue <= 0:
        return
    if revenue_end != cost_end:
        return
    if cost < 0:
        rec["gross_profit_suspect"] = True
        return
    rec["gross_profit"] = revenue - cost
    rec["gross_profit_derived"] = True


def _is_us_filer(facts: dict) -> bool:
    """Yıllık gelir form tipine göre: 10-K => ABD, 20-F/40-F => yabancı (ADR)."""
    units = _gaap(facts).get("Revenues", {}).get("units", {}).get("USD", [])
    forms = {f.get("form") for f in units if f.get("fp") == "FY"}
    if "10-K" in forms or "10-K/A" in forms:
        return True
    if forms and forms <= {"20-F", "20-F/A", "40-F", "40-F/A"}:
        return False
    return True  # bilinmiyorsa US varsay (evren zaten US borsalarından)


def extract_annual(facts: dict) -> list[dict]:
    """companyfacts'tan normalize yıllık kayıtlar (eskiden yeniye)."""
    series, ends, flags, infos = _field_series(_annual_facts, facts)

    years = _record_periods(series)
    annual = []
    for y in years:
        rec = {"fiscal_year": y}
        _put_field_values(rec, y, series)
        lt = rec.get("lt_debt")
        ltc = rec.get("lt_debt_current")
        if lt is not None or ltc is not None:
            rec["total_debt"] = (lt or 0.0) + (ltc or 0.0)
        else:
            rec["total_debt"] = None
        _fill_gross_profit(rec, ends["revenue"].get(y), ends["cost_of_revenue"].get(y))
        _apply_flags(rec, y, flags)
        rec["period_ends"] = _period_ends(rec, y, ends)
        _provenance(rec, y, infos, quarterly=False)
        annual.append(rec)
    return annual


def extract_quarterly(facts: dict) -> list[dict]:
    """companyfacts'tan normalize çeyreklik kayıtlar (eskiden yeniye).

    The additive USD flows hold DISCRETE quarters, Q2-Q4 derived from YTD facts when the filer reports
    no 3M figure (#158); ``derived`` lists those fields and ``ytd`` keeps the cumulative facts.
    """
    series, ends, flags, infos = _field_series(_quarterly_facts, facts)

    periods = _record_periods(series)
    quarterly = []
    for e in periods:
        rec = {"period": e}
        _put_field_values(rec, e, series)
        lt = rec.get("lt_debt")
        ltc = rec.get("lt_debt_current")
        if lt is not None or ltc is not None:
            rec["total_debt"] = (lt or 0.0) + (ltc or 0.0)
        else:
            rec["total_debt"] = None
        _fill_gross_profit(rec, ends["revenue"].get(e), ends["cost_of_revenue"].get(e))
        _apply_flags(rec, e, flags)
        _provenance(rec, e, infos, quarterly=True)
        quarterly.append(rec)
    return quarterly


def _stored_fundamentals(row: dict | None) -> tuple[list[dict], list[dict], dict] | None:
    """``(annual, quarterly, meta)`` of a stored ``edgar_financials`` row, None when it holds no usable lists."""
    if not row:
        return None
    annual = row.get("annual") or []
    quarterly = row.get("quarterly") or []
    meta = row.get("meta")
    if not isinstance(meta, dict):
        meta = {}  # callers .get() on it; a stray string must not crash them (#202)
    if isinstance(annual, list) and isinstance(quarterly, list):
        return annual, quarterly, meta
    return None


def stored_annual_many(tickers) -> dict[str, tuple[list[dict], dict]]:
    """``{ticker: (annual, meta)}`` for the stored rows ``get_annual(no_fetch=True)`` would return, in one query.

    A ticker with no usable stored row is absent: ``get_annual`` would have to go online for it.
    """
    rows = rawstore.get_many("edgar_financials", [t.upper() for t in tickers],
                             columns=["annual", "quarterly", "meta"])
    out = {}
    for ticker, row in rows.items():
        stored = _stored_fundamentals(row)
        if stored:
            out[ticker] = (stored[0], stored[2])
    return out


def get_fundamentals(ticker: str, no_fetch: bool = False) -> tuple[list[dict], list[dict], dict]:
    """Ticker için EDGAR yıllık + çeyreklik finansallar; (annual, quarterly, meta). Cache'li.

    ``no_fetch=True`` — yalnızca cache'den okur; cache yoksa boş döner (ağ yok).
    """
    ticker = ticker.upper()
    stored = _stored_fundamentals(rawstore.get("edgar_financials", ticker))
    if stored:
        return stored

    if no_fetch:
        return [], [], {"source": "sec_edgar", "entity_name": "", "is_us": True, "available": False}

    row = _cik_map().get(ticker)
    if not row:
        return [], [], {"source": "sec_edgar", "entity_name": "", "is_us": True, "available": False}

    try:
        facts = _fetch_facts(row["cik"])
    except Exception as exc:  # pragma: no cover - network
        logger.debug("edgar %s: %s", ticker, exc)
        # ``fetch_failed``: the download failed, which is not "EDGAR has nothing" (Fundamentals' edgar_fetch_failed
        # funnel step, #386 review). Callers that only read ``available`` see no change.
        return [], [], {"source": "sec_edgar", "entity_name": row.get("name", ""), "is_us": True, "available": False,
                        "fetch_failed": True}
    fetched_at = time.time()          # the facts download, stamped on the row (K6, #383 R1)

    if not facts:
        return [], [], {"source": "sec_edgar", "entity_name": row.get("name", ""), "is_us": True, "available": False}

    annual = extract_annual(facts)
    quarterly = extract_quarterly(facts)
    meta = {
        "source": "sec_edgar",
        "entity_name": facts.get("entityName") or row.get("name", ""),
        "is_us": _is_us_filer(facts),
        "available": True,
        "extract_version": EXTRACT_VERSION,
    }
    rawstore.write_rows("edgar_financials", [{
        "ticker": ticker,
        "annual": annual,
        "quarterly": quarterly,
        "meta": meta,
        "fetched_at": fetched_at,
    }])
    return annual, quarterly, meta


def get_annual(ticker: str, no_fetch: bool = False) -> tuple[list[dict], dict]:
    """Ticker için EDGAR yıllık finansallar; (annual, meta) döner. Cache'li."""
    annual, _, meta = get_fundamentals(ticker, no_fetch=no_fetch)
    return annual, meta


def get_quarterly(ticker: str, no_fetch: bool = False) -> tuple[list[dict], dict]:
    """Ticker için EDGAR çeyreklik finansallar; (quarterly, meta) döner. Cache'li."""
    _, quarterly, meta = get_fundamentals(ticker, no_fetch=no_fetch)
    return quarterly, meta


def current_shares(facts: dict) -> float | None:
    """En güncel hisse adedi (DeiEntityCommonStockSharesOutstanding)."""
    units = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    latest = None
    for f in units:
        end = f.get("end") or ""
        val = to_float(f.get("val"))
        if val is None:
            continue
        if latest is None or end > latest[0]:
            latest = (end, val)
    return latest[1] if latest else None
