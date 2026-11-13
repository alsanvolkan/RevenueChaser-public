"""Watch lists: avo's own named lists of symbols, with ordered sections (#786)."""

#: The needs the board reads for its columns (``rcdata.needs.boards``). The lists themselves are this package's
#: own file (``boards/watchlists.duckdb``) and no need at all; these are the numbers shown beside them: the
#: spine's company names, the prices store's bars, newest market cap and dividends, the market-wide series a row
#: like VIX or the 10-year yield stands for, and the SEC statements the Financials tab reads.
#: Plain assignment, not an annotated one: ``boards.declared_reads`` reads this file with ``ast``
#: and only looks at ``ast.Assign``, so ``READS: tuple[...] = ()`` would read as no declaration.
READS = ("fundamentals.companies", "fundamentals.statements", "identity.security", "market.series", "prices.actions",
         "prices.bars", "prices.latest", "prices.security_ids")
