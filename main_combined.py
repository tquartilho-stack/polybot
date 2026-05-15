"""
main_combined.py — corre scorer-first e whale-first em paralelo.
Um único processo, um único servidor HTTP, um único Volume.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.table   import Table

from config import (
    POLY_PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE, POLY_PROXY_ADDRESS,
    RUN_INTERVAL_MINS, MAX_OPEN_POSITIONS, MAX_DAILY_TRADES,
    GAMMA_API, CLAUDE_MODEL,
)
from data.fetcher           import get_filtered_markets
from data.wallet_scanner    import get_top_wallets
from scoring.market_scorer  import score_markets
from agents.agents          import ArbitrageAgent, ConvergenceAgent, WhaleCopyAgent
from consensus              import build_consensus
from execution.executor     import Executor
from execution.exit_manager import ExitManager
from portfolio              import Portfolio
from dashboard_writer       import write as write_scorer_dashboard
from server                 import start_server, is_paused, is_started, is_paused_whale, is_started_whale
from reconcile              import reconcile_portfolio
from pathlib import Path
import json

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt = "%H:%M:%S",
)
log     = logging.getLogger("combined")
console = Console()

DRY_RUN  = os.getenv("DRY_RUN", "true").lower() == "true"
DATA_DIR = Path("/data") if Path("/data").exists() else Path(".")

# Wallet cache partilhado
_wallet_cache = {"data": [], "last_update": 0.0}
WALLET_CACHE_TTL = 60

# Scoring cache partilhado — scorer partilha resultados com whale
_scoring_cache = {"markets": [], "last_update": 0.0}
SCORING_CACHE_TTL = 25 * 60  # 25 minutos

# Custo estimado Claude (Sonnet 4.6: $3 input + $15 output por MTok)
_claude_stats = {"calls_today": 0, "cost_today_usd": 0.0, "last_reset": ""}
COST_PER_CALL  = 0.017  # estimativa por batch

_log_buffer_scorer: list[str] = []
_log_buffer_whale:  list[str] = []

_scorer_cycle = 0
_whale_cycle  = 0
_exit_task_scorer = None
_exit_task_whale  = None


# ── Log handlers ──────────────────────────────────────────────────────────────

class ScorerLogHandler(logging.Handler):
    def emit(self, record):
        _log_buffer_scorer.append(self.format(record))
        if len(_log_buffer_scorer) > 100:
            _log_buffer_scorer.pop(0)

class WhaleLogHandler(logging.Handler):
    def emit(self, record):
        _log_buffer_whale.append(self.format(record))
        if len(_log_buffer_whale) > 100:
            _log_buffer_whale.pop(0)

_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s", "%H:%M:%S")
_sh = ScorerLogHandler(); _sh.setFormatter(_fmt); logging.getLogger().addHandler(_sh)
_wh = WhaleLogHandler();  _wh.setFormatter(_fmt); logging.getLogger().addHandler(_wh)


# ── Shared wallet cache ───────────────────────────────────────────────────────

async def get_wallets_cached():
    now = time.time()
    if now - _wallet_cache["last_update"] > WALLET_CACHE_TTL:
        log.info("A actualizar cache de wallets...")
        _wallet_cache["data"]        = await get_top_wallets()
        _wallet_cache["last_update"] = now
    return _wallet_cache["data"]


# ── Dashboard writers ─────────────────────────────────────────────────────────

def _hours_left(resolves_at) -> str:
    now = datetime.now(timezone.utc)
    h   = (resolves_at - now).total_seconds() / 3600
    if h < 0:  return "expirado"
    if h < 24: return f"{h:.0f}h"
    return f"{h/24:.1f}d"


def _write_dashboard(file: Path, cycle, markets_total, markets_scored, signals, consensus_full, decisions, portfolio, log_buf):
    from datetime import timedelta
    trades    = portfolio.history
    open_pos  = portfolio.positions
    total_pnl = sum(t.pnl_usdc for t in trades)
    wins      = sum(1 for t in trades if t.pnl_usdc > 0)
    win_rate  = (wins / len(trades) * 100) if trades else 0

    data = {
        "updated_at":     datetime.now(timezone.utc).isoformat(),
        "next_cycle_at":  (datetime.now(timezone.utc) + timedelta(minutes=RUN_INTERVAL_MINS)).isoformat(),
        "cycle":          cycle,
        "markets_total":  markets_total,
        "markets_scored": markets_scored,
        "signals":        signals,
        "consensus_full": consensus_full,
        "total_trades":   len(trades),
        "wins":           wins,
        "win_rate":       round(win_rate, 1),
        "total_pnl":      round(total_pnl, 2),
        "claude_calls_today": _claude_stats["calls_today"],
        "claude_cost_today":  round(_claude_stats["cost_today_usd"], 2),
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
        "log": log_buf[-30:],
    }
    file.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")


# ── Exit background ───────────────────────────────────────────────────────────

async def _exit_background(portfolio, exit_manager, label):
    try:
        results = await exit_manager.monitor_loop(list(portfolio.positions))
        for r in results:
            portfolio.close_position(r)
            log.info(f"[{label}/EXIT] {r.trade_id} — PnL ${r.pnl_usdc:+.2f} ({r.exit_reason})")
            # Limpa flag de sem saldo quando uma posição fecha
            if exit_manager.executor.no_balance:
                exit_manager.executor.no_balance = False
                log.info(f"[{label}] Saldo libertado — ciclos retomam no próximo intervalo")
    except Exception as e:
        log.error(f"Erro exit manager {label}: {e}")


# ── Reconcile background (scorer only, a cada 5min) ──────────────────────────

async def _reconcile_background_loop(portfolio):
    """Corre reconcile do scorer a cada 5 minutos em background."""
    await asyncio.sleep(60)  # espera 1 min após arranque
    while True:
        try:
            await reconcile_portfolio(portfolio, POLY_PROXY_ADDRESS, "SCORER")
        except Exception as e:
            log.error(f"[SCORER/RECONCILE-BG] Erro: {e}")
        await asyncio.sleep(5 * 60)


# ── SCORER LOOP ───────────────────────────────────────────────────────────────

async def scorer_loop(executor, portfolio, exit_manager):
    global _scorer_cycle, _exit_task_scorer

    if portfolio.positions:
        log.info(f"[SCORER] {len(portfolio.positions)} posições carregadas — exit manager a iniciar...")
        _exit_task_scorer = asyncio.create_task(_exit_background(portfolio, exit_manager, "SCORER"))

    # Reconcile background — mantém scorer sempre sincronizado com Poly
    asyncio.create_task(_reconcile_background_loop(portfolio))

    while True:
        if not is_started() or is_paused():
            await asyncio.sleep(10)
            continue

        if executor.no_balance:
            log.info("[SCORER] Sem saldo — a aguardar reconcile background...")
            await asyncio.sleep(5 * 60)
            continue

        _scorer_cycle += 1
        console.rule(f"[bold cyan]Scorer · Ciclo {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

        try:
            if len(portfolio.positions) >= portfolio.max_open:
                log.info(f"[SCORER] Máximo de posições atingido — a saltar scoring.")
                _write_dashboard(DATA_DIR / "dashboard_data.json", _scorer_cycle, 0, 0, {}, 0, [], portfolio, list(_log_buffer_scorer))
                await asyncio.sleep(RUN_INTERVAL_MINS * 60)
                continue

            all_markets = await get_filtered_markets()
            scored      = score_markets(all_markets)

            # Tracking de custo Claude
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if _claude_stats["last_reset"] != today:
                _claude_stats["calls_today"]    = 0
                _claude_stats["cost_today_usd"] = 0.0
                _claude_stats["last_reset"]     = today
            n_batches = max(1, len(all_markets) // 20)
            _claude_stats["calls_today"]    += n_batches
            _claude_stats["cost_today_usd"] += n_batches * COST_PER_CALL

            # Guarda no cache partilhado para o whale usar
            _scoring_cache["markets"]     = scored
            _scoring_cache["last_update"] = time.time()
            log.info(f"[SCORER] Cache de scoring actualizado: {len(scored)} mercados | custo hoje estimado: ${_claude_stats['cost_today_usd']:.2f}")
            wallets     = await get_wallets_cached()

            arb_sigs   = ArbitrageAgent().analyze(scored, wallets)
            conv_sigs  = ConvergenceAgent().analyze(scored, wallets)
            whale_sigs = await WhaleCopyAgent().analyze_async(scored, wallets)
            all_sigs   = arb_sigs + conv_sigs + whale_sigs
            signals    = {"arbitrage": len(arb_sigs), "convergence": len(conv_sigs), "whale_copy": len(whale_sigs)}

            decisions      = build_consensus(all_sigs)
            consensus_full = sum(1 for d in decisions if d.consensus_count >= 2)

            for d in decisions:
                if not portfolio.can_trade(): break
                if portfolio.already_open(d.market.condition_id): continue
                pos = executor.execute(d)
                if pos: portfolio.add_position(pos)

            _print_decisions(decisions[:5], "cyan")

            if portfolio.positions:
                if not _exit_task_scorer or _exit_task_scorer.done():
                    _exit_task_scorer = asyncio.create_task(_exit_background(portfolio, exit_manager, "SCORER"))

            console.print(f"[bold cyan]{portfolio.summary()}")
            _write_dashboard(DATA_DIR / "dashboard_data.json", _scorer_cycle, len(all_markets), len(scored), signals, consensus_full, decisions, portfolio, list(_log_buffer_scorer))

        except Exception as e:
            log.error(f"[SCORER] Erro: {e}", exc_info=True)

        await asyncio.sleep(RUN_INTERVAL_MINS * 60)


# ── WHALE LOOP ────────────────────────────────────────────────────────────────

async def whale_loop(executor, portfolio, exit_manager):
    global _whale_cycle, _exit_task_whale

    POLY_DATA_API  = "https://data-api.polymarket.com"
    MIN_WHALES     = 1
    MIN_WHALE_SIZE = 50
    MIN_SCORE      = 5.5

    import httpx
    from collections import defaultdict
    from data.models import AgentSignal, AgentName, Side, Market
    from scoring.market_scorer import score_markets as _score

    if portfolio.positions:
        log.info(f"[WHALE] {len(portfolio.positions)} posições carregadas — exit manager a iniciar...")
        _exit_task_whale = asyncio.create_task(_exit_background(portfolio, exit_manager, "WHALE"))

    async def fetch_positions(client, address):
        try:
            r = await client.get(f"{POLY_DATA_API}/positions", params={"user": address, "sizeThreshold": str(MIN_WHALE_SIZE)}, timeout=10)
            r.raise_for_status()
            d = r.json()
            return d if isinstance(d, list) else []
        except: return []

    async def fetch_candidates(wallets):
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(*[fetch_positions(client, w.address) for w in wallets[:100]], return_exceptions=True)
        whale_map = defaultdict(lambda: {"YES": 0, "NO": 0})
        total = 0
        for r in results:
            if not isinstance(r, list): continue
            for pos in r:
                cid  = pos.get("conditionId", "")
                side = pos.get("outcome", "").upper()
                if cid and side in ("YES", "NO") and float(pos.get("curPrice") or 0) > 0:
                    whale_map[cid][side] += 1
                    total += 1
        candidates = [(cid, side, count) for cid, sides in whale_map.items() for side, count in sides.items() if count >= MIN_WHALES]
        candidates.sort(key=lambda x: x[2], reverse=True)
        log.info(f"[WHALE] {total} posições → {len(candidates)} candidatos")
        return candidates, total

    async def fetch_markets(condition_ids):
        import json as _json
        markets = []
        now = datetime.now(timezone.utc)
        async with httpx.AsyncClient() as client:
            for cid in condition_ids[:80]:
                try:
                    r = await client.get(f"{GAMMA_API}/markets", params={"conditionIds": cid}, timeout=10)
                    r.raise_for_status()
                    for m in (r.json() if isinstance(r.json(), list) else [r.json()]):
                        try:
                            prices_raw = m.get("outcomePrices", '["0.5","0.5"]')
                            if isinstance(prices_raw, str): prices_raw = _json.loads(prices_raw)
                            liquidity = float(m.get("liquidity", 0))
                            if liquidity < 500: continue
                            end = (m.get("endDate") or m.get("endDateIso","")).rstrip("Z")
                            if not end: continue
                            if "T" not in end: end += "T23:59:59"
                            resolves_at = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
                            hours_left  = (resolves_at - now).total_seconds() / 3600
                            if hours_left < 1: continue
                            markets.append(Market(
                                condition_id=m["conditionId"], question=m.get("question",""),
                                yes_price=float(prices_raw[0]), no_price=float(prices_raw[1]),
                                volume_usdc=float(m.get("volume24hr") or m.get("volume") or 0),
                                liquidity_usdc=liquidity, spread=float(m.get("spread",1)),
                                resolves_at=resolves_at, hours_to_resolve=hours_left,
                            ))
                        except: continue
                except: continue
        return markets

    while True:
        if not is_started_whale() or is_paused_whale():
            await asyncio.sleep(10)
            continue

        if executor.no_balance:
            log.info("[WHALE] Sem saldo — a aguardar reconcile do scorer...")
            await asyncio.sleep(5 * 60)
            continue

        _whale_cycle += 1
        console.rule(f"[bold purple]Whale · Ciclo {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

        try:
            if len(portfolio.positions) >= portfolio.max_open:
                log.info(f"[WHALE] Máximo de posições atingido — a saltar scoring.")
                _write_dashboard(DATA_DIR / "dashboard_data_whale.json", _whale_cycle, 0, 0, {"whale_lead":0,"arbitrage":0,"convergence":0}, 0, [], portfolio, list(_log_buffer_whale))
                await asyncio.sleep(RUN_INTERVAL_MINS * 60)
                continue

            wallets    = await get_wallets_cached()
            candidates, total_pos = await fetch_candidates(wallets)
            if not candidates:
                _write_dashboard(DATA_DIR / "dashboard_data_whale.json", _whale_cycle, 0, 0, {"whale_lead":0,"arbitrage":0,"convergence":0}, 0, [], portfolio, list(_log_buffer_whale))
                await asyncio.sleep(RUN_INTERVAL_MINS * 60)
                continue

            whale_markets = await fetch_markets([c[0] for c in candidates[:80]])

            # Usa cache de scoring do scorer se disponível e recente
            now_ts = time.time()
            if _scoring_cache["markets"] and (now_ts - _scoring_cache["last_update"]) < SCORING_CACHE_TTL:
                scored_ids_from_cache = {m.condition_id: m for m in _scoring_cache["markets"]}
                scored_from_cache = [scored_ids_from_cache[c[0]] for c in candidates if c[0] in scored_ids_from_cache]

                if scored_from_cache:
                    log.info(f"[WHALE] Usando cache de scoring: {len(scored_from_cache)} mercados (sem chamadas Claude)")
                    scored = scored_from_cache
                else:
                    log.info(f"[WHALE] Candidatos não no cache — a score {len(whale_markets)} mercados whale")
                    scored = _score(whale_markets, min_score=MIN_SCORE)
            else:
                log.info(f"[WHALE] Cache expirado — a score {len(whale_markets)} mercados whale")
                scored = _score(whale_markets, min_score=MIN_SCORE)

            scored_ids = {m.condition_id: m for m in scored}
            whale_sigs = []
            for cid, side_str, n in candidates:
                if cid not in scored_ids: continue
                m    = scored_ids[cid]
                side = Side.YES if side_str == "YES" else Side.NO
                price = m.yes_price if side == Side.YES else m.no_price
                whale_sigs.append(AgentSignal(agent=AgentName.WHALE_COPY, market=m, side=side, confidence=min(n/5,1.0), reason=f"{n} whales em {side_str}", suggested_price=price+0.01))

            arb_sigs  = ArbitrageAgent().analyze(scored, wallets)
            conv_sigs = ConvergenceAgent().analyze(scored, wallets)
            all_sigs  = whale_sigs + arb_sigs + conv_sigs
            signals   = {"whale_lead": len(whale_sigs), "arbitrage": len(arb_sigs), "convergence": len(conv_sigs)}

            decisions      = build_consensus(all_sigs)
            consensus_full = sum(1 for d in decisions if d.consensus_count >= 2)

            for d in decisions:
                if not portfolio.can_trade(): break
                if portfolio.already_open(d.market.condition_id): continue
                pos = executor.execute(d)
                if pos: portfolio.add_position(pos)

            _print_decisions(decisions[:5], "purple")

            if portfolio.positions:
                if not _exit_task_whale or _exit_task_whale.done():
                    _exit_task_whale = asyncio.create_task(_exit_background(portfolio, exit_manager, "WHALE"))

            console.print(f"[bold purple]{portfolio.summary()}")
            _write_dashboard(DATA_DIR / "dashboard_data_whale.json", _whale_cycle, len(candidates), len(scored), signals, consensus_full, decisions, portfolio, list(_log_buffer_whale))

            # Reconcilia whale após cada ciclo (só remove resolvidas, não adiciona)
            await reconcile_portfolio(portfolio, POLY_PROXY_ADDRESS, "WHALE")

        except Exception as e:
            log.error(f"[WHALE] Erro: {e}", exc_info=True)

        await asyncio.sleep(RUN_INTERVAL_MINS * 60)


# ── Utils ─────────────────────────────────────────────────────────────────────

def _print_decisions(decisions, color):
    if not decisions: return
    t = Table(show_header=True, header_style=f"bold {color}")
    t.add_column("Mercado", max_width=40)
    t.add_column("Side", width=5)
    t.add_column("Cons", width=5)
    t.add_column("Size", width=8)
    t.add_column("Entry", width=7)
    for d in decisions:
        t.add_row(d.market.question[:40], d.side.value, f"{'★'*d.consensus_count}", f"${d.size_usdc:.0f}", f"{d.entry_price:.3f}")
    console.print(t)


# ── Entry ─────────────────────────────────────────────────────────────────────

async def main():
    console.print("[bold]PolyBot Combined v1.0[/bold] — a iniciar...")
    console.print(f"Modo: {'[yellow]DRY RUN[/yellow]' if DRY_RUN else '[red bold]LIVE[/red bold]'}")

    start_server()

    executor = Executor(
        private_key    = POLY_PRIVATE_KEY,
        api_key        = POLY_API_KEY,
        api_secret     = POLY_API_SECRET,
        api_passphrase = POLY_API_PASSPHRASE,
        proxy_addr     = POLY_PROXY_ADDRESS,
        dry_run        = DRY_RUN,
    )

    scorer_portfolio = Portfolio(max_open=MAX_OPEN_POSITIONS, max_daily=MAX_DAILY_TRADES,
                                  state_file=DATA_DIR / "portfolio_state.json")
    whale_portfolio  = Portfolio(max_open=MAX_OPEN_POSITIONS, max_daily=MAX_DAILY_TRADES,
                                  state_file=DATA_DIR / "portfolio_state_whale.json")

    scorer_exit = ExitManager(executor)
    whale_exit  = ExitManager(executor)

    await asyncio.gather(
        scorer_loop(executor, scorer_portfolio, scorer_exit),
        whale_loop(executor, whale_portfolio, whale_exit),
    )


if __name__ == "__main__":
    asyncio.run(main())
