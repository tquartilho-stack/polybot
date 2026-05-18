from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR       = Path("/data") if Path("/data").exists() else Path(".")
DASHBOARD_FILE = DATA_DIR / "dashboard_data.json"


def _hours_left(resolves_at) -> str:
    now = datetime.now(timezone.utc)
    h = (resolves_at - now).total_seconds() / 3600
    if h < 0:
        return "expirado"
    if h < 24:
        return f"{h:.0f}h"
    return f"{h/24:.1f}d"


def write(
    cycle:          int,
    markets_total:  int,
    markets_scored: int,
    signals:        dict,
    consensus_full: int,
    decisions:      list,
    portfolio,
    log_lines:      list[str],
):
    BAD_QUESTIONS = {"GamerLegion vs Natus Vincere"}
    trades   = [t for t in portfolio.history if not any(q in t.market_question for q in BAD_QUESTIONS)]
    open_pos = portfolio.positions

    total_pnl = sum(t.pnl_usdc for t in trades)
    wins      = sum(1 for t in trades if t.pnl_usdc > 0)
    win_rate  = (wins / len(trades) * 100) if trades else 0

    data = {
        "updated_at":     datetime.now(timezone.utc).isoformat(),
        "next_cycle_at":  (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        "cycle":          cycle,
        "markets_total":  markets_total,
        "markets_scored": markets_scored,
        "signals":        signals,
        "consensus_full": consensus_full,
        "total_trades":   len(trades),
        "wins":           wins,
        "win_rate":       round(win_rate, 1),
        "total_pnl":      round(total_pnl, 2),
        "open_positions": [
            {
                "trade_id":     p.trade_id,
                "question":     p.market.question,
                "side":         p.side.value,
                "size_usdc":    p.size_usdc,
                "entry_price":  round(p.entry_price, 3),
                "current_price":round(p.current_price, 3),
                "target_exit":  round(p.target_exit, 3),
                "pnl":          round((p.current_price - p.entry_price) * (p.size_usdc / p.entry_price), 2) if p.entry_price else 0,
                "resolves_in":  _hours_left(p.market.resolves_at),
            }
            for p in open_pos
        ],
        "top_decisions": [
            {
                "question":  d.market.question,
                "side":      d.side.value,
                "consensus": d.consensus_count,
                "size":      d.size_usdc,
                "entry":     round(d.entry_price, 3),
                "target":    round(d.target_exit_price, 3),
                "reason":    d.signals[0].reason if d.signals else "",
            }
            for d in decisions[:8]
        ],
        "recent_trades": [
            {
                "trade_id":  t.trade_id,
                "question":  t.market_question[:50],
                "side":      t.side.value,
                "entry":     round(t.entry_price, 3),
                "exit":      round(t.exit_price, 3),
                "pnl":       round(t.pnl_usdc, 2),
                "reason":    t.exit_reason,
                "hours":     round(t.hold_hours, 1),
            }
            for t in reversed(trades[-20:])
        ],
        "log": log_lines[-30:],
    }

    DASHBOARD_FILE.write_text(
        json.dumps(data, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
