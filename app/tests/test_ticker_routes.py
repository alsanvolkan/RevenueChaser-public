"""Every route that takes a ticker validates it (M8 / ADR-X1, #144).

An unvalidated ``/run/quick`` ticker became a file name outside ``.cache/``
(``{ticker}_report.html``), a ``ticker_payload_cache`` key and raw markup. The
shape is ``app/dashboard.py::_valid_ticker`` (``[A-Z0-9][A-Z0-9.\\-]{0,9}`` after
strip and upper-case), the same one the client combobox accepts.

Also the other route-level items of the same lane: ``/run/uw`` forwards only
schema params (M12), and the Flask ``.html.j2`` pages render autoescaped (F7).
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.core.tests.panel_client import panel_test_client

_BAD = [".", "..", "-", "---", "../../X", "..\\..\\X", "<x>", "X<IMG SRC=X ONERROR=ALERT(1)>", "A/B", "TOOLONGTICKER"]


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return panel_test_client(dashboard.app)


@pytest.fixture
def submitted(monkeypatch):
    seen = []
    monkeypatch.setattr(dashboard.jobs, "submit",
                        lambda module, params: seen.append((module, params)) or "stubjob")
    return seen


class TestRunQuick:
    @pytest.mark.parametrize("ticker", _BAD)
    def test_bad_ticker_is_400_and_no_job(self, client, submitted, ticker):
        r = client.post("/run/quick", data={"ticker": ticker})
        assert r.status_code == 400
        assert r.get_json()["ok"] is False
        assert submitted == []

    @pytest.mark.parametrize("ticker", ["brk.b", "AAC-UN", " wfc-pa ", "BESS-WT"])
    def test_real_symbols_still_run(self, client, submitted, ticker):
        r = client.post("/run/quick", data={"ticker": ticker})
        assert r.status_code == 202
        assert submitted == [("quick", {"ticker": ticker.strip().upper()})]


class TestFinancialsRefresh:
    @pytest.mark.parametrize("ticker", _BAD)
    def test_bad_ticker_is_400_and_nothing_renders(self, client, monkeypatch, ticker):
        calls = []
        monkeypatch.setattr(dashboard.runners, "run_quick_cached",
                            lambda ticker=None: calls.append(ticker))
        r = client.get("/view/module_financials", query_string={"refresh": "1", "ticker": ticker})
        assert r.status_code == 400
        assert calls == []


class TestWatchlistToggle:
    @pytest.mark.parametrize("ticker", ["<x>", "X<IMG SRC=X ONERROR=ALERT(1)>", "TOOLONGTICKER", "A B", "..", "-"])
    def test_bad_ticker_is_400_and_the_watchlist_is_untouched(self, client, rawstore_tmp_db, ticker):
        from urllib.parse import quote

        from RevenueChaser.core import rawstore
        r = client.post("/api/watchlist/toggle/" + quote(ticker, safe=""))
        assert r.status_code == 400
        assert r.get_json()["ok"] is False
        assert rawstore.load_watchlist() == set()


class TestSettingsDrill:
    @pytest.mark.parametrize("ticker", ["<x>", "X<IMG SRC=X ONERROR=ALERT(1)>", "TOOLONGTICKER"])
    def test_bad_ticker_is_400(self, client, monkeypatch, ticker):
        from urllib.parse import quote

        from RevenueChaser.module_settings import inventory
        calls = []
        monkeypatch.setattr(inventory, "ticker_detail", lambda t: calls.append(t))
        r = client.get("/api/settings/ticker/" + quote(ticker, safe=""))
        assert r.status_code == 400
        assert r.get_json() == {"ok": False, "error": "bad ticker"}
        assert calls == []

    def test_a_failed_store_read_is_a_500_not_an_unknown_ticker(self, client, monkeypatch):
        """DF98: a read error must not look like the 404 an unknown ticker gets."""
        from RevenueChaser.module_settings import inventory

        def boom(t):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(inventory, "ticker_detail", boom)
        r = client.get("/api/settings/ticker/AAPL")
        assert r.status_code == 500
        assert r.get_json()["error"] == "store read failed"

    def test_an_unknown_ticker_is_still_a_404(self, client, monkeypatch):
        from RevenueChaser.module_settings import inventory
        monkeypatch.setattr(inventory, "ticker_detail", lambda t: None)
        assert client.get("/api/settings/ticker/ZZZZ").status_code == 404


class _OkUW:
    def verify(self):
        return True, ""

    def close(self):
        pass


class TestRunUw:
    """M12: ``/run/uw`` forwards only the tool's schema params to ``runner(uw, **params)``."""

    def test_unknown_form_keys_are_dropped(self, client, submitted, monkeypatch):
        monkeypatch.setattr(dashboard, "_uw", lambda: _OkUW())
        r = client.post("/run/uw", data={"tool": "flow_alerts", "limit": "10",
                                         "uw": "x", "evil": "1"})
        assert r.status_code == 202
        assert submitted == [("uw", {"tool": "flow_alerts", "limit": "10"})]

    def test_a_form_key_named_uw_still_runs_the_job(self, client, submitted, monkeypatch):
        """The job calls ``runner(uw, **params)``: a forwarded ``uw`` key was a TypeError."""
        from RevenueChaser.app import runners
        from RevenueChaser.module_uw_interaction import engine
        monkeypatch.setattr(dashboard, "_uw", lambda: _OkUW())
        client.post("/run/uw", data={"tool": "flow_alerts", "uw": "x"})
        (_, params), = submitted
        seen = {}
        monkeypatch.setattr(engine, "run_tool",
                            lambda uw, tool, p: seen.update(tool=tool, p=p) or (_ for _ in ()).throw(
                                engine.ToolError("stop")))
        with pytest.raises(runners.JobMessage, match="stop"):
            runners.run_uw_interaction(object(), **params)
        assert seen == {"tool": "flow_alerts", "p": {}}


class TestFlaskJ2Templates:
    """F7 trap 2: Flask autoescapes only .html/.htm/.xml/.xhtml/.svg, so the
    ``.html.j2`` pages rendered raw through ``render_template``."""

    def test_store_locked_page_escapes_its_fields(self, client, monkeypatch):
        from RevenueChaser.core.rawstore import StoreLockedError

        def _locked():
            raise StoreLockedError("/x/<i>raw.duckdb", 4242, "<b>duckdb</b>", "/bin/<u>x",
                                   "<b>detail</b>")
        monkeypatch.setattr(dashboard, "_view_uw_interaction", _locked)
        r = client.get("/view/module_uw_interaction")
        assert r.status_code == 503
        body = r.get_data(as_text=True)
        assert "&lt;b&gt;detail&lt;/b&gt;" in body
        assert "<b>" not in body and "<i>" not in body and "<u>" not in body

    def test_empty_view_escapes_the_label(self):
        with dashboard.app.test_request_context("/"):
            body = dashboard._empty_view("x<b>y")
        assert "X&lt;B&gt;Y" in body
        assert "<B>" not in body
