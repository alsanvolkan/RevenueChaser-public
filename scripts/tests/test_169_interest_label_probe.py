"""#169 P21 probe: reads a store COPY read-only, compares the two interest label lists, writes a CSV."""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import duckdb
import pandas as pd
import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "169_interest_label_probe.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(),
                                reason=f"{_SCRIPT.name} is not in this tree")

_Y1, _Y2 = pd.Timestamp("2022-12-31"), pd.Timestamp("2023-12-31")


def _load():
    # The file name starts with a digit, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location("probe_169", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def probe():
    return _load()


@pytest.fixture(autouse=True)
def _live_store_elsewhere(probe, monkeypatch, tmp_path):
    """The root conftest keeps the test store in ``tmp_path``, where the copies below live too.

    Point the probe's idea of "the live store" at an unrelated folder; the tests that are about
    the real answer of ``live_store_dirs`` load a fresh module (see below) or set their own.
    """
    monkeypatch.setattr(probe, "live_store_dirs", lambda: [tmp_path / "_live_" / ".store"])


def _record(year="2023", interest=55.0):
    return {"fiscal_year": year, "revenue": 1000.0, "ebit": 250.0, "ebitda": 300.0, "net_income": 100.0,
            "total_assets": 5000.0, "total_debt": 100.0, "cash": 10.0, "operating_cash_flow": 120.0,
            "capital_expenditure": 20.0, "depreciation": 50.0, "interest_expense": interest}


def _make_copy(path: Path, payloads: dict[str, dict]) -> Path:
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE ticker_payload_cache (ticker TEXT PRIMARY KEY, payload JSON, "
                 "fetched_at DOUBLE, fetched_at_iso TEXT)")
    for ticker, payload in payloads.items():
        conn.execute("INSERT INTO ticker_payload_cache VALUES (?, ?, 1.0, 'x')", [ticker, json.dumps(payload)])
    conn.close()
    return path


def _frame(rows: dict[str, dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).T


@pytest.fixture
def copy_path(tmp_path):
    return _make_copy(tmp_path / "raw_copy.duckdb", {
        "BANK": {"annual": [_record(interest=55.0)]},       # stored value = its net interest income
        "CORP": {"annual": [_record(interest=5.0)]},
    })


NII_ONLY = _frame({"Net Interest Income": {_Y1: 40.0, _Y2: 55.0}})
EXPENSE = _frame({"Interest Expense": {_Y1: 4.0, _Y2: 5.0}, "Net Interest Income": {_Y1: 40.0, _Y2: 55.0}})


def _fetch(frames):
    return lambda t: frames[t]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- the live store is refused --------------------------------------------------------

def _no_connect(monkeypatch, probe):
    def _boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("the store must not be opened")
    monkeypatch.setattr(probe.duckdb, "connect", _boom)


def test_refuses_a_path_inside_the_live_store(probe, monkeypatch, tmp_path):
    live = tmp_path / "live" / ".store"
    live.mkdir(parents=True)
    monkeypatch.setattr(probe, "live_store_dirs", lambda: [live])
    _no_connect(monkeypatch, probe)
    with pytest.raises(SystemExit, match="live store"):
        probe.run(live / "raw.duckdb", out_dir=tmp_path / "out")
    with pytest.raises(SystemExit, match="live store"):
        probe.run(live / "deeper" / "raw.duckdb", out_dir=tmp_path / "out")


def test_refuses_the_repo_dot_store_and_the_configured_store_dir(monkeypatch, tmp_path):
    real = _load()                                   # a fresh module: its own live_store_dirs
    _no_connect(monkeypatch, real)
    dirs = real.live_store_dirs()
    assert Path(real.config.BASE_DIR, ".store").resolve() in dirs
    assert Path(real.rawstore.DB_PATH).resolve().parent in dirs
    for live in dirs:
        with pytest.raises(SystemExit, match="live store"):
            real.run(live / "raw.duckdb", out_dir=tmp_path / "out")


def test_refuses_a_symlink_that_points_into_the_live_store(probe, monkeypatch, tmp_path):
    live = tmp_path / "live" / ".store"
    live.mkdir(parents=True)
    real = _make_copy(live / "raw.duckdb", {"X": {"annual": [_record()]}})
    link = tmp_path / "innocent_copy.duckdb"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks are not available here")
    monkeypatch.setattr(probe, "live_store_dirs", lambda: [live])
    _no_connect(monkeypatch, probe)
    with pytest.raises(SystemExit, match="live store"):
        probe.run(link, out_dir=tmp_path / "out")


def test_refuses_an_output_folder_inside_the_live_store(probe, monkeypatch, tmp_path, copy_path):
    live = tmp_path / "live" / ".store"
    live.mkdir(parents=True)
    monkeypatch.setattr(probe, "live_store_dirs", lambda: [live])
    with pytest.raises(SystemExit, match="live store"):
        probe.run(copy_path, fetch=_fetch({}), out_dir=live / "out")


def test_a_missing_file_and_a_foreign_database_are_clear_errors(probe, tmp_path):
    with pytest.raises(SystemExit, match="no such file"):
        probe.run(tmp_path / "nope.duckdb", out_dir=tmp_path / "out")
    other = tmp_path / "other.duckdb"
    duckdb.connect(str(other)).close()
    with pytest.raises(SystemExit, match="ticker_payload_cache"):
        probe.run(other, out_dir=tmp_path / "out")


# --- read-only ---------------------------------------------------------------------------

def test_the_copy_is_opened_read_only(probe, copy_path):
    conn = probe.open_copy(copy_path)
    try:
        with pytest.raises(duckdb.Error):
            conn.execute("CREATE TABLE intruder (a INTEGER)")
        with pytest.raises(duckdb.Error):
            conn.execute("DELETE FROM ticker_payload_cache")
    finally:
        conn.close()


def test_the_copys_bytes_are_unchanged_after_a_run(probe, copy_path, tmp_path):
    before = _sha(copy_path)
    probe.run(copy_path, fetch=_fetch({"BANK": NII_ONLY, "CORP": EXPENSE}), out_dir=tmp_path / "out")
    assert _sha(copy_path) == before
    assert not list(copy_path.parent.glob("*.wal"))


def test_the_store_auto_ensure_switch_is_off_before_any_app_import(monkeypatch):
    monkeypatch.delenv("RC_STORE_AUTO_ENSURE", raising=False)
    _load()
    assert os.environ["RC_STORE_AUTO_ENSURE"] == "0"


# --- what the probe reports -------------------------------------------------------------

def test_a_frame_with_only_net_interest_income_is_reported_as_changed(probe, copy_path, tmp_path):
    rows = {r["ticker"]: r for r in probe.run(copy_path, tickers=["BANK"], fetch=_fetch({"BANK": NII_ONLY}),
                                              out_dir=tmp_path / "out")}
    bank = rows["BANK"]
    assert bank["old_from_nii"] is True
    assert bank["old_label"] == "Net Interest Income" and bank["old_interest"] == 55.0
    assert bank["new_label"] is None and bank["new_interest"] is None
    assert bank["changed"] is True
    assert bank["grade_old"] == "healthy" and bank["grade_new"] == "unknown" and bank["grade_changed"] is True
    assert bank["coverage_old"] == pytest.approx(300.0 / 55.0) and bank["coverage_new"] is None


def test_a_filer_that_reports_interest_expense_is_unchanged(probe, copy_path, tmp_path):
    corp = probe.run(copy_path, tickers=["CORP"], fetch=_fetch({"CORP": EXPENSE}), out_dir=tmp_path / "out")[0]
    assert corp["old_from_nii"] is False
    assert corp["old_label"] == corp["new_label"] == "Interest Expense"
    assert corp["old_interest"] == corp["new_interest"] == 5.0
    assert corp["changed"] is False and corp["grade_changed"] is False


def test_the_ticker_list_defaults_to_the_stored_names_and_can_be_overridden(probe, copy_path, tmp_path):
    frames = {"BANK": NII_ONLY, "CORP": EXPENSE, "OTHER": EXPENSE}
    default = probe.run(copy_path, fetch=_fetch(frames), out_dir=tmp_path / "a")
    assert [r["ticker"] for r in default] == ["BANK", "CORP"]
    chosen = probe.run(copy_path, tickers=["corp"], fetch=_fetch(frames), out_dir=tmp_path / "b")
    assert [r["ticker"] for r in chosen] == ["CORP"]


def test_main_passes_the_tickers_override(probe, copy_path, tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "fetch_income", lambda t: EXPENSE)
    assert probe.main(["--store", str(copy_path), "--tickers", "CORP", "--out", str(tmp_path / "o"),
                       "--pause", "0"]) == 0
    with open(tmp_path / "o" / "interest_label_probe.csv", newline="", encoding="utf-8") as fh:
        assert [r["ticker"] for r in csv.DictReader(fh)] == ["CORP"]


def test_a_csv_is_written_with_one_row_per_name(probe, copy_path, tmp_path):
    out = tmp_path / "out"
    probe.run(copy_path, fetch=_fetch({"BANK": NII_ONLY, "CORP": EXPENSE}), out_dir=out)
    with open(out / "interest_label_probe.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == probe.CSV_COLUMNS
    assert {r["ticker"]: r["old_from_nii"] for r in rows} == {"BANK": "True", "CORP": "False"}


def test_a_failed_live_fetch_is_noted_and_does_not_stop_the_list(probe, copy_path, tmp_path):
    def fetch(t):
        if t == "BANK":
            raise RuntimeError("rate limited")
        return EXPENSE

    rows = {r["ticker"]: r for r in probe.run(copy_path, fetch=fetch, out_dir=tmp_path / "out")}
    assert "live fetch failed" in rows["BANK"]["note"] and rows["BANK"]["new_interest"] is None
    assert rows["CORP"]["new_interest"] == 5.0


def test_a_name_with_no_stored_year_is_listed_not_dropped(probe, tmp_path):
    copy = _make_copy(tmp_path / "c.duckdb", {"EMPTY": {"annual": []}})
    row = probe.run(copy, fetch=_fetch({"EMPTY": EXPENSE}), out_dir=tmp_path / "out")[0]
    assert row["ticker"] == "EMPTY" and "no valid stored year" in row["note"]


def test_the_probe_compares_with_the_current_label_list_not_a_copy_of_it(probe, monkeypatch):
    monkeypatch.setitem(probe.yfp._INCOME_FIELDS, "interest_expense", ["Interest Expense", "Net Interest Income"])
    assert probe.new_labels() == ["Interest Expense", "Net Interest Income"]
