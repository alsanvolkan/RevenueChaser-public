"""#303 (D25): the holdings job keeps a change history of the Rotation ETF holdings, and the Rotation Fetch reads them.

History redesign C: ``etf_holdings`` keeps its key (``ticker``, the ETF) and its readers; the store appends a rule-A row
to ``etf_holdings_changes`` in the write's own transaction when an ETF's holdings or provider differ from its latest
change row. The live row also says which provider answered and when (``provider``, ``fetched_at``). The Rotation
Fetch no longer fetches holdings: it reads the stored ones and computes RS on them. Temp stores only, no network.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import calendar, rawstore
from RevenueChaser.core.models.rotation import ETFRotationSnapshot
from RevenueChaser.module_sector_rotation import engine
from RevenueChaser.module_sector_rotation import etf_holdings as eh
from RevenueChaser.module_sector_rotation.tests.d01_seed import seed
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

SYNCED = [t for t, i in ROTATION_ETFS.items() if i.get("group") in ("sector", "theme")]
HOLD = [{"ticker": "NVDA", "name": "NVIDIA", "weight": 14.2}, {"ticker": "AAPL", "name": "Apple", "weight": 12.1}]
# Saturday 2026-10-10 10:00 Amsterdam (the job's slot): rule B gives Friday's session.
SAT = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
END = date(2026, 10, 9)


class _UW:
    def __init__(self, rows=None):
        self.rows, self.holdings_calls = rows or [], []

    def etf_holdings(self, ticker):
        self.holdings_calls.append(ticker)
        return self.rows


def _changes(ticker: str | None = None) -> list[dict]:
    where = f" WHERE ticker = '{ticker}'" if ticker else ""
    rows = rawstore.query(f"SELECT * FROM etf_holdings_changes{where} ORDER BY run_at")
    return [{**r, "entry": json.loads(r["entry"]) if isinstance(r["entry"], str) else r["entry"]} for r in rows]


def _sync(monkeypatch, answer, now=SAT):
    """Run the holdings job with ``answer(ticker)`` as the resolver and ``now`` as the clock; no RS (uw None)."""
    monkeypatch.setattr(eh, "get_etf_holdings", lambda t, uw: answer(t))
    monkeypatch.setattr(calendar, "_now", lambda: now)
    with pytest.raises(JobMessage):
        runners.run_etf_holdings_sync(uw=None)


# ── the resolver says which provider answered ──────────────────────────────────────────────────────────────────────
def test_uw_answer_is_tagged_uw(monkeypatch):
    monkeypatch.setattr(eh, "fetch_from_yfinance", lambda t: pytest.fail("yfinance asked after UW answered"))
    rows = eh.get_etf_holdings("XLK", _UW([{"ticker": "NVDA", "weight": "14.2"}]))
    assert rows == [{"ticker": "NVDA", "name": "", "weight": 14.2}]
    assert rows.provider == "uw"


def test_yfinance_fallback_is_tagged_yfinance(monkeypatch):
    monkeypatch.setattr(eh, "fetch_from_yfinance", lambda t: list(HOLD))
    rows = eh.get_etf_holdings("SKYY", _UW([]), fmp_api_key="")
    assert rows == HOLD and rows.provider == "yfinance"


def test_no_answer_has_no_provider(monkeypatch):
    monkeypatch.setattr(eh, "fetch_from_yfinance", lambda t: [])
    rows = eh.get_etf_holdings("SKYY", _UW([]), fmp_api_key="")
    assert rows == [] and rows.provider is None


def test_the_answer_is_capped_at_ten(monkeypatch):
    many = [{"ticker": f"T{i}", "weight": 20 - i} for i in range(15)]
    rows = eh.get_etf_holdings("XLK", _UW(many))
    assert len(rows) == 10 and rows.provider == "uw"


# ── the job writes provider, fetched_at and one change row per ETF ─────────────────────────────────────────────────
def test_a_sync_stores_provider_and_fetch_time(rawstore_tmp_db, monkeypatch):
    _sync(monkeypatch, lambda t: eh.HoldingsList(HOLD, "yfinance" if t == "SKYY" else "uw"))
    xlk, skyy = rawstore.get("etf_holdings", "XLK"), rawstore.get("etf_holdings", "SKYY")
    assert xlk["entry"] == HOLD and xlk["provider"] == "uw" and skyy["provider"] == "yfinance"
    assert xlk["fetched_at"] == pytest.approx(SAT.timestamp())


def test_a_first_sync_appends_one_change_row_per_etf(rawstore_tmp_db, monkeypatch):
    _sync(monkeypatch, lambda t: eh.HoldingsList(HOLD, "uw"))
    rows = _changes()
    assert sorted(r["ticker"] for r in rows) == sorted(SYNCED)
    xlk = _changes("XLK")[0]
    assert xlk["status"] == "present" and xlk["provider"] == "uw" and xlk["valid_from"] is not None
    assert xlk["fetched_at"] == pytest.approx(SAT.timestamp())
    as_of = {r["ticker"]: r for r in rawstore.changes_as_of("etf_holdings", xlk["run_at"])}
    assert json.loads(as_of["XLK"]["entry"]) == HOLD and as_of["XLK"]["provider"] == "uw"


def test_a_second_run_in_one_session_adds_nothing(rawstore_tmp_db, monkeypatch):
    _sync(monkeypatch, lambda t: eh.HoldingsList(HOLD, "uw"))
    before = len(_changes())
    _sync(monkeypatch, lambda t: eh.HoldingsList(HOLD, "uw"), now=SAT + timedelta(hours=2))
    assert len(_changes()) == before
    # the live row still says when it was last fetched
    assert rawstore.get("etf_holdings", "XLK")["fetched_at"] == pytest.approx((SAT + timedelta(hours=2)).timestamp())


def test_a_changed_etf_appends_one_row_and_keeps_the_old_one(rawstore_tmp_db, monkeypatch):
    _sync(monkeypatch, lambda t: eh.HoldingsList(HOLD, "uw"))
    new = [{"ticker": "NVDA", "name": "NVIDIA", "weight": 15.0}]
    _sync(monkeypatch, lambda t: eh.HoldingsList(new if t == "XLK" else HOLD, "uw"), now=SAT + timedelta(days=7))
    xlk = _changes("XLK")
    assert [r["entry"] for r in xlk] == [HOLD, new]
    assert len(_changes()) == len(SYNCED) + 1


def test_a_provider_switch_is_a_change(rawstore_tmp_db, monkeypatch):
    _sync(monkeypatch, lambda t: eh.HoldingsList(HOLD, "uw"))
    _sync(monkeypatch, lambda t: eh.HoldingsList(HOLD, "yfinance" if t == "XLK" else "uw"),
          now=SAT + timedelta(days=7))
    assert [r["provider"] for r in _changes("XLK")] == ["uw", "yfinance"]


def test_a_failed_fetch_keeps_the_row_and_adds_no_change(rawstore_tmp_db, monkeypatch):
    _sync(monkeypatch, lambda t: eh.HoldingsList(HOLD, "uw"))
    before = len(_changes())
    _sync(monkeypatch, lambda t: eh.HoldingsList([], None), now=SAT + timedelta(days=7))
    assert len(_changes()) == before
    row = rawstore.get("etf_holdings", "XLK")
    assert row["entry"] == HOLD and row["fetched_at"] == pytest.approx(SAT.timestamp())


# ── the Rotation Fetch reads the stored holdings ───────────────────────────────────────────────────────────────────
def _closes(n=70, step=1.0):
    start = END - timedelta(days=n - 1)
    return [{"date": (start + timedelta(days=i)).isoformat(), "close": 100.0 + i * step} for i in range(n)]


class _OhlcUW(_UW):
    def __init__(self):
        super().__init__()
        self.ohlc_calls = []

    def etf_ohlc(self, ticker, days=90, candle_size="1d"):
        self.ohlc_calls.append(ticker)
        return _closes(step=0.0 if ticker == "SPY" else 1.0)


def _snap(t, group="theme"):
    return ETFRotationSnapshot(as_of_date=END, ticker=t, group_name=group, close_price=1.0, rs_1w=0, rs_1m=0,
                               rs_3m=0, rs_ratio=0, rs_momentum=0, rs_ratio_z=0, rs_momentum_z=0,
                               rrg_quadrant="LEADING")


def test_a_rotation_fetch_makes_no_holdings_call(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("etf_holdings", [{"ticker": "AIQ", "entry": HOLD, "provider": "uw"}])
    start = END - timedelta(days=150)                                   # the RS reads D01 since M-ROT-3 (#374)
    seed("SPY", END, start=start, closes=lambda i: 100.0)
    for t in ("NVDA", "AAPL"):
        seed(t, END, start=start, closes=lambda i: 100.0 + i)
    monkeypatch.setattr(eh, "get_etf_holdings", lambda t, uw: pytest.fail("holdings fetched during a Rotation Fetch"))
    monkeypatch.setattr(eh, "fetch_from_yfinance", lambda t: pytest.fail("yfinance holdings during a Rotation Fetch"))
    uw = _OhlcUW()
    out = engine.fetch_all_holdings(uw, [_snap("AIQ"), _snap("ARKQ")])
    assert uw.holdings_calls == []
    assert [r["ticker"] for r in out["AIQ"]] == ["NVDA", "AAPL"]      # the stored holdings, with RS
    assert out["AIQ"][0]["rs_1m"] > 0
    assert "ARKQ" not in out                                            # nothing stored: nothing embedded
    assert uw.ohlc_calls == []                                          # no UW candle either (M-ROT-3)


def test_a_rotation_fetch_writes_no_holdings(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("etf_holdings", [{"ticker": "AIQ", "entry": HOLD, "provider": "uw"}])
    before = (rawstore.get("etf_holdings", "AIQ"), len(_changes()))
    engine.fetch_all_holdings(_OhlcUW(), [_snap("AIQ")])
    assert (rawstore.get("etf_holdings", "AIQ"), len(_changes())) == before


# ── Data Health summary ────────────────────────────────────────────────────────────────────────────────────────────
def test_health_summary_on_an_empty_store(rawstore_tmp_db):
    h = eh.health_summary()
    assert h["expected"] == len(SYNCED) == 39
    assert h["stored"] == 0 and h["newest_session"] is None and h["missing"] == sorted(SYNCED)


def test_health_summary_counts_the_newest_session_providers_and_rs(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_sector_rotation import store as rotation_store
    _sync(monkeypatch, lambda t: eh.HoldingsList(HOLD, "yfinance" if t in ("SKYY", "XLK") else "uw"))
    # XLE: an older fetch (the session before), AIQ: no row at all
    rawstore.write_rows("etf_holdings", [{"ticker": "XLE", "entry": HOLD, "provider": "uw",
                                          "fetched_at": (SAT - timedelta(days=7)).timestamp()}])
    rawstore.delete("etf_holdings", "AIQ")
    rotation_store.save_holdings_cache("ARKQ", [{"ticker": "NVDA", "rs_1m": 1.0}])
    h = eh.health_summary()
    assert h["newest_session"] == "2026-10-09"
    assert h["in_newest"] == len(SYNCED) - 2                          # XLE older, AIQ missing
    assert h["stored"] == len(SYNCED) - 1
    assert h["providers"] == {"uw": len(SYNCED) - 3, "yfinance": 2}
    assert h["missing"] == ["AIQ"]
    assert h["themes"] == 28 and h["rs_stored"] == 1


def test_fmp_answer_is_tagged_fmp(monkeypatch):
    monkeypatch.setattr(eh, "fetch_from_fmp", lambda t, key: list(HOLD))
    monkeypatch.setattr(eh, "fetch_from_yfinance", lambda t: pytest.fail("yfinance asked after FMP answered"))
    rows = eh.get_etf_holdings("SKYY", _UW([]), fmp_api_key="k")
    assert rows == HOLD and rows.provider == "fmp"


def test_health_summary_counts_an_empty_list_as_missing(rawstore_tmp_db, monkeypatch):
    # every provider failed for AIQ and nothing was stored, so the job wrote [] with no provider (review S2)
    _sync(monkeypatch, lambda t: eh.HoldingsList([], None) if t == "AIQ" else eh.HoldingsList(HOLD, "uw"))
    assert rawstore.get("etf_holdings", "AIQ")["entry"] == []
    h = eh.health_summary()
    assert h["missing"] == ["AIQ"]
    assert h["stored"] == h["in_newest"] == len(SYNCED) - 1
    assert h["providers"] == {"uw": len(SYNCED) - 1}
