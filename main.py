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
)
from data.fetcher           import get_filtered_markets
from data.wallet_scanner    import get_top_wallets
from scoring.market_scorer  import score_markets
from agents.agents          import ArbitrageAgent, ConvergenceAgent, WhaleCopyAgent
from consensus              import build_consensus
from execution.executor     import Executor
from execution.exit_manager import ExitManager
from portfolio              import Portfolio
from dashboard_writer       import write as write_dashboard
from server                 import start_server, is_paused

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt = "%H:%M:%S",
)
log     = logging.getLogger("main")
console = Console()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

_wallet_cache = {"data": [], "last_update": 0.0}
WALLET_CACHE_TTL = 6 * 3600

_log_buffer: list[str] = []

class DashboardLogHandler(logging.Handler):
    def emit(self, record):
        _log_buffer.append(self.format(record))
        if len(_log_buffer) > 100:
            _log_buffer.pop(0)

_dash_handler = DashboardLogHandler()
_dash_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(_dash_handler)

_cycle_count   = 0
_exit_task     = None   # task de background para o exit manager


async def get_wallets_cached():
    now = time.time()
    if now - _wallet_cache["last_update"] > WALLET_CACHE_TTL:
        log.info("A actualizar cache de wallets…")
        _wallet_cache["data"]        = await get_top_wallets()
        _wallet_cache["last_update"] = now
        log.info(f"  → {len(_wallet_cache['data'])} wallets em cache")
    return _wallet_cache["data"]


async def run_cycle(portfolio: Portfolio, executor: Executor, exit_manager: ExitManager):
    global _cycle_count, _exit_task
    _cycle_count += 1

    console.rule(f"[bold cyan]Ciclo {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    # 1. Mercados
    log.info("A puxar mercados…")

    # Salta scoring se já estamos no limite de posições abertas
    if len(portfolio.positions) >= portfolio.max_open:
        log.info(f"Máximo de posições abertas atingido ({len(portfolio.positions)}/{portfolio.max_open}) — a saltar scoring.")
        write_dashboard(_cycle_count, 0, 0, {}, 0, [], portfolio, list(_log_buffer))
        return

    all_markets = await get_filtered_markets()
    markets_total = len(all_markets)
    log.info(f"  → {markets_total} mercados após filtros de API")

    if not all_markets:
        log.warning("Sem mercados disponíveis.")
        return

    # 2. Scoring
    log.info("A pontuar com Claude…")
    scored_markets = score_markets(all_markets)
    markets_scored = len(scored_markets)
    log.info(f"  → {markets_scored} mercados com edge detectado")

    if not scored_markets:
        write_dashboard(_cycle_count, markets_total, 0, {}, 0, [], portfolio, list(_log_buffer))
        return

    # 3. Wallets
    wallets = await get_wallets_cached()

    # 4. Agentes
    arb_sigs   = ArbitrageAgent().analyze(scored_markets, wallets)
    conv_sigs  = ConvergenceAgent().analyze(scored_markets, wallets)
    whale_sigs = await WhaleCopyAgent().analyze_async(scored_markets, wallets)

    all_signals = arb_sigs + conv_sigs + whale_sigs
    signals_summary = {
        "arbitrage":   len(arb_sigs),
        "convergence": len(conv_sigs),
        "whale_copy":  len(whale_sigs),
    }
    log.info(f"  → {len(all_signals)} sinais totais dos 3 agentes")

    # 5. Consensus
    decisions      = build_consensus(all_signals)
    consensus_full = sum(1 for d in decisions if d.consensus_count >= 2)

    # 6. Execução
    for decision in decisions:
        if not portfolio.can_trade():
            break
        if portfolio.already_open(decision.market.condition_id):
            continue
        pos = executor.execute(decision)
        if pos:
            portfolio.add_position(pos)

    _print_decisions(decisions[:5])

    # 7. Exit manager em background — não bloqueia o ciclo
    if portfolio.positions:
        # Cancela task anterior se ainda estiver activa
        if _exit_task and not _exit_task.done():
            log.info(f"Exit manager já activo com {len(portfolio.positions)} posições")
        else:
            log.info(f"A lançar exit manager em background ({len(portfolio.positions)} posições)…")
            _exit_task = asyncio.create_task(
                _exit_background(portfolio, exit_manager)
            )

    # 8. Resumo
    console.print(f"\n[bold green]{portfolio.summary()}")

    # 9. Dashboard
    write_dashboard(
        _cycle_count, markets_total, markets_scored,
        signals_summary, consensus_full,
        decisions, portfolio, list(_log_buffer),
    )


async def _exit_background(portfolio: Portfolio, exit_manager: ExitManager):
    """Corre o exit manager em background sem bloquear o loop principal."""
    try:
        results = await exit_manager.monitor_loop(list(portfolio.positions))
        for r in results:
            portfolio.close_position(r)
            log.info(f"[CLOSED] {r.trade_id} — PnL ${r.pnl_usdc:+.2f} ({r.exit_reason})")
    except Exception as e:
        log.error(f"Erro no exit manager background: {e}")


def _print_decisions(decisions):
    if not decisions:
        return
    t = Table(title="Top Decisões", show_header=True, header_style="bold magenta")
    t.add_column("Mercado",  max_width=45)
    t.add_column("Side",     width=5)
    t.add_column("Consenso", width=9)
    t.add_column("Size",     width=8)
    t.add_column("Entry",    width=7)
    t.add_column("Target",   width=7)

    for d in decisions:
        stars = f"{'★' * d.consensus_count}{'☆' * (3 - d.consensus_count)}"
        t.add_row(
            d.market.question[:45], d.side.value, stars,
            f"${d.size_usdc:.0f}", f"{d.entry_price:.3f}", f"{d.target_exit_price:.3f}",
        )
    console.print(t)


async def main():
    console.print("[bold]PolyBot v1.0[/bold] — a iniciar…")
    console.print(f"Modo: {'[yellow]DRY RUN[/yellow]' if DRY_RUN else '[red]LIVE[/red]'}")

    # Inicia servidor HTTP para dashboard
    start_server()

    executor     = Executor(
        private_key    = POLY_PRIVATE_KEY,
        api_key        = POLY_API_KEY,
        api_secret     = POLY_API_SECRET,
        api_passphrase = POLY_API_PASSPHRASE,
        proxy_addr  = POLY_PROXY_ADDRESS,
        dry_run     = DRY_RUN,
    )
    portfolio    = Portfolio(max_open=MAX_OPEN_POSITIONS, max_daily=MAX_DAILY_TRADES)
    exit_manager = ExitManager(executor)

    while True:
        if is_paused():
            log.info("Bot em pausa — aguarda retoma via dashboard.")
        else:
            try:
                await run_cycle(portfolio, executor, exit_manager)
            except Exception as e:
                log.error(f"Erro no ciclo: {e}", exc_info=True)

        log.info(f"A aguardar {RUN_INTERVAL_MINS} minutos…\n")
        await asyncio.sleep(RUN_INTERVAL_MINS * 60)


if __name__ == "__main__":
    asyncio.run(main())
