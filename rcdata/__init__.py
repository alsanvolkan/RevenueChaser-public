"""rcdata — the market store's one library (#206 F1/D1, #217 F12/D4).

A separate builder process (``python -m RevenueChaser.rcdata.build``) fetches raw files to the HDD
(``config.MARKET_RAW_DIR``), types them to Parquet, builds each serving DuckDB file as a new file and swaps it in
(``serving.swap``); the dashboard opens serving files read-only (``serving.open_read``) and boards read them only
through ``read``. Operational state (the request queue, the check rows, the call counter) is SQLite WAL in
``ops.sqlite`` beside the serving files, owned by ``ops``. See docs/data_architecture.md, "The market store".

The write side (``build``) runs only in the builder; nothing a page does writes here.
"""
