"""Co-movement board."""

#: The needs this board reads (``rcdata.needs.boards``; #642 PR 6): the shared universe and the index badges from the
#: spine, the bars, caps and corporate actions from the prices store.
READS = ("identity.membership", "identity.universe", "prices.actions", "prices.bars", "prices.market_cap")
