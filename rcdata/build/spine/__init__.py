"""The identity spine (build plan PR 5): who each listed security is, which tier it is in, its CUSIP, FIGI and GICS,
and which index holds it, served from ``spine.duckdb``.

Nine source datasets fetch raw files and type them to Parquet. Each one's ``build`` loads its Parquet into TEMP
staging tables of the build connection (never stored in the file); only ``sec/company_tickers_mf`` serves a table
of its own (``fund``), and the last dataset of the kind, ``spine/security``, builds the five served tables and
the bridge views from the staging tables:

- ``sec/company_tickers_exchange`` (weekly, If-Modified-Since): the SEC ticker and exchange file, the spine's start;
- ``sec/company_tickers_mf`` (weekly, If-Modified-Since): the SEC fund file, one row per series and share class,
  served as ``fund``. An ETF is a series of a trust, so the equity file has no row for it; the file has no ETF
  field either, so ``is_etf`` carries the rule that decided it. The ETF universe was ``fund`` plus the ``etp``
  rows of ``security`` (SPY is a unit investment trust and GLD a commodity trust: neither is in the fund file);
  it is served as ``etf`` now (#346);
- ``wrds/sec_idhist``, ``wrds/co_hgic``, ``wrds/idxcst_his`` (attended: a person logs in to WRDS and confirms Duo;
  one connection per builder process): Compustat's company and security rows with their identifier history (the
  CIK-CUSIP link), the GICS history, and the S&P index constituents history (the membership check);
- ``openfigi/map`` (weekly): the CUSIPs not mapped yet, under 25 requests a minute;
- ``funds/index_holdings`` (daily): the ETF holdings files of SSGA, iShares and Vanguard and Nasdaq's list API, the
  index membership;
- ``funds/etf_holdings`` (daily): the rotation board's ETFs read whole, from the SPDR and iShares files and UW's
  holdings answer for the themes whose issuer has no keyless file; it serves ``etf_holdings`` itself (no vote);
- ``alphavantage/listing_status`` (daily, keyed) and ``sec/ncen`` (monthly): Alpha Vantage's list of every US
  listing and the SEC's Form N-CEN data sets, the two halves of the ETF list (#346);
- ``spine/security``: no fetch; tier rule v1 (``tier.py``), the served tables ``security``, ``security_history``,
  ``gics_history``, ``index_membership``, ``etf`` (the ETF universe, ``etf.py``) and ``my_universe`` with
  ``my_universe_changes`` (the names we keep and who entered or left, ``my_universe.py``, avo's rules of
  4 Nov 2026), the views under the old ``raw.duckdb`` names (D4), and the build-issue rules
  (``security.ISSUE_KINDS``), whose rows go to ``ops.sqlite`` (D6), synced once the new file is live.

``build_kind`` builds every dataset of the kind in ``DATASETS`` order, so the sources' staging tables exist when
``spine/security`` builds. A source that never ran has empty staging tables, never a missing one.
"""
from __future__ import annotations

KIND = "spine"
