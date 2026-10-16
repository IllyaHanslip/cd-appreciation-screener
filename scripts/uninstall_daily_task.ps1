# Removes the scheduled task created by install_daily_task.ps1.
#
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall_daily_task.ps1

$TaskName = "CD Screener daily prices"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'."
} else {
    Write-Host "'$TaskName' is not installed."
}
