#!/usr/bin/env bash
#
# app.sh — single entry point for RevenueChaser.
#
#   ./app.sh dashboard          start the Flask dashboard on :8000
#   ./app.sh test               run the whole test suite
#   ./app.sh menu               interactive CLI (Earnings Radar / Rotation Map / tests)
#   ./app.sh help               full command list
#
# Everything runs through the repo's own .venv, so no activation is needed and
# the script works from any working directory.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || command -v python)"

cd "$REPO"

# ── output helpers ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'; RST=$'\033[0m'
else
  B=""; DIM=""; GRN=""; YLW=""; RED=""; RST=""
fi
say()  { printf '%s\n' "${DIM}▸ $*${RST}"; }
die()  { printf '%s\n' "${RED}✗ $*${RST}" >&2; exit 1; }

run() { say "$PY $*"; exec "$PY" "$@"; }

require_key() {
  [[ -n "${UNUSUAL_WHALES_API_KEY:-}" ]] && return 0
  [[ -f "$REPO/.env" ]] && grep -q '^UNUSUAL_WHALES_API_KEY=.' "$REPO/.env" && return 0
  printf '%s\n' "${YLW}! UNUSUAL_WHALES_API_KEY is not set (env or .env) — this command needs live UW data.${RST}" >&2
}

usage() {
  cat <<EOF
${B}RevenueChaser${RST} — ./app.sh <command> [args]

${B}Run${RST}
  dashboard [port]        Flask dashboard (default port 8000, opens a browser)
  serve [port]            same, but do not open a browser
  menu                    interactive CLI menu (main.py)

${B}Test${RST}
  test [pytest args]      full suite (900+ tests); extra args pass through
  test-v                  verbose, one line per test
  test-x                  stop at the first failure
  test-fail               re-run only last run's failures
  list                    collect tests without running them

${B}Modules${RST}
  radar                   Earnings Radar via the interactive menu
  rotation                Sector/Theme Rotation Map (RRG)
  spx --asof YYYY-MM-DD   SPX critical thresholds report
  fin --ticker AAPL ...   Fin Research engines (DCF / 3stmt / comps / ...)

${B}LEAPS${RST}
  leaps                   LEAPS accumulation scan

${B}Utility${RST}
  shell                   python REPL with the package importable
  deps                    reinstall requirements into .venv
  migrate-raw-data [flags] one-time import of rawData files + SQLite DBs into raw.duckdb
                           flags: --purge (full cleanup) | --delete | --reset [--force] | --dataset {name,...}
                           see scripts/README.md
  clean                   remove __pycache__, .pytest_cache and stale test scratch
  clean-scratch [--apply] test scratch a killed run left in the temp dir (report without --apply)
  help                    this message

${DIM}Interpreter: $PY${RST}
EOF
}

cmd="${1:-help}"; shift || true

case "$cmd" in
  # ── run ─────────────────────────────────────────────────────────────────────
  dashboard|dash|web)
    port="${1:-8000}"
    say "dashboard -> http://localhost:$port"
    exec "$PY" -c "from RevenueChaser.app.dashboard import main; main(port=$port, open_browser=True)"
    ;;
  serve)
    port="${1:-8000}"
    say "dashboard (no browser) -> http://localhost:$port"
    exec "$PY" -c "from RevenueChaser.app.dashboard import main; main(port=$port, open_browser=False)"
    ;;
  menu)      require_key; run -m RevenueChaser.main ;;
  radar)     require_key; run -m RevenueChaser.main ;;
  rotation)  require_key; run -m RevenueChaser.module_sector_rotation ;;
  spx)       require_key; run -m RevenueChaser.module_spx_gamma "$@" ;;
  fin)       run -m RevenueChaser.module_financials "$@" ;;

  # ── test ────────────────────────────────────────────────────────────────────
  test|tests)  run -m pytest "$REPO" -q "$@" ;;
  test-v)      run -m pytest "$REPO" -v "$@" ;;
  test-x)      run -m pytest "$REPO" -q -x "$@" ;;
  test-fail)   run -m pytest "$REPO" -q --last-failed "$@" ;;
  list)        run -m pytest "$REPO" -q --collect-only "$@" ;;

  # End-to-end: boots a real dashboard subprocess against a throwaway
  # .store/.cache with every provider pointed at a local mock server. Opt-in
  # because it is slower than the unit suite and starts real processes.
  test-e2e)
    export RC_E2E=1; run -m pytest "$REPO/module_testing/e2e" -q -m e2e "$@" ;;
  test-e2e-v)
    export RC_E2E=1; run -m pytest "$REPO/module_testing/e2e" -v -m e2e "$@" ;;
  # Adds the tier that hits the REAL provider APIs (needs working credentials).
  test-e2e-live)
    export RC_E2E=1; run -m pytest "$REPO/module_testing/e2e" -q -m e2e --live "$@" ;;

  # ── LEAPS ───────────────────────────────────────────────────────────────────
  leaps)
    require_key
    run -m RevenueChaser.module_leaps.scan "$@"
    ;;

  # ── utility ─────────────────────────────────────────────────────────────────
  shell|repl)  run ;;
  migrate-raw-data)
    run -m scripts.migrate_rawdata_to_duckdb "$@"
    ;;
  deps)
    say "pip install -r requirements.txt"
    "$PY" -m pip install -r "$REPO/requirements.txt"
    "$PY" -m pip install -e "$REPO" 2>/dev/null || true
    printf '%s\n' "${GRN}✓ dependencies installed${RST}"
    ;;
  clean)
    find "$REPO" -path "$REPO/.venv" -prune -o -name __pycache__ -type d -print0 2>/dev/null | xargs -0 rm -rf 2>/dev/null || true
    rm -rf "$REPO/.pytest_cache"
    printf '%s\n' "${GRN}✓ cleaned __pycache__ and .pytest_cache${RST}"
    # Scratch a killed run left behind; a run in progress is protected by the age rule.
    run -m RevenueChaser.scripts.clean_test_scratch --apply
    ;;
  clean-scratch)
    run -m RevenueChaser.scripts.clean_test_scratch "$@"
    ;;
  help|-h|--help) usage ;;
  *) printf '%s\n' "${RED}✗ unknown command: $cmd${RST}" >&2; echo; usage; exit 2 ;;
esac
