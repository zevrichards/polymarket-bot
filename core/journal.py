"""Append-only trade journal shared by all three bots.

Two files, both gitignored:
  logs/trades.jsonl   -- one JSON object per line, one line per trade
  logs/learnings.md   -- dated free-text observations (for the future nightly
                          self-review step described in the plan; not wired
                          to an automated rewrite loop yet)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
TRADES_PATH = LOG_DIR / "trades.jsonl"
LEARNINGS_PATH = LOG_DIR / "learnings.md"


def log_trade(bot: str, **fields) -> dict:
    """Append a trade record. Returns the record that was written."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bot": bot,
        **fields,
    }
    with TRADES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def read_trades(bot: str | None = None) -> list[dict]:
    if not TRADES_PATH.exists():
        return []
    trades = []
    with TRADES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if bot is None or record.get("bot") == bot:
                trades.append(record)
    return trades


def append_learning(bot: str, text: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    with LEARNINGS_PATH.open("a", encoding="utf-8") as f:
        f.write(f"## {timestamp} - {bot}\n{text}\n\n")
