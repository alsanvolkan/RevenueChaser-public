# Revenue Chaser - start the dashboard (127.0.0.1:8000) with no console window.
# The desktop shortcut runs this hidden through launch.pyw; run_dashboard.bat runs it in a visible window.
# The dashboard itself (app/dashboard.py main) opens its app window and replaces a stale server.
# A dashboard that is already running is stopped first through stop.ps1, which leaves it alone while a job
# is queued or running: a running server holds its log files open, and Windows will not rename them.
# Messages also go to .cache\logs\launcher.log, which the Logs tab shows; the dashboard's stdout and stderr
# go to dashboard.log and dashboard.err.log there, and each start keeps the previous run as *.prev.log.
# PowerShell 5.1 compatible: plain if/else only. ASCII only (5.1 reads a BOM-less script as ANSI).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File start.ps1   (exit 1 when it did not start)

$repo = $PSScriptRoot                  # ...\RevenueChaser, the package folder
$root = Split-Path -Parent $repo       # its parent: the working directory and PYTHONPATH
$url = "http://127.0.0.1:8000"
$readySeconds = 120

# core/config.py LOG_DIR: OUTPUT_DIR\logs, and OUTPUT_DIR is RC_CACHE_DIR when it is set, else the repo's .cache.
if ($env:RC_CACHE_DIR) { $cacheDir = $env:RC_CACHE_DIR } else { $cacheDir = Join-Path $repo ".cache" }
$logDir = Join-Path $cacheDir "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$launcherLog = Join-Path $logDir "launcher.log"

function Keep-PreviousRun($path) {
    # dashboard.err.log -> dashboard.err.prev.log (app/logview.py prev_name reads the same name)
    if (Test-Path -LiteralPath $path) {
        Move-Item -LiteralPath $path -Destination ([IO.Path]::ChangeExtension($path, ".prev.log")) -Force -ErrorAction SilentlyContinue
    }
}

function Say($text, $color) {
    if ($color) { Write-Host $text -ForegroundColor $color } else { Write-Host $text }
    Add-Content -LiteralPath $launcherLog -Value ("{0:HH:mm:ss} {1}" -f (Get-Date), $text) -Encoding UTF8
}

trap {
    Say "start.ps1 failed: $_" "Red"
    exit 1
}

Keep-PreviousRun $launcherLog
Say "Starting the Revenue Chaser dashboard"

# The interpreter, picked as run_dashboard.bat used to: the repo's .venv, the parent's .venv, then python on PATH.
$python = $null
foreach ($candidate in @((Join-Path $repo ".venv\Scripts\python.exe"), (Join-Path $root ".venv\Scripts\python.exe"))) {
    if (Test-Path -LiteralPath $candidate) { $python = $candidate; break }
}
if (-not $python) {
    $found = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $python = $found.Source }
}
if (-not $python) {
    Say "No Python found: no .venv in $repo or $root, and no python on PATH." "Red"
    exit 1
}

# An old dashboard keeps its log files open, so stop it first. stop.ps1 refuses while a job runs (exit 2).
$global:LASTEXITCODE = 0
$stopLines = & (Join-Path $repo "stop.ps1") *>&1 | ForEach-Object { "$_" }
$stopCode = $LASTEXITCODE
foreach ($line in $stopLines) { Say $line }
if ($stopCode -eq 2) {
    Say "The running dashboard was left as it is; opening it in the browser." "Yellow"
    Start-Process "$url/"
    exit 0
}
if ($stopCode -ne 0) {
    Say "The old dashboard was not stopped, so a new one cannot start (see above)." "Red"
    exit 1
}

$out = Join-Path $logDir "dashboard.log"
$err = Join-Path $logDir "dashboard.err.log"
Keep-PreviousRun $out
Keep-PreviousRun $err

$env:PYTHONPATH = $root
# A fatal crash (like the 2026-10-01 access violation in python314.dll) then leaves every thread's Python
# stack in dashboard.err.log instead of vanishing without a trace.
$env:PYTHONFAULTHANDLER = "1"
# Unbuffered, so the Logs tab sees print() output as it happens, and UTF-8, so it reads it right.
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$proc = Start-Process -FilePath $python -ArgumentList "-m", "RevenueChaser.app.dashboard" `
    -WorkingDirectory $root -RedirectStandardOutput $out -RedirectStandardError $err `
    -WindowStyle Hidden -PassThru -ErrorAction Stop
$null = $proc.Handle   # keeps ExitCode readable after the process has ended
Say "Dashboard starting on $url/ with $python (log: $err)"

$ready = $false
$deadline = (Get-Date).AddSeconds($readySeconds)
while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) {
        # For example port 8000 taken by another program (main() exits 2): say why, so the message box shows it.
        Say "The dashboard exited early (exit code $($proc.ExitCode)). Last lines of ${err}:" "Red"
        Get-Content -LiteralPath $err -Tail 8 -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Say "  $_" }
        exit 1
    }
    try {
        $manifest = Invoke-RestMethod -Uri "$url/manifest.json" -TimeoutSec 2 -ErrorAction Stop
        if ($manifest.short_name -eq "REVENUE CHASER") { $ready = $true; break }
    } catch {
        # not up yet
    }
    Start-Sleep -Milliseconds 500
}
if (-not $ready) {
    Say "The dashboard did not answer on $url/ within $readySeconds s - see $err" "Red"
    exit 1
}
Say "Dashboard is up"
exit 0
