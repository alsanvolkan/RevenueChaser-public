"""Value Chain test isolation."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_edgar_docs(tmp_path, monkeypatch):
    """Raw filing files per test: the sync re-slices a stored document instead of
    downloading it (#116), so a file left by another test would change the result."""
    from RevenueChaser.core import edgar_docs
    monkeypatch.setattr(edgar_docs, "EDGAR_DOCS_DIR", tmp_path / "edgar_docs")
