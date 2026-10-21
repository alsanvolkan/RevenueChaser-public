"""Nasdaq Trader symbol directory adapter."""
from __future__ import annotations

import httpx

from RevenueChaser.core.identity.sources import nasdaq
from RevenueChaser.core.identity.tests.helpers import fixture_text


def route(http):
    http.add("nasdaqlisted.txt", fixture_text("nasdaqlisted.txt"))
    http.add("otherlisted.txt", fixture_text("otherlisted.txt"))


def test_happy_path_flags_etfs_and_normalises_tickers(http):
    route(http)
    r = nasdaq.symbol_directory()
    assert r.source == "nasdaq" and r.status == "ok"
    by = {x["ticker"]: x for x in r.rows}
    assert by["AAAP"]["is_etf"] is True and by["AAA"]["is_etf"] is True
    assert by["AAPL"]["is_etf"] is False and by["PDI"]["is_etf"] is False
    assert by["AAPL"]["exchange"] == "Q" and by["PDI"]["exchange"] == "N"
    assert by["AAPL"]["security_name"] == "Apple Inc. - Common Stock"
    assert "BRK-B" in by and "BRK.B" not in by
    assert by["ARCC"]["security_name"].endswith("Closed End Fund")


def test_test_issues_and_the_footer_line_are_dropped(http):
    route(http)
    r = nasdaq.symbol_directory()
    tickers = {x["ticker"] for x in r.rows}
    assert "ZZZTX" not in tickers
    assert not any(t.startswith("FILE CREATION") for t in tickers)
    assert len(r.rows) == len(tickers)            # no duplicates


def test_both_files_are_requested_from_the_default_host(http):
    route(http)
    nasdaq.symbol_directory()
    assert http.urls() == ["https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
                           "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"]


def test_falls_back_to_the_ftp_host_when_www_fails(http):
    http.add("www.nasdaqtrader.com", {}, status=503)
    http.add("ftp.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", fixture_text("nasdaqlisted.txt"))
    http.add("ftp.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", fixture_text("otherlisted.txt"))
    r = nasdaq.symbol_directory()
    assert r.status == "ok" and any(x["ticker"] == "AAPL" for x in r.rows)


def test_5xx_everywhere_is_an_error(http):
    http.add("nasdaqtrader.com", {}, status=500)
    r = nasdaq.symbol_directory()
    assert r.status == "error" and r.rows == [] and "500" in r.error


def test_timeout_is_an_error(http):
    http.add("nasdaqtrader.com", httpx.ReadTimeout("slow"))
    r = nasdaq.symbol_directory()
    assert r.status == "error" and "ReadTimeout" in r.error


def test_a_body_that_is_not_the_directory_is_an_error(http):
    http.add("nasdaqtrader.com", "<html>maintenance</html>")
    assert nasdaq.symbol_directory().status == "error"


def test_one_file_missing_is_an_error_not_a_half_directory(http):
    http.add("nasdaqlisted.txt", fixture_text("nasdaqlisted.txt"))
    http.add("otherlisted.txt", {}, status=404)
    r = nasdaq.symbol_directory()
    assert r.status == "error"


def test_base_url_env_override_disables_the_fallback(http, monkeypatch):
    monkeypatch.setenv("RC_NASDAQTRADER_BASE", "http://127.0.0.1:9/SymDir")
    route(http)
    nasdaq.symbol_directory()
    assert http.urls() == ["http://127.0.0.1:9/SymDir/nasdaqlisted.txt",
                           "http://127.0.0.1:9/SymDir/otherlisted.txt"]
