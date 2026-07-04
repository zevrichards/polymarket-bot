# Polymarket bot watchdog -- checks every 60s, relaunches dead bots,
# sends a Windows toast notification and logs each restart.
# Run at startup via the Startup folder .bat (same mechanism as the bots themselves).

param(
    [int]$IntervalSeconds = 60
)

$RepoRoot  = Split-Path $PSScriptRoot -Parent
$PythonExe = "$RepoRoot\.venv\Scripts\python.exe"
$LogDir    = "$RepoRoot\logs"
$WatchLog  = "$LogDir\watchdog.log"

$Bots = @(
    @{
        Name   = "lag_bot"
        Module = "bots.lag_bot"
        Stdout = "$LogDir\lag_bot_run.log"
        Stderr = "$LogDir\lag_bot_run.err.log"
    }
)

function Write-Log {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $Message"
    Add-Content -Path $WatchLog -Value $line
}

function Send-Toast {
    param([string]$Title, [string]$Body)
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
        $xml.LoadXml("<toast><visual><binding template=`"ToastGeneric`"><text>$Title</text><text>$Body</text></binding></visual></toast>")
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Polymarket Watchdog")
        $notifier.Show([Windows.UI.Notifications.ToastNotification]::new($xml))
    } catch {
        # Toast failed (e.g. no desktop session) -- log only, don't crash the watchdog
    }
}

function Get-BotProcess {
    param([string]$Module)
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match [regex]::Escape($Module) } |
        Select-Object -First 1
}

function Start-Bot {
    param([hashtable]$Bot)
    Start-Process -FilePath $PythonExe `
                  -ArgumentList "-u", "-m", $Bot.Module `
                  -WorkingDirectory $RepoRoot `
                  -RedirectStandardOutput $Bot.Stdout `
                  -RedirectStandardError  $Bot.Stderr `
                  -WindowStyle Hidden
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Self-deduplication: if another watchdog is already running, exit immediately.
# Without this, manual relaunches or a race at startup create multiple instances
# that all fire at the same time, each seeing "bot dead" and each launching a
# copy -- producing the duplicates we saw in practice.
$selfProcs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like '*watchdog.ps1*' -and $_.ProcessId -ne $PID }
if ($selfProcs) {
    Write-Log "another watchdog already running (PID $($selfProcs[0].ProcessId)) -- exiting"
    exit 0
}

Write-Log "watchdog started (interval=${IntervalSeconds}s)"
Send-Toast "Polymarket Watchdog" "Watchdog started -- monitoring lag_bot and market_maker_bot"

# Check immediately on startup (don't wait 60s before the first launch).
$initialCheck = $true

while ($true) {
    foreach ($bot in $Bots) {
        $proc = Get-BotProcess -Module $bot.Module
        if (-not $proc) {
            $ts = Get-Date -Format "HH:mm:ss"
            Write-Log "$($bot.Name) not found -- relaunching"
            Start-Bot -Bot $bot
            if (-not $initialCheck) {
                Send-Toast "Polymarket Watchdog" "$($bot.Name) was dead. Relaunched at $ts."
            }
        }
    }
    $initialCheck = $false
    Start-Sleep -Seconds $IntervalSeconds
}
