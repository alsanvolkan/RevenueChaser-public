"""#383 R3 card C dry run: refuses the live store, opens no socket, writes nothing, and reports the board's own funnel.

Offline: the scenario of ``core/tests/screen_parity.py`` in a temp store.
"""
from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import funnel, rawstore
from RevenueChaser.core.tests import screen_parity as scn
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import pipeline as sp

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "383_c12_dryrun.py"


@pytest.fixture(scope="module")
def dry():
    # The file name starts with a digit, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location("c12_dryrun_383", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scenario(rawstore_tmp_db, monkeypatch):
    scn.seed()
    scn.patch(monkeypatch)


def _copy_dir(tmp_path: Path, name: str = "store_copy", files=("raw.duckdb", "edgar.duckdb", "prices.duckdb")):
    d = tmp_path / name
    d.mkdir(parents=True)
    for f in files:
        (d / f).write_bytes(b"")
    return d


# ── the store it runs on ──────────────────────────────────────────────────────

def test_a_store_folder_or_a_folder_inside_one_is_refused(dry, tmp_path):
    assert "live store" in dry.refusal(_copy_dir(tmp_path, ".store"))
    assert "live store" in dry.refusal(_copy_dir(tmp_path / ".store", "copy"))


def test_a_copy_without_the_three_files_is_refused(dry, tmp_path):
    why = dry.refusal(_copy_dir(tmp_path, files=("raw.duckdb",)))
    assert "edgar.duckdb" in why and "prices.duckdb" in why


def test_a_copy_with_the_three_files_is_accepted(dry, tmp_path):
    assert dry.refusal(_copy_dir(tmp_path)) is None


def test_main_refuses_when_the_store_in_force_is_not_the_copy(dry, tmp_path, rawstore_tmp_db, capsys):
    assert dry.main(["--store", str(_copy_dir(tmp_path)), "--out", str(tmp_path / "out")]) == 2
    assert "not the copy" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


# ── no network, no writes ─────────────────────────────────────────────────────

def test_no_socket_opens_during_the_run(dry):
    before = socket.create_connection
    with dry.offline():
        with pytest.raises(dry.OfflineError):
            socket.create_connection(("example.com", 443))
    assert socket.create_connection is before


def test_no_store_write_lands_during_the_run(dry, rawstore_tmp_db):
    with dry.read_only_store():
        with pytest.raises(dry.StoreWriteError):
            rawstore.write_rows("swing_scores", [{"ticker": "AAA"}])
        with rawstore.connect() as conn, pytest.raises(duckdb.Error):
            conn.execute("INSERT INTO swing_scores (ticker) VALUES ('AAA')")
    assert rawstore.query("SELECT ticker FROM swing_scores") == []
    rawstore.write_rows("swing_scores", [{"ticker": "AAA"}])       # restored on exit
    assert len(rawstore.query("SELECT ticker FROM swing_scores")) == 1


# ── what it reports ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("board,mod,table", [("swing", sp, "swing_scores"),
                                             ("fundamentals", fp, "fundamentals_scores")])
def test_the_dry_run_reports_the_boards_own_funnel_and_persists_nothing(dry, scenario, monkeypatch, board, mod,
                                                                         table):
    asked = []

    def ticker(t):                        # most scenario names have no stored info: a Fetch would ask Yahoo
        asked.append(t)
        raise AssertionError("the dry run scores from the stored info")
    monkeypatch.setattr("yfinance.Ticker", ticker)
    out = dry.run_board(board)
    assert asked == [] and rawstore.query(f"SELECT ticker FROM {table}") == []
    monkeypatch.setattr("yfinance.Ticker", scn._OfflineTicker)
    real = mod.run_scan()
    assert [(s["key"], s["count"]) for s in out["funnel"]] == [(s["key"], s["count"]) for s in real["funnel_steps"]]
    assert funnel.validate(real["funnel_steps"]) == []
    assert out["candidates"] == len(real["candidates"]) > 0 and out["data_as_of"] == "2026-10-14"
    assert out["page"]["total"] == out["page"]["html"] + out["page"]["payload_gzip"] > 0
    assert "candidates" in dry.summary([out])
