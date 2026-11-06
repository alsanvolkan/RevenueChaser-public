"""Each served file's state on the control board (#492 slice B1, step 3): the file on disk, its tables with rows,
and its datasets counted by the Health strip's buckets."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from RevenueChaser.module_settings.tests.test_control_view import _build, control_nd


def _file(view, kind):
    return next(f for f in view["files"] if f["kind"] == kind)


def test_a_file_on_disk_has_its_size_and_write_time(tmp_path):
    path = tmp_path / "prices.duckdb"
    path.write_bytes(b"x" * 1234)
    os.utime(path, (1_793_000_000, 1_793_000_000))
    nd = control_nd()
    nd["storage"]["files"]["prices"]["path"] = str(path)
    s = _file(_build(nd), "prices")["state"]
    assert s["exists"] is True and s["bytes"] == 1234
    assert s["written_at"] == datetime.fromtimestamp(1_793_000_000, timezone.utc).isoformat(timespec="seconds")


def test_a_missing_file_is_not_there_with_nones(tmp_path):
    nd = control_nd()
    nd["storage"]["files"]["prices"]["path"] = str(tmp_path / "gone.duckdb")
    view = _build(nd)
    s = _file(view, "prices")["state"]
    assert (s["exists"], s["bytes"], s["written_at"]) == (False, None, None)
    spine = _file(view, "spine")["state"]
    assert (spine["exists"], spine["bytes"], spine["written_at"]) == (False, None, None), "no path known"


def test_the_tables_with_rows_come_from_the_table_counts():
    f = _file(_build(), "prices")
    s = f["state"]
    names = [t["name"] for t in f["tables"]]
    assert s["tables"] == len(names)
    assert s["tables_with_rows"] == 1
    assert "price_bar_daily" not in s["empty"] and set(s["empty"]) == set(names) - {"price_bar_daily"}


def test_the_counts_bucket_the_datasets_as_the_health_strip_does():
    view = _build()
    prices = _file(view, "prices")["state"]["counts"]
    assert prices == {"fresh": 0, "late": 1, "stale": 0, "never": 1, "total": 2}
    total = {k: sum(f["state"]["counts"][k] for f in view["files"]) for k in ("fresh", "late", "stale", "never")}
    assert total == view["health"]["counts"]
