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
