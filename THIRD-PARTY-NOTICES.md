# Third-party notices

## Code bundled in this repository

| Component | Version | Licence | Notice |
|---|---|---|---|
| [plotly.js](https://github.com/plotly/plotly.js) | 2.35.2 | MIT | [`app/static/vendor/plotly.min.js.LICENSE.txt`](app/static/vendor/plotly.min.js.LICENSE.txt) |
| [Tabler Icons](https://github.com/tabler/tabler-icons) | outline paths, as drawn in the v28 design | MIT | the source icons of the Settings / Data board (`module_settings/control.py`, `ICONS`); Copyright (c) 2020-2024 Paweł Kuna, MIT licence |

Everything else in this repository is the project's own code, under the MIT
licence in [`LICENSE`](LICENSE).

## Python dependencies

The packages in `requirements.txt` are installed from PyPI, not vendored here.
Each keeps its own licence; none is redistributed by this repository.

## Data sources

This repository contains **code only**. It ships no market data, and the
durable store (`.store/`) and the regenerable caches (`.cache/`) are
gitignored. A few small, hand-trimmed response samples live under
`*/tests/fixtures/` so the offline test suite has something to parse; they are
there to exercise parsers, not to republish a data set.

The code can call the following services. Each has its own terms, and using
this code against them is the operator's own responsibility, under their own
account and key:

| Service | Used for | Key needed |
|---|---|---|
| [Unusual Whales](https://unusualwhales.com/api) | options flow, screener, earnings calendar | yes |
| [SEC EDGAR](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | company facts (XBRL), 13F filings | no (a User-Agent is required) |
| [FRED](https://fred.stlouisfed.org/docs/api/fred/) | macro series | yes |
| [yfinance](https://github.com/ranaroussi/yfinance) | free price and profile fallbacks | no |
| [OpenFIGI](https://www.openfigi.com/api) | CUSIP to ticker mapping | optional |
| [DeepSeek](https://platform.deepseek.com/) | optional LLM profile extraction | yes |

No key is stored in this repository. `.env` is gitignored; `.env.example`
lists the variable names with empty values.
