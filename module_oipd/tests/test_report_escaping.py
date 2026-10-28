"""Implied Probability board: untrusted strings never reach markup or script raw (M1)."""
from __future__ import annotations

import re

from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.module_oipd import engine, report

_ATTR = '"><img src=x onerror=alert(1)>'
_SCRIPT = "</script><script>alert(1)</script>"
_EVIL = _ATTR + _SCRIPT


def _evil_payload() -> dict:
    p = engine._empty_payload(_EVIL, "No cached chain for " + _EVIL)
    p.update(error=None, asof=_EVIL, n_contracts=_EVIL, horizon_days=_EVIL,
             risk_free=_EVIL, source=_EVIL, warnings=[_EVIL, "second " + _SCRIPT],
             expiries=[{"date": _EVIL, "dte": _EVIL}])
    return p


def _assert_inert(html: str) -> None:
    # The page's own scripts: Plotly CDN, the shared filter kit (#383 C1b), the watch-list pair (PR 4) and the
    # board's inline block.
    assert len(re.findall(r"<script", html, re.I)) == 4
    assert len(re.findall(r"</script", html, re.I)) == 4
    assert "<img" not in html.lower()


class TestReportEscaping:
    def test_payload_strings_are_escaped_everywhere(self):
        _assert_inert(report.render_oipd_dashboard(_evil_payload()))

    def test_empty_state_with_evil_ticker_is_inert(self):
        """The GET-link case: ``?refresh=1&ticker=<payload>`` with no cached chain."""
        html = report.render_oipd_dashboard(
            engine._empty_payload(_EVIL, f"No cached chain for {_EVIL}. Run a Fetch first."))
        _assert_inert(html)

    def test_placeholder_names_in_data_are_not_substituted(self):
        """A ticker spelling another placeholder must stay a string, not splice JSON."""
        p = engine._empty_payload("__PAYLOAD__", "x")
        html = report.render_oipd_dashboard(p)
        assert 'var DEFAULT_TICKER = "__PAYLOAD__";' in html

    def test_script_side_strings_go_through_rcesc(self):
        html = report.render_oipd_dashboard(_evil_payload())
        assert "function rcEsc(" in html and "function esc(" not in html
        assert "rcEsc(PAYLOAD.ticker)" in html
        assert "PAYLOAD.warnings.map(rcEsc)" in html
        assert "rcEsc(e.date" in html


class TestTickerRoutes:
    def _client(self):
        from RevenueChaser.app import dashboard
        dashboard.app.config["TESTING"] = True
        return panel_test_client(dashboard.app)

    def test_refresh_rejects_a_bad_ticker(self):
        r = self._client().get("/view/module_oipd", query_string={"refresh": "1", "ticker": _EVIL})
        assert r.status_code == 400
        assert b"<img" not in r.data and b"<script" not in r.data

    def test_run_rejects_a_bad_ticker(self):
        r = self._client().post("/run/module_oipd", data={"ticker": _EVIL})
        assert r.status_code == 400

    def test_refresh_accepts_a_normal_ticker(self, monkeypatch):
        from RevenueChaser.app import runners
        seen = {}
        monkeypatch.setattr(runners, "run_oipd_cached",
                            lambda ticker=None: seen.setdefault("t", ticker) and None)
        r = self._client().get("/view/module_oipd", query_string={"refresh": "1", "ticker": "brk.b"})
        assert r.status_code == 200
        assert seen["t"] == "BRK.B"
