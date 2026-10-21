# Points the desktop shortcut "Revenue Chaser" at launch.pyw, run by the venv's pythonw.exe, so the dashboard
# opens with no console window. Creates the shortcut, or repoints an existing one of that name (for example one
# that ran run_dashboard.bat). Run once on this machine:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
# pythonw.exe is picked as start.ps1 picks python.exe: the repo's .venv, the parent's .venv, then PATH.

$repo = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $repo
$pythonw = $null
foreach ($candidate in @((Join-Path $repo ".venv\Scripts\pythonw.exe"), (Join-Path $root ".venv\Scripts\pythonw.exe"))) {
    if (Test-Path -LiteralPath $candidate) { $pythonw = $candidate; break }
}
if (-not $pythonw) {
    $found = Get-Command pythonw -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $pythonw = $found.Source }
}
if (-not $pythonw) {
    Write-Host "pythonw.exe not found (no .venv in $repo or $root, none on PATH) - create the venv first." -ForegroundColor Red
    exit 1
}

$path = Join-Path ([Environment]::GetFolderPath("Desktop")) "Revenue Chaser.lnk"
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
$lnk.TargetPath = $pythonw
$lnk.Arguments = '"' + (Join-Path $repo "launch.pyw") + '"'
$lnk.WorkingDirectory = $repo
$lnk.Description = "Revenue Chaser dashboard (no console window)"
$icon = Join-Path $repo "app\static\icons\icon.ico"
if (Test-Path -LiteralPath $icon) { $lnk.IconLocation = "$icon,0" }
$lnk.Save()
Write-Host "Shortcut ready: $path -> $pythonw launch.pyw"
