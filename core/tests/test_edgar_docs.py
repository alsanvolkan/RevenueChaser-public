"""EDGAR raw HTML files (``edgar_docs/{cik}/{accession}.html.gz``)."""
from __future__ import annotations

from RevenueChaser.core import edgar_docs as ed


def test_write_read_round_trip():
    body = "<html>Item 1. Business — ünïcode</html>" * 20
    info = ed.write_html("320193", "0000320193-25-000001", body)
    assert info["path"].endswith("edgar_docs/320193/0000320193-25-000001.html.gz") \
        or info["path"].endswith("edgar_docs\\320193\\0000320193-25-000001.html.gz")
    assert info["bytes_raw"] > info["bytes_gzip"]  # gzip compresses
    assert len(info["sha256"]) == 64
    assert ed.read_html("320193", "0000320193-25-000001") == body
    assert ed.exists("320193", "0000320193-25-000001")


def test_cik_is_normalised():
    ed.write_html("0000320193", "acc-1", "<html>x</html>")
    # leading-zero CIK resolves to the same file as the plain form
    assert ed.path_for("320193", "acc-1") == ed.path_for("0000320193", "acc-1")


def test_missing_file_returns_none():
    assert ed.read_html("999999", "nope") is None
    assert ed.exists("999999", "nope") is False


def test_rewrite_is_atomic_and_replaces():
    ed.write_html("320193", "acc-2", "<html>old</html>")
    ed.write_html("320193", "acc-2", "<html>new</html>")
    assert ed.read_html("320193", "acc-2") == "<html>new</html>"
