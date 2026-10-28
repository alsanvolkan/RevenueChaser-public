"""The identity spine (build plan PR 5): who each listed security is, which tier it is in, its CUSIP, FIGI and GICS,
and which index holds it, served from ``spine.duckdb``.

Six source datasets fetch raw files and type them to Parquet; none of them writes a serving table of its own. Each
one's ``build`` loads its Parquet into TEMP staging tables of the build connection (never stored in the file), and
the last dataset of the kind, ``spine/security``, builds the four served tables and the bridge views from them:

- ``sec/company_tickers_exchange`` (weekly, If-Modified-Since): the SEC ticker and exchange file, the spine's start;
- ``wrds/sec_idhist``, ``wrds/co_hgic``, ``wrds/idxcst_his`` (attended: a person logs in to WRDS and confirms Duo;
  one connection per builder process): Compustat's company and security rows with their identifier history (the
  CIK-CUSIP link), the GICS history, and the S&P index constituents history (the membership check);
- ``openfigi/map`` (weekly): the CUSIPs not mapped yet, under 25 requests a minute;
- ``funds/index_holdings`` (daily): the ETF holdings files of SSGA, iShares and Vanguard and Nasdaq's list API, the
  index membership;
- ``spine/security``: no fetch; tier rule v1 (``tier.py``), the served tables ``security``, ``security_history``,
  ``gics_history`` and ``index_membership``, the views under the old ``raw.duckdb`` names (D4), and the build-issue
  rules (``issues.py``), whose rows go to ``ops.sqlite`` (D6), synced once the new file is live.

``build_kind`` builds every dataset of the kind in ``DATASETS`` order, so the sources' staging tables exist when
``spine/security`` builds. A source that never ran has empty staging tables, never a missing one.
"""
from __future__ import annotations

KIND = "spine"
