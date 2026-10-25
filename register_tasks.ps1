# Install the daily store backup (RC-Store-Backup, 04:00 local) in Windows Task Scheduler.
#
# Thin caller: the registry, the task settings (runs on battery, wakes the PC, catches up a
# missed run) and the installer live in core/automations.py. The Settings board's
# Automations panel does the same with its Install button. Nothing installs unless you run
# this or press that button.
#
# The backup snapshots every durable store file (raw, edgar, history and prices DuckDB), zips
# the edgar_docs tree, mirrors the SPX Gamma tree incrementally (only new or changed files)
# and keeps the 5 newest raw snapshots, plus a copy of the first raw snapshot
# of each ISO week (one per week, in weekly/; the newest 12 are kept).
#
# Usage:  powershell -ExecutionPolicy Bypass -File register_tasks.ps1 [-Remove]

param([switch]$Remove)

$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path        # ...\RevenueChaser
$Root = Split-Path -Parent $Repo                                 # package parent
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$Action = if ($Remove) { "remove" } else { "install" }
Push-Location $Root
try { & $Python -m RevenueChaser.core.automations $Action RC-Store-Backup; $rc = $LASTEXITCODE }
finally { Pop-Location }
exit $rc
