# Creates "CD Screener" shortcuts on the Desktop and in the Start menu.
# Double-clicking one starts the dashboard and opens it in your browser.
#
#   powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
#
# To pin it to the taskbar: right-click the Start-menu entry > Pin to taskbar.

$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Project ".venv\Scripts\python.exe"
$Icon = Join-Path $Project "assets\cd_screener.ico"
if (-not (Test-Path $Python)) { throw "Virtual environment not found at $Python" }
if (-not (Test-Path $Icon)) { & $Python (Join-Path $Project "scripts\make_icon.py") }

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "CD Screener.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "CD Screener.lnk")
)
foreach ($path in $targets) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $Python
    $link.Arguments = "-m cd_screener open"
    $link.WorkingDirectory = $Project
    $link.IconLocation = "$Icon,0"
    $link.Description = "CD Appreciation Screener - open the dashboard"
    $link.WindowStyle = 7   # start the server window minimised; the browser opens on its own
    $link.Save()
    Write-Host "Created $path"
}
