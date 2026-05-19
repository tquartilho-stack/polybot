"""
reconcile.py — reconcilia portfolio_state com posições reais do Polymarket.
- Remove posições que já não existem na Poly (resolvidas/fechadas) e regista PnL
- Adiciona posições em falta ao scorer (posições na Poly mas não no portfolio)
- Actualiza token_id e current_price das posições existentes
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


async def _fetch_clob_price(client: httpx.AsyncClient, token_id: str) -> float:
    try:
        r = await client.get(f"{CLOB_API}/last-trade-price", params={"token_id": token_id}, timeout=5)
        r.raise_for_status()
        price = float(r.json().get("price", 0))
        if price > 0:
            return price
        r2 = await client.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=5)
        r2.raise_for_status()
        return float(r2.json().get("mid", 0))
    except:
        return 0.0


BLACKLISTED_CONDITIONS = {
    "0x4f60e49a9c6265c2567eedbf183500f8f2f10cd81b1468e4c5c4c1bf6f5c74ae",  # CS GamerLegion NaVi
}

async def _build_missing_position(p: dict) -> dict | None:
    """Constrói dict de posição a partir de dados da Data API."""
    try:
        cid      = p.get("conditionId", "")
        if cid in BLACKLISTED_CONDITIONS:
            return None
        token_id = p.get("asset", "")
        outcome  = p.get("outcome", "YES").strip().upper()
        if outcome not in ("YES", "NO"):
            outcome = "YES"

        size    = float(p.get("size", 0))
        initial = float(p.get("initialValue") or 0)
        price   = round(initial / size, 6) if size > 0 else float(p.get("avgPrice") or 0.5)
        if price <= 0 or price >= 1:
            price = 0.5

        async with httpx.AsyncClient() as client:
            cur = await _fetch_clob_price(client, token_id) if token_id else price
        if cur <= 0:
            cur = price

        title   = p.get("title", "")
        end_str = (p.get("endDate") or "").rstrip("Z")
        if end_str:
            if "T" not in end_str:
                end_str += "T23:59:59"
            resolves_at = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc)
        else:
            resolves_at = datetime.now(timezone.utc)

        hours_left = max((resolves_at - datetime.now(timezone.utc)).total_seconds() / 3600, 0)
        target     = round(price + 0.90 * (1.0 - price), 4)

        return {
            "trade_id":         f"sync_{cid[:8]}_{outcome[:2]}",
            "condition_id":     cid,
            "token_id":         token_id,
            "question":         title,
            "yes_price":        cur if outcome == "YES" else round(1 - cur, 4),
            "no_price":         cur if outcome == "NO"  else round(1 - cur, 4),
            "volume_usdc":      0.0,
            "liquidity_usdc":   0.0,
            "spread":           0.01,
            "resolves_at":      resolves_at.isoformat(),
            "hours_to_resolve": hours_left,
            "side":             outcome,
            "size_usdc":        round(size * price, 2),
            "entry_price":      price,
            "entry_time":       datetime.now(timezone.utc).isoformat(),
            "target_exit":      target,
            "current_price":    cur,
            "peak_price":       cur,
            "volume_baseline":  0.0,
        }
    except Exception as e:
        log.warning(f"Erro ao construir posição em falta: {e}")
        return None


async def reconcile_portfolio(portfolio, proxy_address: str, label: str = "") -> int:
    """
    Reconcilia portfolio com posições reais do Polymarket.
    - Remove posições fechadas/resolvidas e regista PnL
    - Adiciona posições em falta (scorer only)
    - Actualiza token_id e preços
    Devolve número de posições removidas.
    """
    real = await fetch_real_positions(proxy_address)
    if not real and portfolio.positions:
        log.warning(f"[{label}/RECONCILE] API sem dados — a saltar para não remover posições por engano")
        return 0

    real_by_key = {}
    for p in real:
        cid     = p.get("conditionId", "")
        outcome = p.get("outcome", "YES").upper()
        if outcome not in ("YES", "NO"):
            outcome = "YES"
        real_by_key[f"{cid}_{outcome}"] = p

    # ── Remove posições que já não existem na Poly ────────────────────────────
    to_remove = []
    for pos in portfolio.positions:
        key = f"{pos.market.condition_id}_{pos.side.value}"
        if key not in real_by_key:
            to_remove.append(pos)

    for pos in to_remove:
        pnl        = _calculate_resolved_pnl(pos)
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
        log.info(f"[{label}/RECONCILE] Resolvida: {pos.trade_id} {pos.market.question[:40]} PnL ${pnl:+.2f}")

    # ── Adiciona posições em falta (scorer only) ──────────────────────────────
    # Condition IDs com loop confirmado — nunca re-adicionar
    if label in ("SCORER", ""):
        portfolio_keys = {f"{pos.market.condition_id}_{pos.side.value}" for pos in portfolio.positions}
        added = 0
        for key, p in real_by_key.items():
            cid_check = p.get("conditionId", "")
            if cid_check in BLACKLISTED_CONDITIONS:
                log.info(f"[{label}/RECONCILE] Ignorada posição blacklisted: {p.get('title','')[:40]}")
                continue
            if key not in portfolio_keys:
                # Não adicionar posições já expiradas com preço residual (ex: Eurovision resolvido mas curPrice=0.001)
                cur_price = float(p.get("curPrice") or 0)
                redeemable = p.get("redeemable", False)
                if cur_price == 0 and redeemable:
                    continue
                # Não adicionar posições já resolvidas com preço >= 0.95 (aguardam redemption automática)
                if cur_price >= 0.95:
                    log.info(f"[{label}/RECONCILE] Ignorada posição resolvida (curPrice={cur_price}): {p.get('title','')[:40]}")
                    continue
                # Não adicionar posições com preço muito baixo E já expiradas (resolvidas sem redemption)
                end_date = (p.get("endDate") or "").rstrip("Z")
                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("T", "T") if "T" in end_date else end_date + "T23:59:59").replace(tzinfo=timezone.utc)
                        already_expired = (datetime.now(timezone.utc) - end_dt).total_seconds() > 3600
                        if already_expired and cur_price < 0.02:
                            log.info(f"[{label}/RECONCILE] Ignorada posição expirada com preço residual: {p.get('title','')[:40]}")
                            continue
                    except:
                        pass
                pos_dict = await _build_missing_position(p)
                if not pos_dict:
                    continue
                try:
                    pos = portfolio._dict_to_position(pos_dict)
                    portfolio.positions.append(pos)
                    portfolio._save()
                    added += 1
                    log.info(f"[{label}/RECONCILE] Posição em falta adicionada: {pos_dict['question'][:40]}")
                except Exception as e:
                    log.warning(f"[{label}/RECONCILE] Erro ao adicionar posição: {e}")
        if added:
            log.info(f"[{label}/RECONCILE] {added} posições adicionadas automaticamente")

    # ── Actualiza token_id e preços das posições existentes ───────────────────
    updated = False
    async with httpx.AsyncClient() as client:
        for pos in portfolio.positions:
            key      = f"{pos.market.condition_id}_{pos.side.value}"
            real_pos = real_by_key.get(key)
            if not real_pos:
                continue

            token_id = real_pos.get("asset", "")
            if token_id and not pos.token_id:
                pos.token_id = token_id
                updated = True

            if pos.token_id:
                cur = await _fetch_clob_price(client, pos.token_id)
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
    Calcula PnL quando posição foi resolvida no Polymarket.
    Se current_price >= 0.95 assume resolução positiva (exit a 1.0).
    Se current_price <= 0.05 assume resolução negativa (exit a 0.0).
    Caso contrário usa current_price.
    """
    if pos.entry_price <= 0:
        return 0.0
    shares = pos.size_usdc / pos.entry_price
    if pos.current_price >= 0.95:
        exit_price = 1.0
    elif pos.current_price <= 0.05:
        exit_price = 0.0
    else:
        exit_price = pos.current_price
    return round((exit_price - pos.entry_price) * shares, 2)
