"""#87 evidence script: reads a store before/after the type filter and writes nothing to it."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from RevenueChaser.core import rawstore

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "87_r4_type_filter_evidence.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(),
                                reason=f"{_SCRIPT.name} is not in this tree")


@pytest.fixture(scope="module")
def evidence():
    # The file name starts with a digit, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location("evidence_87", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed():
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": cap, "name": t + " Inc", "price": 10.0,
         "sector": "Information Technology", "quote": {"symbol": t}}
        for t, cap in (("AAA", 9e9), ("BBB", 8e9), ("FUND", 7e9), ("NOROW", 6e9),
                       ("TINY", 1e6))])
    rawstore.replace_all("security_master", [
        {"ticker": "AAA", "security_type": "common"}, {"ticker": "BBB", "security_type": "adr"},
        {"ticker": "FUND", "security_type": "cef"}, {"ticker": "TINY", "security_type": "common"}])
    rawstore.write_rows("chain_filings", [
        {"ticker": t, "cik": "1", "form": "10-K", "filed": "2026-02-01", "accession": "a" + t,
         "primary_doc": "http://x", "item_text": "x", "quality": "ok", "fetched_at": 1.0}
        for t in ("AAA", "FUND")])
    rawstore.write_rows("chain_mentions", [
        {"src": t, "dst_name": "Z", "dst_ticker": "ZZZ", "relation": "customer",
         "snippet": "s", "filed": "2026-02-01"} for t in ("AAA", "FUND")])


def test_main_returns_before_and_after_and_leaves_the_store_byte_identical(
        rawstore_tmp_db, evidence, tmp_path, capsys):
    _seed()
    raw, edgar = Path(rawstore_tmp_db), Path(rawstore_tmp_db).with_name("edgar.duckdb")
    assert raw.exists() and edgar.exists()
    sha = (_sha(raw), _sha(edgar))

    res = evidence.main(["--db", str(raw), "--out", str(tmp_path / "out"),
                         "--boards", "universe,value_chain,oipd"])

    assert (_sha(raw), _sha(edgar)) == sha                      # nothing was written
    assert res["boards"] == ["universe", "value_chain", "oipd"]
    b, a = res["before"], res["after"]
    assert b["universe"]["classification_universe"] == 4        # AAA BBB FUND NOROW
    assert a["universe"]["classification_universe"] == 3
    assert (b["universe"]["funds_etfs"], a["universe"]["funds_etfs"]) == (0, 1)
    assert a["universe"]["excluded_tickers_any_cap"] == 1
    assert (b["value_chain"]["filings"], a["value_chain"]["filings"]) == (2, 1)
    assert (b["value_chain"]["edges"], a["value_chain"]["edges"]) == (2, 1)
    assert (b["oipd"]["picker_pool"], a["oipd"]["picker_pool"]) == (4, 3)
    assert res["delta"]["universe"]["classification_universe"] == -1
    out = tmp_path / "out"
    assert "### universe" in (out / "evidence.md").read_text(encoding="utf-8")
    assert "| classification_universe | 4 | 3 | -1 |" in capsys.readouterr().out
    assert (out / "evidence.json").exists()
    assert rawstore.DB_PATH == raw                              # the guard restored the module


def test_the_guard_refuses_a_write_and_opens_read_only(rawstore_tmp_db, evidence):
    _seed()
    original = rawstore.connect
    with evidence.read_only_guard(rawstore_tmp_db):
        with pytest.raises(RuntimeError, match="evidence script is read-only"):
            rawstore.connect(read_only=False)
        with pytest.raises(RuntimeError, match="evidence script is read-only"):
            rawstore.connect()                                   # the default is a write too
        with pytest.raises(RuntimeError, match="evidence script is read-only"):
            rawstore.execute("DELETE FROM equity_universe")
        with pytest.raises(RuntimeError, match="evidence script is read-only"):
            rawstore.replace_all("equity_universe", [], allow_empty=True)
        # reads work ...
        assert len(rawstore.query("SELECT ticker FROM equity_universe")) == 5
        # ... and the DuckDB handle itself is read-only, behind the first check
        conn = rawstore._connect_with_retry(delays=(), path=Path(rawstore_tmp_db))
        try:
            with pytest.raises(Exception, match="(?i)read-only"):
                conn.execute("CREATE TABLE evidence_probe (a INTEGER)")
        finally:
            conn.close()
    assert rawstore.connect is original                          # restored


def test_a_write_attempt_inside_a_board_fails_loudly(rawstore_tmp_db, evidence, tmp_path, monkeypatch):
    _seed()

    def writer(real_types):
        rawstore.execute("DELETE FROM equity_universe")
        return {}, {}

    monkeypatch.setitem(evidence.BOARDS, "universe", writer)
    with pytest.raises(RuntimeError, match="evidence script is read-only"):
        evidence.main(["--db", str(rawstore_tmp_db), "--out", str(tmp_path / "o"),
                       "--boards", "universe"])
    assert len(rawstore.query("SELECT ticker FROM equity_universe")) == 5


def test_a_missing_store_is_an_error_not_an_empty_report(evidence, tmp_path):
    with pytest.raises(SystemExit):
        evidence.main(["--db", str(tmp_path / "nope.duckdb"), "--out", str(tmp_path / "o")])


def test_an_unknown_board_is_rejected(rawstore_tmp_db, evidence, tmp_path):
    with pytest.raises(SystemExit):
        evidence.main(["--db", str(rawstore_tmp_db), "--out", str(tmp_path / "o"),
                       "--boards", "nope"])
