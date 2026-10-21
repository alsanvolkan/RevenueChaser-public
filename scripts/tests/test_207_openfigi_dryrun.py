"""#207 dry-run script: the first OpenFIGI map on a copy of a backup, against a fake OpenFIGI (no network)."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from RevenueChaser.app.tests.test_sec13f_map_207 import FIGI, KEY, _OpenFigi
from RevenueChaser.core import rawstore
from RevenueChaser.core.config import DATA_DIR
from RevenueChaser.core.providers import openfigi
from RevenueChaser.module_sec_13f import engine
from RevenueChaser.module_sec_13f.tests.test_sec13f_openfigi_207 import _seed

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "207_openfigi_dryrun.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(),
                                reason=f"{_SCRIPT.name} is not in this tree")

Q = "2026-06-30"


@pytest.fixture
def dryrun(monkeypatch, tmp_path):
    # The file name starts with a digit, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location("dryrun_207", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(openfigi, "map_cusips", openfigi.map_cusips)      # main() wraps it: restore after
    return mod


@pytest.fixture
def figi(monkeypatch):
    fake = _OpenFigi()
    monkeypatch.setattr(openfigi, "_post", fake)
    monkeypatch.setattr(openfigi, "_sleep", lambda s: None)
    monkeypatch.setenv("OPENFIGI_API_KEY", KEY)
    return fake


@pytest.fixture
def snapshot(rawstore_tmp_db):
    """A backup-like raw store: AAPL plus a dual-class pair the legacy map merged onto GOOGL, and one CUSIP
    OpenFIGI does not know."""
    _seed([("a1", "1000", Q, "111111111", 300, 3000), ("b1", "3000", Q, "222222222", 70, 7000),
           ("b1", "3000", Q, "333333333", 30, 3000), ("b2", "4000", Q, "999999999", 5, 500)],
          names={"999999999": "NOBODY KNOWS CORP"})
    engine.save_cusip_ticker({"111111111": "AAPL", "222222222": "GOOGL", "333333333": "GOOGL"})
    engine.recompute_totals()
    return Path(rawstore.DB_PATH)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_snapshot_or_copy_inside_the_live_store_is_refused(dryrun, tmp_path):
    with pytest.raises(SystemExit, match="refused"):
        dryrun.make_copy(DATA_DIR / "raw.duckdb", out_dir=tmp_path)
    with pytest.raises(SystemExit, match="refused"):
        dryrun.make_copy(tmp_path / "raw.duckdb", out_dir=DATA_DIR / "research")


def test_the_cap_cannot_go_over_400(dryrun):
    with pytest.raises(SystemExit, match="between 1 and 400"):
        dryrun.main(["--max-requests", "401"])


def test_a_dry_run_maps_the_copy_reports_and_leaves_the_snapshot_alone(dryrun, figi, snapshot, tmp_path):
    before = _digest(snapshot)
    assert dryrun.main(["--snapshot", str(snapshot), "--spot", "GOOGL,GOOG"]) == 0
    assert _digest(snapshot) == before                                     # the backup is never written
    copy = tmp_path / "out" / "store_copy" / "raw.duckdb"
    assert Path(rawstore.DB_PATH) == copy and figi.requests == 1
    assert {c: e["ticker"] for c, e in engine.load_cusip_ticker().items()} == FIGI        # in the copy
    (md,) = (tmp_path / "out").glob("dryrun_*.md")
    text = md.read_text(encoding="utf-8")
    assert "requests 1 (cap 400); 429s 0" in text and "3 mapped, 1 unknown to OpenFIGI" in text
    assert "## Guard: PASS" in text
    assert "- GOOGL -> GOOG, GOOGL (222222222, 333333333)" in text
    assert "| 333333333 | GOOGL | GOOG | $3,000 |" in text
    assert "| GOOG | 0 / $0 / - | 30 / $3,000 / 333333333 |" in text
    assert "cusip_ticker 3 -> 3 rows (old rows deleted)" in text
    assert (tmp_path / "out" / md.name.replace(".md", ".json")).exists()


def test_a_lower_cap_stops_the_step_and_writes_no_map(dryrun, figi, snapshot, tmp_path):
    figi.throttle = 99
    assert dryrun.main(["--snapshot", str(snapshot), "--max-requests", "2"]) == 0
    assert figi.requests == 2
    (md,) = (tmp_path / "out").glob("dryrun_*.md")
    text = md.read_text(encoding="utf-8")
    assert "stopped at the cap of 2" in text and "No complete map: nothing to compare" in text
    assert engine.load_cusip_ticker()["333333333"] == "GOOGL"              # the copy keeps the stored map


def test_a_refused_guard_is_reported_and_the_copy_keeps_its_map(dryrun, figi, snapshot, tmp_path, monkeypatch):
    def refuse(before, totals):
        raise engine.DeriveRefused("the derive would cut the mapped value of 2026-06-30")
    monkeypatch.setattr(engine, "guard", refuse)
    assert dryrun.main(["--snapshot", str(snapshot)]) == 0
    (md,) = (tmp_path / "out").glob("dryrun_*.md")
    text = md.read_text(encoding="utf-8")
    assert "## Guard: REFUSED: the derive would cut the mapped value of 2026-06-30" in text
    assert "Copy write: none: the guard refused" in text
    assert engine.load_cusip_ticker()["333333333"] == "GOOGL"


@pytest.fixture
def legacy_snapshot(snapshot):
    """``snapshot`` plus three legacy CUSIPs OpenFIGI does not know: one on a prefix OpenFIGI confirms for the same
    ticker (222222: GOOGL), one on a prefix of another ticker (333333: GOOG), one on an unknown prefix."""
    _seed([("c1", "5000", Q, "222222888", 11, 1100), ("c1", "5000", Q, "222222900", 3, 300),
           ("c1", "5000", Q, "333333999", 13, 1300),
           ("c1", "5000", Q, "G54950103", 17, 1700)])
    engine.save_cusip_ticker({**engine.load_cusip_ticker(), "222222888": "GOOGL", "222222900": "GOOGL",
                              "333333999": "QQQ",
                              "G54950103": "HEI"})
    engine.recompute_totals()
    return snapshot


def test_the_report_counts_and_values_the_legacy_prefix_fallback(dryrun, figi, legacy_snapshot, tmp_path):
    assert dryrun.main(["--snapshot", str(legacy_snapshot)]) == 0
    text = next((tmp_path / "out").glob("dryrun_*.md")).read_text(encoding="utf-8")
    assert "## Legacy prefix fallback (option B minus 9x), held in 2026-06-30" in text
    assert "- candidates: 4 CUSIPs, $4,400" in text
    assert "- kept (prefix confirms the legacy ticker, issue not 9x): 1 CUSIPs, $1,100" in text
    assert "- dropped: option-like issue number (90-99): 1 CUSIPs, $300" in text
    assert "- dropped: prefix of another ticker: 1 CUSIPs, $1,300" in text
    assert "- dropped: prefix unknown to OpenFIGI: 1 CUSIPs, $1,700" in text
    assert "| 333333999 | QQQ | $1,300 | GOOG |" in text and "| G54950103 | HEI | $1,700 | - |" in text
    stored = engine.load_cusip_ticker()                                     # the copy, after a passing guard
    assert stored["222222888"] == {"ticker": "GOOGL", "source": "legacy"} and "G54950103" not in stored
    assert "222222900" not in stored                                        # the default drops 9x issues
    assert "| | before (stored map) | after (new map) |" in text


def test_answers_from_an_earlier_copy_ask_openfigi_nothing(dryrun, figi, legacy_snapshot, tmp_path):
    assert dryrun.main(["--snapshot", str(legacy_snapshot), "--no-derive"]) == 0
    assert figi.requests == 1
    copy = tmp_path / "out" / "store_copy" / "raw.duckdb"
    for md in (tmp_path / "out").glob("dryrun_*"):
        md.unlink()
    assert dryrun.main(["--snapshot", str(legacy_snapshot), "--answers-from", str(copy)]) == 0
    assert figi.requests == 1                                                # no second request
    text = next((tmp_path / "out").glob("dryrun_*.md")).read_text(encoding="utf-8")
    assert f"OpenFIGI answers: reused from {copy} (no request)" in text
    assert "- kept (prefix confirms the legacy ticker, issue not 9x): 1 CUSIPs, $1,100" in text
    assert engine.load_cusip_ticker()["222222888"]["source"] == "legacy"


def test_answers_from_a_copy_missing_a_held_cusip_are_refused_before_any_request(dryrun, figi, legacy_snapshot,
                                                                                 tmp_path):
    old = tmp_path / "old" / "raw.duckdb"
    old.parent.mkdir()
    import duckdb
    with duckdb.connect(str(old)) as conn:
        conn.execute("CREATE TABLE identity_cusip AS SELECT * FROM (VALUES ('111111111', 'present', 'AAPL', 1.0)) "
                     "t(cusip, status, ticker, run_at)")
    with pytest.raises(SystemExit, match="held CUSIP"):
        dryrun.main(["--snapshot", str(legacy_snapshot), "--answers-from", str(old)])
    assert figi.requests == 0


def test_answers_from_the_live_store_are_refused(dryrun):
    with pytest.raises(SystemExit, match="refused"):
        dryrun.read_answers(DATA_DIR / "raw.duckdb")


def test_the_report_splits_the_kept_bucket_by_issue_number_and_prefix_tickers(dryrun, figi, legacy_snapshot,
                                                                              tmp_path):
    """#427 review MUST-2: the 9x (option-like) and multi-ticker-prefix share of the prefix-confirmed CUSIPs."""
    assert dryrun.main(["--snapshot", str(legacy_snapshot)]) == 0
    text = next((tmp_path / "out").glob("dryrun_*.md")).read_text(encoding="utf-8")
    assert "### Prefix-confirmed CUSIPs by issue number and prefix tickers" in text
    assert "| issue not 9x | 1 / $1,100 | 0 / $0 |" in text
    assert "| issue 9x (option-like) | 1 / $300 | 0 / $0 |" in text
    assert "- the default (9x dropped) keeps: 1 CUSIPs, $1,100; B-strict would keep: 1 CUSIPs, $1,100 " \
        "(mode of this run: B minus 9x)" in text


def test_strict_mode_keeps_only_single_ticker_non_option_prefixes(dryrun, figi, legacy_snapshot, tmp_path):
    assert dryrun.main(["--snapshot", str(legacy_snapshot), "--strict"]) == 0
    text = next((tmp_path / "out").glob("dryrun_*.md")).read_text(encoding="utf-8")
    assert "(mode of this run: B-strict)" in text and "Map rule: B-strict" in text
    stored = engine.load_cusip_ticker()
    assert stored["222222888"]["source"] == "legacy" and "222222900" not in stored
    assert engine._FALLBACK_STRICT is False                                 # the flag does not leak


def _copy_missing(tmp_path, answered):
    """An earlier store copy whose identity_cusip answers only ``answered`` (the rest were job errors)."""
    import duckdb
    old = tmp_path / "old" / "raw.duckdb"
    old.parent.mkdir()
    with duckdb.connect(str(old)) as conn:
        conn.execute("CREATE TABLE identity_cusip (cusip VARCHAR, status VARCHAR, ticker VARCHAR, run_at DOUBLE)")
        conn.executemany("INSERT INTO identity_cusip VALUES (?, ?, ?, 1.0)", answered)
    return old


def test_unanswered_as_refused_counts_the_missing_answers_as_job_errors_and_asks_nothing(dryrun, figi,
                                                                                        legacy_snapshot, tmp_path):
    """#427 review MUST-1: the first PC run left 4,660 job-errored CUSIPs without a row."""
    old = _copy_missing(tmp_path, [("111111111", "present", "AAPL"), ("222222222", "present", "GOOGL"),
                                   ("333333333", "present", "GOOG")])
    assert dryrun.main(["--snapshot", str(legacy_snapshot), "--answers-from", str(old),
                        "--unanswered-as-refused"]) == 0
    assert figi.requests == 0
    text = next((tmp_path / "out").glob("dryrun_*.md")).read_text(encoding="utf-8")
    assert "requests 0 (cap 400)" in text and "5 refused" in text
    assert "## Legacy prefix fallback (option B minus 9x)" in text
    assert "- kept (prefix confirms the legacy ticker, issue not 9x): 1 CUSIPs, $1,100" in text


def test_unanswered_as_refused_needs_answers_from(dryrun):
    with pytest.raises(SystemExit, match="--unanswered-as-refused needs --answers-from"):
        dryrun.main(["--unanswered-as-refused"])


def test_the_report_counts_a_multi_ticker_prefix_as_kept_by_default_and_dropped_by_strict(dryrun, figi,
                                                                                          legacy_snapshot, tmp_path):
    old = _copy_missing(tmp_path, [("111111111", "present", "AAPL"), ("222222222", "present", "GOOGL"),
                                   ("222222333", "present", "GOOGX"), ("333333333", "present", "GOOG")])
    assert dryrun.main(["--snapshot", str(legacy_snapshot), "--answers-from", str(old),
                        "--unanswered-as-refused"]) == 0
    text = next((tmp_path / "out").glob("dryrun_*.md")).read_text(encoding="utf-8")
    assert "| issue not 9x | 0 / $0 | 1 / $1,100 |" in text
    assert "- the default (9x dropped) keeps: 1 CUSIPs, $1,100; B-strict would keep: 0 CUSIPs, $0" in text
