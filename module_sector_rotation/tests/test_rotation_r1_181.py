"""#181 Rotation R-1, trimmed (M-ROT-1, #374): render and failure fixes on the Sector Rotation board.

Offline: temp stores, fake engines and fake UW clients, no network.

- DF66 / ROT-1: a range Fetch renders the newest stored ``as_of`` per ticker, and the header shows the newest date;
- ROT-4: holdings RS covers theme ETFs only; sector rows keep the sector -> industry tree (and no sector ETF's
  holdings are priced, so no UW call for them);
- ROT-5: a degraded live Fetch does not replace a same-day snapshot that covers more ETFs, an empty one does not
  rewrite the board, and both say so in a ``JobMessage``;
- a missing z prints a dash and sorts last (not ``+0.00``); yields and index levels print no ``$`` (DF68);
- SR-1: the render asks UW for no ADV (fixed dot size, said in the caption); no PNG is exported; the dead
  ``backfill_rotation*`` helpers are gone;
- DF69 text: the yield note says the inversion is not applied (D-M7 default);
- DF70 / #181 R20: outflows are negative scores only and inflows positive ones, so the two lists never overlap
  (D-M8 default);
- DF74: no two rotation symbols share a display name; the Morningstar sector names are unchanged.
"""
from __future__ import annotations

import importlib.util
import json
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot
from RevenueChaser.module_sector_rotation import engine as rot_engine
from RevenueChaser.module_sector_rotation import etf_holdings as eh
from RevenueChaser.module_sector_rotation import report as rot_report
from RevenueChaser.module_sector_rotation import store as rotation_store
from RevenueChaser.module_sector_rotation.money_flow import get_money_flow_leaderboards
from RevenueChaser.module_sector_rotation.tests.bars_seed import seed
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()   # Tuesday 18:00 ET: a complete session
DAY = date(2026, 10, 6)
WANTED = [t for t in ROTATION_ETFS if t != "SPY"]
SECTORS = sorted(t for t, i in ROTATION_ETFS.items() if i.get("group") == "sector")
THEMES = sorted(t for t, i in ROTATION_ETFS.items() if i.get("group") == "theme")
OK_HEALTH = {"calls": 40, "failed": 0, "auth_error": None, "failures": []}


def _snap(t: str, d: date = DAY, close=100.0, rz=0.5, mz=0.5, quadrant="LEADING") -> ETFRotationSnapshot:
    return ETFRotationSnapshot(as_of_date=d, ticker=t, group_name=ROTATION_ETFS[t]["group"], close_price=close,
                               rs_1w=1.0, rs_1m=1.0, rs_3m=1.0, rs_ratio=1.0, rs_momentum=1.0,
                               rs_ratio_z=rz, rs_momentum_z=mz, rrg_quadrant=quadrant)


def _closes(n=80, step=1.0, first=100.0):
    start = date(2026, 6, 1)
    return [{"date": (start + timedelta(days=i)).isoformat(), "close": first + i * step} for i in range(n)]


class _UW:
    """A healthy client: daily candles for anything asked, and the tickers it was asked for."""

    def __init__(self):
        self.calls: list[str] = []

    def etf_ohlc(self, ticker, days=90, candle_size="1d"):
        self.calls.append(ticker)
        return _closes(step=0.0 if ticker == "SPY" else 1.0)

    def health(self):
        return dict(OK_HEALTH)


@pytest.fixture
def board(rawstore_tmp_db, tmp_path, monkeypatch):
    """The Rotation runner writing its board into ``tmp_path``; PNG export (if any) only recorded."""
    ledger.take_notes()
    monkeypatch.setattr(runners, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(runners, "ROTATION_LATEST", tmp_path / "sector_rotation_latest.html")
    monkeypatch.setattr(rot_report, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(rot_report, "coarse_theme_catalog", lambda: {})
    monkeypatch.setattr(runners, "run_id", lambda: "job-r1")
    monkeypatch.setattr(runners, "time", SimpleNamespace(time=lambda: COMPLETE))
    png = []
    if hasattr(runners, "export_rotation_png"):          # before the fix: record instead of drawing (slow)
        monkeypatch.setattr(runners, "export_rotation_png", lambda snaps: png.append(len(snaps)))
    return SimpleNamespace(dir=tmp_path, png=png)


def _engine(monkeypatch, snaps):
    class _Engine:
        def __init__(self, uw, benchmark="SPY"):
            pass

        def process_all(self):
            return list(snaps)

    monkeypatch.setattr(rot_engine, "RotationEngine", _Engine)


def _table_rows(html: str) -> list[str]:
    """Tickers of the main table's rows, in order."""
    return re.findall(r'<tr data-group="[^"]*" data-ticker="([^"]+)">', html)


def _meta(html: str) -> str:
    return re.search(r'<div class="meta">([^<]*)</div>', html).group(1)


def _cell(html: str, ticker: str, col: str) -> str:
    row = re.search(rf'<tr data-group="[^"]*" data-ticker="{re.escape(ticker)}">(.*?)</tr>', html).group(1)
    return re.search(rf'<td[^>]*data-col="{col}"[^>]*>([^<]*)</td>', row).group(1)


def _fragments(html: str) -> dict:
    return json.loads(re.search(r"var ROTATION_HOLDINGS = (.*?);  // ticker", html).group(1))


def _header():
    (head,) = rawstore.query("SELECT * FROM result_runs WHERE board = 'module_sector_rotation'", db="history")
    return head


# ── DF66 / ROT-1: a range Fetch renders the newest as_of per ticker ──────────────────────────────────────────────────

def test_a_range_fetch_over_three_stored_dates_renders_one_row_per_ticker_and_the_newest_date(board, monkeypatch):
    dates = [date(2026, 8, 5), date(2026, 8, 12), date(2026, 8, 19)]
    for i, d in enumerate(dates):
        rotation_store.save_snapshot([_snap(t, d, close=100.0 + i) for t in ("XLK", "XLE", "IGV")], as_of=d)
    rotation_store.save_snapshot([_snap("XLK", dates[0]), _snap("XLE", dates[0]), _snap("IGV", dates[0]),
                                  _snap("AIQ", dates[0]), _snap("US10Y", dates[0], close=4.25)], as_of=dates[0])
    monkeypatch.setattr(rotation_store, "backfill_weekly", lambda uw, s, e: 0)
    runners.run_rotation_latest(None, start="2026-08-01", end="2026-08-31")
    html = runners.ROTATION_LATEST.read_text(encoding="utf-8")
    rows = _table_rows(html)
    assert sorted(rows) == ["AIQ", "IGV", "US10Y", "XLE", "XLK"]   # one row per ticker, not one per (ticker, date)
    assert _meta(html).startswith("2026-08-19")                     # the newest date, not the first row's (AIQ: 08-05)
    assert "5 ETFs" in _meta(html)
    assert _cell(html, "XLK", "close") == "$102.00"                 # XLK's newest row
    assert _cell(html, "US10Y", "close") == "4.25"                  # its only (older) row still shows


# ── ROT-4: holdings RS for themes only; the sector tree survives ─────────────────────────────────────────────────────

def test_fetch_all_holdings_asks_for_no_sector_etf(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("etf_holdings", [{"ticker": "XLK", "entry": [{"ticker": "SECX", "name": "S", "weight": "5"}]},
                                         {"ticker": "AIQ", "entry": [{"ticker": "THMX", "name": "T", "weight": "5"}]}])
    for t in ("SPY", "SECX", "THMX"):                                        # the RS reads D01 (M-ROT-3, #374)
        seed(t, DAY, start=DAY - timedelta(days=150))
    asked, real = [], eh.stored_holdings
    monkeypatch.setattr(eh, "stored_holdings", lambda t: asked.append(t) or real(t))
    uw = _UW()
    out = rot_engine.fetch_all_holdings(uw, [_snap("XLK"), _snap("AIQ")])
    assert set(out) == {"AIQ"} and [r["ticker"] for r in out["AIQ"]] == ["THMX"]
    assert "XLK" not in asked and uw.calls == []


def test_a_sector_row_keeps_its_industry_tree_even_when_holdings_name_it(board):
    held = [{"ticker": "NVDA", "name": "NVIDIA", "weight": 14.0, "close": 100.0, "rs_1w": 1.0, "rs_1m": 1.0,
             "rs_3m": 1.0}]
    tree = [{"industry": "Software", "holdings": [{"ticker": "MSFT", "name": "Microsoft", "market_cap": 3e12,
                                                   "weight": 100.0}]}]
    out = rot_report.export_rotation_dashboard([_snap("XLK"), _snap("AIQ")], filename="tree.html",
                                               holdings={"XLK": held, "AIQ": held}, sector_holdings={"XLK": tree})
    frags = _fragments(out.read_text(encoding="utf-8"))
    assert "Industry holdings" in frags["XLK"] and "MSFT" in frags["XLK"] and "Top holdings" not in frags["XLK"]
    assert "Top holdings" in frags["AIQ"]


def test_a_live_fetch_with_a_full_rrg_stays_complete_and_records_theme_holdings_only(board, monkeypatch):
    _engine(monkeypatch, [_snap(t) for t in WANTED])
    rawstore.write_rows("etf_holdings", [{"ticker": t, "entry": [{"ticker": f"H{t}", "name": t, "weight": "5"}]}
                                         for t in SECTORS + THEMES])
    for t in ["SPY", *(f"H{t}" for t in SECTORS + THEMES)]:                 # the RS reads D01 (M-ROT-3, #374)
        seed(t, DAY, start=DAY - timedelta(days=150))
    tree = [{"industry": "Software", "holdings": [{"ticker": "MSFT", "name": "Microsoft", "market_cap": 3e12,
                                                   "weight": 100.0}]}]
    monkeypatch.setattr(rot_engine, "sector_etf_trees", lambda: {t: tree for t in SECTORS})
    uw = _UW()
    assert runners.run_rotation_latest(uw) == runners.ROTATION_LATEST          # no JobMessage: nothing went wrong
    head = _header()
    ctx = json.loads(head["context"])
    assert head["quality"] == "complete" and not head["quality_note"]
    assert ctx["without_holdings"] == SECTORS and ctx["expandable"] == len(THEMES) and ctx["missing"] == []
    held = rawstore.query("SELECT DISTINCT group_name FROM rotation_holdings_history", db="history")
    assert [r["group_name"] for r in held] == ["theme"]                         # the sector rows are gone
    assert uw.calls == []                                                       # no UW candle for any holding
    frags = _fragments(runners.ROTATION_LATEST.read_text(encoding="utf-8"))
    assert all("Industry holdings" in frags[t] for t in SECTORS)


# ── ROT-5: a degraded or empty live Fetch ────────────────────────────────────────────────────────────────────────────

def test_a_live_fetch_missing_two_etfs_keeps_the_complete_stored_same_day_snapshot(board, monkeypatch):
    rotation_store.save_snapshot([_snap(t, close=50.0) for t in WANTED], as_of=DAY)
    before = rawstore.query("SELECT * FROM rotation_snapshots WHERE as_of_date = ? ORDER BY ticker",
                            [DAY.isoformat()])
    _engine(monkeypatch, [_snap(t, close=99.0) for t in WANTED[2:]])
    monkeypatch.setattr(rot_engine, "fetch_all_holdings", lambda uw, snaps: {})
    monkeypatch.setattr(rot_engine, "sector_etf_trees", lambda: {})
    with pytest.raises(JobMessage) as exc:
        runners.run_rotation_latest(_UW())
    msg = exc.value.message
    assert WANTED[0] in msg and WANTED[1] in msg and "kept the stored" in msg
    assert rawstore.query("SELECT * FROM rotation_snapshots WHERE as_of_date = ? ORDER BY ticker",
                          [DAY.isoformat()]) == before                      # the day was not replaced
    html = runners.ROTATION_LATEST.read_text(encoding="utf-8")
    assert len(_table_rows(html)) == len(WANTED) and _cell(html, "XLK", "close") == "$50.00"   # the stored rows
    head = _header()
    assert head["quality"] == "partial" and "kept the stored" in head["quality_note"]


def test_a_degraded_live_fetch_with_nothing_better_stored_is_saved_and_says_so(board, monkeypatch):
    _engine(monkeypatch, [_snap(t) for t in WANTED[2:]])
    monkeypatch.setattr(rot_engine, "fetch_all_holdings", lambda uw, snaps: {})
    monkeypatch.setattr(rot_engine, "sector_etf_trees", lambda: {})
    with pytest.raises(JobMessage) as exc:
        runners.run_rotation_latest(_UW())
    assert WANTED[0] in exc.value.message and WANTED[1] in exc.value.message
    stored = rawstore.query("SELECT ticker FROM rotation_snapshots WHERE as_of_date = ?", [DAY.isoformat()])
    assert len(stored) == len(WANTED) - 2


def test_an_empty_live_fetch_does_not_rewrite_the_board(board, monkeypatch):
    runners.ROTATION_LATEST.write_text("<html>the last good board</html>", encoding="utf-8")
    _engine(monkeypatch, [])
    with pytest.raises(JobMessage, match="no ETF"):
        runners.run_rotation_latest(_UW())
    assert runners.ROTATION_LATEST.read_text(encoding="utf-8") == "<html>the last good board</html>"
    assert _header()["quality"] == "partial"


# ── z default, DF68 ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_missing_z_renders_a_dash_and_sorts_last(board):
    out = rot_report.export_rotation_dashboard(
        [_snap("XLK", rz=None, mz=None), _snap("XLE", rz=0.3, mz=-0.5)], filename="z.html")
    html = out.read_text(encoding="utf-8")
    assert _cell(html, "XLK", "rs_ratio_z") == "—" and _cell(html, "XLK", "rs_momentum_z") == "—"
    assert _cell(html, "XLE", "rs_ratio_z") == "+0.30"
    assert _table_rows(html) == ["XLE", "XLK"]                                 # same quadrant: the missing z last


def test_yields_and_index_levels_print_no_dollar_sign(board):
    out = rot_report.export_rotation_dashboard(
        [_snap("XLK", close=200.0), _snap("US10Y", close=4.25), _snap("MOVE", close=95.3),
         _snap("XLE", close=None)], filename="px.html")
    html = out.read_text(encoding="utf-8")
    assert _cell(html, "XLK", "close") == "$200.00"
    assert _cell(html, "US10Y", "close") == "4.25" and _cell(html, "MOVE", "close") == "95.30"
    assert _cell(html, "XLE", "close") == "—"                                  # a missing close, not a crash


# ── SR-1, PNG, dead helpers ──────────────────────────────────────────────────────────────────────────────────────────

def test_the_render_asks_uw_for_no_adv_and_the_caption_says_dots_are_fixed(board):
    class _NoScreener:
        def screener_batch(self, tickers):
            pytest.fail("the render asked UW for ADV")

    out = rot_report.export_rotation_dashboard([_snap("XLK"), _snap("XLE")], filename="adv.html",
                                               uw=_NoScreener())
    html = out.read_text(encoding="utf-8")
    assert "Dot size fixed" in html and "Dot size ∝" not in html


def test_a_live_fetch_exports_no_png_and_the_dead_helpers_are_gone(board, monkeypatch):
    _engine(monkeypatch, [_snap(t) for t in WANTED])
    monkeypatch.setattr(rot_engine, "fetch_all_holdings", lambda uw, snaps: {})
    monkeypatch.setattr(rot_engine, "sector_etf_trees", lambda: {})
    runners.run_rotation_latest(_UW())
    assert board.png == [] and not list(board.dir.glob("*.png"))
    for name in ("export_rotation_png", "RRG_PNG", "backfill_rotation", "backfill_rotation_weekly"):
        assert not hasattr(runners, name), name
    assert importlib.util.find_spec("RevenueChaser.module_sector_rotation.rrg_plot") is None


# ── DF69 text (D-M7 default) ─────────────────────────────────────────────────────────────────────────────────────────

def test_the_yield_note_says_the_inversion_is_not_applied(board):
    html = rot_report.export_rotation_dashboard([_snap("US10Y", close=4.25)], filename="y.html").read_text(
        encoding="utf-8")
    assert "inversion is not applied" in html
    assert "is <b>inverted</b> before RS" not in html and "inverted so falling yields" not in html
    trail = rot_report._trail_html(date(2026, 8, 1), date(2026, 8, 31), [], {})
    assert "Yield symbols are inverted" not in trail and "inversion is not applied" in trail


# ── DF70 / R20 (D-M8 default) ────────────────────────────────────────────────────────────────────────────────────────

def _flow_frame(spec: dict[str, float]):
    """One row per day for each ticker; ``spec`` gives its daily step (positive: up days, negative: down days)."""
    import pandas as pd
    rows = [{"ticker": t, "date": f"2026-07-{i + 1:02d}", "close": 100.0 + i * step, "volume": 1000.0}
            for t, step in spec.items() for i in range(25)]
    return pd.DataFrame(rows)


def test_outflows_hold_no_score_at_or_above_zero():
    res = get_money_flow_leaderboards(_flow_frame({"UP1": 1.0, "UP2": 0.5, "UP3": 2.0}), window=21, top_n=10)
    assert res["outflows"] == []
    assert {r["ticker"] for r in res["inflows"]} == {"UP1", "UP2", "UP3"}


def test_inflows_and_outflows_never_overlap():
    res = get_money_flow_leaderboards(_flow_frame({"UP": 1.0, "DOWN": -1.0, "DOWN2": -0.5}), window=21, top_n=10)
    assert all(r["flow_score"] < 0 for r in res["outflows"]) and all(r["flow_score"] > 0 for r in res["inflows"])
    assert not {r["ticker"] for r in res["inflows"]} & {r["ticker"] for r in res["outflows"]}
    assert [r["ticker"] for r in res["inflows"]] == ["UP"]


# ── DF74 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_only_the_region_duplicates_are_renamed():
    # DF74: EIDO/IDX (region group, read by no taxonomy) get distinct names. SMH/SOXX keep "Semiconductors":
    # it is the label the boards show, so a rename would leak into the theme filters (reverted 639da86).
    names = [i["name"] for i in ROTATION_ETFS.values()]
    assert sorted({n for n in names if names.count(n) > 1}) == ["Semiconductors"]


def test_the_theme_names_carry_no_etf_suffix():
    """The rotation theme names are the labels a board shows; an ETF suffix would leak into the filters."""
    names = [i["name"] for i in ROTATION_ETFS.values() if i.get("group") == "theme"]
    assert "Semiconductors" in names
    assert not [n for n in names if "(VanEck)" in n or "(iShares)" in n]


def test_the_sector_names_stay_the_morningstar_names():
    assert {ROTATION_ETFS[t]["name"] for t in SECTORS} == {
        "Communication Services", "Energy", "Financial Services", "Industrials", "Basic Materials", "Technology",
        "Consumer Defensive", "Real Estate", "Utilities", "Healthcare", "Consumer Cyclical"}
