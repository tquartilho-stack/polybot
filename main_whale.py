"""
main_whale.py — bot whale-first.

Logica invertida:
  1. Pega posicoes reais das top 50 wallets
  2. Filtra: mercados onde 2+ whales estao no mesmo lado
  3. Claude scorer confirma edge (mais permissivo: min score 5.5)
  4. Agentes de confirmacao (arb + convergence)
  5. Consensus + execucao

Escreve dashboard_data_whale.json para o dashboard unificado.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import httpx
from rich.console import Console
from rich.table   import Table

from config import (
    POLY_PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE, POLY_PROXY_ADDRESS,
    RUN_INTERVAL_MINS, MAX_OPEN_POSITIONS, MAX_DAILY_TRADES, GAMMA_API,
)
from data.wallet_scanner    import get_top_wallets
from data.models            import Market, Side, AgentSignal, AgentName
from scoring.market_scorer  import score_markets
from agents.agents          import ArbitrageAgent, ConvergenceAgent
from consensus              import build_consensus
from execution.executor     import Executor
from execution.exit_manager import ExitManager
from portfolio              import Portfolio

# Portfolio separado para o bot whale
import portfolio as _portfolio_module
_portfolio_module.STATE_FILE = _portfolio_module.Path("portfolio_state_whale.json")
from pathlib import Path

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt = "%H:%M:%S",
)
log     = logging.getLogger("whale_main")
console = Console()

DRY_RUN          = os.getenv("DRY_RUN", "true").lower() == "true"
DASHBOARD_FILE   = Path("dashboard_data_whale.json")
POLY_DATA_API    = "https://data-api.polymarket.com"

MIN_WHALES       = 2
MIN_WHALE_SIZE   = 50
MIN_SCORE_WHALE  = 5.5

_wallet_cache = {"data": [], "last_update": 0.0}
WALLET_CACHE_TTL = 6 * 3600
_log_buffer: list[str] = []
_cycle_count = 0
_exit_task   = None


class DashboardLogHandler(logging.Handler):
    def emit(self, record):
        _log_buffer.append(self.format(record))
        if len(_log_buffer) > 100:
            _log_buffer.pop(0)

_dash_handler = DashboardLogHandler()
_dash_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(_dash_handler)


# ── Parsers ───────────────────────────────────────────────────────────────────

def _parse_whale_markets(raw_list: list[dict]) -> list[Market]:
    """Filtros minimos — so rejeita mercados expirados ou sem liquidez."""
    markets = []
    now = datetime.now(timezone.utc)
    for m in raw_list:
        try:
            prices_raw = m.get("outcomePrices", '["0.5","0.5"]')
            if isinstance(prices_raw, str):
                prices_raw = json.loads(prices_raw)
            yes_price = float(prices_raw[0])
            no_price  = float(prices_raw[1])

            liquidity = float(m.get("liquidity", 0))
            if liquidity < 500:
                continue

            end = (m.get("endDate") or m.get("endDateIso", "")).rstrip("Z")
            if not end:
                continue
            if "T" not in end:
                end += "T23:59:59"
            resolves_at = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
            hours_left  = (resolves_at - now).total_seconds() / 3600
            if hours_left < 1:
                continue

            markets.append(Market(
                condition_id     = m["conditionId"],
                question         = m.get("question", ""),
                yes_price        = yes_price,
                no_price         = no_price,
                volume_usdc      = float(m.get("volume24hr") or m.get("volume") or 0),
                liquidity_usdc   = liquidity,
                spread           = float(m.get("spread", 1)),
                resolves_at      = resolves_at,
                hours_to_resolve = hours_left,
            ))
        except Exception:
            continue
    return markets


# ── Fetchers ──────────────────────────────────────────────────────────────────

async def _fetch_positions(client: httpx.AsyncClient, address: str) -> list[dict]:
    try:
        r = await client.get(
            f"{POLY_DATA_API}/positions",
            params={"user": address, "sizeThreshold": str(MIN_WHALE_SIZE)},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.debug(f"Falha posicoes {address[:10]}: {e}")
        return []


async def fetch_whale_candidates(wallets) -> tuple[list, int]:
    """Devolve (candidatos, total_posicoes)."""
    async with httpx.AsyncClient() as client:
        tasks   = [_fetch_positions(client, w.address) for w in wallets[:50]]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    whale_map = defaultdict(lambda: {"YES": 0, "NO": 0})
    total     = 0
    for r in results:
        if not isinstance(r, list):
            continue
        for pos in r:
            cid  = pos.get("conditionId", "")
            side = pos.get("outcome", "").upper()
            if cid and side in ("YES", "NO"):
                whale_map[cid][side] += 1
                total += 1

    candidates = []
    for cid, sides in whale_map.items():
        for side, count in sides.items():
            if count >= MIN_WHALES:
                candidates.append((cid, side, count))

    candidates.sort(key=lambda x: x[2], reverse=True)
    log.info(f"Whale scan: {total} posicoes -> {len(candidates)} candidatos com {MIN_WHALES}+ whales")
    return candidates, total


async def fetch_markets_by_ids(condition_ids: list[str]) -> list[dict]:
    raw = []
    async with httpx.AsyncClient() as client:
        for cid in condition_ids[:80]:
            try:
                r = await client.get(
                    f"{GAMMA_API}/markets",
                    params={"conditionIds": cid},
                    timeout=10,
                )
                r.raise_for_status()
                data = r.json()
                if data:
                    items = data if isinstance(data, list) else [data]
                    raw.extend(items)
            except Exception as e:
                log.debug(f"Falha mercado {cid[:12]}: {e}")
    return raw


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _hours_left(resolves_at) -> str:
    now = datetime.now(timezone.utc)
    h   = (resolves_at - now).total_seconds() / 3600
    if h < 0:  return "expirado"
    if h < 24: return f"{h:.0f}h"
    return f"{h/24:.1f}d"


def write_dashboard(cycle, markets_total, markets_scored, signals, consensus_full, decisions, portfolio):
    trades    = portfolio.history
    open_pos  = portfolio.positions
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
                "trade_id":      p.trade_id,
                "question":      p.market.question,
                "side":          p.side.value,
                "size_usdc":     p.size_usdc,
                "entry_price":   round(p.entry_price, 3),
                "current_price": round(p.current_price, 3),
                "target_exit":   round(p.target_exit, 3),
                "pnl":           round((p.current_price - p.entry_price) * (p.size_usdc / p.entry_price), 2) if p.entry_price else 0,
                "resolves_in":   _hours_left(p.market.resolves_at),
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
                "trade_id": t.trade_id,
                "question": t.market_question[:50],
                "side":     t.side.value,
                "entry":    round(t.entry_price, 3),
                "exit":     round(t.exit_price, 3),
                "pnl":      round(t.pnl_usdc, 2),
                "reason":   t.exit_reason,
                "hours":    round(t.hold_hours, 1),
            }
            for t in reversed(trades[-20:])
        ],
        "log": _log_buffer[-30:],
    }
    DASHBOARD_FILE.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")


# ── Ciclo ─────────────────────────────────────────────────────────────────────

async def run_cycle(portfolio: Portfolio, executor: Executor, exit_manager: ExitManager):
    global _cycle_count, _exit_task
    _cycle_count += 1

    console.rule(f"[bold purple]Whale-First · Ciclo {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    # 1. Wallets
    now = time.time()
    if now - _wallet_cache.get("last_update", 0) > WALLET_CACHE_TTL:
        log.info("A actualizar cache de wallets...")
        _wallet_cache["data"]        = await get_top_wallets()
        _wallet_cache["last_update"] = now
    wallets = _wallet_cache["data"]

    # 2. Posicoes das whales -> candidatos
    # Salta scoring se já estamos no limite de posições abertas
    if len(portfolio.positions) >= portfolio.max_open:
        log.info(f"Maximo de posicoes abertas atingido ({len(portfolio.positions)}/{portfolio.max_open}) - a saltar scoring.")
        write_dashboard(_cycle_count, 0, 0, {"whale_lead": 0, "arbitrage": 0, "convergence": 0}, 0, [], portfolio)
        return

    candidates, total_positions = await fetch_whale_candidates(wallets)

    if not candidates:
        log.warning("Sem candidatos whale.")
        write_dashboard(_cycle_count, 0, 0, {"whale_lead": 0, "arbitrage": 0, "convergence": 0}, 0, [], portfolio)
        return

    # 3. Pega dados dos mercados candidatos (filtros minimos)
    condition_ids = [c[0] for c in candidates[:80]]
    log.info(f"A puxar dados de {len(condition_ids)} mercados candidatos...")
    raw_markets   = await fetch_markets_by_ids(condition_ids)
    whale_markets = _parse_whale_markets(raw_markets)
    log.info(f"  -> {len(whale_markets)} mercados validos")

    if not whale_markets:
        write_dashboard(_cycle_count, len(candidates), 0, {"whale_lead": len(candidates), "arbitrage": 0, "convergence": 0}, 0, [], portfolio)
        return

    # 4. Scorer como confirmacao (mais permissivo)
    scored = score_markets(whale_markets, min_score=MIN_SCORE_WHALE)
    log.info(f"  -> {len(scored)} mercados confirmados pelo scorer")

    # 5. Sinais whale-lead
    scored_ids = {m.condition_id: m for m in scored}
    whale_sigs = []
    for cid, side_str, n_whales in candidates:
        if cid not in scored_ids:
            continue
        market = scored_ids[cid]
        side   = Side.YES if side_str == "YES" else Side.NO
        price  = market.yes_price if side == Side.YES else market.no_price
        whale_sigs.append(AgentSignal(
            agent           = AgentName.WHALE_COPY,
            market          = market,
            side            = side,
            confidence      = min(n_whales / 5, 1.0),
            reason          = f"{n_whales} whales em {side_str}",
            suggested_price = price + 0.01,
        ))

    # 6. Agentes de confirmacao
    arb_sigs  = ArbitrageAgent().analyze(scored, wallets)
    conv_sigs = ConvergenceAgent().analyze(scored, wallets)

    all_signals     = whale_sigs + arb_sigs + conv_sigs
    signals_summary = {
        "whale_lead":  len(whale_sigs),
        "arbitrage":   len(arb_sigs),
        "convergence": len(conv_sigs),
    }
    log.info(f"  -> {len(all_signals)} sinais totais")

    # 7. Consensus
    decisions      = build_consensus(all_signals)
    consensus_full = sum(1 for d in decisions if d.consensus_count >= 2)

    # 8. Execucao
    for decision in decisions:
        if not portfolio.can_trade():
            break
        if portfolio.already_open(decision.market.condition_id):
            continue
        pos = executor.execute(decision)
        if pos:
            portfolio.add_position(pos)

    _print_decisions(decisions[:5])

    # 9. Exit manager em background
    if portfolio.positions:
        if _exit_task and not _exit_task.done():
            log.info("Exit manager ja activo")
        else:
            _exit_task = asyncio.create_task(_exit_background(portfolio, exit_manager))

    console.print(f"\n[bold purple]{portfolio.summary()}")
    write_dashboard(_cycle_count, len(candidates), len(scored), signals_summary, consensus_full, decisions, portfolio)


async def _exit_background(portfolio, exit_manager):
    try:
        results = await exit_manager.monitor_loop(list(portfolio.positions))
        for r in results:
            portfolio.close_position(r)
            log.info(f"[CLOSED] {r.trade_id} - PnL ${r.pnl_usdc:+.2f} ({r.exit_reason})")
    except Exception as e:
        log.error(f"Erro exit manager: {e}")


def _print_decisions(decisions):
    if not decisions:
        return
    t = Table(title="Top Decisoes [Whale-First]", show_header=True, header_style="bold purple")
    t.add_column("Mercado",  max_width=45)
    t.add_column("Side",     width=5)
    t.add_column("Consenso", width=9)
    t.add_column("Size",     width=8)
    t.add_column("Entry",    width=7)
    t.add_column("Target",   width=7)
    for d in decisions:
        stars = f"{'*' * d.consensus_count}{'o' * (3 - d.consensus_count)}"
        t.add_row(d.market.question[:45], d.side.value, stars, f"${d.size_usdc:.0f}", f"{d.entry_price:.3f}", f"{d.target_exit_price:.3f}")
    console.print(t)


# ── Entry ─────────────────────────────────────────────────────────────────────

async def main():
    console.print("[bold purple]PolyBot Whale-First v1.0[/bold purple] - a iniciar...")
    console.print(f"Modo: {'[yellow]DRY RUN[/yellow]' if DRY_RUN else '[red]LIVE[/red]'}")

    executor     = Executor(private_key=POLY_PRIVATE_KEY, api_key=POLY_API_KEY, api_secret=POLY_API_SECRET, api_passphrase=POLY_API_PASSPHRASE, proxy_addr=POLY_PROXY_ADDRESS, dry_run=DRY_RUN)
    portfolio    = Portfolio(max_open=MAX_OPEN_POSITIONS, max_daily=MAX_DAILY_TRADES)
    exit_manager = ExitManager(executor)

    while True:
        try:
            await run_cycle(portfolio, executor, exit_manager)
        except Exception as e:
            log.error(f"Erro no ciclo: {e}", exc_info=True)
        log.info(f"A aguardar {RUN_INTERVAL_MINS} minutos...\n")
        await asyncio.sleep(RUN_INTERVAL_MINS * 60)


if __name__ == "__main__":
    asyncio.run(main())
