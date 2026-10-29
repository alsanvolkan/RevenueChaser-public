"""#166 end to end: a failing Unusual Whales call is named on the job and the board, and never becomes data.

Each test breaks one provider call through the mock server and drives the real dashboard:

* SPY is absent from the prices store: the holdings sync keeps the stored RS (no zero rows) and names the theme
  ETFs; since the RS reads the prices store it asks for no candle, so a broken SPY candle route cannot reach it;
* Money Flow ranks the stored bars since M-ROT-3 (#374), the prices store's since PR 9: an ETF the store lacks flags
  the leaderboard partial on the board, it never replaces the newest complete leaderboard in result history, and no
  candle is asked for;
* the range backfill reads the stored bars since M-ROT-2 (#374), so no candle answer reaches it: with SPY absent from
  the prices store it writes no day and says why, and an ETF the store lacks is left out of the days it writes, with
  no provider call;
* (G9: OIPD reads the options store and pulls no chain, so its second-page case is gone; the builder's
  ``uw/option_list`` tests cover a failed page: ``rcdata/tests``.)

The healthy path is asserted next to each failure so a fix that blanks everything cannot pass.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest


CANDLES = 200
THEME_HOLDINGS = [{"ticker": t, "name": f"{t} Corp", "weight": str(w), "updated": "2026-09-01"}
                  for t, w in (("AAA", 9), ("BBB", 6), ("CCC", 3))]
BOOM = {"error": "boom"}


# ── fixtures ─────────────────────────────────────────────────────────────────────────────

def _synthetic_ohlc(ticker: str) -> list[dict]:
    seed = int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16)
    drift = ((seed % 21) - 10) / 1000.0
    start, price, rows = date(2026, 2, 1), 100.0, []
    for i in range(CANDLES):
        price *= 1.0 + drift + (((seed >> i % 16) & 3) - 1.5) / 2000.0
        rows.append({"date": (start + timedelta(days=i)).isoformat(), "open": round(price * 0.995, 4),
                     "high": round(price * 1.01, 4), "low": round(price * 0.99, 4),
                     "close": round(price, 4), "total_volume": 1_000_000 + i})
    return rows


@pytest.fixture
def routes(provider_routes):
    """OHLC for any ticker, three holdings for every ETF."""
    provider_routes.route("/api/stock/", payload=lambda path: {
        "data": _synthetic_ohlc(path.strip("/").split("/")[2] if len(path.strip("/").split("/")) > 2 else "SPY")})
    provider_routes.route("/api/etfs/", payload={"data": THEME_HOLDINGS})
    return provider_routes


def _break(routes, ticker: str) -> None:
    routes.route(f"/api/stock/{ticker}/ohlc", payload=BOOM, status=500)


def _heal(routes, ticker: str) -> None:
    routes._routes.pop(f"/api/stock/{ticker}/ohlc", None)


def _query(dashboard, sql: str, params=()) -> list[tuple]:
    import duckdb
    conn = duckdb.connect(str(dashboard.store_dir / "raw.duckdb"), read_only=True)
    try:
        return conn.execute(sql, list(params)).fetchall()
    finally:
        conn.close()


def _api_moneyflow(dashboard) -> dict:
    return dashboard.get("/api/moneyflow").json()


def _seed_bars(dashboard, monkeypatch, skip=(), only=None) -> None:
    """Bars in the dashboard's prices store (``price_bar_daily``) for SPY and every rotation ETF but ``skip``, from
    ``bars_seed.FIRST`` to the latest complete session: what the prices build leaves (M-ROT-2, #374; PR 9)."""
    import RevenueChaser.core.rawstore as rawstore
    from RevenueChaser.core import calendar
    from RevenueChaser.module_sector_rotation import engine
    from RevenueChaser.module_sector_rotation.tests.bars_seed import seed

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")

    monkeypatch.setattr("RevenueChaser.core.config.MARKET_DIR", dashboard.env["RC_MARKET_DIR"])  # the prices store (G3)
    rawstore.ensure_all_tables()
    session = calendar.latest_complete_session()
    for t in ["SPY", *engine.rrg_tickers()]:
        if t not in skip and (only is None or t in only):
            seed(t, session)


def _drop_bars(ticker: str) -> None:
    """Remove ``ticker``'s stored bars from the prices store ``_seed_bars`` pointed at."""
    from RevenueChaser.rcdata.tests import prices_store
    prices_store.delete_bars(ticker)


def _record_complete_leaderboard(board: dict) -> None:
    """Record ``board`` in result history as a complete run after the latest session's close, whatever the real
    clock says: the guard's newest complete session (ROT-9). A Fetch the dashboard ran after a close records one
    itself; one it ran mid-session records its header only."""
    from datetime import datetime, time, timezone
    from RevenueChaser.core import calendar
    from RevenueChaser.module_sector_rotation import history as rot_history

    session = calendar.latest_complete_session()
    run_at = datetime.combine(session, time(23, 0), tzinfo=timezone.utc).timestamp()      # 19:00/18:00 ET
    rot_history.record_money_flow(board, run_id="e2e-complete", run_at=run_at, days=30, window=21, top_n=10)


def _candle_calls(routes) -> list[str]:
    return [q.path for q in routes.requests("/api/stock/") if "/ohlc" in q.path]


# ── SPY candles down ─────────────────────────────────────────────────────────────────────

class TestSpyCandlesDown:
    def test_the_holdings_sync_keeps_the_stored_rs_and_names_the_theme_etfs(self, dashboard, routes, monkeypatch):
        """The holdings RS reads SPY from the prices store, so the store's own coverage decides, not a fetch.

        Seeded, the theme ETFs get their RS rows and no candle is asked for; with SPY's bars dropped the run keeps
        the rows it already stored (no zero rows) and names the ETFs, still without a candle.
        """
        _seed_bars(dashboard, monkeypatch)
        healthy = dashboard.run_job("/run/holdings")
        assert healthy.ok and "RS computed for 0 theme" not in healthy.message, healthy
        stored = _query(dashboard, "SELECT ticker, rows FROM rotation_holdings ORDER BY ticker")
        assert stored
        assert _candle_calls(routes) == []

        _drop_bars("SPY")
        broken = dashboard.run_job("/run/holdings")
        assert broken.ok, broken
        assert "too few SPY sessions" in broken.message and "Kept stored RS for" in broken.message
        assert _query(dashboard, "SELECT ticker, rows FROM rotation_holdings ORDER BY ticker") == stored
        assert _candle_calls(routes) == []

    def test_a_broken_spy_candle_route_cannot_reach_the_holdings_job(self, dashboard, routes, monkeypatch):
        """What #166 asked of the job — ask for SPY once, survive a 500 — the prices store answers by construction:
        the route is broken and the RS is computed anyway, because the job never calls it."""
        _seed_bars(dashboard, monkeypatch)
        _break(routes, "SPY")
        res = dashboard.run_job("/run/holdings")
        assert res.ok and "RS computed for 0 theme" not in res.message, res
        assert "too few SPY sessions" not in (res.message or ""), res
        assert _candle_calls(routes) == []

    def test_a_range_backfill_without_spy_in_the_prices_store_writes_no_day_and_says_so(
            self, dashboard, routes, monkeypatch):
        _seed_bars(dashboard, monkeypatch, skip={"SPY"})
        _break(routes, "SPY")
        res = dashboard.run_job("/run/rotation", start="2026-06-01", end="2026-08-30")
        dashboard.wait_until_idle()
        assert res.ok, res
        assert "days not written: the prices store holds no SPY bar" in res.message, res
        assert dashboard.table_count("rotation_snapshots") == 0

    def test_the_same_range_with_spy_in_the_prices_store_writes_the_days(self, dashboard, routes, monkeypatch):
        _seed_bars(dashboard, monkeypatch)
        res = dashboard.run_job("/run/rotation", start="2026-06-01", end="2026-08-30")
        dashboard.wait_until_idle()
        assert res.ok and "not written" not in (res.message or ""), res
        assert dashboard.table_count("rotation_snapshots") > 0
        zero = _query(dashboard, "SELECT count(*) FROM rotation_snapshots WHERE rs_1m = 0 AND rs_3m = 0")[0][0]
        assert zero == 0


# ── one ETF down ─────────────────────────────────────────────────────────────────────────

class TestOneEtfDown:
    def test_a_range_backfill_asks_no_candle_and_leaves_out_an_etf_the_store_lacks(
            self, dashboard, routes, monkeypatch):
        _seed_bars(dashboard, monkeypatch, skip={"XLK"})
        _break(routes, "XLK")
        res = dashboard.run_job("/run/rotation", start="2026-06-01", end="2026-08-30")
        dashboard.wait_until_idle()
        assert res.ok and "not written" not in (res.message or ""), res
        assert dashboard.table_count("rotation_snapshots") > 0
        assert _query(dashboard, "SELECT count(*) FROM rotation_snapshots WHERE ticker = 'XLK'")[0][0] == 0
        assert _candle_calls(routes) == []                       # nor does the queued Money Flow (M-ROT-3)

    def test_a_partial_leaderboard_never_replaces_a_whole_one(self, dashboard, routes, monkeypatch):
        _seed_bars(dashboard, monkeypatch)
        assert dashboard.run_job("/run/moneyflow").ok
        whole = _api_moneyflow(dashboard)
        assert whole["inflows"] and "partial" not in whole and "missing" not in whole
        assert set(whole) == {"inflows", "outflows", "generated_at", "as_of"}
        _record_complete_leaderboard(whole)

        _drop_bars("SMH")
        _break(routes, "SMH")
        held = dashboard.run_job("/run/moneyflow")
        assert held.ok and "kept the stored leaderboard" in held.message and "SMH" in held.message, held
        assert _api_moneyflow(dashboard) == whole

        _seed_bars(dashboard, monkeypatch, only={"SMH"})
        assert dashboard.run_job("/run/moneyflow").ok
        assert "partial" not in _api_moneyflow(dashboard)
        assert _candle_calls(routes) == []                       # stored bars only: no candle for any ETF (M-ROT-3)

    def test_with_nothing_stored_the_partial_leaderboard_is_saved_flagged(self, dashboard, routes, monkeypatch):
        _seed_bars(dashboard, monkeypatch, skip={"SMH"})
        res = dashboard.run_job("/run/moneyflow")
        assert res.ok and "saved flagged" in res.message and "SMH" in res.message, res
        flagged = _api_moneyflow(dashboard)
        assert flagged["partial"] is True and flagged["missing"] == ["SMH"] and flagged["inflows"]


# ── the board note (headless Chrome) ─────────────────────────────────────────────────────

def _chrome() -> str | None:
    override = os.environ.get("RC_CHROME")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    return None


@pytest.fixture
def chrome() -> str:
    path = _chrome()
    if path is None:
        msg = "no Chrome/Chromium for the money-flow note: set RC_CHROME or install google-chrome / chromium"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg)
        pytest.skip(msg)
    return path


def _dump_dom(chrome: str, url: str, profile: Path) -> str:
    out = subprocess.run(
        [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
         f"--user-data-dir={profile}",
         # as in ``test_board_sec_13f``: a board's CDN plotly request would pause the virtual clock
         "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1",
         "--virtual-time-budget=6000", "--dump-dom", url],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert "<html" in out.stdout.lower(), f"Chrome dumped no DOM: {out.stderr[-1500:]}"
    return out.stdout


def _note(dom: str) -> str | None:
    m = re.search(r'<div id="mf-note"[^>]*>(.*?)</div>', dom, re.S)
    return m.group(1) if m else None


class TestMoneyFlowNoteOnTheBoard:
    def test_the_board_shows_an_amber_note_naming_the_missing_etf(self, dashboard, routes, chrome, tmp_path,
                                                                   monkeypatch):
        _seed_bars(dashboard, monkeypatch, skip={"SMH"})
        _break(routes, "SMH")
        assert dashboard.run_job("/run/rotation").ok
        dashboard.wait_until_idle()
        note = _note(_dump_dom(chrome, f"{dashboard.url}/view/module_sector_rotation", tmp_path / "p1"))
        assert note is not None and "SMH" in note and "Partial leaderboard" in note

    def test_a_whole_leaderboard_shows_no_note(self, dashboard, routes, chrome, tmp_path, monkeypatch):
        _seed_bars(dashboard, monkeypatch)
        assert dashboard.run_job("/run/rotation").ok
        dashboard.wait_until_idle()
        dom = _dump_dom(chrome, f"{dashboard.url}/view/module_sector_rotation", tmp_path / "p2")
        assert 'id="mf-inflows"' in dom and _note(dom) is None
