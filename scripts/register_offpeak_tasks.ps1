# Install the off-peak jobs (RC-Relatedness-OffPeak, RC-ValueChain-Sync) in Windows Task Scheduler.
#
# Thin caller: the registry, the schedule and the installer live in core/automations.py (the
# Settings board's Automations panel installs the same rows). The CLIs are themselves
# off-peak-gated (core/offpeak.py) and core/job_lock.py prevents overlaps. Nothing installs
# unless you run this or press Install on the panel.
#
#   powershell -ExecutionPolicy Bypass -File scripts/register_offpeak_tasks.ps1
#   powershell ... -File scripts/register_offpeak_tasks.ps1 -Remove
#
# -DryRun is gone: the installer has one source now. To see what would be installed and the
# live state, open the Automations panel or run `python -m RevenueChaser.core.automations list`.

param([switch]$Remove)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$workDir = Split-Path -Parent $repo
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$action = if ($Remove) { "remove" } else { "install" }
$rc = 0
Push-Location $workDir
try {
    foreach ($name in "RC-Relatedness-OffPeak", "RC-ValueChain-Sync") {
        & $py -m RevenueChaser.core.automations $action $name
        if ($LASTEXITCODE -ne 0) { $rc = $LASTEXITCODE }
    }
} finally { Pop-Location }
exit $rc
