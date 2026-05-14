from __future__ import annotations
import json
import logging
from datetime import datetime, date, timezone
from pathlib import Path

from data.models import OpenPosition, TradeResult, Side, Market

log = logging.getLogger(__name__)

DATA_DIR   = Path("/data") if Path("/data").exists() else Path(".")
STATE_FILE = DATA_DIR / "portfolio_state.json"


class Portfolio:
    def __init__(self, max_open: int, max_daily: int, state_file: Path = None):
        self.max_open    = max_open
        self.max_daily   = max_daily
        self.state_file  = state_file or STATE_FILE
        self.positions:  list[OpenPosition]  = []
        self.history:    list[TradeResult]   = []
        self._daily_count: dict[str, int]    = {}
        self._load()

    def can_trade(self) -> bool:
        today = str(date.today())
        daily = self._daily_count.get(today, 0)
        if daily >= self.max_daily:
            log.warning(f"Limite diário atingido ({daily}/{self.max_daily})")
            return False
        if len(self.positions) >= self.max_open:
            log.warning(f"Máximo de posições abertas atingido ({len(self.positions)}/{self.max_open})")
            return False
        return True

    def already_open(self, condition_id: str) -> bool:
        return any(p.market.condition_id == condition_id for p in self.positions)

    def add_position(self, pos: OpenPosition):
        self.positions.append(pos)
        today = str(date.today())
        self._daily_count[today] = self._daily_count.get(today, 0) + 1
        self._save()

    def close_position(self, result: TradeResult):
        self.positions = [p for p in self.positions if p.trade_id != result.trade_id]
        self.history.append(result)
        self._save()

    def total_pnl(self) -> float:
        return sum(t.pnl_usdc for t in self.history)

    def win_rate(self) -> float:
        if not self.history:
            return 0.0
        return sum(1 for t in self.history if t.pnl_usdc > 0) / len(self.history)

    def summary(self) -> str:
        return (
            f"Trades: {len(self.history)}  |  "
            f"Win rate: {self.win_rate():.0%}  |  "
            f"PnL total: ${self.total_pnl():+,.2f}  |  "
            f"Posições abertas: {len(self.positions)}"
        )

    def _save(self):
        try:
            self.state_file.write_text(json.dumps({
                "daily_count": self._daily_count,
                "positions":   [self._position_to_dict(p) for p in self.positions],
                "history":     [self._result_to_dict(r) for r in self.history],
            }, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning(f"Não foi possível guardar estado: {e}")

    def _load(self):
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._daily_count = data.get("daily_count", {})

            # Carrega posições abertas
            for p in data.get("positions", []):
                try:
                    self.positions.append(self._dict_to_position(p))
                except Exception as e:
                    log.warning(f"Erro ao carregar posição: {e}")
                    continue

            # Carrega histórico
            for r in data.get("history", []):
                try:
                    self.history.append(TradeResult(
                        trade_id        = r["trade_id"],
                        market_question = r["market_question"],
                        side            = Side(r["side"]),
                        size_usdc       = r["size_usdc"],
                        entry_price     = r["entry_price"],
                        exit_price      = r["exit_price"],
                        pnl_usdc        = r["pnl_usdc"],
                        hold_hours      = r["hold_hours"],
                        exit_reason     = r["exit_reason"],
                        closed_at       = datetime.fromisoformat(r["closed_at"]),
                    ))
                except Exception:
                    continue

            if self.positions:
                log.info(f"Portfolio carregado: {len(self.positions)} posições abertas, {len(self.history)} trades fechados")

        except Exception as e:
            log.warning(f"Erro ao carregar portfolio: {e}")

    def _position_to_dict(self, p: OpenPosition) -> dict:
        return {
            "trade_id":       p.trade_id,
            "condition_id":   p.market.condition_id,
            "question":       p.market.question,
            "yes_price":      p.market.yes_price,
            "no_price":       p.market.no_price,
            "volume_usdc":    p.market.volume_usdc,
            "liquidity_usdc": p.market.liquidity_usdc,
            "spread":         p.market.spread,
            "resolves_at":    p.market.resolves_at.isoformat(),
            "hours_to_resolve": p.market.hours_to_resolve,
            "side":           p.side.value,
            "size_usdc":      p.size_usdc,
            "entry_price":    p.entry_price,
            "entry_time":     p.entry_time.isoformat(),
            "target_exit":    p.target_exit,
            "current_price":  p.current_price,
            "peak_price":     p.peak_price,
            "volume_baseline":p.volume_baseline,
        }

    def _dict_to_position(self, d: dict) -> OpenPosition:
        market = Market(
            condition_id     = d["condition_id"],
            question         = d["question"],
            yes_price        = d["yes_price"],
            no_price         = d["no_price"],
            volume_usdc      = d["volume_usdc"],
            liquidity_usdc   = d["liquidity_usdc"],
            spread           = d["spread"],
            resolves_at      = datetime.fromisoformat(d["resolves_at"]),
            hours_to_resolve = d["hours_to_resolve"],
        )
        return OpenPosition(
            trade_id        = d["trade_id"],
            market          = market,
            side            = Side(d["side"]),
            size_usdc       = d["size_usdc"],
            entry_price     = d["entry_price"],
            entry_time      = datetime.fromisoformat(d["entry_time"]),
            target_exit     = d["target_exit"],
            current_price   = d.get("current_price", d["entry_price"]),
            peak_price      = d.get("peak_price", d["entry_price"]),
            volume_baseline = d.get("volume_baseline", 0),
        )

    def _result_to_dict(self, r: TradeResult) -> dict:
        return {
            "trade_id":        r.trade_id,
            "market_question": r.market_question,
            "side":            r.side.value,
            "size_usdc":       r.size_usdc,
            "entry_price":     r.entry_price,
            "exit_price":      r.exit_price,
            "pnl_usdc":        r.pnl_usdc,
            "hold_hours":      r.hold_hours,
            "exit_reason":     r.exit_reason,
            "closed_at":       r.closed_at.isoformat(),
        }
