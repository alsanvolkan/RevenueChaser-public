"""The SEC's Insider Transactions Data Sets: how ``sec/form4`` gets its history without one request per filing
(2014 to today is about 2 million Form 4s; the sets are 52 zips).

``GET <RC_FORM345_INDEX_URL>`` (default ``<www.sec.gov>/data-research/sec-markets-data/insider-transactions-data-sets``)
links one zip per quarter, ``<yyyy>q<n>_form345.zip``, back to 2006q1 (83 on 2026-11-06, in two folders, so each link
is resolved against the page). The window is the filing dates the set holds. The index read, the name parsing and the
download are the 13F sets' (``sec13f_sets``), which is why they take the suffix and the raw folder as arguments.

A zip holds tab-separated ``SUBMISSION``, ``REPORTINGOWNER``, ``NONDERIV_TRANS``, ``DERIV_TRANS``,
``NONDERIV_HOLDING``, ``DERIV_HOLDING``, ``FOOTNOTES`` and ``OWNER_SIGNATURE``: Forms 3, 4 and 5 together, the form in
``SUBMISSION.DOCUMENT_TYPE``.

**Nothing is typed from them yet** (avo, 2026-11-06: fetch the raw now, the parquet and the DuckDB follow, likely on a
faster machine). So a backfill keeps the zips and the forms walk takes only the days after the newest set — the days a
set covers hold no rows until the typing lands, and it will read the kept zips without asking the SEC again.

Raw: ``raw/sec/form345_sets/<zip>`` with a ``.meta.json`` sidecar, as the 13F sets are kept; each download is one
paced SEC request (``sec.Pacer``), 10 to 80 MB. A later backfill asks again with ``If-Modified-Since`` (the SEC
answers 304, no body), so a set already held costs a request and no bytes.
"""
from __future__ import annotations

import os
from datetime import date

from .. import base
from . import sec, sec13f_sets

INDEX_ENV = "RC_FORM345_INDEX_URL"
INDEX_PATH = "/data-research/sec-markets-data/insider-transactions-data-sets"
SUFFIX = "_form345"
SUB = "form345_sets"
LABEL = "insider transactions"


def index_url() -> str:
    return os.getenv(INDEX_ENV) or sec.www_base() + INDEX_PATH


def fetch(ctx: base.RunContext, start: date, end: date) -> date | None:
    """Download every listed set whose window meets ``start``..``end``; the last day they cover (None when none
    does). Not typed: see the module docstring."""
    pacer = sec.Pacer(ctx)
    covered = None
    listed = sec13f_sets.listed(ctx, pacer, index_url(), SUFFIX, LABEL)
    for name, (first, last, url) in sorted(listed.items(), key=lambda kv: kv[1][0]):
        if max(first, start) > min(last, end):
            continue
        _path, replaced = sec13f_sets.download(ctx, pacer, name, url, SUB)
        if replaced:
            ctx.notes.append(f"{LABEL} data set {name}: changed since it was read, replaced")
        covered = last if covered is None else max(covered, last)
    if covered is None:
        ctx.notes.append(f"{LABEL} data sets: none covers {start} to {end}")
    else:
        ctx.notes.append(f"{LABEL} data sets: kept as raw up to {covered}, not typed yet; the forms walk takes the "
                         "days after it")
    return covered
