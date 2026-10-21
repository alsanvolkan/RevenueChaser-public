"""Earnings Radar v1 board, end to end: Fetch → history → restart → Refresh (#215).

The Fetch reads its inputs live (calendar, per-ticker UW reads) and stores nothing of them; its store
footprint is one ``result_runs`` header plus ``earnings_cards_history`` rows in ``history.duckdb`` (#278).
The mock tape covers the calendar and the common UW reads but not a Rotation map, so the run lands
``partial`` with that named cause. A fixture gap and a real breakage differ in the header, which is why the
tests pin it rather than only "the job finished".

Earnings is not store-refreshable (``_STORE_REFRESHABLE``): Refresh serves the written board and, with the
board file gone, answers the fetch-only empty state. Both paths make no network call.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest

DAY = "2026-08-12"           # an explicit Wednesday: no test reads today's date (AGENTS.md)
CALENDAR = {"data": [{
    "symbol": "NVDA", "sector": "Technology", "full_name": "NVIDIA Corp", "marketcap": "3000000000000",
    "expected_move_perc": "0.06", "street_mean_est": "1.2", "report_time": "premarket"}]}
ROTATION_CAUSE = "rotation quadrant map failed or came back empty"


@pytest.fixture
def calendar(provider_routes):
    provider_routes.route("/api/earnings/premarket", payload=CALENDAR)
    return provider_routes


@pytest.fixture
def fetched(dashboard, calendar):
    result = dashboard.run_job("/run/earnings", start=DAY, end=DAY)
    assert result.ok, f"Earnings fetch failed: {result}\n{dashboard.logs()}"
    dashboard.wait_until_idle()
    return result


def _history(dashboard, sql):
    conn = duckdb.connect(str(dashboard.store_dir / "history.duckdb"), read_only=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


class TestFetch:
    def test_fetch_records_one_run_header_with_a_named_cause(self, dashboard, fetched):
        rows = _history(dashboard, "SELECT board, quality, quality_note FROM result_runs")
        assert len(rows) == 1
        board, quality, note = rows[0]
        assert board == "module_earnings_radar"
        assert quality == "partial", f"mock tape has no Rotation map; expected a partial run, got {quality}: {note}"
        assert ROTATION_CAUSE in note

    def test_fetch_stores_the_scored_cards(self, dashboard, fetched):
        assert _history(dashboard, "SELECT ticker FROM earnings_cards_history") == [("NVDA",)]

    def test_fetch_writes_the_board_artifact(self, dashboard, fetched):
        assert dashboard.artifact("earnings_radar_latest.html").exists()

    def test_the_board_shows_the_fetched_name(self, dashboard, fetched):
        r = dashboard.view("module_earnings_radar")
        assert r.status_code == 200 and "NVDA" in r.text

    def test_the_fetch_actually_called_the_provider(self, provider_routes, fetched):
        assert provider_routes.requests("/api/earnings/premarket"), "fetch must read the calendar"
        assert provider_routes.requests("/api/stock/NVDA/"), "fetch must read the per-ticker UW data"

    def test_a_day_with_no_events_still_gets_a_header(self, dashboard, provider_routes):
        """README: every Fetch gets a header, also one with no events."""
        provider_routes.route("/api/earnings/", payload={"data": []})
        result = dashboard.run_job("/run/earnings", start=DAY, end=DAY)
        assert result.status == "done" and "No earnings events" in (result.message or "")
        dashboard.wait_until_idle()
        assert len(_history(dashboard, "SELECT 1 FROM result_runs")) == 1


class TestRefreshIsOffline:
    def test_refresh_serves_the_board_with_no_network_calls(self, dashboard, provider_routes, fetched):
        before = provider_routes.request_count
        r = dashboard.view("module_earnings_radar", refresh=True)
        assert r.status_code == 200 and "NVDA" in r.text
        assert provider_routes.request_count == before, (
            f"refresh reached the network: {[q.path for q in provider_routes.requests()[before:]]}")

    def test_refresh_without_the_board_file_is_the_fetch_only_empty_state(self, dashboard, provider_routes,
                                                                          fetched, clear_cache):
        """Not store-refreshable: no re-render from the store, a 200 empty state, still no network."""
        clear_cache()
        before = provider_routes.request_count
        r = dashboard.view("module_earnings_radar", refresh=True)
        assert r.status_code == 200 and "NVDA" not in r.text
        assert provider_routes.request_count == before

    def test_refresh_survives_a_restart(self, dashboard, fetched):
        dashboard.restart()
        assert "NVDA" in dashboard.view("module_earnings_radar", refresh=True).text

    def test_a_rejected_key_does_not_break_viewing_the_board(self, dashboard, fetched, dead_uw_token):
        """AGENTS.md: a board that needs no network must not require credentials."""
        dashboard.restart()
        r = dashboard.view("module_earnings_radar", refresh=True)
        assert r.status_code == 200 and "NVDA" in r.text


class TestEmptyStore:
    def test_a_board_with_no_data_renders_an_empty_state(self, dashboard):
        r = dashboard.view("module_earnings_radar")
        assert r.status_code == 200


_DRIVE = r"""<script>
window.addEventListener("load", function(){ setTimeout(function(){
  function vis(t){
    var row = null;
    document.querySelectorAll("tbody tr[data-ticker]").forEach(function(r){
      if (r.getAttribute("data-ticker") === t) row = r;
    });
    return row ? (row.style.display || "shown") : "missing";
  }
  var radio = document.querySelector('[role=radio][data-v="S&P 500"]');
  if (radio && radio.getAttribute("aria-disabled") !== "true") radio.click();
  document.documentElement.setAttribute("data-after-index", "NVDA=" + vis("NVDA") + ";AMD=" + vis("AMD"));
  var chip = document.querySelector('#chips [data-f="wl"]');
  if (chip) chip.click();
  document.documentElement.setAttribute("data-after-chip",
    "NVDA=" + vis("NVDA") + ";active=" + (chip && chip.classList.contains("active")));
}, 0); });
</script>"""


def _chrome() -> str | None:
    override = os.environ.get("RC_CHROME")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if Path(path).exists():
            return path
    return None


def _drive_filters(html: str, tmp_path: Path) -> str:
    chrome = _chrome()
    if chrome is None:
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail("no Chrome/Chromium for the Earnings filter kit")
        pytest.skip("no Chrome/Chromium for the Earnings filter kit")
    i = html.lower().rfind("</body>")
    page = tmp_path / "earnings_kit.html"
    page.write_text(html[:i] + _DRIVE + html[i:] if i >= 0 else html + _DRIVE, encoding="utf-8")
    out = subprocess.run(
        [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
         f"--user-data-dir={tmp_path / 'chrome-profile'}",
         # as in ``test_board_sec_13f``: a board's CDN plotly request would pause the virtual clock
         "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1",
         "--virtual-time-budget=6000", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert "<html" in out.stdout.lower(), f"Chrome dumped no DOM: {out.stderr[-1500:]}"
    assert "data-after-index" in out.stdout, out.stdout[-2000:]
    return out.stdout


def _event(symbol, name):
    return {"symbol": symbol, "sector": "Technology", "full_name": name, "marketcap": "3000000000000",
            "expected_move_perc": "0.06", "street_mean_est": "1.2", "report_time": "premarket"}


class TestFilterKit:
    """KE (#372): Index S&P 500 hides non-members; the board's chips still AND with the kit."""

    def test_picking_sp500_hides_non_members_and_the_watchlist_chip_still_ands(
            self, dashboard, provider_routes, tmp_path, monkeypatch):
        import RevenueChaser.core.rawstore as rawstore
        monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
        rawstore.write_rows("index_constituents", [{"slug": "sp500", "ticker": "NVDA"}])
        provider_routes.route("/api/earnings/premarket", payload={"data": [
            _event("NVDA", "NVIDIA Corp"), _event("AMD", "Advanced Micro Devices")]})
        result = dashboard.run_job("/run/earnings", start=DAY, end=DAY)
        assert result.ok, f"Earnings fetch failed: {result}\n{dashboard.logs()}"
        dashboard.wait_until_idle()
        html = dashboard.view("module_earnings_radar").text
        assert 'id="rc-filters"' in html and "RCFilters.mount(" in html
        for f in ("score6", "accel", "bull", "liq", "wl"):
            assert f'data-f="{f}"' in html
        assert 'data-ticker="NVDA"' in html and 'data-ticker="AMD"' in html
        dom = _drive_filters(html, tmp_path)
        after_index = dom.split("data-after-index", 1)[-1].split(">", 1)[0]
        assert "NVDA=shown" in after_index and "AMD=none" in after_index   # the S&P 500 member stays
        assert "NVDA=none" in dom.split("data-after-chip", 1)[-1].split(">", 1)[0]
        assert "active=true" in dom
