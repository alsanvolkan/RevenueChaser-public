"""Welcome board: the opening screen. Its rows live in its own ``boards/welcome.duckdb`` (DECISIONS S9, S16)."""

#: The needs this board reads (``rcdata.needs.boards``). #643 PR 5: the four market tiles. The earnings card
#: (``earnings.next``, ``identity.security``) and the freshness card (ops ``job_runs``) join it in PR 5b and PR 6,
#: which is why ``earnings_calendar`` and ``job_runs`` are still in the old-link list.
READS = ("macro.dgs10", "market.series", "market.spx_close", "market.vix")
