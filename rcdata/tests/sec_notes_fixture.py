"""Inline-XBRL documents for the note-block reader (#753), each one a shape the 20-filing sample actually holds.

Written out rather than captured, for two reasons: a real annual report is 3 to 15 MB of HTML, and the shapes that
matter are a few tags each. The sample's own measurements (``2026-11-12_1110_sec-annual-report-census.md``) say
which shapes those are: a note continued over several elements, a note tagged inside another note, a note tagged
inside a continuation, a fact with no period end, both taxonomies' spellings, a filer's own prefix, and the cover
facts that are tagged the same way and are not notes.

Every document is deliberately small and ugly: unclosed ``<p>``, an attribute in the other case, text split by
tags. A real filer's HTML is worse than this, not better.
"""
from __future__ import annotations

#: The ``ix:header`` every filing opens with: the contexts, and the cover facts that are not note blocks.
HEADER = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
<body><div style="display:none"><ix:header><ix:resources>
  <xbrli:context id="FY"><xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate>
    <xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="COVER"><xbrli:period><xbrli:instant>2026-02-20</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:context id="NODATE"><xbrli:period></xbrli:period></xbrli:context>
</ix:resources></ix:header>
<ix:nonNumeric contextRef="COVER" name="dei:TradingSymbol">ACME</ix:nonNumeric>
<ix:nonNumeric contextRef="COVER" name="dei:SecurityExchangeName">NYSE</ix:nonNumeric>
<ix:nonNumeric contextRef="COVER" name="dei:EntityFilerCategory">Large Accelerated Filer</ix:nonNumeric>
</div>"""

#: One note whose text runs over three elements: the block, then two continuations. The chain is the text.
CONTINUED = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="d2">
  <p>Note 7. Debt</p><p>We borrowed money.</p></ix:nonNumeric>
<p>A paragraph of the page that belongs to no block.</p>
<ix:continuation id="d2" continuedAt="d3"><p>The term loan matures in 2029.</p></ix:continuation>
<ix:continuation id="d3"><p>The revolver is undrawn.</p></ix:continuation>
</body></html>"""

#: A note tagged inside another note: the filer tags its debt note and then its term-loan subsection within it.
NESTED = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock">
  <p>Note 7. Debt</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock"><p>The term loan matures in 2029.</p>
  </ix:nonNumeric>
  <p>That is all.</p></ix:nonNumeric>
</body></html>"""

#: A note tagged inside a continuation of another note -- the same nesting, one element further along the chain.
NESTED_IN_CONTINUATION = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="c2">
  <p>Note 7. Debt</p></ix:nonNumeric>
<ix:continuation id="c2"><p>Continued.</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock"><p>The term loan matures in 2029.</p>
  </ix:nonNumeric></ix:continuation>
</body></html>"""

#: A chain that points at an id the document does not hold, and one that points back at itself.
BROKEN_CHAIN = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:CommitmentsDisclosureTextBlock" continuedAt="gone">
  <p>We have commitments.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock" continuedAt="l2"><p>We lease.</p></ix:nonNumeric>
<ix:continuation id="l2" continuedAt="l2"><p>And lease again.</p></ix:continuation>
</body></html>"""

#: A block whose context has no end date, and a block the filer marked nil.
NO_PERIOD_END = HEADER + """
<ix:nonNumeric contextRef="NODATE" name="us-gaap:IncomeTaxDisclosureTextBlock"><p>We pay tax.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:SegmentReportingDisclosureTextBlock" xsi:nil="true"/>
</body></html>"""

#: An IFRS 20-F: ``DisclosureOf...Explanatory`` is a disclosure, ``DescriptionOfAccountingPolicy...Explanatory`` a
#: policy, ``...TableTextBlock`` a table, and the filer's own prefix is kept as filed.
IFRS_AND_PREFIXES = HEADER + """
<ix:nonNumeric contextRef="FY" name="ifrs-full:DisclosureOfBorrowingsExplanatory"><p>We borrowed.</p>
</ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="ifrs-full:DescriptionOfAccountingPolicyForBorrowingsExplanatory">
  <p>We measure borrowings at amortised cost.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:SignificantAccountingPoliciesTextBlock"><p>Our policies.</p>
</ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:ScheduleOfDebtTableTextBlock"><p>Debt table.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="acme:OurOwnDisclosureTextBlock"><p>Our own note.</p></ix:nonNumeric>
</body></html>"""

#: The filer escaped the note's own markup, so the parser hands the tags over as text.
ESCAPED = HEADER + """
<ix:nonNumeric contextRef="FY" escape="true" name="us-gaap:DebtDisclosureTextBlock">
&lt;p&gt;Note 7. Debt&lt;/p&gt;&lt;p&gt;We borrowed&amp;nbsp;money.&lt;/p&gt;</ix:nonNumeric>
</body></html>"""

#: A note tagged inside a cover fact: the cover fact is not a row, so it cannot be a parent.
INSIDE_A_COVER_FACT = HEADER + """
<ix:nonNumeric contextRef="COVER" name="dei:AuditorName">Some Auditors LLP
  <ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>We borrowed money.</p>
  </ix:nonNumeric></ix:nonNumeric>
</body></html>"""

#: A document with no inline XBRL at all: 0.6% of the stored annual reports (the census).
NO_IXBRL = "<html><body><p>Item 1. Business</p><p>We make widgets.</p></body></html>"
