"""#481: Asymmetric Growth takes the institutional share from 13F, as every other board does (owner, 9 Nov 2026:
"#481 de 13F den alsin, 13F henuz zip yayinlanmamis ceyrekleri de dolduruyor artik").

The share is the newest **settled** 13F quarter's shares divided by the stored shares outstanding — the 13F board's
``% Out`` rule (``module_sec_13f/report.py``), read through ``core/ownership``. The yfinance
``heldPercentInstitutions`` is no longer read: a name with no settled 13F quarter shows no share. A quarter whose
filing deadline has not passed holds a part of itself (#514), so it feeds neither the share nor the 13F block.

A scan of the synthetic store, no network.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from datetime import date

from RevenueChaser.core import ownership, rawstore
from RevenueChaser.rcdata.tests import prices_store
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import universe
from RevenueChaser.module_asymmetric_growth.tests.test_ag_info_only_150 import _QUOTE, _YF_ANNUAL

RUN_AT = datetime(2026, 1, 15, 21, 35, tzinfo=timezone.utc).timestamp()    # a complete session


def _payload(ticker: str) -> None:
    """A stored Yahoo payload whose ``.info`` carries a share the card must NOT show any more."""
    info = {"country": "United States", "exchange": "NMS", "longName": f"{ticker} Inc",
            "heldPercentInstitutions": 0.62}
    cache_manager.save(ticker, {"info": info, "annual": copy.deepcopy(_YF_ANNUAL), "market_cap": 5e9,
                                "source": "yfinance"})


#: The day the seeded cover-page count carries; any day the prices store can hold will do.
_SHARES_AS_OF = date(2026, 1, 2)


def _shares_out(ticker: str, shares: float, source: str = ownership.UW_INFO) -> None:
    """The denominator, where the boards read it since #646 PR 1: the served cover-page count.

    The old ``ownership_snapshot`` row goes in too, with its ``institutional_pct``, because these tests also prove
    the card shows the 13F share and not that stored number.
    """
    rawstore.write_rows(ownership.TABLE, [{
        "ticker": ticker, "source": source, "session_date": ownership.session_of(RUN_AT)[0],
        "shares_outstanding": shares, "insider_pct": None, "institutional_pct": 0.11,
        "fetched_at": RUN_AT, "run_id": "seed", "run_at": RUN_AT}])
    prices_store.put_bars([{"ticker": ticker, "session_date": _SHARES_AS_OF, "close": 10.0}], replace=False)
    con = prices_store._connect()
    try:
        con.execute("INSERT INTO shares_class (security_id, measure, as_of, value, symbol, source) "
                    "VALUES (?, 'outstanding', ?, ?, ?, 'sec/cover_shares')",
                    [f"sym:{ticker}", _SHARES_AS_OF, shares, ticker])
        con.execute("CHECKPOINT")
    finally:
        con.close()


def _q(shares: float, complete: bool = True) -> dict:
    return {"holders": 10, "new_holders": 2, "exited_holders": 1, "buy_flow": 5.0, "sell_flow": 1.0,
            "shares": shares, "complete": complete}


TOTALS = {"AAA": {"2025-12-31": _q(300.0), "2026-03-31": _q(400.0), "2026-06-30": _q(50.0, complete=False)},
          "NOSO": {"2026-03-31": _q(400.0)}}


def _scan(monkeypatch, tickers) -> dict:
    universe(monkeypatch, [{"ticker": t, "quote": dict(_QUOTE), "market_cap": 5e9} for t in tickers])
    monkeypatch.setattr(engine.ownership, "sec13f_totals", lambda: copy.deepcopy(TOTALS))
    return engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)


def _cards(result: dict) -> dict[str, dict]:
    return {c["ticker"]: c for c in result["cards"]}


def test_the_share_is_the_settled_13f_quarter_over_shares_outstanding(rawstore_tmp_db, monkeypatch):
    _payload("AAA")
    _shares_out("AAA", 1000.0)

    card = _cards(_scan(monkeypatch, ["AAA"]))["AAA"]

    assert card["inst_ownership_pct"] == 0.4                 # 400 / 1000; not .info's 0.62, not the stored 0.11
    assert card["ownership"] == {"source": "sec13f", "session": "2026-03-31", "shares_out": "stored",
                                 "split_factor": 1.0}


def test_a_quarter_still_arriving_feeds_neither_the_share_nor_the_block(rawstore_tmp_db, monkeypatch):
    _payload("AAA")
    _shares_out("AAA", 1000.0)

    card = _cards(_scan(monkeypatch, ["AAA"]))["AAA"]

    assert card["period_ends"]["inst_13f"] == ["2026 Q1", "2025 Q4"]    # not the partial 2026 Q2
    assert card["inst_shares"] == 400.0
    assert abs(card["inst_shares_delta_pct"] - 1 / 3) < 1e-12


def test_no_13f_quarter_means_no_share_not_the_yahoo_one(rawstore_tmp_db, monkeypatch):
    _payload("NONE")
    _shares_out("NONE", 1000.0)

    card = _cards(_scan(monkeypatch, ["NONE"]))["NONE"]

    assert card["inst_ownership_pct"] is None and card["ownership"] is None


def test_the_summary_counts_the_13f_shares_and_names_the_quarter(rawstore_tmp_db, monkeypatch):
    for t in ("AAA", "NOSO", "NONE"):
        _payload(t)
    _shares_out("AAA", 1000.0)

    result = _scan(monkeypatch, ["AAA", "NOSO", "NONE"])

    assert result["summary"]["ownership"] == {"sec13f": 2, "quarter": "2026-03-31"}   # AAA, NOSO


def test_a_summary_counted_from_stored_cards_reports_the_counts_as_unknown():
    """``ticker_scores`` keeps no provenance, so a Refresh with no Fetch header cannot count them."""
    card = {"ticker": "AAA", "sector": "Technology", "total_points": 1, "themes": [], "flags": [],
            "tech_breakthrough": False, "source": "sec_edgar"}
    assert engine._summary([card], None, {})["ownership"] == {"sec13f": None, "quarter": None}


# ── the measured gaps (live data, 9 Nov 2026): 1,219 of 4,159 names had a stored shares outstanding ─────────────

def test_without_stored_shares_out_the_cards_own_market_cap_over_price_is_the_denominator(rawstore_tmp_db,
                                                                                           monkeypatch):
    _payload("NOSO")

    card = _cards(_scan(monkeypatch, ["NOSO"]))["NOSO"]

    implied = card["market_cap"] / card["price"]
    assert abs(card["inst_ownership_pct"] - 400.0 / implied) < 1e-12
    assert card["ownership"] == {"source": "sec13f", "session": "2026-03-31", "shares_out": "market_cap/price",
                                 "split_factor": 1.0}


def test_a_stored_shares_out_is_named_too(rawstore_tmp_db, monkeypatch):
    _payload("AAA")
    _shares_out("AAA", 1000.0)

    card = _cards(_scan(monkeypatch, ["AAA"]))["AAA"]

    assert card["ownership"]["shares_out"] == "stored"


def test_a_share_above_100_percent_is_shown_as_a_check_not_a_number():
    """GOSS read 6,242 % on live data: 13F shares on a pre-reverse-split base. The card says so, not the number."""
    from RevenueChaser.module_asymmetric_growth import report
    html = report.render_asymmetric_growth_hunter_html({"asof": "", "cards": [], "summary": {},
                                                        "thresholds": engine.THRESHOLDS})
    assert "inst_ownership_pct > 1" in html and "above 100%" in html


# ── a split after the 13F quarter (avo, 9 Nov 2026: "wrds ile bu bolunmeleri ... cekiyoruz ... oradan baksak?") ──

def _split_store(monkeypatch, factor: float) -> list:
    """The prices store at the read boundary: AAA's bar on the quarter end carries ``split_factor`` (a 1-for-25
    reverse split after it serves 25: today's shares are the quarter's ÷ 25). Returns the bar windows asked."""
    from RevenueChaser.rcdata import read
    asked = []
    monkeypatch.setattr(read, "security_ids", lambda tickers: {t: f"sid:{t}" for t in tickers})

    def bars(ids, start=None, end=None, basis="nominal"):
        asked.append((sorted(ids), start, end, basis))
        return [{"security_id": "sid:AAA", "session_date": end, "split_factor": factor}]

    monkeypatch.setattr(read, "bars", bars)
    return asked


def test_a_reverse_split_after_the_quarter_puts_the_13f_shares_on_todays_basis(rawstore_tmp_db, monkeypatch):
    _payload("AAA")
    _shares_out("AAA", 1000.0)
    asked = _split_store(monkeypatch, 25.0)

    card = _cards(_scan(monkeypatch, ["AAA"]))["AAA"]

    assert abs(card["inst_ownership_pct"] - 400.0 / 25.0 / 1000.0) < 1e-12
    assert card["ownership"]["split_factor"] == 25.0
    assert len(asked) == 1 and asked[0][3] == "split"            # one read per quarter, not one per card


def test_an_unreadable_prices_store_costs_the_share_but_never_the_scan(rawstore_tmp_db, monkeypatch):
    """Since #646 PR 1 the denominator is served too, so an unreadable store leaves the card without a share -
    which is the honest answer - and the scan still finishes with its split factor at 1.0."""
    from RevenueChaser.rcdata import read
    _payload("AAA")
    _shares_out("AAA", 1000.0)

    def broken(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(read, "security_ids", broken)

    card = _cards(_scan(monkeypatch, ["AAA"]))["AAA"]

    assert card["inst_ownership_pct"] is None and card["ownership"]["split_factor"] == 1.0
