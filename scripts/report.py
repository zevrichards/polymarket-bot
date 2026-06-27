"""Prints a win/loss/PnL summary per bot from logs/trades.jsonl.

Run anytime after a bot has been scanning for a while:
    python -m scripts.report

Only counts trades that have actually settled (kind="resolution" records,
written by core/resolution.py once Polymarket reports a clear outcome).
Open/unresolved positions are reported separately so it's clear they're not
included in the win-rate or PnL totals yet.
"""
from __future__ import annotations

import json
from pathlib import Path

from core import journal

ROOT = Path(__file__).resolve().parent.parent
DIRECTIONAL_STATE = ROOT / "logs" / "paper_state.json"
ORACLE_STATE = ROOT / "logs" / "oracle_paper_state.json"
MM_STATE = ROOT / "logs" / "mm_state.json"
LAG_STATE = ROOT / "logs" / "lag_paper_state.json"


def _read_balance(state_path: Path, starting_balance: float = 100.0) -> float | None:
    if not state_path.exists():
        return None
    with state_path.open(encoding="utf-8") as f:
        return json.load(f).get("balance")


def report_directional(bot_name: str, state_path: Path) -> None:
    trades = journal.read_trades(bot_name)
    entries = [t for t in trades if t.get("kind") == "entry"]
    resolutions = [t for t in trades if t.get("kind") == "resolution"]
    stop_losses = [t for t in trades if t.get("kind") == "stop_loss_exit"]

    closed_token_ids = {r["token_id"] for r in resolutions} | {s["token_id"] for s in stop_losses}
    open_positions = [e for e in entries if e["token_id"] not in closed_token_ids]

    wins = sum(1 for r in resolutions if r.get("won"))
    losses = sum(1 for r in resolutions if not r.get("won"))
    resolution_pnl = sum(r.get("pnl", 0.0) for r in resolutions)
    stop_loss_pnl = sum(s.get("pnl", 0.0) for s in stop_losses)
    total_pnl = resolution_pnl + stop_loss_pnl
    balance = _read_balance(state_path)

    print(f"\n=== {bot_name} ===")
    print(f"  balance:          {'$%.2f' % balance if balance is not None else 'n/a (no state file yet)'}")
    print(f"  entries logged:   {len(entries)}")
    print(f"  resolved:         {len(resolutions)}  (W {wins} / L {losses})")
    if resolutions:
        win_rate = wins / len(resolutions)
        print(f"  win rate:         {win_rate:.1%}")
    if stop_losses:
        print(f"  stop-loss exits:  {len(stop_losses)}  (pnl {'+' if stop_loss_pnl >= 0 else ''}{stop_loss_pnl:.2f})")
    print(f"  realized PnL:     {'+' if total_pnl >= 0 else ''}{total_pnl:.2f}")
    print(f"  open/unresolved:  {len(open_positions)}")
    if open_positions:
        for pos in open_positions[-5:]:
            print(f"    - {pos.get('market_slug')} / {pos.get('outcome')} @ {pos.get('avg_price', 0):.3f}")


def report_lag_bot_diagnostics() -> None:
    """Bot 4's distinguishing feature is the model-vs-market disagreement
    it trades on -- show the average edge taken and how it broke down,
    since that's the thing actually being tested, not just win/loss."""
    entries = [t for t in journal.read_trades("lag_bot") if t.get("kind") == "entry"]
    if not entries:
        return
    avg_edge = sum(e.get("edge", 0.0) for e in entries) / len(entries)
    avg_model_p = sum(e.get("model_p", 0.0) for e in entries) / len(entries)
    avg_market_p = sum(e.get("market_p", 0.0) for e in entries) / len(entries)
    print(f"  avg model P:      {avg_model_p:.3f}")
    print(f"  avg market P:     {avg_market_p:.3f}")
    print(f"  avg edge taken:   {avg_edge:+.3f}")


def report_market_maker() -> None:
    """A resolution with inventory_settled == 0 does NOT necessarily mean
    "never filled" -- a quote can be bought into and sold back out before
    resolution (a real, PnL-bearing round trip) and still end at exactly
    zero inventory. Filtering on final inventory undercounts real trades
    and was wrong in an earlier version of this report. The correct test
    for "was this token ever actually filled" is whether any fill record
    (kind != "resolution") exists for that token_id -- quotes that were
    NEVER filled are the only ones that are truly riskless (cost=0,
    payout=0, pnl=0 by construction), so excluding only those should
    leave the "real" PnL total exactly equal to the raw total. See
    BUILD_INTELLIGENCE_REPORT.md Sessions 7-9 for why this distinction
    matters in the first place -- the never-filled quotes were inflating
    the win/loss count without representing real trades."""
    trades = journal.read_trades("market_maker_bot")
    resolutions = [t for t in trades if t.get("kind") == "resolution"]
    fills = [t for t in trades if t.get("kind") != "resolution"]
    filled_token_ids = {f["token_id"] for f in fills}

    real = [r for r in resolutions if r["token_id"] in filled_token_ids]
    never_filled = [r for r in resolutions if r["token_id"] not in filled_token_ids]

    real_wins = [r for r in real if r.get("won")]
    real_losses = [r for r in real if not r.get("won")]
    real_pnl = sum(r.get("pnl", 0.0) for r in real)
    avg_win = sum(r["pnl"] for r in real_wins) / len(real_wins) if real_wins else 0.0
    avg_loss = sum(r["pnl"] for r in real_losses) / len(real_losses) if real_losses else 0.0

    open_inventory = 0.0
    open_markets = 0
    if MM_STATE.exists():
        with MM_STATE.open(encoding="utf-8") as f:
            quotes = json.load(f)
        for q in quotes.values():
            if q.get("inventory", 0):
                open_inventory += q["inventory"]
                open_markets += 1

    print(f"\n=== market_maker_bot ===")
    print(f"  fills logged:           {len(fills)}")
    print(f"  resolved, ever filled:  {len(real)}  (W {len(real_wins)} / L {len(real_losses)})")
    if real:
        print(f"  win rate (real trades): {len(real_wins) / len(real):.1%}")
    print(f"  avg win / avg loss:     {avg_win:+.2f} / {avg_loss:+.2f}")
    print(f"  realized PnL:           {'+' if real_pnl >= 0 else ''}{real_pnl:.2f}")
    print(f"  never filled (excluded):{len(never_filled)} resolved quotes with zero fills, zero PnL by construction")
    print(f"  open inventory:         {open_inventory:.2f} shares across {open_markets} market(s)")


def main() -> None:
    report_directional("directional_bot", DIRECTIONAL_STATE)
    report_directional("oracle_bot", ORACLE_STATE)
    report_market_maker()
    report_directional("lag_bot", LAG_STATE)
    report_lag_bot_diagnostics()
    print()


if __name__ == "__main__":
    main()
