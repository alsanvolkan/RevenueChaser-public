"""PR 3, sub-step 6: ``tools/compare_pin`` puts the old (all-expiry) and the new (0-1 DTE) pin and squeeze side by
side for stored days, read-only and with no network, for avo's before/after look on real data (step S3)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb
import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_spx_gamma import snapshot as snap
from RevenueChaser.module_spx_gamma.tools import compare_pin

from . import rcdata_fixture as fx
from .conftest import FakeUW

DAY = "2026-10-26"
MAGNET = 6750.0


def _handler(path, params):
    if path.endswith("/gex-levels"):
        return {"data": {"call_wall": "6900", "gamma_flip": "6700", "gamma_magnet": "6750", "put_wall": "6600"}}
    if path.endswith("/greek-exposure/expiry"):
        return {"data": [{"date": DAY, "expiry": DAY, "dte": 0, "call_gex": "5", "put_gex": "-1"}]}
    if path.endswith("/flow-per-strike"):
        return []
    if path.endswith("/spot-exposures/strike"):
        # all-expiry: $12B at the magnet strike, the old basis of the $10B test
        return {"data": [{"strike": "6750", "call_gamma_oi": "12000000000", "put_gamma_oi": "0"}]
                if params["min_strike"] < 6750 else []}
    if path.endswith("/strike-expiry"):
        return {"data": [{"expiry": DAY, "strike": "6750", "call_gex": "50", "put_gex": "-1"},
                         {"expiry": DAY, "strike": "6760", "call_gex": "-5", "put_gex": "-30"}]}
    return {"data": []}


@pytest.fixture
def stored_day(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc))
    s, nxt = fx.LAST, calendar.next_trading_day(fx.LAST)
    # 0-1 DTE: $4B at the magnet (no pin on the new basis), -$300M at 6760 (a squeeze trigger on the new basis)
    fx.serve(spx={s: [fx.uw_row(s, s, MAGNET, 3e9, 0), fx.uw_row(s, nxt, MAGNET, 1e9, 0),
                      fx.uw_row(s, s, 6760, 0, -3e8)]})
    snap.fetch_snapshot(FakeUW(_handler, prefix=""), DAY, data_root=tmp_path / "raw")
    return tmp_path


def _tree(root):
    return sorted((p.relative_to(root).as_posix(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())


def test_one_line_per_day_with_both_bases(stored_day, capsys):
    assert compare_pin.main(["--days", DAY, "2026-10-23", "--data-root", str(stored_day)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    line = lines[0]
    assert line.startswith(f"{DAY} magnet=6,750")
    assert "0-1DTE=$4.00B" in line and "all-expiry=$12.00B" in line
    assert "pin new=- old=ACTIVE_PIN" in line
    # the all-expiry rows have nothing at 6760: $0 on that basis, so no trigger there (review SF1, today's rules)
    assert "squeeze new=6,760 ($-0.30B) old=-" in line
    assert "vix new=" in line and "@2026-10-26" in line and "cot new=2026-10-20" in line
    assert "spx new=6,749.00@2026-10-26" in line
    assert lines[1] == "2026-10-23 no complete raw day (manifest.json missing)"


def test_it_writes_nothing_and_opens_duckdb_read_only(stored_day, capsys, monkeypatch):
    opened = []
    real = duckdb.connect

    def spy(*a, **k):
        opened.append(k.get("read_only"))
        return real(*a, **k)
    monkeypatch.setattr(duckdb, "connect", spy)
    before = _tree(stored_day)
    compare_pin.main(["--days", DAY, "--data-root", str(stored_day)])
    assert opened and all(ro is True for ro in opened), opened
    assert _tree(stored_day) == before, "the tool wrote into the board's data root"


def test_an_older_day_shows_its_own_files_as_the_old_side(stored_day, capsys):
    day = stored_day / "raw" / DAY
    (day / "vix_family.json").write_text(json.dumps({"tickers": {"^VIX": 21.5}}), encoding="utf-8")
    (day / "cftc_tff.json").write_text(json.dumps({"report_date": "2026-10-13T00:00:00.000"}), encoding="utf-8")
    compare_pin.main(["--days", DAY, "--data-root", str(stored_day)])
    out = capsys.readouterr().out
    assert "@2026-10-26 old=21.50 |" in out and "cot new=2026-10-20 old=2026-10-13" in out
