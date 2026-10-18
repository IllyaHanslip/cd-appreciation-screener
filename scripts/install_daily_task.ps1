# Registers a Windows scheduled task that runs the daily price fetch in the background.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_daily_task.ps1   (as administrator)
#
# Runs daily at 12:00 (no window; Windows notifications when it starts and finishes). If the
# PC is off or asleep at 12:00, it runs as soon as possible afterwards: the task also starts
# when the PC wakes up, when you log on, and every hour until midnight. Before 12:00, or once
# the day's run has finished, those extra starts do nothing. Output: logs\daily.log
# Remove it with scripts\uninstall_daily_task.ps1, or in Task Scheduler (taskschd.msc).

param([string]$Time = "12:00")

$ErrorActionPreference = "Stop"
$TaskName = "CD Screener daily prices"
$Project = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Project ".venv\Scripts\pythonw.exe"   # pythonw = no console window
if (-not (Test-Path $Pythonw)) { throw "Virtual environment not found at $Pythonw" }
$User = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m cd_screener daily --not-before $Time" `
    -WorkingDirectory $Project

# 1. Every day at $Time, then hourly until midnight (catches a missed or failed run).
$start = [datetime]::Today.Add([timespan]::Parse($Time))
$daily = New-ScheduledTaskTrigger -Daily -At $start
# Local time with no UTC offset, so it stays at $Time when the clocks change (BST/GMT).
$daily.StartBoundary = $start.ToString("yyyy-MM-dd'T'HH:mm:ss")
$daily.Repetition = (New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration ($start.Date.AddDays(1) - $start)).Repetition

# 2. When you log on, after 2 minutes.
$logon = New-ScheduledTaskTrigger -AtLogOn -User $User
$logon.Delay = "PT2M"

# 3. When the PC wakes from sleep ("returned from a low power state"), after 2 minutes so the
#    network and the clock have caught up.
$eventTrigger = Get-CimClass -Namespace "Root/Microsoft/Windows/TaskScheduler" -ClassName MSFT_TaskEventTrigger
$wake = New-CimInstance -CimClass $eventTrigger -ClientOnly
$wake.Enabled = $true
$wake.Delay = "PT2M"
$wake.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">' +
    "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]" +
    '</Select></Query></QueryList>'

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 30)
# Interactive = runs in your session (so notifications can appear); no password stored.
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($daily, $logon, $wake) `
    -Settings $settings -Principal $principal -Force -ErrorAction Stop `
    -Description "Fetches Discogs prices for the CD Appreciation Screener ($Project). Log: logs\daily.log" | Out-Null

Write-Host "Installed '$TaskName': daily at $Time. If the PC was off or asleep then, it runs"
Write-Host "when it wakes up, when you log on, or at the next hour - once a day, never before $Time."
Write-Host "Log file: $Project\logs\daily.log"
