"""#34 D20: Asymmetric Growth reads the stored ``ownership_snapshot`` institutional share before the live ``.info``
one.

The number is display only — the card footer ``Institutional: x% held`` — so nothing scores on it and no score may
move. The live ``.info`` is what the board showed until now, and the Yahoo client swallows a failed ``.info`` to
``{}`` (#23/#169): measured on the PC, 314 of 4,145 cards had a card value with no stored row (reading the store
*instead of* ``info`` would lose them) and 44 had a stored row with no card value. Hence stored first, ``info``
second, and the run summary counts which way each card went.

A scan of the synthetic store, no network; the stored sessions are pinned dates, so nothing reads today's clock.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from RevenueChaser.core import ownership, rawstore
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import universe
from RevenueChaser.module_asymmetric_growth.tests.test_ag_info_only_150 import _QUOTE, _YF_ANNUAL, _edgar
from RevenueChaser.module_asymmetric_growth.tests.test_ag_ownership_300 import _OWN, _OwnedYahoo
from RevenueChaser.module_asymmetric_growth.tests.test_ag_vendor_297 import _run

UTC = timezone.utc
OLD = datetime(2026, 1, 15, 21, 35, tzinfo=UTC).timestamp()         # Thu 16:35 EST: session 01-15, complete
OLD_LABEL = "2026-01-15"
LATER = datetime(2026, 1, 16, 21, 35, tzinfo=UTC).timestamp()       # Fri: session 01-16, complete
LATER_LABEL = "2026-01-16"


# ── the harness ──────────────────────────────────────────────────────────────

def _payload(ticker: str, institutions: float | None, annual=None) -> None:
    """A stored Yahoo payload: the name scores, with the live ``.info`` ownership number of the caller's choice."""
    info = {"country": "United States", "exchange": "NMS", "longName": f"{ticker} Inc"}
    if institutions is not None:
        info["heldPercentInstitutions"] = institutions
    cache_manager.save(ticker, {"info": info, "annual": copy.deepcopy(annual or _YF_ANNUAL),
                                "market_cap": 5e9, "source": "yfinance"})


def _store(ticker: str, source: str, pct: float | None, run_at: float = OLD) -> None:
    """One ``ownership_snapshot`` row, written by hand: ``row_uw`` never fills an institutional share, and a NULL
    value is a row the writer does keep."""
    rawstore.write_rows(ownership.TABLE, [{
        "ticker": ticker, "source": source, "session_date": ownership.session_of(run_at)[0],
        "shares_outstanding": None, "insider_pct": None, "institutional_pct": pct,
        "fetched_at": run_at, "run_id": "seed", "run_at": run_at}])


def _scan(monkeypatch, tickers) -> dict:
    rows = [{"ticker": t, "quote": dict(_QUOTE), "market_cap": 5e9} for t in tickers]
    universe(monkeypatch, rows)                 # the universe, the 13F totals and the spine are stubbed
    return engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)


def _cards(result: dict) -> dict[str, dict]:
    return {c["ticker"]: c for c in result["cards"]}


# ── 1. stored first ─────────────────────────────────────────────────────────

def test_a_stored_row_beats_the_live_info_number(rawstore_tmp_db, monkeypatch):
    _payload("STORED", institutions=0.62)
    _store("STORED", ownership.YFINANCE, 0.11)

    card = _cards(_scan(monkeypatch, ["STORED"]))["STORED"]

    assert card["inst_ownership_pct"] == 0.11               # not 0.62: two clearly different numbers


# ── 2. the live fallback ────────────────────────────────────────────────────

def test_a_name_with_no_stored_row_keeps_the_info_number_and_counts_live(rawstore_tmp_db, monkeypatch):
    _payload("LIVE", institutions=0.62)
    _store("STORED", ownership.YFINANCE, 0.11)              # a row for a name that is not scored

    result = _scan(monkeypatch, ["LIVE"])

    assert _cards(result)["LIVE"]["inst_ownership_pct"] == 0.62
    assert result["summary"]["ownership"] == {"stored": 0, "live": 1, "session": None}


# ── 3. the provenance line ──────────────────────────────────────────────────

def test_the_provenance_line_names_the_source_and_the_stored_session(rawstore_tmp_db, monkeypatch):
    _payload("OLD", institutions=0.62)
    _store("OLD", ownership.YFINANCE, 0.11, run_at=OLD)

    card = _cards(_scan(monkeypatch, ["OLD"]))["OLD"]

    assert card["ownership"] == {"source": "yfinance", "session": OLD_LABEL}
    assert card["ownership"]["session"] != engine._run_day().isoformat()    # the row's session, never today


# ── 4. the summary counts add up ────────────────────────────────────────────

def test_stored_plus_live_is_every_card_that_shows_a_number(rawstore_tmp_db, monkeypatch):
    _payload("STORED", institutions=0.62)
    _payload("LIVE", institutions=0.62)
    _payload("NONE", institutions=None)                     # neither a stored row nor a live field
    _store("STORED", ownership.YFINANCE, 0.11, run_at=OLD)

    result = _scan(monkeypatch, ["STORED", "LIVE", "NONE"])
    counts = result["summary"]["ownership"]
    with_a_number = [c for c in result["cards"] if c["inst_ownership_pct"] is not None]

    assert (counts["stored"], counts["live"], counts["session"]) == (1, 1, OLD_LABEL)
    assert counts["stored"] + counts["live"] == len(with_a_number) == 2
    assert _cards(result)["NONE"]["inst_ownership_pct"] is None
    assert _cards(result)["NONE"]["ownership"] is None      # no number, no provenance to claim


# ── 5. both sources, both labels, the two precedence rules ───────────────────

def test_uw_and_yfinance_rows_are_both_read_and_uw_wins_inside_a_session(rawstore_tmp_db, monkeypatch):
    _payload("TIE", institutions=0.62)
    _payload("NEWER", institutions=0.62)
    _store("TIE", ownership.YFINANCE, 0.55, run_at=OLD)
    _store("TIE", ownership.UW_INFO, 0.21, run_at=OLD)       # same session: the UW row wins
    _store("NEWER", ownership.UW_INFO, 0.21, run_at=OLD)
    _store("NEWER", ownership.YFINANCE, 0.55, run_at=LATER)  # a newer session beats the older UW row

    cards = _cards(_scan(monkeypatch, ["TIE", "NEWER"]))

    assert (cards["TIE"]["inst_ownership_pct"], cards["TIE"]["ownership"]) == \
        (0.21, {"source": "uw_info", "session": OLD_LABEL})
    assert (cards["NEWER"]["inst_ownership_pct"], cards["NEWER"]["ownership"]) == \
        (0.55, {"source": "yfinance", "session": LATER_LABEL})


# ── 6. an unreadable store still scans ──────────────────────────────────────

def test_a_locked_ownership_table_falls_back_to_info_and_the_scan_completes(rawstore_tmp_db, monkeypatch):
    _payload("LOCK", institutions=0.62)
    _store("LOCK", ownership.YFINANCE, 0.11)
    real = rawstore.query

    def locked(sql, params=None, **kwargs):
        if ownership.TABLE in sql:
            raise RuntimeError("database is locked")
        return real(sql, params, **kwargs)

    monkeypatch.setattr(ownership.rawstore, "query", locked)
    result = _scan(monkeypatch, ["LOCK"])

    assert _cards(result)["LOCK"]["inst_ownership_pct"] == 0.62         # every name falls back
    assert result["summary"]["ownership"] == {"stored": 0, "live": 1, "session": None}


# ── 7. data safety: the stored path costs no provider call ──────────────────

def test_the_stored_path_makes_no_extra_provider_call(rawstore_tmp_db, monkeypatch):
    """A Fetch: the store answers AAA, BBB has to read Yahoo. Each name costs exactly the one call it already did."""
    rawstore.write_rows("edgar_financials", [_edgar("AAA"), _edgar("BBB")])
    _store("AAA", ownership.YFINANCE, 0.11)
    yahoo = _OwnedYahoo(monkeypatch)                        # counts get_info / get_financials; owns 0.65

    result = _run(monkeypatch, ["AAA", "BBB"])

    assert sorted(yahoo.info_calls) == ["AAA", "BBB"] and yahoo.full_calls == []
    cards = _cards(result)
    assert cards["AAA"]["inst_ownership_pct"] == 0.11       # the store answered, no second request
    assert cards["BBB"]["inst_ownership_pct"] == _OWN["heldPercentInstitutions"]   # the live number, as before


# ── the read itself: one per scan, for every scanned name ───────────────────

def test_the_store_is_read_once_per_scan_not_once_per_card(rawstore_tmp_db, monkeypatch):
    _payload("AAA", institutions=0.62)
    _payload("BBB", institutions=0.62)
    _store("AAA", ownership.YFINANCE, 0.11)
    seen = []
    real = ownership.latest_institutional
    monkeypatch.setattr(engine.ownership, "latest_institutional",
                        lambda tickers: seen.append(list(tickers)) or real(tickers))

    _scan(monkeypatch, ["AAA", "BBB"])

    assert seen == [["AAA", "BBB"]]


# ── the card face is unchanged in shape ─────────────────────────────────────

def test_a_card_without_a_yahoo_info_still_takes_the_stored_number(rawstore_tmp_db):
    """The FF40 case this lane reduces (#23/#169): an ``info`` that arrived empty, and a stored row behind it."""
    payload = {"ticker": "EMPTY", "info": {}, "annual": copy.deepcopy(_YF_ANNUAL), "market_cap": 5e9,
               "source": "yfinance"}
    card = engine.score_ticker("EMPTY", payload, {"price": 100.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6,
                                                  "above_sma50": True},
                               stored_ownership={"institutional_pct": 0.11, "source": "yfinance",
                                                 "session": None, "session_label": OLD_LABEL})
    assert card["inst_ownership_pct"] == pytest.approx(0.11)
    assert card["ownership"] == {"source": "yfinance", "session": OLD_LABEL}


# ── a summary counted from stored cards claims nothing ─────────────────────

def test_a_summary_counted_from_stored_cards_reports_the_counts_as_unknown():
    """``ticker_scores`` keeps no provenance, so a Refresh with no Fetch header cannot say stored from live."""
    card = {"ticker": "AAA", "sector": "Technology", "total_points": 1, "themes": [], "flags": [],
            "tech_breakthrough": False, "source": "sec_edgar"}
    assert engine._summary([card], None, {})["ownership"] == {"stored": None, "live": None, "session": None}
