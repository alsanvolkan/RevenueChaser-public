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
LOG = os.environ["RC_WRDS_STUB_LOG"]

class Connection:
    def __init__(self, wrds_username=None):
        print("Loading library list...", flush=True)       # what the real wrds.Connection prints on stdout
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"login": wrds_username, "figi_key": "OPENFIGI_API_KEY" in os.environ}) + "\\n")
        if wrds_username == "refused":
            raise RuntimeError("PAM authentication failed for user refused at wrds-pgdata.wharton.upenn.edu")
        if wrds_username == "asks":
            input("Enter your WRDS username: ")             # a prompt: there is no person on this stdin
        print("Done", flush=True)

    def raw_sql(self, sql, chunksize=None, return_iter=False):
        print("running", sql, flush=True)
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
    monkeypatch.setenv("RC_WRDS_STUB_LOG", str(log))
    monkeypatch.setenv(wrds.PYTHON_ENV, sys.executable)
    monkeypatch.setenv("RC_WRDS_NO_TTY", "1")              # a developer's terminal must never become the prompt
    monkeypatch.setenv("OPENFIGI_API_KEY", "must-not-reach-the-worker")
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
    assert log.read_text().splitlines() == ['{"login": "avo", "figi_key": false}']   # one login, no foreign keys
    import duckdb
    rows = duckdb.connect().execute(wrds.raw_rows(a, [("gvkey", "TEXT"), ("efffrom", "DATE"),
                                                      ("exchg", "INTEGER")])).fetchall()
    assert rows == [("001", date(2020, 1, 2), 11), ("002", None, None), ("003", date(2021, 3, 4), 14)]


def test_a_failed_login_is_answered_and_the_worker_ends(stub, monkeypatch):
    tmp, _ = stub
    monkeypatch.setenv("WRDS_USERNAME", "refused")
    w = wrds.Worker()
    try:
        with pytest.raises(wrds.LoginFailed, match="PAM authentication failed") as e:
            w.query("SELECT 1", tmp / "a.jsonl.gz")
        with pytest.raises(wrds.LoginFailed, match="gone|ended"):
            w.query("SELECT 1", tmp / "b.jsonl.gz")
    finally:
        w.close()
    assert w.proc.returncode == 2
    assert "wharton" not in str(e.value) and "refused" not in str(e.value).split(":", 1)[1]   # no user, no host


def test_a_prompt_with_no_person_is_a_failed_login_not_a_hang(stub, monkeypatch):
    tmp, _ = stub
    monkeypatch.setenv("WRDS_USERNAME", "asks")
    w = wrds.Worker()
    try:
        with pytest.raises(wrds.LoginFailed, match="EOFError"):
            w.query("SELECT 1", tmp / "a.jsonl.gz")
    finally:
        w.close()


def test_a_line_out_of_protocol_kills_the_worker_and_is_a_failed_login(stub, monkeypatch, tmp_path):
    """Whatever reaches the protocol pipe that is not an answer ends the worker: never read past it."""
    monkeypatch.setenv("WRDS_USERNAME", "avo")
    w = wrds.Worker()
    import io
    w.proc.stdout = io.StringIO("Loading library list...\n{\"ok\": true, \"rows\": 1}\n")
    try:
        with pytest.raises(wrds.LoginFailed, match="out of protocol"):
            w.query("SELECT 1", tmp_path / "a.jsonl.gz")
        with pytest.raises(wrds.LoginFailed):
            w.query("SELECT 1", tmp_path / "b.jsonl.gz")
    finally:
        w.close()


def test_no_user_name_is_a_failed_login_before_the_worker_starts(monkeypatch):
    monkeypatch.delenv("WRDS_USERNAME", raising=False)
    monkeypatch.setenv(wrds.PYTHON_ENV, sys.executable)
    with pytest.raises(wrds.LoginFailed, match="WRDS_USERNAME"):
        wrds.Worker()


def test_the_worker_does_not_import_the_revenuechaser_package():
    text = wrds.WORKER.read_text(encoding="utf-8")
    assert "RevenueChaser" not in text.split('"""', 2)[2] and "import wrds" in text


def test_an_error_masks_the_user_name_as_a_word_not_inside_other_words(monkeypatch):
    """Delta-2 N4: user "ab" is masked where it stands alone, not inside "table" or "abc"."""
    from RevenueChaser.rcdata.build.spine import wrds_worker
    monkeypatch.setenv("WRDS_USERNAME", "ab")
    text = wrds_worker._error(RuntimeError('role "ab" cannot read table abc; user ab'))
    assert text == 'RuntimeError: role "<user>" cannot read table abc; user <user>'
