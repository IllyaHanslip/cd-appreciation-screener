# Registers a Windows scheduled task that runs the daily price fetch in the background.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_daily_task.ps1   (as administrator)
#
# Runs daily at 12:00 (no window; Windows notifications when it starts and finishes). If the
# PC is off or asleep at 12:00, it runs as soon as possible afterwards. The job skips work
# already done today, so extra runs are harmless. Output: logs\daily.log
# Remove it with scripts\uninstall_daily_task.ps1, or in Task Scheduler (taskschd.msc).

param([string]$Time = "12:00")

$ErrorActionPreference = "Stop"
$TaskName = "CD Screener daily prices"
$Project = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Project ".venv\Scripts\pythonw.exe"   # pythonw = no console window
if (-not (Test-Path $Pythonw)) { throw "Virtual environment not found at $Pythonw" }

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m cd_screener daily" -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 30)
# Interactive = runs in your session (so notifications can appear); no password stored.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Fetches Discogs prices for the CD Appreciation Screener ($Project). Log: logs\daily.log" | Out-Null

Write-Host "Installed '$TaskName': daily at $Time, or as soon as possible after if the PC was off."
Write-Host "Log file: $Project\logs\daily.log"
