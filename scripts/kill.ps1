# Kill switch: halts the lag_bot and blocks all new position entries.
#
# Creates logs/KILL_SWITCH, which lag_bot checks before every new entry.
# Existing open positions remain monitored for stop-loss and resolution --
# the bot keeps running but refuses to open anything new.
#
# To resume trading: run scripts/unkill.ps1
# To completely stop the process too: run scripts/kill.ps1 -StopProcess

param(
    [switch]$StopProcess
)

$RepoRoot     = Split-Path $PSScriptRoot -Parent
$KillFile     = "$RepoRoot\logs\KILL_SWITCH"
$WatchLog     = "$RepoRoot\logs\watchdog.log"

New-Item -ItemType Directory -Force -Path "$RepoRoot\logs" | Out-Null
Set-Content -Path $KillFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') kill switch activated"

Write-Host "KILL SWITCH ACTIVATED" -ForegroundColor Red
Write-Host "lag_bot will not open any new positions."
Write-Host "Existing positions continue to be monitored."
Write-Host ""
Write-Host "Run scripts/unkill.ps1 to resume trading."

Add-Content -Path $WatchLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  *** KILL SWITCH ACTIVATED ***"

if ($StopProcess) {
    Write-Host ""
    Write-Host "Stopping lag_bot process..." -ForegroundColor Yellow
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'bots\.lag_bot' }
    if ($procs) {
        foreach ($proc in $procs) {
            Stop-Process -Id $proc.ProcessId -Force
            Write-Host "Stopped PID $($proc.ProcessId)"
        }
        Add-Content -Path $WatchLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  lag_bot process stopped via kill switch"
    } else {
        Write-Host "No running lag_bot process found."
    }
}
