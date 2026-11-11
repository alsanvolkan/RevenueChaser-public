from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_embedder(monkeypatch):
    """No test loads the fastembed model (#669): its first use downloads it. A test that wants vectors sets its own
    ``embeddings._embed``; every other one scores on the tag cosine, the board's fallback."""
    from RevenueChaser.module_relatedness import embeddings

    def _refuse(texts):
        raise RuntimeError("the embedding model is not loaded in tests")
    monkeypatch.setattr(embeddings, "_embed", _refuse)


@pytest.fixture(autouse=True)
def _spine_file():
    """A spine with an empty ``security`` table, so a profile run can start (#642 D3: no spine stops it). A test that
    names companies or sectors writes its rows with ``spine_fixture.write_security``; one that builds the real spine
    swaps this file out."""
    from RevenueChaser.rcdata.tests import spine_fixture
    spine_fixture.write_security([])
