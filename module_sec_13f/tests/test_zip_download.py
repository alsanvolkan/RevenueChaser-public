"""SEC Form 13F data-set download (backend plan S11: B17, B18, B19). All network mocked."""
from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_sec_13f import engine as sf

# The real page's link shape: site-relative /files/... hrefs.
INDEX_HTML = """
<a href="/files/structureddata/data/form-13f-data-sets/2024q4_form13f.zip">2024 Q4</a>
<a href="/files/structureddata/data/form-13f-data-sets/01mar2025-31may2025_form13f.zip">x</a>
<a href="/files/structureddata/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip">x</a>
<a href="/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip">x</a>
<a href='/files/structureddata/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip'>dup</a>
"""
INDEX = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("INFOTABLE.tsv", "x")
    return buf.getvalue()


class TestIndex:
    def test_download_url_comes_from_the_href_path(self):
        got = sf.parse_zip_index(INDEX_HTML, INDEX)
        assert got["2024q4_form13f.zip"] == (
            "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/2024q4_form13f.zip")
        assert "/dera/data/form-13f/" not in "".join(got.values())
        assert len(got) == 4  # duplicate link collapsed

    @pytest.mark.parametrize("name,end", [
        ("2024q4_form13f.zip", date(2024, 12, 31)),
        ("2025q1_form13f.zip", date(2025, 3, 31)),
        ("01jun2026-31aug2026_form13f.zip", date(2026, 8, 31)),
        ("1mar2025-31may2025_form13f.zip", date(2025, 5, 31)),
        ("weird_form13f.zip", None),
    ])
    def test_window_end_from_name(self, name, end):
        assert sf.zip_window_end(name) == end


class TestMissing:
    def test_ingested_on_disk_and_old_zips_are_skipped(self, rawstore_tmp_db, tmp_path):
        rawstore.write_rows("sec13f_ingested_zips", [
            {"filename": "01mar2025-31may2025_form13f.zip", "ingested_at": "x"}])
        (tmp_path / "01mar2026-31may2026_form13f.zip").write_bytes(b"z")
        listed = sf.parse_zip_index(INDEX_HTML, INDEX)
        assert list(sf.missing_zips(listed, tmp_path)) == ["01jun2026-31aug2026_form13f.zip"]

    def test_download_writes_atomically_and_only_the_missing(self, rawstore_tmp_db, tmp_path,
                                                             monkeypatch):
        from RevenueChaser.core.providers import sec_edgar
        fetched = []
        monkeypatch.setattr(sec_edgar, "get_text", lambda url, client=None: INDEX_HTML)

        def fake_bytes(url, client=None):
            fetched.append(url)
            return _zip_bytes()

        monkeypatch.setattr(sec_edgar, "get_bytes", fake_bytes)
        rawstore.write_rows("sec13f_ingested_zips", [
            {"filename": "01mar2025-31may2025_form13f.zip", "ingested_at": "x"},
            {"filename": "01mar2026-31may2026_form13f.zip", "ingested_at": "x"}])
        got = sf.download_missing_zips(tmp_path)
        assert [p.name for p in got] == ["01jun2026-31aug2026_form13f.zip"]
        assert fetched == ["https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
                           "01jun2026-31aug2026_form13f.zip"]
        assert not list(tmp_path.glob("*.part"))

    def test_a_truncated_download_leaves_no_zip(self, rawstore_tmp_db, tmp_path, monkeypatch):
        from RevenueChaser.core.providers import sec_edgar
        monkeypatch.setattr(sec_edgar, "get_text", lambda url, client=None: INDEX_HTML)
        monkeypatch.setattr(sec_edgar, "get_bytes", lambda url, client=None: b"PK\x03\x04trunc")
        with pytest.raises(RuntimeError, match="not a valid zip"):
            sf.download_missing_zips(tmp_path)
        assert not list(tmp_path.glob("*_form13f.zip*"))


class TestCheckJob:
    def _patch(self, monkeypatch):
        from RevenueChaser.core.providers import sec_edgar
        monkeypatch.setattr(sec_edgar, "get_text", lambda url, client=None: INDEX_HTML)
        monkeypatch.setattr(sec_edgar, "get_bytes",
                            lambda *a, **k: pytest.fail("the check must not download"))

    def test_reports_a_new_set(self, rawstore_tmp_db, tmp_path, monkeypatch):
        from RevenueChaser.app import jobs, runners
        self._patch(monkeypatch)
        monkeypatch.setattr(sf, "OWNERSHIP_DIR", tmp_path)
        with pytest.raises(jobs.JobMessage, match="01jun2026-31aug2026_form13f.zip"):
            runners.run_sec13f_check()

    def test_reports_up_to_date(self, rawstore_tmp_db, tmp_path, monkeypatch):
        from RevenueChaser.app import jobs, runners
        self._patch(monkeypatch)
        monkeypatch.setattr(sf, "OWNERSHIP_DIR", tmp_path)
        rawstore.write_rows("sec13f_ingested_zips", [
            {"filename": n, "ingested_at": "x"} for n in (
                "01mar2025-31may2025_form13f.zip", "01mar2026-31may2026_form13f.zip",
                "01jun2026-31aug2026_form13f.zip")])
        with pytest.raises(jobs.JobMessage, match="Up to date.*01jun2026-31aug2026"):
            runners.run_sec13f_check()
