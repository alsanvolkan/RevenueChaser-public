"""Every board must report the date its data is computed against.

Regression: the Swing board read "Last run: 2026-09-02 09:51:27" over bars that
stopped at 2026-08-31. ``generated_at`` is only when the scan executed — and the
dashboard's self-healing view re-runs a module with ``no_fetch=True``, which
touches no network, so a cached re-render stamps "now" over week-old data.

The label must therefore come from the DATA, not the run, and it must exist for
every tab rather than the ones someone remembered to wire up.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import provenance, rawstore


class TestResolvers:
    def test_swing_and_fundamentals_report_the_newest_bar(self, rawstore_tmp_db):
        """Their own names' newest D01 bar since K7 (#383 R3; ``test_provenance_k7_383.py``)."""
        from RevenueChaser.core.tests.d01_seed import seed_d01
        assert provenance.data_as_of("module_swing_potential") is None
        seed_d01("AAPL", ["2026-08-28", "2026-08-31"], 1.0)
        seed_d01("MSFT", ["2026-08-27", "2026-08-28"], 1.0)
        rawstore.replace_all("swing_scores", [{"ticker": "AAPL"}, {"ticker": "MSFT"}])
        rawstore.replace_all("fundamentals_scores", [{"ticker": "MSFT"}])
        assert provenance.data_as_of("module_swing_potential") == "2026-08-31"
        assert provenance.data_as_of("module_fundamentals") == "2026-08-28"

    def test_discord_reports_the_newest_stored_message_day(self, rawstore_tmp_db):
        from RevenueChaser.module_discord import loader, store
        from RevenueChaser.module_discord.tests.conftest import FIXTURE
        assert provenance.data_as_of("module_discord") is None
        store.save_export(loader.load_export_file(FIXTURE))
        newest = max(m["ts"] for m in store.messages("900000000000000010"))
        assert provenance.data_as_of("module_discord") == newest[:10]

    def test_earnings_reports_the_session_of_its_stored_calendar(self, rawstore_tmp_db):
        """DF13: the Earnings board reads ``earnings_calendar`` (the newest stored session), never the per-ticker
        payload cache, so a newer cache fetch says nothing about its data. Asymmetric Growth's date is D01's since
        #380 S4 (``test_provenance_k7_383.py``)."""
        rawstore.write_rows("ticker_payload_cache", [{
            "ticker": "AAPL", "payload": {}, "fetched_at": 1788223354.41, "fetched_at_iso": ""}])   # 2026-09-01
        assert provenance.data_as_of("module_earnings_radar") is None
        rawstore.write_rows("earnings_calendar", [
            {"session_date": "2026-08-27", "earnings_date": "2026-09-03", "ticker": "AAPL", "report_session": "AMC"},
            {"session_date": "2026-08-28", "earnings_date": "2026-09-03", "ticker": "AAPL", "report_session": "AMC"},
        ])
        assert provenance.data_as_of("module_earnings_radar") == "2026-08-28"

    def test_rotation_uses_the_newest_snapshot_date(self, rawstore_tmp_db):
        rawstore.write_rows("rotation_snapshots", [
            {"as_of_date": "2026-09-01", "ticker": "XLK", "group": "sector"},
            {"as_of_date": "2026-08-14", "ticker": "XLF", "group": "sector"},
        ])
        assert provenance.data_as_of("module_sector_rotation") == "2026-09-01"

    def test_sec13f_reports_the_newest_period_in_the_totals(self, rawstore_tmp_db):
        """The board renders ``sec13f_totals``, so that is the date it is "as of" — not a newer
        period that was ingested but not derived yet (#206)."""
        rawstore.write_rows("sec13f_totals", [
            {"ticker": "AAPL", "entry": {"2025-12-31": {"shares": 1.0}, "2026-03-31": {"shares": 2.0}}},
            {"ticker": "MSFT", "entry": {"2025-12-31": {"shares": 1.0}}},
        ])
        rawstore.write_rows("sec13f_submissions", [
            {"accession": "a1", "cik": "1", "period": "2026-06-30",
             "filing_date": "01-AUG-2026", "type": "13F-HR"}])
        assert provenance.data_as_of("module_sec_13f") == "2026-03-31"

    def test_sec13f_with_no_totals_has_no_data_date(self, rawstore_tmp_db):
        rawstore.write_rows("sec13f_submissions", [
            {"accession": "a1", "cik": "1", "period": "2026-06-30",
             "filing_date": "01-AUG-2026", "type": "13F-HR"}])
        assert provenance.data_as_of("module_sec_13f") is None

    def test_leaps_prefers_prints_and_falls_back_to_the_cache(self, rawstore_tmp_db):
        rawstore.write_rows("leaps_cache", [
            {"ticker": "NOK", "payload": {"latest": {"as_of": "2026-08-27"}, "history": []}}])
        assert provenance.data_as_of("module_leaps") == "2026-08-27"
        rawstore.write_rows("leaps_flow_prints", [
            {"id": "p1", "ticker": "AAPL", "discovered_at": "2026-09-02"}])
        assert provenance.data_as_of("module_leaps") == "2026-09-02"

    def test_financials_reports_the_last_tickers_fetch_day(self, rawstore_tmp_db, tmp_path,
                                                           monkeypatch):
        """Financials now recomputes from .store, so its data date = when the raw
        yfinance payload behind the rendered ticker was fetched."""
        from RevenueChaser.core import config
        monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
        rawstore.write_rows("ticker_payload_cache", [{
            "ticker": "AAPL", "payload": {}, "fetched_at": 1788223354.41,
            "fetched_at_iso": "2026-09-01T00:00:00+00:00"}])
        # no state file yet -> nothing rendered -> no data date
        assert provenance.data_as_of("module_financials") is None
        state = tmp_path / "financials" / "financials_latest.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"ticker": "AAPL"}), encoding="utf-8")
        assert provenance.data_as_of("module_financials") == "2026-09-01"

    def test_an_unknown_module_reports_nothing(self, rawstore_tmp_db):
        assert provenance.data_as_of("module_does_not_exist") is None

    def test_a_broken_resolver_never_breaks_the_board(self, rawstore_tmp_db, monkeypatch):
        monkeypatch.setitem(provenance.RESOLVERS, "module_swing_potential",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert provenance.data_as_of("module_swing_potential") is None


class TestTheLabelIsAboutDataNotTheRun:
    def test_data_date_is_independent_of_when_the_scan_ran(self, rawstore_tmp_db):
        """The exact bug: a scan run today over bars from days ago."""
        from RevenueChaser.core.tests.d01_seed import seed_d01
        seed_d01("AAPL", ["2026-08-28", "2026-08-31"], 1.0)
        rawstore.replace_all("swing_scores", [{"ticker": "AAPL"}])
        from RevenueChaser.module_swing_potential.pipeline import build_payload
        p = build_payload([], run_stats={"data_as_of": provenance.data_as_of("module_swing_potential")})
        assert p["data_as_of"] == "2026-08-31"
        assert p["generated_at"][:10] != p["data_as_of"], (
            "generated_at is the run time; it must not be mistaken for the data date")

    def test_no_internal_jargon_reaches_the_payload(self, rawstore_tmp_db):
        from RevenueChaser.module_swing_potential.pipeline import build_payload
        p = build_payload([], run_stats={})
        assert "model_only" not in p, "no_fetch is an implementation detail, not a UI field"


class TestApi:
    def _client(self):
        from RevenueChaser.app import dashboard
        return dashboard.app.test_client()

    def test_endpoint_returns_the_data_date(self, rawstore_tmp_db):
        from RevenueChaser.core.tests.d01_seed import seed_d01
        seed_d01("AAPL", ["2026-08-28", "2026-08-31"], 1.0)
        rawstore.replace_all("swing_scores", [{"ticker": "AAPL"}])
        d = self._client().get("/api/data_asof/module_swing_potential").get_json()
        assert d == {"module": "module_swing_potential", "data_as_of": "2026-08-31"}

    def test_endpoint_is_lenient_for_an_empty_store(self, rawstore_tmp_db):
        d = self._client().get("/api/data_asof/module_sec_13f").get_json()
        assert d["data_as_of"] is None

    def test_endpoint_never_500s_on_a_bogus_module(self, rawstore_tmp_db):
        r = self._client().get("/api/data_asof/../../etc/passwd")
        assert r.status_code in (200, 404)


def test_every_dashboard_tab_declares_a_data_date():
    """Gate: a new board must say what its data date is — even if the answer is
    'none'. Otherwise it silently shows a blank while looking authoritative."""
    from RevenueChaser.app import dashboard
    tabs = {t["key"] for t in dashboard.TABS if t.get("type") == "tab"}
    undeclared = sorted(tabs - set(provenance.RESOLVERS))
    assert not undeclared, (
        f"tabs missing from core/provenance.RESOLVERS: {undeclared} — add a resolver, "
        f"or map to None if the board holds no downloaded data")


def test_the_template_asks_for_the_data_date_on_every_tab():
    """The label is wired once in showTab, not per board."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent.parent / "templates" / "dashboard.html"
            ).read_text(encoding="utf-8")
    assert 'id="data-label"' in html
    assert "checkDataAsOf(key);" in html, "showTab must request it for whichever tab opened"
    assert "model-only" not in html, "internal jargon must not reach the UI"


def test_template_edits_do_not_need_a_server_restart():
    """debug=False makes Jinja cache the compiled template for the process
    lifetime, so a dashboard.html fix silently does not appear until restart —
    which is exactly why the first 'Last run' fix looked like it had not landed."""
    from RevenueChaser.app.dashboard import app
    assert app.jinja_env.auto_reload is True


class TestSessionOf:
    """#283: one helper maps mixed time columns (epoch, ISO text, date) to a session date."""

    def test_maps_each_column_type_to_the_ny_session(self):
        from datetime import datetime, timezone
        sat = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc).timestamp()
        assert provenance.session_of(sat) == "2026-07-17"
        assert provenance.session_of("2026-07-18T11:00:00-04:00") == "2026-07-17"
        assert provenance.session_of("2026-07-15") == "2026-07-15"

    def test_never_raises(self):
        assert provenance.session_of(None) is None
        assert provenance.session_of("garbage") is None
        assert provenance.session_of(1e18) is None
