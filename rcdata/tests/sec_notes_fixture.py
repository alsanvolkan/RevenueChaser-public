"""Inline-XBRL documents for the note-block reader (#753), each one a shape the 20-filing sample holds -- or a
shape the first review of PR 769 showed the reader got wrong and nothing here reached.

Written out rather than captured, for two reasons: a real annual report is 3 to 15 MB of HTML, and the shapes that
matter are a few tags each. The sample's own measurements (``2026-11-12_1110_sec-annual-report-census.md``) say
which shapes those are. The rest are faults: a chain that cannot be followed, a document that ends mid-block, two
elements with one id, prose a tag-stripper would eat.

The documents are deliberately ugly where a filer's are: an unclosed ``<p>``, an attribute in the other case, a
word split by a ``<span>``, a table with no whitespace between its cells. A real filer's HTML is worse than this,
not better.
"""
from __future__ import annotations

#: The ``ix:header`` every filing opens with: the contexts, and the cover facts that are not note blocks.
HEADER = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
<body><div style="display:none"><ix:header><ix:resources>
  <xbrli:context id="FY"><xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate>
    <xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="COVER"><xbrli:period><xbrli:instant>2026-02-20</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:context id="STARTONLY"><xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate>
    </xbrli:period></xbrli:context>
</ix:resources></ix:header>
<ix:nonNumeric contextRef="COVER" name="dei:TradingSymbol">ACME</ix:nonNumeric>
<ix:nonNumeric contextRef="COVER" name="dei:SecurityExchangeName">NYSE</ix:nonNumeric>
<ix:nonNumeric contextRef="COVER" name="dei:EntityFilerCategory">Large Accelerated Filer</ix:nonNumeric>
</div>"""
_END = "\n</body></html>"

#: One note whose text runs over three elements: the block, then two continuations. The chain is the text.
CONTINUED = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="d2">
  <p>Note 7. Debt</p><p>We borrowed money.</p></ix:nonNumeric>
<p>A paragraph of the page that belongs to no block.</p>
<ix:continuation id="d2" continuedAt="d3"><p>The term loan matures in 2029.</p></ix:continuation>
<ix:continuation id="d3"><p>The revolver is undrawn.</p></ix:continuation>""" + _END

#: The chain's element sits *before* the block that names it. Nothing in the spec forbids it.
CONTINUATION_BEFORE_ITS_BLOCK = HEADER + """
<ix:continuation id="early"><p>Continued text that comes first in the file.</p></ix:continuation>
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="early">
  <p>Note 7. Debt</p></ix:nonNumeric>""" + _END

#: A note tagged inside another note: the filer tags its debt note and then its term-loan subsection within it.
NESTED = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock">
  <p>Note 7. Debt</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock"><p>The term loan matures in 2029.</p>
  </ix:nonNumeric>
  <p>That is all.</p></ix:nonNumeric>""" + _END

#: A note tagged inside a continuation of another note -- the same nesting, one element further along the chain.
NESTED_IN_CONTINUATION = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="c2">
  <p>Note 7. Debt</p></ix:nonNumeric>
<ix:continuation id="c2"><p>Continued.</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock"><p>The term loan matures in 2029.</p>
  </ix:nonNumeric></ix:continuation>""" + _END

#: The inner note is in the chain's *second* element, so its parent is found only by following the chain twice.
NESTED_IN_SECOND_CONTINUATION = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="h1">
  <p>Note 7. Debt</p></ix:nonNumeric>
<ix:continuation id="h1" continuedAt="h2"><p>First continuation.</p></ix:continuation>
<ix:continuation id="h2"><p>Second continuation.</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock"><p>The term loan matures in 2029.</p>
  </ix:nonNumeric></ix:continuation>""" + _END

#: A chain element nested inside the block it continues, and one nested inside the element before it. Read once
#: through the enclosing frame, these must not be appended a second time.
CONTINUATION_INSIDE_ITS_OWN_BLOCK = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="k1">
  <p>Note 7. Debt</p><div><ix:continuation id="k1" continuedAt="k2"><p>Inner one.</p>
  <ix:continuation id="k2"><p>Inner two.</p></ix:continuation></ix:continuation></div></ix:nonNumeric>""" + _END

#: A chain that points at an id the document does not hold, and one that points back at itself.
BROKEN_CHAIN = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:CommitmentsDisclosureTextBlock" continuedAt="gone">
  <p>We have commitments.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock" continuedAt="l2"><p>We lease.</p></ix:nonNumeric>
<ix:continuation id="l2" continuedAt="l2"><p>And lease again.</p></ix:continuation>""" + _END

#: A two-step loop: p names q and q names p.
TWO_STEP_LOOP = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock" continuedAt="p1"><p>We lease.</p></ix:nonNumeric>
<ix:continuation id="p1" continuedAt="q1"><p>P.</p></ix:continuation>
<ix:continuation id="q1" continuedAt="p1"><p>Q.</p></ix:continuation>""" + _END

#: Two elements with one id (the second wins and the first is unreachable), and a continuation no chain names.
DUPLICATE_AND_ORPHAN_CONTINUATIONS = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="same">
  <p>Note 7. Debt</p></ix:nonNumeric>
<ix:continuation id="same"><p>First with this id.</p></ix:continuation>
<ix:continuation id="same"><p>Second with this id.</p></ix:continuation>
<ix:continuation id="claimed-by-nobody"><p>Text no block reaches.</p></ix:continuation>""" + _END

#: Two blocks naming one continuation: its text ends up in both, which the top-level sum would double.
SHARED_CONTINUATION = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="both">
  <p>Debt.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock" continuedAt="both"><p>Leases.</p></ix:nonNumeric>
<ix:continuation id="both"><p>Shared text.</p></ix:continuation>""" + _END

#: The document ends inside a block, as a truncated stored file does. Every later block becomes its child.
UNCLOSED_BLOCK = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>Note 7. Debt</p>
<p>A paragraph of the page.</p>
<ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock"><p>We lease.</p></ix:nonNumeric>
<p>Page footer.</p>"""

#: A block whose context has only a start date, one whose ``contextRef`` names no context at all, and a nil fact.
NO_PERIOD_END = HEADER + """
<ix:nonNumeric contextRef="STARTONLY" name="us-gaap:IncomeTaxDisclosureTextBlock"><p>We pay tax.</p>
</ix:nonNumeric>
<ix:nonNumeric contextRef="NOSUCHCONTEXT" name="us-gaap:LeasesTextBlock"><p>We lease.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:SegmentReportingDisclosureTextBlock" xsi:nil="true"/>""" + _END

#: An IFRS 20-F, both policy spellings, the table suffix in both numbers, and the filer's own prefix kept as filed.
IFRS_AND_PREFIXES = HEADER + """
<ix:nonNumeric contextRef="FY" name="ifrs-full:DisclosureOfBorrowingsExplanatory"><p>We borrowed.</p>
</ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="ifrs-full:DescriptionOfAccountingPolicyForBorrowingsExplanatory">
  <p>We measure borrowings at amortised cost.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="ifrs-full:DisclosureOfMaterialAccountingPolicyInformationExplanatory">
  <p>Our material accounting policies.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:SignificantAccountingPoliciesTextBlock"><p>Our policies.</p>
</ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:ScheduleOfDebtTableTextBlock"><p>Debt table.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:ScheduleOfLeasesTablesTextBlock"><p>Lease tables.</p>
</ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="acme:OurOwnDisclosureTextBlock"><p>Our own note.</p></ix:nonNumeric>""" + _END

#: The filer escaped the note's own markup, so the parser hands the tags over as text -- and its entities were
#: escaped twice, so ``&amp;nbsp;`` has to come back as a space.
ESCAPED = HEADER + """
<ix:nonNumeric contextRef="FY" escape="true" name="us-gaap:DebtDisclosureTextBlock">
&lt;p&gt;Note 7. Debt&lt;/p&gt;&lt;p&gt;We borrowed&amp;nbsp;money.&lt;/p&gt;</ix:nonNumeric>""" + _END

#: Angle brackets a note really contains. A reader that strips ``<...>`` from text eats the sentences between
#: them: the first version of this module turned this block into "Customers below 30%." and counted nothing.
PROSE_WITH_ANGLE_BRACKETS = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:ConcentrationRiskDisclosureTextBlock">
  <p>Customers below &lt; 5% of revenue are not named.</p>
  <p>The sentence between the brackets must survive.</p>
  <p>Gross margin was &gt; 30% in every segment.</p></ix:nonNumeric>""" + _END

#: A table with no whitespace between its cells, which is how a filer writes one.
TABLE_CELLS = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:ScheduleOfDebtTableTextBlock"><table><thead><tr><th>Instrument</th>
<th>2025</th><th>2024</th></tr></thead><tbody><tr><td>Term loan</td><td>500</td><td>600</td></tr>
<tr><td>Revolver</td><td>0</td><td>0</td></tr></tbody></table></ix:nonNumeric>""" + _END

#: ``ix:exclude`` is the spec's way of cutting the page's own furniture out of a fact's text.
EXCLUDED_PAGE_FURNITURE = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>Note 7. Debt</p>
  <ix:exclude><p>Acme Corporation | 2025 Form 10-K | 87</p></ix:exclude>
  <p>The term loan matures in 2029.</p></ix:nonNumeric>""" + _END

#: An unclosed ``<p>``, an attribute in the other case, a word split by a ``<span>``, and a ``<style>`` block
#: inside the note -- all four are things filers do, and none of them may change the words we store.
UGLY_MARKUP = HEADER + """
<ix:nonNumeric CONTEXTREF="FY" NAME="us-gaap:DebtDisclosureTextBlock"><style>.n{font-weight:bold}</style>
  <p>Note 7. Debt
  <p>The term <span>lo</span><span>an</span> matures in 2029.
  <div>That is all.</div></ix:nonNumeric>""" + _END

#: A filing whose inline prefix is not ``ix``. The spec binds the namespace, not the prefix.
OTHER_IX_PREFIX = (HEADER.replace("ix:", "ixbrl11:") + """
<ixbrl11:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>We borrowed money.</p>
</ixbrl11:nonNumeric>""" + _END)

#: A note tagged inside a cover fact: the cover fact is not a row, so it cannot be a parent.
INSIDE_A_COVER_FACT = HEADER + """
<ix:nonNumeric contextRef="COVER" name="dei:AuditorName">Some Auditors LLP
  <ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>We borrowed money.</p>
  </ix:nonNumeric></ix:nonNumeric>""" + _END

#: A document with no inline XBRL at all: 0.6% of the stored annual reports (the census).
NO_IXBRL = "<html><body><p>Item 1. Business</p><p>We make widgets.</p></body></html>"

# ── shapes the second review round showed nothing reached ──────────────────────────────────────────────────────

#: A chain element sitting inside a *different* kept block. Its words then belong to two top-level rows and the
#: document does not say which, so the stored total is long by that much.
CHAIN_ELEMENT_INSIDE_ANOTHER_BLOCK = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="over">
  <p>Debt.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock"><p>Leases.</p>
  <ix:continuation id="over"><p>Text inside the leases note.</p></ix:continuation></ix:nonNumeric>""" + _END

#: The other direction: a child's own chain reaches outside its parent, so part of the child's text is in no
#: top-level row and the stored total is short.
CHILD_CHAIN_LEAVES_ITS_PARENT = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="a2">
  <p>Debt.</p></ix:nonNumeric>
<ix:continuation id="a2"><p>Debt continued.</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock" continuedAt="b2"><p>Term loan.</p>
  </ix:nonNumeric></ix:continuation>
<ix:continuation id="b2"><p>Term loan continued.</p></ix:continuation>""" + _END

#: A parent that cuts no text of its own, so it is dropped as empty. Its child must not be left pointing at it.
#: The child's words are all in its continuation, which sits outside the parent, so the parent really does
#: enclose nothing -- a parent wrapped around a child with inline text would inherit that text and not be empty.
EMPTY_PARENT_BLOCK = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><ix:nonNumeric contextRef="FY"
  name="us-gaap:LongTermDebtTextBlock" continuedAt="c9"></ix:nonNumeric></ix:nonNumeric>
<ix:continuation id="c9"><p>The term loan matures in 2029.</p></ix:continuation>""" + _END

#: An ``ix:exclude`` the filer never closed: after it, nothing is collected and every later block comes out empty.
UNCLOSED_EXCLUDE = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>Note 7. Debt</p>
  <ix:exclude><p>Acme | 87</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock"><p>We lease.</p></ix:nonNumeric>""" + _END

#: The chain's second element nested inside its first, which is itself outside the block. The only shape that
#: pins the stitch's memory of what it has already read.
CHAIN_INSIDE_EARLIER_CHAIN_ELEMENT = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="e1">
  <p>Note 7. Debt</p></ix:nonNumeric>
<ix:continuation id="e1" continuedAt="e2"><p>One.</p>
  <ix:continuation id="e2"><p>Two.</p></ix:continuation></ix:continuation>""" + _END

#: A continuation with no id at all: nothing can name it, so its text reaches no block.
CONTINUATION_WITH_NO_ID = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="nowhere">
  <p>Note 7. Debt</p></ix:nonNumeric>
<ix:continuation><p>Text with no id to reach it by.</p></ix:continuation>""" + _END

#: Sentences that look like markup and are not. Every one of these was eaten by a version of this reader: the
#: first by an HTML stripper, the second by a tag pattern that allowed free text after the tag name.
PROSE_THAT_LOOKS_LIKE_MARKUP = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock">
  <p>Covenant holds where EBITDA&lt;x and leverage&gt;y for the period.</p>
  <p>The facility bears interest at a floor of &lt;LIBOR + 2%&gt;.</p>
  <p>We use &lt;b&gt; for bold in our own filings.</p>
  <p>Our site is &lt;www.example.com&gt; and the form was &lt;S-1&gt;.</p></ix:nonNumeric>""" + _END
