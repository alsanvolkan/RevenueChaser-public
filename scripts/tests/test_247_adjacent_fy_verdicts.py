"""#247 verdict-count script: it runs offline on a store snapshot, leaves the files unchanged and finds the change.

The given files are only copied: the script opens copies under ``<out>/snap/``, every DuckDB file read-only, and the
folder of the given files is left exactly as it was (B1 of the Phase A review). A fixture store holds two US names:
WEEK, a 52/53-week filer whose two key-adjacent records ended 727 days apart, and EVEN, two records one year apart. Only WEEK's AG revenue growth (and so its points) may change. The script runs
in a child process, as on the PC, because it re-points the store module at the snapshot files. Fundamentals'
prefilter reads each name's D04 quote since #383 R3 C12, so the fixture records one for each name.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from RevenueChaser.core import rawstore

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "247_adjacent_fy_verdicts.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(),
                                reason=f"{_SCRIPT.name} is not in this tree")

_QUOTE = {"regularMarketPrice": 95.0, "fiftyTwoWeekHigh": 100.0, "fiftyDayAverage": 90.0,
          "averageDailyVolume10Day": 2e6, "averageDailyVolume3Month": 1e6}


def _annual(year: int, end: str, revenue: float, shares: float) -> dict:
    return {"fiscal_year": year, "revenue": revenue, "gross_profit": revenue * 0.6, "diluted_shares": shares,
            "period_ends": {"revenue": end, "gross_profit": end, "diluted_shares": end}}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _folder_state(d: Path) -> dict:
    """Names, sizes, hashes and the folder's own mtime: a file created and removed there still moves the mtime."""
    return {"mtime": d.stat().st_mtime_ns,
            "files": {p.name: (p.stat().st_size, _sha(p)) for p in sorted(d.iterdir()) if p.is_file()}}


def test_the_script_counts_the_52_53_week_case_and_writes_nothing(rawstore_tmp_db, tmp_path):
    rawstore.write_rows("equity_universe", [
        {"ticker": t, "market_cap": 5e9, "sector": "Technology", "price": 95.0, "name": t, "quote": _QUOTE}
        for t in ("WEEK", "EVEN")])
    rawstore.write_rows("quote_snapshot", [
        {"ticker": t, "session_date": "2026-10-09", "source": "yfinance", "market_cap": 5e9, "price": 95.0,
         "week_52_high": 100.0, "payload": _QUOTE, "fetched_at": 1.0, "run_id": "t", "run_at": 1.0}
        for t in ("WEEK", "EVEN")])
    meta = {"source": "sec_edgar", "entity_name": "x", "is_us": True, "available": True}
    rawstore.write_rows("edgar_financials", [
        {"ticker": "WEEK", "annual": [_annual(2022, "2022-01-02", 100.0, 100.0),
                                      _annual(2023, "2023-12-30", 150.0, 101.0)], "quarterly": [], "meta": meta},
        {"ticker": "EVEN", "annual": [_annual(2022, "2022-12-31", 100.0, 100.0),
                                      _annual(2023, "2023-12-31", 150.0, 101.0)], "quarterly": [], "meta": meta},
    ])
    raw, edgar = Path(rawstore.DB_PATH), rawstore.edgar_db_path()
    # The PC passes backup files, which sit in the backup folder; copy the pair into a folder of their own.
    given = tmp_path / "backups"
    given.mkdir()
    raw = Path(shutil.copy(raw, given / "raw_20261010.duckdb"))
    edgar = Path(shutil.copy(edgar, given / "edgar_20261010.duckdb"))
    before = _folder_state(given)

    out = tmp_path / "out"
    env = {**os.environ, "PYTHONPATH": str(_SCRIPT.parents[3])}
    done = subprocess.run([sys.executable, str(_SCRIPT), "--raw", str(raw), "--edgar", str(edgar),
                           "--out", str(out)], capture_output=True, text=True, env=env, timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]

    res = json.loads((out / "verdicts.json").read_text(encoding="utf-8"))
    ag = next(b for b in res["boards"] if b["board"] == "ag")
    assert ag["scored"] == 2
    assert ag["changed_by_rule"]["rev_growth"] == 1
    assert ag["changed_tickers"] == ["WEEK"]
    week = ag["examples"][0]
    assert week["old"]["rev_growth"] is True and week["new"]["rev_growth"] is None
    assert week["new"]["points"] == week["old"]["points"] - 1
    assert ag["dilution_dropped"] == []
    fund = next(b for b in res["boards"] if b["board"] == "fundamentals")
    assert fund["scored"] == 2
    assert "dilution warnings dropped" in (out / "verdicts.md").read_text(encoding="utf-8")
    assert _folder_state(given) == before                         # nothing opened, written or removed beside them
    assert sorted(p.name for p in (out / "snap").glob("*.duckdb")) == ["edgar.duckdb", "raw.duckdb"]


def test_every_duckdb_file_is_opened_read_only(tmp_path):
    """The wrap gives a write on a file handle an error, and leaves ``:memory:`` writable."""
    script = f"""
import importlib.util, duckdb, sys
spec = importlib.util.spec_from_file_location("v247", {str(_SCRIPT)!r})
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
db = {str(tmp_path / "x.duckdb")!r}
duckdb.connect(db).execute("CREATE TABLE t (a INT)").close()
mod._force_read_only_duckdb()
duckdb.connect(":memory:").execute("CREATE TABLE m (a INT)")
try:
    duckdb.connect(db).execute("INSERT INTO t VALUES (1)")
except duckdb.Error:
    sys.exit(0)
sys.exit(3)
"""
    env = {**os.environ, "PYTHONPATH": str(_SCRIPT.parents[3])}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


def test_the_script_refuses_name_lookups():
    script = f"""
import importlib.util, socket, sys
spec = importlib.util.spec_from_file_location("v247", {str(_SCRIPT)!r})
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
mod._block_network()
for call in (lambda: socket.getaddrinfo("localhost", 9),
             lambda: socket.socket().connect_ex(("127.0.0.1", 9))):
    try:
        call()
    except OSError as exc:
        assert "network blocked" in str(exc), exc
    else:
        sys.exit(3)
"""
    env = {**os.environ, "PYTHONPATH": str(_SCRIPT.parents[3])}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
