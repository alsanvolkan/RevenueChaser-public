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

# The floor under which a filing text is not sent to the writer at all (#313). Value Chain's own
# MIN_ITEM_CHARS, so "this is a sliced Item 1, not a table-of-contents fragment" means the same thing on both
# boards. A model asked from a one-line description does not refuse; it writes the profile from the company
# name (measured on the live store 2026-10-30: 20 of 1,503 pilot names carry an empty Item 1 and 58 one under
# this floor, AAP's being 1 character).
MIN_INPUT_CHARS = vc_config.MIN_ITEM_CHARS

# v3 (#400, Relatedness v2 Step 1): one structured JSON profile from the whole Item 1 / Item 4 (no input cut), read
# in chunks above 60k characters, JSON mode and no thinking (``profile_v3``). Every v2 profile reads stale and is
# redone by the next run of the board's writer. v2 (#388 D5) named the company instead of the ticker.
# v3b (#400 10-K package, avo "basla" 10 Nov 2026): the B input -- the new Item 1 cut, without its non-business
# sections, with the filing's customer sentences and XBRL customers in the prompt. Every v3 profile reads stale.
# v3c (#717, avo "prompt degisikligini de yap" 11 Nov 2026): a customer or supplier also carries the listed
# ``parent`` the text names and whether the text presents it as ``public``. Bumped before R1 on purpose: it makes
# the 388 profiles of today stale (about $1), where the same change after R1 would restale ~4,750 (about $14).
PROMPT_VERSION = "v3c"

# $ hard-stop: cap the number of LLM calls per run (~1.5x the expected spend).
# The cap's cost per call, measured on deepseek-flash: the 10 Nov 2026 pilot wrote 400 names in 668 calls for about
# 1 USD. It replaces the 0.006 the cap carried before, which was "about 4x" of the v2 figure, guessed until the pilot
# could measure it (review of PR #557). A chunked filing is several calls, each counted.
EST_USD_PER_CALL = 0.0018
# A full round is every company with usable text: about 4,750 names at 1.67 calls each, so ~7,900 calls, ~14 USD
# (#712). The cap sits above that so one run covers the round instead of stopping six times and looking finished.
USD_CAP = float(os.getenv("RC_RELATEDNESS_USD_CAP", "25.0"))
LLM_WORKERS = 8
# The writer comparison's Claude calls at a time (avo, 11 Nov 2026: "8li paralel"); the other writers go one by one.
COMPARE_WORKERS = LLM_WORKERS
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
# Similarity v2 (#669, plan 2026-10-30 Step 2): score = 0.6 s_do + 0.25 s_chain + 0.15 s_struct, with
# s_chain = 0.6 type Jaccard + 0.4 shared-name bonus. Starting weights from the Fable review (section 4.3);
# the validation set (Step 4) tunes them and writes the chosen values here with the run id.
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_CACHE = OUTPUT_DIR / "fastembed"
W_DO, W_CHAIN, W_STRUCT = 0.6, 0.25, 0.15
W_CHAIN_TYPES, W_CHAIN_NAMES = 0.6, 0.4
TOP_PEERS = 25
# A peer is shown only with at least this many shared tags (#395, review RL-3). No score floor: measured 4 Nov 2026
# on 93 DeepSeek profiles, floor 0.10 -> hand P@10 0.459, 0.02 -> 0.726, none -> 0.728 / recall@25 0.799; a floor
# hides true peers because the idf lowers the cosine scale.
PEER_MIN_SCORE = 0.0
PEER_MIN_SHARED = 2
