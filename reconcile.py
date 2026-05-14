"""
reconcile.py — reconcilia portfolio_state com posições reais do Polymarket.
Chamado após cada BUY/SELL e a cada ciclo como sanity check.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

POLY_DATA_API = "https://data-api.polymarket.com"
CLOB_API      = "https://clob.polymarket.com"


async def fetch_real_positions(proxy_address: str) -> list[dict]:
    """Pega posições reais do Polymarket."""
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                f"{POLY_DATA_API}/positions",
                params={"user": proxy_address, "sizeThreshold": "0.01"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            return [p for p in data if float(p.get("size", 0)) > 0] if isinstance(data, list) else []
        except Exception as e:
            log.warning(f"Falha ao puxar posições reais: {e}")
            return []


async def fetch_clob_price(token_id: str) -> float:
    """Lê preço actual via CLOB."""
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                f"{CLOB_API}/last-trade-price",
                params={"token_id": token_id},
                timeout=5,
            )
            r.raise_for_status()
            price = float(r.json().get("price", 0))
            if price > 0:
                return price
            # Fallback para midpoint
            r2 = await client.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": token_id},
                timeout=5,
            )
            r2.raise_for_status()
            return float(r2.json().get("mid", 0))
        except:
            return 0.0


async def reconcile_portfolio(portfolio, proxy_address: str, label: str = "") -> int:
    """
    Reconcilia portfolio com posições reais do Polymarket.
    Devolve número de posições removidas.
    """
    real = await fetch_real_positions(proxy_address)
    if not real and portfolio.positions:
        log.warning(f"[{label}/RECONCILE] API sem dados — a saltar para não remover posições por engano")
        return 0

    real_by_key = {}
    for p in real:
        cid = p.get("conditionId", "")
        outcome = p.get("outcome", "YES").upper()
        if outcome not in ("YES", "NO"):
            outcome = "YES"
        real_by_key[f"{cid}_{outcome}"] = p

    # Remove posições que já não existem no Polymarket e regista PnL
    to_remove = []
    for pos in portfolio.positions:
        key = f"{pos.market.condition_id}_{pos.side.value}"
        if key not in real_by_key:
            to_remove.append(pos)

    for pos in to_remove:
        # Tenta calcular PnL real via Data API histórico
        pnl = _calculate_resolved_pnl(pos)
        hold_hours = (datetime.now(timezone.utc) - pos.entry_time).total_seconds() / 3600

        from data.models import TradeResult
        result = TradeResult(
            trade_id        = pos.trade_id,
            market_question = pos.market.question,
            side            = pos.side,
            size_usdc       = pos.size_usdc,
            entry_price     = pos.entry_price,
            exit_price      = pos.current_price,
            pnl_usdc        = pnl,
            hold_hours      = hold_hours,
            exit_reason     = "resolved",
        )
        portfolio.close_position(result)
        log.info(f"[{label}/RECONCILE] Posição resolvida: {pos.trade_id} {pos.market.question[:40]} — PnL ${pnl:+.2f}")

    # Actualiza token_id e current_price das posições existentes
    updated = False
    for pos in portfolio.positions:
        key = f"{pos.market.condition_id}_{pos.side.value}"
        real_pos = real_by_key.get(key)
        if not real_pos:
            continue

        token_id = real_pos.get("asset", "")
        if token_id and not pos.token_id:
            pos.token_id = token_id
            updated = True

        if pos.token_id:
            cur = await fetch_clob_price(pos.token_id)
            if cur > 0 and abs(cur - pos.current_price) > 0.001:
                pos.current_price = cur
                if cur > pos.peak_price:
                    pos.peak_price = cur
                updated = True

    if updated:
        portfolio._save()

    removed = len(to_remove)
    log.info(f"[{label}/RECONCILE] {len(portfolio.positions)} posições (real: {len(real)}, removidas: {removed})")
    return removed


def _calculate_resolved_pnl(pos) -> float:
    """
    Calcula PnL quando posição foi resolvida/fechada no Polymarket sem SELL do bot.
    Usa current_price como exit_price.
    """
    if pos.entry_price <= 0:
        return 0.0
    shares = pos.size_usdc / pos.entry_price
    # Se current_price é 0 ou muito baixo, provavelmente resolveu a 0 (perdeu)
    exit_price = pos.current_price if pos.current_price > 0.01 else 0.0
    return round((exit_price - pos.entry_price) * shares, 2)
