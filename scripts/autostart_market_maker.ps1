# Launched by the "PolymarketBot_MarketMaker" scheduled task (trigger: at logon)
# so market_maker_bot survives a PC reboot without manual relaunch.
# Does NOT survive a Claude app auto-update (no logon event fires for that) --
# see BUILD_INTELLIGENCE_REPORT.md Session 12.
$root = "C:\Users\CSI\polymarket-bot"
$py = "$root\.venv\Scripts\python.exe"

Start-Process -FilePath $py -ArgumentList "-u","-m","bots.market_maker_bot" `
    -WorkingDirectory $root `
    -RedirectStandardOutput "$root\logs\market_maker_bot_run.log" `
    -RedirectStandardError "$root\logs\market_maker_bot_run.err.log" `
    -WindowStyle Hidden
