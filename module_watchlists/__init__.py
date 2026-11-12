"""Watch lists: avo's own named lists of symbols, with ordered sections (#786)."""

#: Nothing from the data layer yet: this package only writes and reads its own file
#: (``boards/watchlists.duckdb``). The board PR that draws the columns adds the reads it needs
#: (prices, fundamentals, market series) here.
#: Plain assignment, not an annotated one: ``boards.declared_reads`` reads this file with ``ast``
#: and only looks at ``ast.Assign``, so ``READS: tuple[...] = ()`` would read as no declaration.
READS = ()
