# Revenue Chaser - stop the dashboard that start.ps1 runs hidden on 127.0.0.1:8000.
# Refuses unless the dashboard says idle (GET /api/idle): no job queued or running, no job lock held by any
# process, no other process's live marker in either store dir. Stopping the server would cut a job off (and drop
# the queue): wait until it finishes, or pass -Force. Never stops processes by name; only the port-8000
# listener whose command line runs RevenueChaser.app.dashboard.
# PowerShell 5.1 compatible, ASCII only.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File stop.ps1 [-Force]
# Exit codes (start.ps1 reads them): 0 stopped, or not running; 1 not stopped (another program holds the
# port, the dashboard did not answer, or it did not stop); 2 not stopped because the dashboard is not idle.

param([switch]$Force)

$port = 8000

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $listener) {
    Write-Host "The Revenue Chaser dashboard is not running."
    exit 0
}

$procId = $listener.OwningProcess
$cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId").CommandLine
if (-not ($cmd -and $cmd -match 'RevenueChaser\.app\.dashboard')) {
    Write-Host "Port $port is used by another program (PID $procId), not the Revenue Chaser dashboard. It was left running."
    exit 1
}

if (-not $Force) {
    try {
        $state = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/idle" -TimeoutSec 5 -ErrorAction Stop
    } catch {
        # Fail closed: without an answer we cannot tell whether a job would be cut off.
        Write-Host "Could not ask the dashboard whether it is idle ($($_.Exception.Message)). Run stop.ps1 -Force to stop it anyway."
        exit 1
    }
    if ($state.idle -ne $true) {
        $why = (@($state.reasons) | ForEach-Object { "$_" }) -join "; "
        Write-Host "Not idle (a job queued or running, or a store writer at work): $why. Wait until it finishes, or run stop.ps1 -Force."
        exit 2
    }
}

Stop-Process -Id $procId -Force

$still = $true
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    $still = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $still) { break }
    Start-Sleep -Milliseconds 250
}
if ($still) {
    Write-Host "The dashboard (PID $procId) still holds port $port after 10 s."
    exit 1
}

Write-Host "Stopped the Revenue Chaser dashboard (PID $procId)."
exit 0
