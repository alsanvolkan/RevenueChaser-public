"""Forecast-ledger freeze, end to end (#128): a Fetch freezes, a Refresh never does.

The dashboard runs as a subprocess, so the test clock reaches it through ``RC_E2E_NOW`` (an ISO UTC
time read by ``harness/dashboard_entrypoint.py``, which replaces ``core.calendar._now``). Bars are
seeded the way RC-Daily-Bars leaves D01 after the close: they end on the faked session D and carry a real
``fetched_at``, which is later than D's close + 30 min because D is in the past. Since C12 (#383 R3) the board
scores ZETA from those D01 bars and its D04 quote, and the freeze checks the same D01 bars (K8b).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from RevenueChaser.module_testing.e2e.harness.screener_seed import seed_d01, seed_quote

D = "2026-09-25"                                    # a Friday, in the past on any machine running this
POST_CLOSE = "2026-09-25T21:00:00+00:00"            # 17:00 ET (EDT)
INTRADAY = "2026-09-25T16:00:00+00:00"              # 12:00 ET

_ANNUAL = [
    {"fiscal_year": "2024", "revenue": 100.0e6, "gross_profit": 60.0e6, "operating_cash_flow": 25.0e6,
     "capex": 4.0e6, "total_debt": 10.0e6, "cash": 30.0e6, "total_assets": 200.0e6,
     "total_equity": 100.0e6, "net_income": 10.0e6, "ebit": 12.0e6, "depreciation": 3.0e6},
    {"fiscal_year": "2025", "revenue": 130.0e6, "gross_profit": 78.0e6, "operating_cash_flow": 30.0e6,
     "capex": 4.0e6, "total_debt": 10.0e6, "cash": 30.0e6, "total_assets": 220.0e6,
     "total_equity": 110.0e6, "net_income": 14.0e6, "ebit": 16.0e6, "depreciation": 3.0e6},
]


@pytest.fixture
def fake_now(request) -> str:
    """The subprocess's clock. Override with ``@pytest.mark.parametrize('fake_now', [...], indirect=True)``."""
    return getattr(request, "param", POST_CLOSE)


@pytest.fixture
def e2e_env(e2e_env, fake_now):
    return {**e2e_env, "RC_E2E_NOW": fake_now}


def _bars() -> pd.DataFrame:
    idx = pd.bdate_range(end=D, periods=300)
    close = pd.Series(100 + pd.Series(range(300), index=idx) * 0.1, index=idx)
    return pd.DataFrame({"Open": close.shift(1).fillna(close.iloc[0]), "High": close * 1.01,
                         "Low": close * 0.99, "Close": close, "Volume": 1_000_000})


@pytest.fixture
def seeded(dashboard, monkeypatch):
    import RevenueChaser.core.rawstore as rawstore

    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    rawstore.ensure_all_tables()
    quote = {"regularMarketPrice": 130.0, "fiftyTwoWeekHigh": 135.0, "fiftyDayAverage": 120.0,
             "averageDailyVolume10Day": 2_000_000}
    rawstore.replace_all("equity_universe", [{
        "ticker": "ZETA", "cik": "", "name": "Zeta Corp", "market_cap": 2.0e9, "price": 130.0,
        "sector": "Information Technology", "industry": "Software",
        "industry_group": "Software & Services", "sub_industry": "Application Software",
        "quote": quote}])
    for t in ("ZETA", "SPY"):
        seed_d01(t, _bars())                        # fetched_at = real now, bars end on D
    seed_quote("ZETA", datetime.strptime(D, "%Y-%m-%d").date(), quote, 2.0e9)
    rawstore.write_rows("edgar_financials", [{
        "ticker": "ZETA", "annual": _ANNUAL, "quarterly": [],
        "meta": {"source": "sec_edgar", "entity_name": "Zeta Corp", "is_us": True, "available": True},
        "fetched_at": time.time()}])
    return dashboard


def _ledger(dashboard) -> list[dict]:
    """Ledger rows straight from the temp store (the dashboard is idle between calls)."""
    import duckdb
    db = dashboard.store_dir / "raw.duckdb"
    conn = duckdb.connect(str(db), read_only=True)
    try:
        cur = conn.execute("SELECT * FROM forecast_ledger ORDER BY source, rank")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _fetch(dashboard):
    job = dashboard.run_job("/run/module_fundamentals")
    dashboard.wait_until_idle()
    return job


def _cache_mid_session(dashboard):
    """Make the seeded D bars look written at 11:30 ET on D: partial, before the close.

    A Refresh reads no bar at all (K4), whatever their age; that is what this proves.
    """
    import RevenueChaser.core.rawstore as rawstore
    fetched_at = datetime(2026, 9, 25, 15, 30, tzinfo=timezone.utc).timestamp()
    with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
        conn.execute("UPDATE price_bars_daily SET fetched_at = ?", [fetched_at])


def test_a_post_close_fetch_freezes_the_shown_pick(dashboard, seeded):
    job = _fetch(dashboard)
    assert job.ok, job
    rows = _ledger(dashboard)
    assert [(r["source"], r["subject"], r["rank"], str(r["forecast_date"]), str(r["target_date"]),
             r["target_type"]) for r in rows] == [("fundamentals", "ZETA", 1, D, D, "pick")]
    assert rows[0]["run_id"] == job.id                       # the job's id is the run id
    from RevenueChaser.module_fundamentals import config as fund_config
    assert rows[0]["model_version"].startswith(f"{fund_config.SELECTION_VERSION}+")   # stamped with the board's version
    assert rows[0]["shown_price"] is not None
    assert job.notes == ["Froze 1 Fundamentals rows for 2026-09-25."]
    assert job.message is None                               # a message is what the dashboard pops up
    assert "forecast ledger: Froze 1 Fundamentals rows for 2026-09-25." in dashboard.logs()


def test_a_refresh_adds_no_rows_and_makes_no_network_calls(dashboard, provider_routes, seeded):
    before = provider_routes.request_count
    assert dashboard.view("module_fundamentals", refresh=True).status_code == 200
    assert dashboard.table_count("forecast_ledger") == 0
    assert provider_routes.request_count == before
    assert dashboard.yf_calls() == []


def test_a_refresh_after_a_fetch_adds_no_rows(dashboard, seeded):
    _fetch(dashboard)
    frozen = _ledger(dashboard)
    assert len(frozen) == 1
    assert dashboard.view("module_fundamentals", refresh=True).status_code == 200
    assert _ledger(dashboard) == frozen


def test_a_second_fetch_the_same_day_changes_nothing(dashboard, seeded):
    _fetch(dashboard)
    frozen = _ledger(dashboard)
    job = _fetch(dashboard)
    assert job.ok, job
    assert _ledger(dashboard) == frozen
    assert job.notes == ["Fundamentals not frozen: already frozen for 2026-09-25."]
    assert job.message is None


@pytest.mark.parametrize("fake_now", [INTRADAY], indirect=True)
def test_the_subprocess_clock_seam_is_honoured_inside_the_session_window(dashboard, seeded):
    """With the clock at 12:00 ET the Fetch must refuse to freeze. The real clock (whatever day the
    suite runs on) would give a different answer, so this proves ``RC_E2E_NOW`` reached the dashboard."""
    job = _fetch(dashboard)
    assert job.ok, job
    assert dashboard.table_count("forecast_ledger") == 0
    assert [n.split(":")[0] for n in job.notes] == ["Fundamentals not frozen"]
    assert "inside the US session window" in job.notes[0]
    assert job.message is None


def test_the_seeded_session_date_comes_from_the_faked_clock_not_the_real_one(dashboard, seeded):
    """A real clock in the future would name a later session (the seeded bars end on D)."""
    _fetch(dashboard)
    assert {str(r["forecast_date"]) for r in _ledger(dashboard)} == {D}


def test_a_fetch_does_not_refetch_bars_that_were_cached_after_the_close(dashboard, seeded):
    """The picks and SPY read D01 (C12, U1): a Fetch makes no yfinance bar read."""
    job = _fetch(dashboard)
    assert job.ok, job
    assert not [c for c in dashboard.yf_calls() if c.startswith("history:")]


def test_a_refresh_makes_no_yfinance_call_even_when_bars_were_cached_mid_session(
        dashboard, provider_routes, seeded):
    """The refetch rule belongs to a Fetch. Refresh (``no_fetch``) never evaluates it (AGENTS.md section 2).

    A Refresh shows the last Fetch and never re-scores (K4, #383 R1), so a Fetch runs first; the bars are then
    made mid-session and the Refresh must still reach nothing and freeze nothing."""
    assert _fetch(dashboard).ok
    ledger = dashboard.table_count("forecast_ledger")
    _cache_mid_session(dashboard)
    before, yf_before = provider_routes.request_count, dashboard.yf_calls()
    page = dashboard.view("module_fundamentals", refresh=True)
    assert page.status_code == 200
    rows = dashboard.get("/view/module_fundamentals/payload.json").json()["rows"]     # the page's rows (K10)
    assert [r["ticker"] for r in rows] == ["ZETA"]   # the Refresh shows the Fetch, whatever the bars' age now
    assert dashboard.yf_calls() == yf_before
    assert provider_routes.request_count == before
    assert dashboard.table_count("forecast_ledger") == ledger


# ── a Fetch opens no popup (headless Chrome) ─────────────────────────────────────────────────────────

FETCH_ROUTES = ("/run/swing", "/run/module_fundamentals", "/run/module_asymmetric_growth", "/run/universe")


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
        msg = "no Chrome/Chromium for the no-popup check: set RC_CHROME or install google-chrome / chromium"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg)
        pytest.skip(msg)
    return path


# Runs before any dashboard script. ``alert`` records into the <html> element, so the dumped DOM shows it.
# The page marks every job that finished before it loaded as "seen" (no alert storm on a reload), so the
# hook forgets that once the page's own startup is done and starts polling: ``poll()`` then meets the four
# finished Fetches the way it meets a Fetch that finishes while the page is open. The last ``alert`` is the
# hook's own: it proves the recorder works, so "no alert" cannot be an empty pass.
_ALERT_HOOK = """<script>
window.__alerts = [];
window.alert = function (m) {
  window.__alerts.push(String(m));
  document.documentElement.setAttribute('data-alerts', window.__alerts.join('\\n'));
};
window.confirm = function () { return false; };
// The page's own startup calls eleven endpoints and ``poll()`` reloads an iframe per finished job. Each one is
// a real request, and under ``--virtual-time-budget`` a pending request pauses the virtual clock, so one slow
// answer means ``--dump-dom`` never returns (on a hosted runner it did not: two rounds of 120 s). Every request
// is answered here instead, from the body the test read itself, so the browser needs no network at all. What is
// under test is untouched: ``poll()`` runs on the real ``/api/jobs`` body and alerts, or does not, from it.
window.__jobs = %(jobs)s;
window.fetch = function (url) {
  var body = String(url).indexOf('/api/jobs') >= 0 ? window.__jobs : {};
  return Promise.resolve({ok: true, status: 200,
                          json: function () { return Promise.resolve(body); },
                          text: function () { return Promise.resolve(JSON.stringify(body)); }});
};
window.addEventListener('load', function () {
  setTimeout(function () {
    window._seenDone = {};
    startPolling();
    setTimeout(function () {
      document.documentElement.setAttribute('data-seen', String(Object.keys(window._seenDone).length));
      alert('hook-selftest');
    }, 4000);
  }, 1500);
});
</script>"""


def _alerts_and_seen(chrome: str, dashboard, tmp_path: Path) -> tuple[list[str], int]:
    html = dashboard.get("/").text
    jobs = dashboard.get("/api/jobs").text                 # the real payload, handed to the page by the hook
    blank = tmp_path / "blank.html"
    blank.write_text("<!doctype html><title>blank</title>", encoding="utf-8")
    # ``poll()`` reloads the iframe of every module a finished job produces. Point them at a local file: the
    # reload still happens, so the alert path is unchanged, but it fetches nothing from the dashboard.
    framed = re.sub(r'data-src="[^"]*"', 'data-src="%s"' % blank.as_uri(), html)
    assert framed != html, "the dashboard page has no iframe to point at a blank file"
    hooked = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + _ALERT_HOOK % {"jobs": jobs}, framed, count=1,
                    flags=re.I)
    assert hooked != framed, "the dashboard page has no <head> to hook"
    page = tmp_path / "dashboard_hooked.html"
    page.write_text(hooked, encoding="utf-8")
    argv = [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
            f"--user-data-dir={tmp_path / 'chrome-profile'}",
            # Nothing here reaches the network: the hook answers every request and the iframes point at a local
            # file, so this only makes a stray host fail at once instead of pausing the virtual clock.
            "--host-resolver-rules=MAP * ~NOTFOUND",
            "--virtual-time-budget=12000", "--dump-dom", page.as_uri()]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=120)
    except subprocess.TimeoutExpired as exc:               # it never returned: say what it had to say
        err = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        raise AssertionError(f"Chrome did not finish in 120 s. Its stderr: {err[-2000:]}") from exc
    assert "<html" in out.stdout.lower(), f"Chrome dumped no DOM: {out.stderr[-1500:]}"
    root = re.search(r"<html[^>]*>", out.stdout, re.I).group(0)
    alerts = re.search(r'data-alerts="([^"]*)"', root)
    seen = re.search(r'data-seen="([^"]*)"', root)
    return (alerts.group(1).split("\n") if alerts else []), int(seen.group(1)) if seen else -1


def test_a_fetch_of_swing_fundamentals_ag_and_universe_opens_no_alert(dashboard, seeded, chrome, tmp_path):
    """The freeze outcome used to ride in the job ``message``, which the dashboard shows with ``alert()``:
    every Fetch of these four jobs ended in a popup ("Fundamentals not frozen: inside the US session
    window..."). It is the job's ``notes`` now, and the page shows no popup."""
    jobs = [dashboard.run_job(route) for route in FETCH_ROUTES]
    assert all(j.ok for j in jobs), jobs
    alerts, seen = _alerts_and_seen(chrome, dashboard, tmp_path)
    assert seen == len(FETCH_ROUTES), "the page's poll() did not reach the four finished jobs"
    assert alerts == ["hook-selftest"], alerts
    assert all(j.message is None and j.notes for j in jobs), jobs        # the outcome is kept, in ``notes``
