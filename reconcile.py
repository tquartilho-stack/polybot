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


async def reconcile_portfolio(portfolio, proxy_address: str, label: str = ""):
    """
    Reconcilia portfolio com posições reais do Polymarket.
    - Remove posições que já não existem no Polymarket
    - Actualiza token_id e current_price das posições existentes
    - Adiciona posições que existem no Polymarket mas não no portfolio (não esperado mas possível)
    """
    real = await fetch_real_positions(proxy_address)
    if not real:
        log.warning(f"[{label}/RECONCILE] Sem posições reais ou erro na API")
        return

    real_by_cid = {}
    for p in real:
        cid = p.get("conditionId", "")
        outcome = p.get("outcome", "YES").upper()
        if outcome not in ("YES", "NO"):
            outcome = "YES"
        key = f"{cid}_{outcome}"
        real_by_cid[key] = p

    # Remove posições fantasma (não existem no Polymarket)
    to_remove = []
    for pos in portfolio.positions:
        key = f"{pos.market.condition_id}_{pos.side.value}"
        if key not in real_by_cid:
            log.info(f"[{label}/RECONCILE] Posição {pos.trade_id} não encontrada no Polymarket — a remover")
            to_remove.append(pos)

    for pos in to_remove:
        portfolio.positions.remove(pos)
        if to_remove:
            portfolio._save()

    # Actualiza token_id e current_price das posições existentes
    updated = False
    for pos in portfolio.positions:
        key = f"{pos.market.condition_id}_{pos.side.value}"
        real_pos = real_by_cid.get(key)
        if not real_pos:
            continue

        # Actualiza token_id se não tiver
        token_id = real_pos.get("asset", "")
        if token_id and not pos.token_id:
            pos.token_id = token_id
            updated = True
            log.info(f"[{label}/RECONCILE] token_id actualizado para {pos.trade_id}")

        # Actualiza current_price via CLOB
        if pos.token_id:
            cur = await fetch_clob_price(pos.token_id)
            if cur > 0 and abs(cur - pos.current_price) > 0.001:
                pos.current_price = cur
                if cur > pos.peak_price:
                    pos.peak_price = cur
                updated = True

    if updated:
        portfolio._save()

    log.info(
        f"[{label}/RECONCILE] {len(portfolio.positions)} posições "
        f"(real: {len(real)}, removidas: {len(to_remove)})"
    )
