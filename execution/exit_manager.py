from __future__ import annotations
import asyncio
import logging
import json
from datetime import datetime, timezone

import httpx

from config import EXIT_PROFIT_TARGET, VOLUME_SPIKE_MULT, POLL_INTERVAL_SECS, GAMMA_API, CLOB_API
from data.models import OpenPosition, TradeResult, Side

log = logging.getLogger(__name__)


class ExitManager:
    def __init__(self, executor):
        self.executor = executor
        self.closed_trades: list[TradeResult] = []

    async def monitor_loop(self, portfolio) -> None:
        """
        Monitoriza posições abertas continuamente.
        Recebe o portfolio directamente para detectar novas posições adicionadas após arranque.
        Corre indefinidamente em background.
        """
        async with httpx.AsyncClient() as http:
            while True:
                positions = list(portfolio.positions)

                if not positions:
                    await asyncio.sleep(POLL_INTERVAL_SECS)
                    continue

                to_close: list[tuple[OpenPosition, str]] = []

                for pos in positions:
                    try:
                        exit_reason = await self._check_exit(pos, http)
                        if exit_reason:
                            to_close.append((pos, exit_reason))
                    except Exception as e:
                        log.error(f"Erro ao verificar {pos.trade_id}: {e}")

                for pos, reason in to_close:
                    result = await self._close(pos, reason)
                    if result:
                        portfolio.close_position(result)
                        self.closed_trades.append(result)
                        log.info(f"[EXIT] {result.trade_id} — PnL ${result.pnl_usdc:+.2f} ({result.exit_reason})")
                        if self.executor.no_balance:
                            self.executor.no_balance = False
                            log.info("[EXIT] Saldo libertado — ciclos retomam")

                await asyncio.sleep(POLL_INTERVAL_SECS)

    async def _check_exit(self, pos: OpenPosition, http: httpx.AsyncClient) -> str | None:
        current_price = 0.0

        if pos.token_id:
            try:
                r = await http.get(
                    f"{CLOB_API}/last-trade-price",
                    params={"token_id": pos.token_id},
                    timeout=10,
                )
                r.raise_for_status()
                current_price = float(r.json().get("price", 0))
            except:
                pass

        if current_price == 0:
            try:
                r = await http.get(
                    f"{GAMMA_API}/markets",
                    params={"conditionIds": pos.market.condition_id},
                    timeout=10,
                )
                r.raise_for_status()
                data = r.json()
                if not data:
                    return None
                market_data = data[0] if isinstance(data, list) else data
                import json as _json
                prices_raw = market_data.get("outcomePrices", '["0.5","0.5"]')
                if isinstance(prices_raw, str):
                    prices_raw = _json.loads(prices_raw)
                current_price = float(prices_raw[0]) if pos.side == Side.YES else float(prices_raw[1])
            except Exception as e:
                log.warning(f"Falha ao puxar preco {pos.trade_id}: {e}")
                return None

        pos.current_price = current_price
        if current_price > pos.peak_price:
            pos.peak_price = current_price

        if current_price >= pos.target_exit:
            return "target"

        try:
            r = await http.get(
                f"{GAMMA_API}/markets",
                params={"conditionIds": pos.market.condition_id},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            market_data = data[0] if isinstance(data, list) else data
            current_volume = float(market_data.get("volume24hr", 0))
            if pos.volume_baseline > 0 and current_volume > 0:
                if current_volume / pos.volume_baseline >= VOLUME_SPIKE_MULT:
                    return "volume_spike"
        except:
            pass

        now = datetime.now(timezone.utc)
        mins_left = (pos.market.resolves_at - now).total_seconds() / 60
        if mins_left < 15:
            return "settlement"

        return None

    async def _close(self, pos: OpenPosition, reason: str) -> TradeResult | None:
        exit_price = pos.current_price if pos.current_price > 0 else pos.entry_price
        hold_hours = (datetime.now(timezone.utc) - pos.entry_time).total_seconds() / 3600
        pnl        = (exit_price - pos.entry_price) * (pos.size_usdc / pos.entry_price)

        log.info(
            f"[EXIT/{reason.upper()}] {pos.trade_id} — "
            f"{pos.side.value} @ {exit_price:.3f} "
            f"(entry {pos.entry_price:.3f}) — PnL ${pnl:+.2f}"
        )

        if not self.executor.dry_run:
            await self._sell_position(pos, exit_price)

        return TradeResult(
            trade_id        = pos.trade_id,
            market_question = pos.market.question,
            side            = pos.side,
            size_usdc       = pos.size_usdc,
            entry_price     = pos.entry_price,
            exit_price      = exit_price,
            pnl_usdc        = pnl,
            hold_hours      = hold_hours,
            exit_reason     = reason,
        )

    async def _sell_position(self, pos: OpenPosition, exit_price: float):
        try:
            from py_clob_client_v2.clob_types import OrderArgsV2

            token_id = self.executor._get_token_id(pos.market.condition_id, pos.side)
            if not token_id:
                log.error(f"Sem token_id para venda de {pos.trade_id}")
                return

            shares = round(pos.size_usdc / pos.entry_price, 2)

            try:
                result = self.executor.clob.create_and_post_market_order(
                    OrderArgsV2(
                        token_id = token_id,
                        price    = exit_price,
                        size     = shares,
                        side     = "SELL",
                    )
                )
                log.info(f"Ordem SELL mercado: {pos.trade_id} — {result}")
            except Exception:
                aggressive_price = round(max(exit_price * 0.95, 0.01), 3)
                order_args = OrderArgsV2(
                    token_id = token_id,
                    price    = aggressive_price,
                    size     = shares,
                    side     = "SELL",
                )
                result = self.executor.clob.create_and_post_order(order_args)
                log.info(f"Ordem SELL limite agressiva @ {aggressive_price}: {pos.trade_id} — {result}")

        except Exception as e:
            err_str = str(e).lower()
            if "not enough balance" in err_str or "balance is not enough" in err_str or "balance: 0" in err_str:
                log.warning(f"[SELL] Sem saldo/allowance para {pos.trade_id} — posição já resolvida ou criada via sync")
            else:
                log.error(f"Erro ao vender {pos.trade_id}: {e}")
