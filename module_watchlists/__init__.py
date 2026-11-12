"""Watch lists: avo's own named lists of symbols, with ordered sections (#786)."""

#: The needs the board reads for its columns (``rcdata.needs.boards``). The lists themselves are this package's
#: own file (``boards/watchlists.duckdb``) and no need at all; these are the numbers shown beside them: the
#: spine's company names, the prices store's bars and newest market cap, and the market-wide series a row like
#: VIX or the 10-year yield stands for.
#: Plain assignment, not an annotated one: ``boards.declared_reads`` reads this file with ``ast``
#: and only looks at ``ast.Assign``, so ``READS: tuple[...] = ()`` would read as no declaration.
READS = ("identity.security", "market.series", "prices.bars", "prices.latest")
