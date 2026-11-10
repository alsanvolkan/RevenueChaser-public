"""Local embeddings of the v3 profile for Relatedness similarity v2 (#669, plan 2026-10-30 Step 2).

One vector per company, from its ``summary`` and one line per segment, the segment lines weighted by their
revenue ``share``. The model runs on the box (``fastembed``, ONNX, CPU, no key, no fee). A vector is cached in
``relatedness_embeddings`` and made again only when the text it was made from changes.

The stored ``vector`` BLOB is ``HASH_BYTES`` of sha256 over the embedded text, then the float32 values. The text
hash rides in the blob because the table (schema 13) has no column for it, and a new column would be a version bump
during the DuckDB freeze. ``dim`` is the number of float32 values.
"""
from __future__ import annotations

import hashlib
import json
import logging

import numpy as np

from . import config

logger = logging.getLogger(__name__)

HASH_BYTES = 16
_MODEL = None


def pieces(pj: dict | None) -> list[tuple[str, float]]:
    """``[(text, weight)]`` for one v3 profile: the summary at weight 1, each segment line at its share.

    Segments without a share split one weight equally. No summary and no segment: ``[]`` (no vector)."""
    pj = pj or {}
    out = []
    summary = str(pj.get("summary") or "").strip()
    if summary:
        out.append((summary, 1.0))
    segs = []
    for s in pj.get("segments") or []:
        if not isinstance(s, dict):
            continue
        parts = [str(s.get("name") or "").strip()]
        for f in ("products", "end_markets"):
            vals = [str(v).strip() for v in (s.get(f) or []) if str(v).strip()]
            if vals:
                parts.append(", ".join(vals))
        line = ": ".join(p for p in parts if p)
        if line:
            share = s.get("share")
            segs.append((line, float(share) if isinstance(share, (int, float)) and share > 0 else None))
    if segs:
        known = [w for _, w in segs if w is not None]
        if len(known) == len(segs):
            out += segs
        else:
            out += [(line, 1.0 / len(segs)) for line, _ in segs]
    return out


def text_hash(ps: list[tuple[str, float]]) -> bytes:
    text = json.dumps([config.EMBED_MODEL, ps], ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).digest()[:HASH_BYTES]


def _embed(texts: list[str]) -> np.ndarray:
    """Rows of unit vectors for ``texts``. The model loads once per process, from ``config.EMBED_CACHE``."""
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding
        config.EMBED_CACHE.mkdir(parents=True, exist_ok=True)
        _MODEL = TextEmbedding(model_name=config.EMBED_MODEL, cache_dir=str(config.EMBED_CACHE))
    return np.asarray(list(_MODEL.embed(texts)), dtype="float32")


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def company_vectors(pjs: dict[str, dict], write: bool = True) -> dict[str, np.ndarray]:
    """``{ticker: unit float32 vector}`` for every ticker whose profile has text to embed.

    Cached vectors whose text hash still matches are reused; the rest are embedded in one batch and, when
    ``write`` (a Fetch; a Refresh writes nothing), upserted to ``relatedness_embeddings``."""
    from . import store
    want = {t: pieces(pj) for t, pj in pjs.items()}
    want = {t: ps for t, ps in want.items() if ps}
    if not want:
        return {}
    hashes = {t: text_hash(ps) for t, ps in want.items()}
    out: dict[str, np.ndarray] = {}
    for t, blob in store.load_vectors(config.EMBED_MODEL, list(want)).items():
        if blob[:HASH_BYTES] == hashes[t]:
            out[t] = np.frombuffer(blob[HASH_BYTES:], dtype="float32")
    todo = [t for t in want if t not in out]
    if todo:
        texts = [txt for t in todo for txt, _ in want[t]]
        rows = _embed(texts)
        i = 0
        for t in todo:
            n = len(want[t])
            w = np.asarray([wt for _, wt in want[t]], dtype="float32")
            out[t] = _unit((rows[i:i + n] * w[:, None]).sum(axis=0)).astype("float32")
            i += n
        if write:
            store.save_vectors(config.EMBED_MODEL,
                               {t: hashes[t] + out[t].tobytes() for t in todo}, dim=int(rows.shape[1]))
        logger.info("relatedness embeddings: %d embedded, %d cached", len(todo), len(out) - len(todo))
    return out
