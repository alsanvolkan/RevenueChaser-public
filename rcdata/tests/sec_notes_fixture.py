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

# ── shapes the third review round showed nothing reached ───────────────────────────────────────────────────────

#: The ordinary shape of a nested note, and the one this file was missing: a child with a continuation of its own,
#: inside its parent, with a paragraph of the parent between the two. Round 2's fix reported two faults on it --
#: ``continuation_overlap`` and ``child_text_outside_parent`` -- so on a real filing, where this is the common
#: case, every row would have carried a complaint.
HEALTHY_NESTED_CHILD_CHAIN = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>Note 7. Debt</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock" continuedAt="h2"><p>Term loan.</p>
  </ix:nonNumeric><p>A parent paragraph in between.</p>
  <ix:continuation id="h2"><p>Term loan continued.</p></ix:continuation></ix:nonNumeric>""" + _END

#: The overlap one level down: the element is not inside another *block*, it is inside another block's
#: *continuation*. Same double count, and round 2's check -- which asked only about block frames -- said nothing.
CHAIN_INSIDE_ANOTHER_BLOCKS_CONTINUATION = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="over2"><p>Debt.</p>
  </ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock" continuedAt="L2"><p>Leases.</p></ix:nonNumeric>
<ix:continuation id="L2"><p>Leases continued.</p>
  <ix:continuation id="over2"><p>Text inside the leases chain.</p></ix:continuation></ix:continuation>""" + _END

#: Kept block, empty block, kept child: the only shape where "nearest kept ancestor" differs from "top-level".
KEPT_GRANDPARENT_EMPTY_PARENT = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>Note 7. Debt</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:ScheduleOfDebtTableTextBlock"><ix:nonNumeric contextRef="FY"
    name="us-gaap:LongTermDebtTextBlock" continuedAt="g9"></ix:nonNumeric></ix:nonNumeric></ix:nonNumeric>
<ix:continuation id="g9"><p>Term loan.</p></ix:continuation>""" + _END

#: Two kept blocks, each tagged inside the continuation the other claims, so each is the other's parent and the
#: filing has no top-level row at all: its whole text counts as zero characters.
MUTUAL_PARENT_BLOCKS = HEADER + """
<ix:continuation id="d1"><ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock" continuedAt="d2">
  <p>Leases.</p></ix:nonNumeric></ix:continuation>
<ix:continuation id="d2"><ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"
  continuedAt="d1"><p>Debt.</p></ix:nonNumeric></ix:continuation>""" + _END

#: A block claiming the continuation it is itself tagged inside, which makes the owner map name the block itself.
BLOCK_CLAIMING_THE_CONTINUATION_IT_SITS_IN = HEADER + """
<ix:continuation id="cz"><p>Text of the continuation.</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock">
    <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock" continuedAt="cz"></ix:nonNumeric>
  </ix:nonNumeric></ix:continuation>""" + _END

#: Three blocks claiming one continuation. ``continuation_shared`` counts the continuation, not the blocks that
#: reached it: the paragraph is one piece of text in three rows, however many rows that is.
THREE_BLOCKS_ONE_CONTINUATION = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="s1"><p>A.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock" continuedAt="s1"><p>B.</p></ix:nonNumeric>
<ix:nonNumeric contextRef="FY" name="us-gaap:SegmentReportingDisclosureTextBlock" continuedAt="s1">
  <p>C.</p></ix:nonNumeric>
<ix:continuation id="s1"><p>Shared.</p></ix:continuation>""" + _END

#: An escaped note as a filer really writes one: attributes on the tags, a table, an escaped stylesheet, a URL
#: whose ``&copy=`` must not become a copyright sign, and a ``<td nowrap>`` the tag pattern refuses on
#: purpose -- widening it to take a valueless attribute would also take ``<Table of Contents>``, which is prose.
#: The tags were escaped once by the filer, so their own entities arrive escaped twice (``&amp;amp;nbsp;``) --
#: but the URL's ampersand was escaped only once, which is what filers do, and the second pass has to leave it
#: alone: ``html.unescape`` over the whole string takes the legacy name ``&copy`` without its semicolon.
ESCAPED_WITH_ATTRIBUTES = HEADER + """
<ix:nonNumeric contextRef="FY" escape="true" name="us-gaap:DebtDisclosureTextBlock">
&lt;style&gt;.n{font-weight:bold}&lt;/style&gt;&lt;p style="margin:0"&gt;Note 7. Debt&lt;/p&gt;
&lt;p&gt;Filed&amp;nbsp;at x.test/f?id=1&amp;copy=2 by &lt;a href="https://x.test"&gt;us&lt;/a&gt;.&lt;/p&gt;
&lt;table&gt;&lt;tr&gt;&lt;td nowrap&gt;Term loan&lt;/td&gt;
&lt;td align="right"&gt;500&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;
&lt;span class="x"&gt;That is &lt;b&gt;all&lt;/b&gt;.&lt;/span&gt;</ix:nonNumeric>""" + _END

#: A cover fact and a ``<style>`` the filer never closed. Each is its own silence: the first swallows every later
#: block as a child of something that is not a row, the second puts ``html.parser`` in CDATA mode so the rest of
#: the document is never read at all.
UNCLOSED_COVER_FACT = HEADER + """
<ix:nonNumeric contextRef="COVER" name="dei:AuditorName">
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>Note 7. Debt</p></ix:nonNumeric>""" + _END
UNCLOSED_STYLE = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>Note 7. Debt</p>
<style>.n{font-weight:bold}
<p>Never read, because the parser is still in the stylesheet.</p>""" + _END

# ── shapes the fourth review round showed nothing reached ──────────────────────────────────────────────────────

#: A long note over three pages, with a sub-block whose table starts on page two and ends on page three. The
#: child's chain is inside the *parent's continuation*, not inside the parent's own element -- which is what
#: every multi-page note looks like, and what round 3's fix reported as ``continuation_overlap``.
MULTI_PAGE_NOTE_WITH_A_SUB_BLOCK = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="g2"><p>Note 7. Debt</p>
  </ix:nonNumeric>
<ix:continuation id="g2" continuedAt="g3"><p>Page two.</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:ScheduleOfDebtTableTextBlock" continuedAt="c2"><p>Table top.</p>
  </ix:nonNumeric></ix:continuation>
<ix:continuation id="g3"><ix:continuation id="c2"><p>Table bottom.</p></ix:continuation>
  <p>Page three.</p></ix:continuation>""" + _END

#: Parent and child continued by two elements that wrap the same text, with the child's outermost. Nothing in
#: the spec orders those two, and round 3's fix answered differently depending on which came first.
COEXTENSIVE_CONTINUATIONS = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="g2"><p>Debt.</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:ScheduleOfDebtTableTextBlock" continuedAt="c2"><p>Table top.</p>
  </ix:nonNumeric></ix:nonNumeric>
<ix:continuation id="c2"><ix:continuation id="g2"><p>Table bottom.</p></ix:continuation></ix:continuation>""" + _END

#: A grandchild whose chain leaves its parent but stays inside the grandparent. Its words are in a top-level row,
#: so the acceptance sum is not short and nothing should be counted.
CHILD_CHAIN_INSIDE_THE_GRANDPARENT = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="G"><p>Note 7. Debt</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:ScheduleOfDebtTableTextBlock"><p>Table.</p>
    <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock" continuedAt="k"><p>Loan.</p>
    </ix:nonNumeric></ix:nonNumeric></ix:nonNumeric>
<ix:continuation id="G"><ix:continuation id="k"><p>Loan continued.</p></ix:continuation></ix:continuation>""" + _END

#: A child continued inside one of the parent's own paragraphs, so the parent reads it as one sentence and the
#: child is stored as two lines. The difference is the stitch's newline and nothing else.
CHILD_CONTINUED_INSIDE_ONE_PARAGRAPH = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock">Note 7. The loan
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock" continuedAt="s2">bears interest
  </ix:nonNumeric><ix:continuation id="s2">at 5%.</ix:continuation> It matures in 2029.
  </ix:nonNumeric>""" + _END

#: A child whose out-of-parent continuation carries no text at all: nothing is missing from the row above it.
EMPTY_CHAIN_ELEMENT_OUTSIDE_THE_PARENT = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"><p>Note 7. Debt</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock" continuedAt="c2"><p>Term loan.</p>
  </ix:nonNumeric></ix:nonNumeric>
<ix:continuation id="c2"></ix:continuation>""" + _END

#: A row that merely *leads into* a cycle: 0 -> 1 -> 2 -> 1. Breaking it at the row with the lowest ``seq``
#: rather than at a row on the cycle leaves the cycle standing and counts row 0's text twice.
TAIL_INTO_A_PARENT_CYCLE = HEADER + """
<ix:continuation id="x"><ix:nonNumeric contextRef="FY" name="us-gaap:LeasesTextBlock"><p>Zero.</p>
  </ix:nonNumeric></ix:continuation>
<ix:continuation id="d1"><ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock"
  continuedAt="d2"><p>One.</p></ix:nonNumeric></ix:continuation>
<ix:continuation id="d2" continuedAt="x"><ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock"
  continuedAt="d1"><p>Two.</p></ix:nonNumeric></ix:continuation>""" + _END

#: The chain's first element nested inside its second, so the second's own text already holds the first's: one
#: paragraph lands in the row twice. The mirror of ``CONTINUATION_INSIDE_ITS_OWN_BLOCK``, and the direction the
#: doubling guard could not see.
CHAIN_ELEMENT_ENCLOSING_AN_EARLIER_ONE = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="e"><p>Note 7. Debt</p>
  </ix:nonNumeric>
<ix:continuation id="k"><p>Outer.</p>
  <ix:continuation id="e" continuedAt="k"><p>Inner paragraph.</p></ix:continuation></ix:continuation>""" + _END

#: An escaped note that is clean, and the only case that asserts so: its own prose brackets arrive escaped twice,
#: a multi-line ``<STYLE type="text/css">`` and an ``<article>`` are markup and go, and two stylesheets have a
#: paragraph between them that must survive. Nothing tag-shaped is left over, so ``escaped_markup_residue`` must
#: stay silent -- which is what the row being *clean* means, as against merely readable.
CLEAN_ESCAPED_NOTE = HEADER + """
<ix:nonNumeric contextRef="FY" escape="true" name="us-gaap:DebtDisclosureTextBlock">
&lt;STYLE type="text/css"&gt;.n{
font-weight:bold}&lt;/style&gt;&lt;article&gt;&lt;p&gt;A floor of &amp;lt;LIBOR + 2%&amp;gt; applies.&lt;/p&gt;
&lt;p&gt;It holds while a &lt; b, whatever the filer escaped.&lt;/p&gt;&lt;style&gt;.m{}&lt;/style&gt;
&lt;p&gt;That is all.&lt;/p&gt;&lt;/article&gt;</ix:nonNumeric>""" + _END

#: An escaped note that mentions ``<style-guide>``, which is a word a note really uses and not an opening tag,
#: and that holds an escaped ``<script>`` as well as an escaped ``<style>``. The word must survive: a stripper
#: whose opening is "``<style``, then anything" would take it and eat the sentence up to the real stylesheet's
#: close. It stays in the text, and because it is tag-shaped the row is counted as not clean, which is honest --
#: this row is readable and we know it carries something we did not take out.
ESCAPED_NOTE_MENTIONING_A_STYLE_TAG = HEADER + """
<ix:nonNumeric contextRef="FY" escape="true" name="us-gaap:DebtDisclosureTextBlock">
&lt;p&gt;See our &lt;style-guide&gt; for house style.&lt;/p&gt;
&lt;style&gt;.m{font-weight:bold}&lt;/style&gt;&lt;script&gt;alert(1)&lt;/script&gt;
&lt;p&gt;That is all.&lt;/p&gt;</ix:nonNumeric>""" + _END
#: An escaped note whose markup still carries a ``<!DOCTYPE``. It is not a tag the drop takes, so it stays in the
#: text and the row is counted as not clean: readable, and known to carry something nothing took out.
ESCAPED_WITH_A_DOCTYPE = HEADER + """
<ix:nonNumeric contextRef="FY" escape="true" name="us-gaap:DebtDisclosureTextBlock">
&lt;!DOCTYPE html&gt;&lt;p&gt;Note 7. Debt&lt;/p&gt;</ix:nonNumeric>""" + _END

# ── shapes the fifth review round showed nothing reached ───────────────────────────────────────────────────────

#: A chain element that encloses the **block itself**, not an earlier element of its chain: the block's own text is
#: then inside the element and the row stores it twice. The guard saw one direction only, and this document --
#: unlike ``BLOCK_CLAIMING_THE_CONTINUATION_IT_SITS_IN`` -- has a block that cuts text of its own, which is what
#: makes the doubling real rather than a parent with nothing in it.
CHAIN_ELEMENT_ENCLOSING_ITS_OWN_BLOCK = HEADER + """
<ix:continuation id="k2"><ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="k2">
  <p>Note 7. Debt of 500.</p></ix:nonNumeric><p>Tail.</p></ix:continuation>""" + _END

#: An escaped note holding a Turkish dotted capital I. ``"İ".lower()`` is **two** code points, so an offset
#: found in a lower-cased copy of the text lands one character late for every one of them before it -- which ate
#: the ``$1`` out of ``$1,250 million`` and reported the row clean. The amount is the test.
ESCAPED_NOTE_WITH_A_DOTTED_CAPITAL_I = HEADER + """
<ix:nonNumeric contextRef="FY" escape="true" name="us-gaap:DebtDisclosureTextBlock">
&lt;p&gt;Turkcell İletişim and İş Bankası&lt;/p&gt;&lt;style&gt;.n{}&lt;/style&gt;$1,250 million
&lt;p&gt;End.&lt;/p&gt;</ix:nonNumeric>""" + _END

#: An escaped ``<script>`` nothing closes, and after it a ``<style>`` that *is* closed. Ending the scan at the
#: first unclosed opening stored the stylesheet as the note's prose; skipping it keeps both answers right and the
#: scan linear. The unclosed script's own text stays, because nothing in the document says where it ends.
ESCAPED_NOTE_WITH_AN_UNCLOSED_SCRIPT = HEADER + """
<ix:nonNumeric contextRef="FY" escape="true" name="us-gaap:DebtDisclosureTextBlock">
&lt;p&gt;Note 7.&lt;/p&gt;&lt;script&gt;var x;&lt;p&gt;Debt is 500.&lt;/p&gt;
&lt;style&gt;.n{color:red}&lt;/style&gt;&lt;p&gt;End.&lt;/p&gt;</ix:nonNumeric>""" + _END

#: An escaped ``<style>`` whose only closing carries the *other* name, and a ``</style >`` with a space in it. The
#: first must not be treated as closed by ``</script>``; the second must still close.
ESCAPED_CLOSINGS_THAT_DO_NOT_MATCH = HEADER + """
<ix:nonNumeric contextRef="FY" escape="true" name="us-gaap:DebtDisclosureTextBlock">
&lt;p&gt;Note 7.&lt;/p&gt;&lt;style&gt;.a{}&lt;/script&gt;&lt;p&gt;Debt is 500.&lt;/p&gt;
&lt;style&gt;.b{}&lt;/style &gt;&lt;p&gt;End.&lt;/p&gt;</ix:nonNumeric>""" + _END

#: An escaped note opening with an XML declaration, which is not a tag the pattern takes and so stays in the text.
ESCAPED_WITH_AN_XML_DECLARATION = HEADER + """
<ix:nonNumeric contextRef="FY" escape="true" name="us-gaap:DebtDisclosureTextBlock">
&lt;?xml version="1.0"?&gt;&lt;p&gt;Note 7. Debt&lt;/p&gt;</ix:nonNumeric>""" + _END

#: Four levels, with the deepest block's chain ending in the **top-level** note's own continuation. Nothing else
#: here goes deeper than three, so nothing else would notice a lineage walk that stopped two levels up -- and that
#: is the shape four versions of ``continuation_overlap`` got wrong, each time on a document that is fine.
FOUR_DEEP_NESTING_WITH_A_CHAIN = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="t2"><p>Note 7. Debt</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock"><p>Term loans.</p>
    <ix:nonNumeric contextRef="FY" name="us-gaap:ScheduleOfDebtTableTextBlock"><p>Schedule.</p>
      <ix:nonNumeric contextRef="FY" name="us-gaap:DebtInstrumentTextBlock" continuedAt="d4"><p>Facility A.</p>
      </ix:nonNumeric></ix:nonNumeric></ix:nonNumeric></ix:nonNumeric>
<ix:continuation id="t2"><p>Page two.</p>
  <ix:continuation id="d4"><p>Facility A continued.</p></ix:continuation></ix:continuation>""" + _END

#: Two continuations wrapping the same text with the **grandchild's** outermost: a row's line is not only its
#: children but everything below it, however deep.
COEXTENSIVE_WITH_THE_GRANDCHILD_OUTERMOST = HEADER + """
<ix:nonNumeric contextRef="FY" name="us-gaap:DebtDisclosureTextBlock" continuedAt="g5"><p>Debt.</p>
  <ix:nonNumeric contextRef="FY" name="us-gaap:LongTermDebtTextBlock"><p>Term loans.</p>
    <ix:nonNumeric contextRef="FY" name="us-gaap:ScheduleOfDebtTableTextBlock" continuedAt="c5"><p>Schedule.</p>
    </ix:nonNumeric></ix:nonNumeric></ix:nonNumeric>
<ix:continuation id="c5"><ix:continuation id="g5"><p>Last page.</p></ix:continuation></ix:continuation>""" + _END
