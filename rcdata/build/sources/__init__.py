"""The market-wide series sources of ``market_series``: one module per provider, each with the same four names.

- ``SOURCE`` / ``DATASET_DIR``: the raw and Parquet folder, ``<source>/<dataset>``.
- ``fetch(ctx, as_of)``: the daily raw files (skipped when they exist) as ``[(raw path, meta)]``-producing calls
  through ``market_series.save_raw``; returns the raw paths.
- ``backfill(ctx, start, end)``: the history; returns the raw paths.
- ``parse(text, meta)``: the rows of one raw file, keyed ``(series_id, obs_date)``, without provenance.

Every base URL is an ``RC_*_BASE`` environment variable with the real endpoint as its default
(docs/data_retrieval.md).
"""
