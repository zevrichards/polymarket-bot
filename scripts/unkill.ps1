# Remove the kill switch, allowing lag_bot to resume entering new positions.
# The watchdog will relaunch lag_bot if it isn't running.

$RepoRoot = Split-Path $PSScriptRoot -Parent
$KillFile = "$RepoRoot\logs\KILL_SWITCH"
$WatchLog = "$RepoRoot\logs\watchdog.log"

if (Test-Path $KillFile) {
    Remove-Item $KillFile -Force
    Write-Host "Kill switch removed. lag_bot will resume entering positions." -ForegroundColor Green
    Add-Content -Path $WatchLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  kill switch removed -- trading resumed"
} else {
    Write-Host "Kill switch was not active."
}
