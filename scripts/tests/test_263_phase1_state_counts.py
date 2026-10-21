"""#263 Phase 1 state counts: a read-only, offline count of a copied store (synthetic stores only)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import rawstore

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "263_phase1_state_counts.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(),
                                reason=f"{_SCRIPT.name} is not in this tree")

REF = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()     # the provenance generated_at below
DAY = 86400.0


@pytest.fixture(scope="module")
def counts():
    # The file name starts with a digit, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location("counts_263", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the synthetic store ─────────────────────────────────────────────────────
def _create(con, name: str, drop: tuple = ()) -> None:
    cols = [c for c in rawstore.MANIFEST[name]["columns"] if c.split()[0] not in drop]
    con.execute(f"CREATE TABLE {name} ({', '.join(cols)})")


def _insert(con, name: str, rows: list[dict]) -> None:
    json_cols = {c.split()[0] for c in rawstore.MANIFEST[name]["columns"] if c.split()[1:2] == ["JSON"]}
    for r in rows:
        cols = list(r)
        vals = [json.dumps(r[c]) if c in json_cols and r[c] is not None else r[c] for c in cols]
        con.execute(f"INSERT INTO {name} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)


# extra Yahoo quote keys: identifier-like ones, two that are not (dividendRate, averageDailyVolume10Day), and
# null values (BBB's exchange and bid), which do not count as filled
QUOTE_EXTRA = {
    "AAA": {"symbol": "AAA", "exchange": "NMS", "quoteType": "EQUITY", "messageBoardId": "finmb_1",
            "bid": 49.9, "dividendRate": 1.0, "fullExchangeName": "NasdaqGS"},
    "BBB": {"symbol": "BBB", "exchange": None, "quoteType": "EQUITY", "bid": None},
    "BRK-B": {"symbol": "BRK-B", "exchange": "NYQ", "quoteType": "EQUITY", "messageBoardId": "finmb_2"},
    "FUND": {"quoteType": "ETF"},
}


def _quote(ok: bool, age_days: float | None, ticker: str = "") -> dict:
    q = {"regularMarketPrice": 50.0 if ok else 1.0, "averageDailyVolume10Day": 1e6,
         "fiftyTwoWeekHigh": 55.0, "fiftyDayAverage": 45.0}
    if age_days is not None:
        q["regularMarketTime"] = int(REF - age_days * DAY)
    q.update(QUOTE_EXTRA.get(ticker, {}))
    return q


# ticker, cap, quote passes the prefilter, quote age (days) -- in scan (insertion) order
UNIVERSE = [("TINY", 1e7, False, 0), ("NOCAP", None, True, None), ("SMALL", 6e7, True, 2),
            ("MID", 1.5e8, True, 3), ("AAA", 5e9, True, 1), ("FUND", 2e9, True, 40),
            ("BBB", 3e8, True, 5), ("BRK-B", 9e11, True, 20)]
# the universe's own columns (sector, industry, industry_group, sub_industry; the two lower levels are NULL since
# #265). BBB drifts from its master row (Basic Materials / Steel vs Industrials / Engineering & Construction) and
# still carries the old lower levels, which the next apply writes NULL; NOCAP (no master row) has a leftover sub_industry.
UNI_LEVELS = {"AAA": ("Technology", "Software - Application", None, None),
              "BBB": ("Basic Materials", "Steel", "Capital Goods", "Industrial Machinery"),
              "BRK-B": ("Financial Services", "Insurance - Diversified", None, None),
              "FUND": ("Unclassified", "Unclassified", None, None),
              "TINY": ("Unclassified", "Unclassified", None, None),
              "MID": ("Healthcare", "Unclassified", None, None),
              "SMALL": (None, None, None, None), "NOCAP": ("Other", None, None, "Leftover")}


def _m(t, cik, typ, tsrc, sec, ssrc, ind=None, isrc=None):
    return {"ticker": t, "cik": cik, "security_type": typ, "type_source": tsrc, "sector": sec,
            "sector_source": ssrc, "industry": ind, "industry_source": isrc}


MASTER = [
    _m("AAA", "1001", "common", "openfigi", "Technology", "provider_uw",
       "Software - Application", "provider_uw"),
    _m("BBB", "1002", "common", "openfigi", "Industrials", "provider_uw",
       "Engineering & Construction", "provider_uw"),
    _m("BRK-B", "1003", "common", "openfigi", "Financial Services", "provider_yf",
       "Insurance - Diversified", "provider_yf"),
    _m("FUND", None, "etp", "nasdaq_etf", *("Unclassified", "type_rule(etp: no GICS)") * 2),
    _m("TINY", "1004", "common", "openfigi", *("Unclassified", "none") * 2),
    _m("MID", None, None, None, "Healthcare", "provider_uw"),
    _m("SMALL", None, "unknown", "unknown", None, None),
    _m("GONE", "1005", "common", "openfigi", "Energy", "provider_uw"),      # not in the universe
]


def _c(source, ticker, status="ok", payload=None, fetched="2026-09-30T00:00:00Z", **cols):
    return {"source": source, "ticker": ticker, "status": status, "fetched_at": fetched,
            "payload": payload or {}, **cols}


CACHE = [
    _c("sp1500", "AAA", sector="Information Technology", payload={"index": "sp500"}),
    _c("sp1500", "BRK-B", sector="Financials", payload={"index": "sp500"}),
    _c("sp1500", "BBB", sector="Industrials", payload={"index": "sp400"}),
    _c("sp1500", "XOUT", sector="Energy", payload={"index": "sp500"}),
    _c("sp1500", "TINY", sector="Unmapped Spelling", payload={"index": "sp600"}),   # no Morningstar name: no reference
    _c("uw_sector", "AAA", sector="Technology", industry="Software - Application",
       security_type="Common Stock", payload={"market_cap": 5e9}),
    _c("uw_sector", "BBB", sector="Basic Materials", industry="Steel",
       security_type="Common Stock", payload={"market_cap": 3e8}),
    _c("uw_sector", "BRK.B", sector="Financial Services", industry="Insurance - Diversified",
       security_type="Common Stock", payload={"market_cap": 9e11}),
    _c("uw_sector", "MID", sector="Technology", industry="Software - Application",
       security_type="Common Stock", payload={"market_cap": 1.5e8}),
    _c("uw_sector", "UWONLY", sector="Healthcare", industry="Biotechnology",
       security_type="ETF", payload={"market_cap": 4e8}, fetched="2026-10-01T00:00:00Z"),
    _c("yahoo_sector", "AAA", sector="Technology"),
    _c("yahoo_sector", "BBB", sector="Industrials"),
    _c("yahoo_sector", "BRK-B", sector="Financial Services"),
    _c("yahoo_sector", "MID", sector="Healthcare"),
    _c("yahoo_sector", "TINY", sector="Technology"),
    _c("openfigi", "AAA", security_type="Common Stock", payload={"figi": "BBG000000001"}),
    _c("openfigi", "BBB", status="no_match", payload={"figi": None}),
    _c("nasdaq", "AAA", is_etf=False, payload={"exchange": "Q"}),
    _c("nasdaq", "FUND", is_etf=True, payload={"exchange": "P"}),
    _c("nasdaq", "NQONLY", is_etf=False, payload={"exchange": "N"}),
    _c("sec_exchange", "AAA", cik="1001", payload={"exchange": "Nasdaq"}),
    _c("sec_exchange", "BBB", cik="1002", payload={"exchange": "NYSE"}),
    _c("sec_exchange", "BRK-B", cik="1003", payload={"exchange": "NYSE"}),
    _c("sec_exchange", "OTCX", cik="1006", payload={"exchange": "OTC"}),
]


def build_store(path: Path, skip: tuple = ()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        tables = {
            "equity_universe": [
                {"ticker": t, "market_cap": cap, "sector": UNI_LEVELS[t][0], "industry": UNI_LEVELS[t][1],
                 "industry_group": UNI_LEVELS[t][2], "sub_industry": UNI_LEVELS[t][3], "price": 50.0,
                 "name": t + " Inc",
                 "quote": _quote(ok, age, t)} for t, cap, ok, age in UNIVERSE],
            "equity_universe_provenance": [
                {"key": "__meta__", "generated_at": "2026-10-01T00:00:00+00:00", "refreshed_by": "Swing Potential"},
                {"key": "__stats__", "generated_at": "2026-10-01T01:00:00+00:00", "refreshed_by": "Swing Potential"}],
            "security_master": MASTER,
            "identity_conflicts": [
                {"ticker": "AAA", "kind": "sector", "field": "sector", "side_a": "a", "side_b": "b"},
                {"ticker": "BBB", "kind": "type", "field": "security_type", "side_a": "a", "side_b": "b"}],
            "identity_source_cache": CACHE,
            "identity_runs": [
                {"run_id": "r1", "started_at": "2026-09-30T00:00:00Z", "status": "partial",
                 "sources": {"sec_exchange": {"status": "ok"}, "uw_sector": {"status": "error"}}},
                {"run_id": "r2", "started_at": "2026-10-01T00:00:00Z", "status": "ok",
                 "sources": {"sec_exchange": {"status": "ok"}, "uw_sector": {"status": "ok"}}}],
            "index_constituents": ([{"slug": "sp500", "ticker": t} for t in ("AAA", "BRK.B", "UWX")]
                                   + [{"slug": "nasdaq", "ticker": "AAA"}]),
            "sec_company_tickers": [{"ticker": "AAA", "cik": "1001"}, {"ticker": "BBB", "cik": "1002"},
                                    {"ticker": "brk-b", "cik": "1003"}],
            "cusip_ticker": [{"cusip": "000000001", "entry": "AAA"},
                             {"cusip": "000000002", "entry": {"ticker": "BRK.B", "name": "Berkshire"}},
                             {"cusip": "000000003", "entry": {"ticker": "zzz"}}],
            "sec_company_sic": [{"ticker": "AAA", "cik": "1001", "sic": "7372"},
                                {"ticker": "BBB", "cik": "0001002", "sic": "3560"},
                                {"ticker": "ZZZ", "cik": "9999", "sic": "1000"}],
        }
        for name, rows in tables.items():
            if name in skip:
                continue
            _create(con, name)
            _insert(con, name, rows)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return path


@pytest.fixture
def store(tmp_path):
    return build_store(tmp_path / "backup" / "raw_20261001T000000Z.duckdb")


@pytest.fixture
def phase2_3(monkeypatch, counts):
    monkeypatch.setattr(counts, "PRE_C12_MAX_PHASE2", 3)     # the cap C12 removed is frozen in the script


def _run(counts, store, tmp_path):
    return counts.main(["--db", str(store), "--out", str(tmp_path / "out")])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── the numbers, section by section ─────────────────────────────────────────
def test_universe_bands_null_cap_and_provenance(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["universe"]
    assert s["rows"] == 8 and s["null_cap"] == 1
    # MC_CHUNKS, half-open [lo, hi): BBB at exactly $300M sits in $300M-$2B
    assert [n for _label, n in s["bands"]] == [1, 1, 1, 1, 2, 0, 1]
    assert s["outside_edges"] == 0
    assert {p["key"]: p["refreshed_by"] for p in s["provenance"]} == {
        "__meta__": "Swing Potential", "__stats__": "Swing Potential"}


def test_master_coverage(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["master"]
    assert (s["master_rows"], s["universe_and_master"]) == (8, 7)
    assert (s["universe_minus_master"], s["master_minus_universe"]) == (1, 1)
    assert s["master_minus_universe_by_type"] == {"common": 1}
    assert s["universe_minus_master_sample"] == ["NOCAP"]


def test_types_excluded_and_classification_universe(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["types"]
    by = s["by_type"]
    assert (by["common"]["all"], by["common"]["floor"]) == (4, 3)
    assert by["common"]["sources"] == {"openfigi": 4}
    assert (by["etp"]["all"], by["etp"]["floor"]) == (1, 1)
    assert (by["(NULL)"]["all"], by["(no master row)"]["all"], by["unknown"]["all"]) == (1, 1, 1)
    assert (s["excluded_all"], s["excluded_floor"]) == (1, 1)
    assert s["excluded_by_type"] == {"etp": 1}
    # COALESCE(cap, 0) >= $300M (BBB at exactly the floor stays), minus the excluded FUND
    assert s["classification_universe"] == 3
    assert s["floor_rows"] == 4


def test_sector_sources_nulls_unclassified_conflicts_and_drift(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["sectors"]
    assert s["sp1500_members"] == 4
    a = s["splits"]["all"]
    assert a["n"] == 8
    assert a["sector_source"] == {"provider_uw": 3, "provider_yf": 1, "type_rule(etp: no GICS)": 1,
                                  "none": 1, "(NULL)": 1, "(no master row)": 1}
    assert a["industry_source"] == {"provider_uw": 2, "provider_yf": 1, "type_rule(etp: no GICS)": 1,
                                    "none": 1, "(NULL)": 2, "(no master row)": 1}
    assert (a["null_sector"], a["null_industry"], a["unclassified"], a["lower_levels_filled"]) == (1, 2, 3, 2)
    assert "null_group" not in a and "null_sub" not in a
    f = s["splits"]["floor"]
    assert f["n"] == 4 and f["sector_source"] == {"provider_uw": 2, "provider_yf": 1, "type_rule(etp: no GICS)": 1}
    assert f["unclassified"] == 1
    assert s["splits"]["sp1500"]["sector_source"] == {"provider_uw": 2, "provider_yf": 1, "none": 1}
    assert s["splits"]["not_sp1500"]["n"] == 4
    assert s["conflicts_by_kind"] == {"sector": 1, "type": 1}
    d = s["drift"]
    assert d["rows"] == 1 and d["sample"] == ["BBB"]
    assert (d["sector"], d["industry"], d["industry_group"], d["sub_industry"], d["next_earnings_date"]) == (
        1, 1, 1, 1, 0)


def test_sector_agreement_uses_the_provider_mapping(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["agreement"]
    uw = s["uw_vs_sp1500"]
    assert (uw["n"], uw["agree"]) == (2, 1)                 # AAA agrees, BBB (Materials) does not
    assert uw["cap_pct"] == pytest.approx(100 * 5e9 / 5.3e9, abs=0.05)
    yf = s["yahoo_vs_sp1500"]
    assert (yf["n"], yf["agree"]) == (3, 3)                 # Morningstar names renamed to GICS
    out = s["uw_vs_yahoo_outside"]
    assert (out["n"], out["agree"]) == (1, 0)               # MID: Technology vs Healthcare


def test_ticker_spelling_and_missed_joins(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["spelling"]["tables"]
    assert s["equity_universe"]["rows"] == 8 and s["equity_universe"]["-"] == 1
    assert s["identity_source_cache:uw_sector"]["."] == 1
    assert s["identity_source_cache:uw_sector"]["missed"] == 1          # BRK.B -> BRK-B
    assert s["index_constituents:sp500"]["missed"] == 1
    assert s["sec_company_tickers"]["lower"] == 1 and s["sec_company_tickers"]["missed"] == 1
    assert s["cusip_ticker"]["rows"] == 3 and s["cusip_ticker"]["lower"] == 1
    assert s["cusip_ticker"]["missed"] == 1
    assert s["identity_source_cache:sp1500"]["missed"] == 0


def test_index_lists_compare_after_normalising(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["index"]
    assert s["per_slug"] == {"nasdaq": 1, "sp500": 3}
    sp = s["sp500"]
    assert (sp["uw"], sp["wiki"], sp["both"], sp["only_uw"], sp["only_wiki"]) == (3, 3, 2, 1, 1)
    assert (sp["only_uw_sample"], sp["only_wiki_sample"]) == (["UWX"], ["XOUT"])


def test_candidate_membership_lists(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["membership"]
    sec = s["sec_exchange"]
    assert (sec["rows"], sec["in_universe"], sec["not_in_universe"]) == (4, 3, 1)
    assert sec["not_in_universe_by"] == {"OTC": 1}
    assert (sec["universe_not_in_source"], sec["floor_universe_not_in_source"]) == (5, 1)
    nq = s["nasdaq"]
    assert (nq["rows"], nq["in_universe"], nq["not_in_universe"], nq["etf_rows_dropped"]) == (2, 1, 1, 1)
    uw = s["uw_sector"]
    assert (uw["rows"], uw["in_universe"], uw["not_in_universe"]) == (5, 4, 1)   # BRK.B joins once normalised
    assert uw["not_in_universe_by"] == {"ETF": 1}
    assert uw["universe_not_in_source"] == 4
    big = s["uw_sector_cap300"]
    assert (big["rows"], big["in_universe"], big["not_in_universe"]) == (4, 3, 1)


def test_keys(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["keys"]
    assert s["cik"] == {"all": [4, 8], "floor": [3, 4]}
    assert (s["figi"]["universe_with_row"], s["figi"]["with_figi"]) == (2, 1)
    c = s["cusip"]
    assert (c["entries"], c["in_universe_exact"], c["extra_after_norm"]) == (3, 1, 1)
    assert (s["sic"]["rows_for_master_ciks"], s["sic"]["universe_with_sic"]) == (2, 2)


def test_shortlist_is_the_first_max_phase2_in_scan_order(counts, store, tmp_path, phase2_3):
    s = _run(counts, store, tmp_path)["sections"]["shortlist"]
    assert (s["rows"], s["excluded_dropped"], s["prefiltered"], s["hygiene"]) == (8, 1, 6, 1)
    assert s["max_phase2"] == 3
    assert s["shortlist"] == ["NOCAP", "SMALL", "MID"]                   # scan order, not cap order
    assert s["top_by_cap"] == ["BRK-B", "AAA", "BBB"]
    assert s["in_top_by_cap"] == 0
    assert [n for _label, n in s["bands_shortlist"]] == [0, 1, 1, 0, 0, 0, 0]
    assert s["null_cap_shortlist"] == 1
    assert s["fundamentals_identical"] is True


def test_quote_age_bins(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["quote_age"]
    assert s["reference"] == "2026-10-01T00:00:00+00:00"
    assert s["bins"] == {"<=3d": 4, "3-10d": 1, "10-30d": 1, ">30d": 1, "missing": 1, "after_reference": 0}


def test_identity_runs_and_cache_status(counts, store, tmp_path):
    s = _run(counts, store, tmp_path)["sections"]["identity_runs"]
    assert [r["run_id"] for r in s["runs"]] == ["r2", "r1"]
    assert s["runs"][1]["by_status"] == {"error": ["uw_sector"], "ok": ["sec_exchange"]}
    assert s["cache"]["openfigi"]["by_status"] == {"no_match": 1, "ok": 1}
    assert s["cache"]["uw_sector"]["max_fetched_at"] == "2026-10-01T00:00:00Z"


def test_quote_fields_fill_sorted_and_identifier_keys(counts, store, tmp_path):
    res = _run(counts, store, tmp_path)
    s = res["sections"]["quote_fields"]
    assert (s["rows"], s["rows_with_quote"], s["distinct_keys"]) == (8, 8, 12)
    keys = [(k["key"], k["n"]) for k in s["keys"]]
    assert keys == [("averageDailyVolume10Day", 8), ("fiftyDayAverage", 8), ("fiftyTwoWeekHigh", 8),
                    ("regularMarketPrice", 8), ("regularMarketTime", 7), ("quoteType", 4), ("symbol", 3),
                    ("exchange", 2), ("messageBoardId", 2), ("bid", 1), ("dividendRate", 1),
                    ("fullExchangeName", 1)]                     # a null value is not filled (BBB)
    assert {k["key"]: k["pct"] for k in s["keys"]}["regularMarketTime"] == 87.5
    ids = {k["key"]: k for k in s["identifier_keys"]}
    # substring isin/cusip/figi/cik/sedol/uuid/symbol/exchange/market/quotetype, or a name ending in "id"
    assert set(ids) == {"regularMarketPrice", "regularMarketTime", "quoteType", "symbol", "exchange",
                        "fullExchangeName", "messageBoardId", "bid"}
    assert ids["symbol"]["examples"] == ["AAA", "BBB", "BRK-B"]              # three rows, scan order
    assert ids["quoteType"]["examples"] == ["EQUITY", "ETF"]                 # distinct values only
    assert ids["exchange"]["examples"] == ["NMS", "NYQ"] and ids["exchange"]["n"] == 2
    md = res["markdown"].split("## 13.")[1]
    assert "dividendRate 1 (12.5%)" in md and "finmb_1" in md
    assert "engine.py:468" in md
    rows = (tmp_path / "out" / "quote_fields.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 13 and rows[1].startswith("averageDailyVolume10Day,8,")


def test_quote_fields_markdown_shows_the_top_keys_when_the_list_is_long(counts, store, tmp_path, monkeypatch):
    monkeypatch.setattr(counts, "QUOTE_KEYS_FULL_MAX", 5)
    monkeypatch.setattr(counts, "QUOTE_KEYS_TOP", 3)
    md = _run(counts, store, tmp_path)["markdown"].split("## 13.")[1]
    assert "+9 more" in md and "quote_fields.csv" in md
    assert "dividendRate" not in md                                         # not top 3, not an identifier
    assert "messageBoardId" in md                                           # the identifier list stays whole


# ── output, safety and tolerance ────────────────────────────────────────────
def test_writes_markdown_json_and_csv_and_keeps_the_markdown_compact(counts, store, tmp_path, capsys):
    res = _run(counts, store, tmp_path)
    out = tmp_path / "out"
    md = (out / "counts.md").read_text(encoding="utf-8")
    assert md == res["markdown"] and md in capsys.readouterr().out
    assert json.loads((out / "counts.json").read_text(encoding="utf-8"))["sections"]["universe"]["rows"] == 8
    assert (out / "missed_joins.csv").read_text(encoding="utf-8").count("BRK") >= 3
    assert len(md.splitlines()) <= 170
    assert "scan order" in md and "inferred" in md
    for n in range(1, 14):
        assert f"## {n}." in md


def test_refuses_a_file_inside_a_store_directory(counts, tmp_path, capsys):
    db = build_store(tmp_path / ".store" / "raw_copy.duckdb")
    with pytest.raises(SystemExit) as exc:
        counts.main(["--db", str(db), "--out", str(tmp_path / "out")])
    assert exc.value.code != 0
    assert ".store" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_refuses_the_store_dir_set_before_the_override(counts, tmp_path, monkeypatch, capsys):
    live = tmp_path / "live_store"
    db = build_store(live / "raw_copy.duckdb")
    monkeypatch.setattr(counts, "PRIOR_STORE_DIR", str(live))
    with pytest.raises(SystemExit) as exc:
        counts.main(["--db", str(db), "--out", str(tmp_path / "out")])
    assert exc.value.code != 0
    assert "RC_STORE_DIR" in capsys.readouterr().err


def test_refuses_a_file_named_like_a_live_store_file(counts, tmp_path, capsys):
    db = build_store(tmp_path / "elsewhere" / "raw.duckdb")
    with pytest.raises(SystemExit) as exc:
        counts.main(["--db", str(db), "--out", str(tmp_path / "out")])
    assert exc.value.code != 0
    assert "copy" in capsys.readouterr().err


def test_the_store_is_opened_read_only_and_left_byte_identical(counts, store, tmp_path, monkeypatch):
    sha = _sha(store)
    real, seen = counts.open_readonly, {}

    def spy(path):
        con = real(path)
        with pytest.raises(duckdb.Error, match="read-only"):
            con.execute("CREATE TABLE written (i INTEGER)")
        seen["refused"] = True
        return con

    monkeypatch.setattr(counts, "open_readonly", spy)
    _run(counts, store, tmp_path)
    assert seen == {"refused": True}
    assert _sha(store) == sha


def test_a_socket_connect_raises_during_the_run(counts, store, tmp_path, monkeypatch):
    before = (socket.socket.connect, socket.create_connection, socket.getaddrinfo)

    def probe(ctx):
        socket.create_connection(("example.com", 80))

    monkeypatch.setattr(counts, "SECTIONS", counts.SECTIONS + [("net_probe", probe)])
    with pytest.raises(counts.OfflineError):
        _run(counts, store, tmp_path)
    assert (socket.socket.connect, socket.create_connection, socket.getaddrinfo) == before


def test_the_app_store_layer_is_not_used_during_the_run(counts, store, tmp_path, monkeypatch):
    def probe(ctx):
        rawstore.query("SELECT 1")

    monkeypatch.setattr(counts, "SECTIONS", counts.SECTIONS + [("rawstore_probe", probe)])
    res = _run(counts, store, tmp_path)
    assert "rawstore" in res["sections"]["rawstore_probe"]["error"]
    assert rawstore.query.__module__ == "RevenueChaser.core.rawstore"      # restored after the run


def test_a_missing_table_prints_table_missing_and_the_run_continues(counts, tmp_path):
    db = build_store(tmp_path / "b" / "raw_partial.duckdb",
                     skip=("security_master", "identity_source_cache", "cusip_ticker", "identity_runs"))
    res = counts.main(["--db", str(db), "--out", str(tmp_path / "out")])
    md = res["markdown"]
    assert "table missing: security_master" in md
    assert res["sections"]["master"]["missing"] == ["security_master"]
    assert res["sections"]["universe"]["rows"] == 8                      # other sections still ran
    assert res["sections"]["quote_age"]["bins"]["<=3d"] == 4
    assert "## 12." in md


def test_a_missing_column_is_reported_like_a_missing_table(counts, tmp_path):
    path = tmp_path / "c" / "raw_cols.duckdb"
    build_store(path, skip=("security_master",))
    con = duckdb.connect(str(path))
    _create(con, "security_master", drop=("type_source",))
    con.close()
    res = counts.main(["--db", str(path), "--out", str(tmp_path / "out")])
    assert "table missing: security_master.type_source" in res["markdown"]
    assert res["sections"]["master"]["master_rows"] == 0


def test_the_cli_runs_in_a_fresh_process_offline(store, tmp_path):
    out = tmp_path / "cli_out"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    proc = subprocess.run([sys.executable, str(_SCRIPT), "--db", str(store), "--out", str(out)],
                          capture_output=True, text=True, env=env, timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("# #263")
    assert json.loads((out / "counts.json").read_text(encoding="utf-8"))["sections"]["master"]["master_rows"] == 8
