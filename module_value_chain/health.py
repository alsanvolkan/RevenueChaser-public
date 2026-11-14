"""The board's SEC EDGAR call accounting (#299).

The Value Chain board renders the amber "data incomplete" banner
(``report.render_value_chain_dashboard`` -> ``provider_health_html``), but nothing built a
:class:`~RevenueChaser.core.providers.result.ProviderHealth`, so the banner was dead code and the run's
``result_runs.provider_health`` stayed NULL: a failed SEC fetch was invisible on the board and unrecorded.

One counter for the whole module, as ``module_fundamentals.pipeline`` does it: the request sites in
``edgar_text`` count every call, a Fetch or a sync resets it before it starts, and the payload carries
``health()``. A Refresh makes no request, so it leaves the field out rather than overwriting a run's banner
with zeros.
"""
from __future__ import annotations

from RevenueChaser.core.providers.result import ProviderHealth

EDGAR = ProviderHealth("SEC EDGAR")   # the name module_fundamentals already shows in the banner
