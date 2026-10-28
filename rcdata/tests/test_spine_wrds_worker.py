"""``wrds_worker.py`` as a real subprocess under this interpreter, with a stub ``wrds`` package on PYTHONPATH (no
network): one login for several queries, chunked answers streamed whole to gzip JSON lines, a failed query answered
while the connection stays, and a failed login answered before the worker ends."""
from __future__ import annotations

import gzip
import json
import sys
from datetime import date

import pytest

from RevenueChaser.rcdata.build.spine import wrds

STUB = '''
import datetime, json, os, pandas as pd
LOG = os.environ["STUB_LOG"]

class Connection:
    def __init__(self, wrds_username=None):
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"login": wrds_username}) + "\\n")
        if wrds_username == "refused":
            raise RuntimeError("PAM authentication failed")

    def raw_sql(self, sql, chunksize=None, return_iter=False):
        if "boom" in sql:
            raise ValueError("relation comp.boom does not exist")
        frames = [pd.DataFrame({"GVKEY": ["001", "002"], "efffrom": [pd.Timestamp("2020-01-02"), pd.NaT],
                                "exchg": [11.0, float("nan")]}),
                  pd.DataFrame({"GVKEY": ["003"], "efffrom": [datetime.date(2021, 3, 4)], "exchg": [14.0]})]
        assert return_iter and chunksize
        return iter(frames)

    def close(self):
        pass
'''


@pytest.fixture
def stub(tmp_path, monkeypatch):
    pkg = tmp_path / "stub" / "wrds"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(STUB, encoding="utf-8")
    log = tmp_path / "logins.jsonl"
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "stub"))
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv(wrds.PYTHON_ENV, sys.executable)
    return tmp_path, log


def _lines(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(x) for x in fh]


def test_one_login_for_several_queries_and_chunks_streamed_whole(stub, monkeypatch):
    tmp, log = stub
    monkeypatch.setenv("WRDS_USERNAME", "avo")
    w = wrds.Worker()
    try:
        a, b = tmp / "a.jsonl.gz", tmp / "b.jsonl.gz"
        assert w.query("SELECT * FROM comp.sec_idhist", a) == 3
        assert w.query("SELECT * FROM comp.co_hgic", b) == 3
        with pytest.raises(wrds.base.SourceError, match="comp.boom"):
            w.query("SELECT * FROM comp.boom", tmp / "c.jsonl.gz")
        assert w.query("SELECT * FROM comp.company", tmp / "d.jsonl.gz") == 3     # the connection stays
    finally:
        w.close()
    assert _lines(a) == [{"gvkey": "001", "efffrom": "2020-01-02", "exchg": "11"},
                         {"gvkey": "002", "efffrom": None, "exchg": None},
                         {"gvkey": "003", "efffrom": "2021-03-04", "exchg": "14"}]
    assert not (tmp / "c.jsonl.gz").exists() and not list(tmp.glob("*.tmp"))
    assert log.read_text().splitlines() == ['{"login": "avo"}']
    import duckdb
    rows = duckdb.connect().execute(wrds.raw_rows(a, [("gvkey", "TEXT"), ("efffrom", "DATE"),
                                                      ("exchg", "INTEGER")])).fetchall()
    assert rows == [("001", date(2020, 1, 2), 11), ("002", None, None), ("003", date(2021, 3, 4), 14)]


def test_a_failed_login_is_answered_and_the_worker_ends(stub, monkeypatch):
    tmp, _ = stub
    monkeypatch.setenv("WRDS_USERNAME", "refused")
    w = wrds.Worker()
    try:
        with pytest.raises(wrds.LoginFailed, match="PAM authentication failed"):
            w.query("SELECT 1", tmp / "a.jsonl.gz")
        with pytest.raises(wrds.LoginFailed, match="gone|ended"):
            w.query("SELECT 1", tmp / "b.jsonl.gz")
    finally:
        w.close()
    assert w.proc.returncode == 2


def test_the_worker_does_not_import_the_revenuechaser_package():
    text = wrds.WORKER.read_text(encoding="utf-8")
    assert "RevenueChaser" not in text.split('"""', 2)[2] and "import wrds" in text
