"""Relatedness board configuration (200-ticker spike).

Business similarity from 10-K Item 1 / 20-F Item 4 text: LLM structured profile
per company, then a local cosine over the profile attributes. See
docs/plan_200ticker.md.
"""
from __future__ import annotations

import os

from RevenueChaser.core.config import OUTPUT_DIR
from RevenueChaser.module_value_chain import config as vc_config

RELATEDNESS_LATEST = OUTPUT_DIR / "relatedness_latest.html"
RELATEDNESS_JSON = OUTPUT_DIR / "relatedness" / "relatedness_latest.json"

# Pilot: seed names + a sector-diverse fill from the liquid universe.
PILOT_N = 200
THINKING_SAMPLE = 20          # first N companies also profiled in thinking mode
MAX_INPUT_CHARS = 12_000      # cost control: first chars of Item 1/4
# The floor under which a filing text is not sent to the writer at all (#313). Value Chain's own
# MIN_ITEM_CHARS, so "this is a sliced Item 1, not a table-of-contents fragment" means the same thing on both
# boards. A model asked from a one-line description does not refuse; it writes the profile from the company
# name (measured on the live store 2026-10-30: 20 of 1,503 pilot names carry an empty Item 1 and 58 one under
# this floor, AAP's being 1 character).
MIN_INPUT_CHARS = vc_config.MIN_ITEM_CHARS

# Profile mode: thinking-high only (see docs/plan_rest.md Phase 6 and
# the private notes validation/validation_thinking_vs_nonthinking_50.md). Thinking costs more tokens
# (reasoning counts against max_tokens) but produces richer, more stable peers.
THINKING = True
REASONING_EFFORT = "high"
# v2 (#388 D5): the prompt names the company (``equity_universe.name``) instead of the ticker. Every v1 profile reads
# stale and is redone by the off-peak runs within their $ cap.
PROMPT_VERSION = "v2"

# $ hard-stop: cap the number of LLM calls per run (~1.5x the expected spend).
EST_USD_PER_CALL = 0.00143          # thinking-high, measured
USD_CAP = float(os.getenv("RC_RELATEDNESS_USD_CAP", "8.0"))
LLM_WORKERS = 8
# Grok writer (#388): a run whose first N replies hold no usable profile stops (``unusable``) before it counts an
# attempt on any group; the endpoint or the model is then wrong, not the companies.
GROK_UNUSABLE_STOP = 5

# Fixed profile schema. Similarity is defined on these fields only.
PROFILE_FIELDS = (
    "products", "end_markets", "business_model",
    "customer_types", "value_chain_role", "keywords",
)
FIELD_WEIGHTS = {
    "products": 1.0,
    "end_markets": 1.0,
    "business_model": 0.8,
    "customer_types": 0.8,
    "value_chain_role": 0.6,
    "keywords": 0.4,
}
TOP_PEERS = 25
