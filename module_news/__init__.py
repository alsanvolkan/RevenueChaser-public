"""News board: today's US economic calendar, the day's highlighted release and the market headlines.

Its results live in its own ``boards/news.duckdb`` (DECISIONS S9, S16), written only by this package, incrementally.
"""

#: The need this board reads (``rcdata.needs.boards``). The calendar comes from the builder's hourly
#: ``uw/economic_calendar`` (#643 PR 3, #684): no old table is read. The headlines and the X lane are the board's
#: own live tape (#673, #674, #681, #686) and are written into its board file, not served by the builder, so they
#: are not needs: the board keeps its IBKR and Grok calls by design, as the Settings record says.
READS = ("market.economic_calendar",)
