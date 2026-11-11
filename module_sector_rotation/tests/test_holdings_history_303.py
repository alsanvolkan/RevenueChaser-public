"""#303 (D25), #642 PR 5: the Rotation Fetch reads the spine's holdings, and Data Health reports them.

The holdings job, its UW/yfinance chain and its change history are gone (#642 PR 5): the spine's
``funds/etf_holdings`` is the one writer. The Rotation Fetch fetches no holdings: it reads the spine's lists and
computes RS on them. Temp stores only, no network.
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.core import rawstore
from RevenueChaser.rcdata.tests import spine_fixture
from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot
from RevenueChaser.module_sector_rotation import engine
from RevenueChaser.module_sector_rotation import etf_holdings as eh
from RevenueChaser.module_sector_rotation.tests.bars_seed import seed
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

SYNCED = [t for t, i in ROTATION_ETFS.items() if i.get("group") in ("sector", "theme")]
HOLD = [{"ticker": "NVDA", "name": "NVIDIA", "weight": 14.2}, {"ticker": "AAPL", "name": "Apple", "weight": 12.1}]
END = date(2026, 10, 9)


class _UW:
    def __init__(self, rows=None):
        self.rows, self.holdings_calls = rows or [], []

    def etf_holdings(self, ticker):
        self.holdings_calls.append(ticker)
        return self.rows


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
    spine_fixture.write_etf_holdings({"AIQ": HOLD})                   # the spine's lists (#642 PR 4)
    start = END - timedelta(days=150)                                   # the RS reads D01 since M-ROT-3 (#374)
    seed("SPY", END, start=start, closes=lambda i: 100.0)
    for t in ("NVDA", "AAPL"):
        seed(t, END, start=start, closes=lambda i: 100.0 + i)
    uw = _OhlcUW()
    out = engine.fetch_all_holdings(uw, [_snap("AIQ"), _snap("ARKQ")])
    assert uw.holdings_calls == []
    assert [r["ticker"] for r in out["AIQ"]] == ["NVDA", "AAPL"]      # the stored holdings, with RS
    assert out["AIQ"][0]["rs_1m"] > 0
    assert "ARKQ" not in out                                            # nothing stored: nothing embedded
    assert uw.ohlc_calls == []                                          # no UW candle either (M-ROT-3)


def test_a_rotation_fetch_writes_no_raw_holdings(rawstore_tmp_db):
    """The old ``raw.etf_holdings`` stays unwritten: nothing writes it since #642 PR 5."""
    spine_fixture.write_etf_holdings({"AIQ": HOLD})
    engine.fetch_all_holdings(_OhlcUW(), [_snap("AIQ")])
    assert rawstore.query("SELECT count(*) AS n FROM etf_holdings")[0]["n"] == 0


# ── Data Health summary ────────────────────────────────────────────────────────────────────────────────────────────
def test_health_summary_on_an_empty_store(rawstore_tmp_db):
    h = eh.health_summary()
    assert h["expected"] == len(SYNCED) == 34
    assert h["stored"] == 0 and h["newest_session"] is None and h["missing"] == sorted(SYNCED)


def _spine(provider_of=lambda t: "ssga", skip=("AIQ",), as_of="2026-10-09"):
    """Every board ETF but ``skip`` gets ``HOLD`` on ``as_of``, one spine write per provider."""
    by: dict[str, dict] = {}
    for t in SYNCED:
        if t not in skip:
            by.setdefault(provider_of(t), {})[t] = HOLD
    for provider, lists in by.items():
        spine_fixture.write_etf_holdings(lists, as_of=as_of, provider=provider)


def test_health_summary_counts_the_newest_day_providers_and_rs(rawstore_tmp_db):
    from RevenueChaser.module_sector_rotation import store as rotation_store
    _spine(lambda t: "uw" if t in ("SKYY", "AIQ") else "ssga", skip=("AIQ", "XLE"))
    spine_fixture.write_etf_holdings({"XLE": HOLD}, as_of="2026-10-02", provider="ssga")   # an older day
    rotation_store.save_holdings_cache("ARKQ", [{"ticker": "NVDA", "rs_1m": 1.0}])
    h = eh.health_summary()
    assert h["newest_session"] == "2026-10-09"
    assert h["in_newest"] == len(SYNCED) - 2                          # XLE older, AIQ missing
    assert h["stored"] == len(SYNCED) - 1
    assert h["lines"] == 2 * (len(SYNCED) - 1)
    assert h["providers"] == {"ssga": len(SYNCED) - 2, "uw": 1}
    assert h["missing"] == ["AIQ"]
    assert h["themes"] == 23 and h["rs_stored"] == 1


# ── the typed numbers (#124 S4) ───────────────────────────────────────────────────────────────────────────────────
def test_health_is_the_summary_numbers_typed(rawstore_tmp_db):
    """Covered is the ETFs with spine holdings against the board's 34; the provider share is the provider of
    every ETF's list. The newest day is the spine's, not a population."""
    from RevenueChaser.core import datahealth

    _spine(lambda t: "ishares" if t in ("SOXX", "IBB") else "ssga", skip=())

    n = eh.health_summary()
    h = eh.health()

    assert h.covered == n["stored"] == len(SYNCED) == 34
    assert h.expected == n["expected"] == 34
    assert (h.newest, h.expected_as_of) == (n["newest_session"], None) == ("2026-10-09", None)
    assert h.source_share == {p: c / n["stored"] for p, c in n["providers"].items()}
    assert h.problems == ()
    assert datahealth.coverage_text(h) == "100.0% (34 of 34)"


def test_health_counts_a_missing_etf(rawstore_tmp_db):
    from RevenueChaser.core import datahealth

    _spine()

    n = eh.health_summary()
    h = eh.health()

    assert (h.covered, h.expected) == (n["stored"], n["expected"]) == (33, 34)
    assert n["missing"] == ["AIQ"] and n["providers"] == {"ssga": 33}
    assert datahealth.coverage_text(h) == "97.1% (33 of 34)"
