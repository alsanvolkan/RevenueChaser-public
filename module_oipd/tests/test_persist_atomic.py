"""``engine._persist`` writes ``oipd_chain`` and ``oipd_meta`` in ONE transaction. Offline; the store is a temp DuckDB."""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_oipd import engine


def _contract(sym: str, strike: float) -> dict:
    return {"option_symbol": sym, "expiry": "2026-12-18", "option_type": "call", "strike": strike,
            "bid": 1.0, "ask": 1.2, "last_price": 1.1, "volume": 10.0, "open_interest": 20.0,
            "iv": 0.3, "delta": 0.5, "tape_time": None}


def _meta(fetched: str) -> dict:
    return {"ticker": "TST", "as_of": "2026-10-23", "spot": 100.0, "risk_free": 0.04,
            "expiry_count": 1, "horizon_days": 30, "fetched_at": fetched}


def _syms() -> list[str]:
    return sorted(r["option_symbol"] for r in rawstore.query("SELECT option_symbol FROM oipd_chain"))


def _fail_meta(monkeypatch):
    """Make the ``oipd_meta`` write raise, whichever store API ``_persist`` uses."""
    orig_store, orig_tx = rawstore.write_rows, rawstore._Tx.write_rows

    def boom(name):
        raise RuntimeError("meta write died")

    def store_write(name, rows):
        return boom(name) if name == "oipd_meta" else orig_store(name, rows)

    def tx_write(self, name, rows):
        return boom(name) if name == "oipd_meta" else orig_tx(self, name, rows)

    monkeypatch.setattr(rawstore, "write_rows", store_write)
    monkeypatch.setattr(rawstore._Tx, "write_rows", tx_write)


def test_success_writes_both(rawstore_tmp_db):
    engine._persist("TST", [_contract("A", 90), _contract("B", 100)], _meta("t1"))
    assert _syms() == ["A", "B"]
    assert rawstore.get("oipd_meta", "TST")["fetched_at"] == "t1"


def test_meta_failure_rolls_back_the_chain(rawstore_tmp_db, monkeypatch):
    engine._persist("TST", [_contract("OLD", 90)], _meta("t0"))
    _fail_meta(monkeypatch)
    with pytest.raises(RuntimeError):
        engine._persist("TST", [_contract("NEW", 95)], _meta("t1"))
    assert _syms() == ["OLD"]
    assert rawstore.get("oipd_meta", "TST")["fetched_at"] == "t0"


def test_rerun_after_failure_writes_both_once(rawstore_tmp_db, monkeypatch):
    with monkeypatch.context() as m:
        _fail_meta(m)
        with pytest.raises(RuntimeError):
            engine._persist("TST", [_contract("A", 90), _contract("B", 100)], _meta("t1"))
    assert _syms() == []
    engine._persist("TST", [_contract("A", 90), _contract("B", 100)], _meta("t1"))
    assert _syms() == ["A", "B"]
    assert rawstore.count("oipd_meta") == 1
