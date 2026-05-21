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
    GAMMA_API, CLAUDE_MODEL, FULL_SIZE_USDC, HALF_SIZE_USDC,
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

# Posições reais da Poly (scorer proxy)
_real_positions: dict = {"scorer": []}

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


def _write_dashboard(file: Path, cycle, markets_total, markets_scored, signals, consensus_full, decisions, portfolio, log_buf, real_pos=None):
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
        "open_positions": (
            [
                {
                    "trade_id":      p.get("proxyWallet", "")[:8],
                    "question":      p.get("title", ""),
                    "side":          p.get("outcome", "YES"),
                    "size_usdc":     round(float(p.get("initialValue") or 0), 2),
                    "entry_price":   round(float(p.get("avgPrice") or 0), 3),
                    "current_price": round(float(p.get("curPrice") or 0), 3),
                    "target_exit":   0,
                    "pnl":           round(float(p.get("cashPnl") or 0), 2),
                    "resolves_in":   "",
                }
                for p in real_pos
            ] if real_pos else [
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
            ]
        ),
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
        await exit_manager.monitor_loop(portfolio)
    except Exception as e:
        log.error(f"Erro exit manager {label}: {e}")


# ── Reconcile background (scorer only, a cada 5min) ──────────────────────────

async def _reconcile_background_loop(portfolio):
    """Corre reconcile do scorer a cada 5 minutos em background."""
    await asyncio.sleep(60)  # espera 1 min após arranque
    while True:
        try:
            from reconcile import fetch_real_positions
            real = await fetch_real_positions(POLY_PROXY_ADDRESS)
            _real_positions["scorer"] = real
            await reconcile_portfolio(portfolio, POLY_PROXY_ADDRESS, "SCORER")
        except Exception as e:
            log.error(f"[SCORER/RECONCILE-BG] Erro: {e}")
        await asyncio.sleep(5 * 60)


# ── SCORER LOOP ───────────────────────────────────────────────────────────────


def _has_opposite_side(portfolio, condition_id: str, side) -> bool:
    """Verifica se já existe posição no lado oposto do mesmo mercado."""
    from data.models import Side
    opposite = Side.NO if side == Side.YES else Side.YES
    return any(
        p.market.condition_id == condition_id and p.side == opposite
        for p in portfolio.positions
    )

async def scorer_loop(executor, portfolio, exit_manager, whale_portfolio=None):
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
                _write_dashboard(DATA_DIR / "dashboard_data.json", _scorer_cycle, 0, 0, {}, 0, [], portfolio, list(_log_buffer_scorer), real_pos=_real_positions.get("scorer"))
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
                # Dedup cross-bot: não comprar se whale já tem posição no mesmo mercado
                if whale_portfolio and whale_portfolio.already_open(d.market.condition_id): continue
                # Não comprar lado oposto de posição já aberta
                if _has_opposite_side(portfolio, d.market.condition_id, d.side): continue
                if whale_portfolio and _has_opposite_side(whale_portfolio, d.market.condition_id, d.side): continue
                pos = executor.execute(d)
                if pos: portfolio.add_position(pos)

            _print_decisions(decisions[:5], "cyan")

            if portfolio.positions:
                if not _exit_task_scorer or _exit_task_scorer.done():
                    _exit_task_scorer = asyncio.create_task(_exit_background(portfolio, exit_manager, "SCORER"))

            console.print(f"[bold cyan]{portfolio.summary()}")
            _write_dashboard(DATA_DIR / "dashboard_data.json", _scorer_cycle, len(all_markets), len(scored), signals, consensus_full, decisions, portfolio, list(_log_buffer_scorer), real_pos=_real_positions.get("scorer"))

        except Exception as e:
            log.error(f"[SCORER] Erro: {e}", exc_info=True)

        await asyncio.sleep(RUN_INTERVAL_MINS * 60)


# ── WHALE LOOP (copy trader — 4 wallets fixas) ────────────────────────────────

WHALE_COPY_WALLETS = [
    "0x204f72f35326db932158cba6adff0b9a1da95e14",
    "0x9495425feeb0c250accb89275c97587011b19a27",
    "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
    "0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a",
]

WHALE_QUESTION_BLACKLIST = ["rihanna"]

async def whale_loop(executor, portfolio, exit_manager, scorer_portfolio=None):
    global _whale_cycle, _exit_task_whale

    import httpx
    from datetime import date, timedelta
    from data.models import Side, Market, AgentSignal, AgentName
    from consensus import TradeDecision

    POLY_DATA_API = "https://data-api.polymarket.com"
    _first_run = True

    if portfolio.positions:
        log.info(f"[WHALE] {len(portfolio.positions)} posições carregadas — exit manager a iniciar...")
        _exit_task_whale = asyncio.create_task(_exit_background(portfolio, exit_manager, "WHALE"))

    async def fetch_wallet_positions(client, address):
        try:
            all_positions = []
            offset = 0
            while True:
                r = await client.get(
                    f"{POLY_DATA_API}/positions",
                    params={"user": address, "sizeThreshold": "0.01", "limit": 100, "offset": offset},
                    timeout=15,
                )
                r.raise_for_status()
                d = r.json()
                if not isinstance(d, list) or not d:
                    break
                all_positions.extend(d)
                if len(d) < 100:
                    break
                offset += 100
            return all_positions
        except Exception as e:
            log.warning(f"[WHALE] Erro fetch {address[:10]}: {e}")
            return []

    while True:
        if not is_started_whale() or is_paused_whale():
            _first_run = True
            await asyncio.sleep(10)
            continue

        if executor.no_balance:
            log.info("[WHALE] Sem saldo...")
            await asyncio.sleep(5 * 60)
            continue

        _whale_cycle += 1
        console.rule(f"[bold purple]Whale · Ciclo {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

        try:
            if len(portfolio.positions) >= portfolio.max_open:
                log.info("[WHALE] Máximo de posições atingido.")
                await asyncio.sleep(RUN_INTERVAL_MINS * 60)
                continue

            # Fetch posições das 4 wallets
            async with httpx.AsyncClient() as client:
                results = await asyncio.gather(
                    *[fetch_wallet_positions(client, addr) for addr in WHALE_COPY_WALLETS],
                    return_exceptions=True,
                )

            # Agrega: (condition_id, side) → melhor pos da Data API
            from collections import defaultdict
            wallet_map: dict[tuple, set] = defaultdict(set)
            pos_data: dict[tuple, dict] = {}  # guarda dados da posição para usar depois

            cutoff = date.today() + timedelta(hours=24)

            for addr, res in zip(WHALE_COPY_WALLETS, results):
                if not isinstance(res, list):
                    continue
                for pos in res:
                    cid       = pos.get("conditionId", "")
                    side      = pos.get("outcome", "").upper()
                    cur_price = float(pos.get("curPrice") or 0)
                    end_date_str = pos.get("endDate", "")
                    if not cid or side not in ("YES", "NO"):
                        continue
                    if not (0.02 < cur_price < 0.98):
                        continue
                    try:
                        end_date = date.fromisoformat(end_date_str[:10])
                        if end_date > cutoff:
                            continue
                    except Exception:
                        continue
                    wallet_map[(cid, side)].add(addr)
                    pos_data[(cid, side)] = pos

            log.info(f"[WHALE] {len(wallet_map)} posições candidatas ({sum(1 for v in wallet_map.values() if len(v)>=2)} com 2+ wallets)")

            # Filtra já abertas e lado oposto
            candidates = {}
            for (cid, side_str), wallets in wallet_map.items():
                if portfolio.already_open(cid):
                    continue
                if _has_opposite_side(portfolio, cid, Side.YES if side_str == "YES" else Side.NO):
                    continue
                if scorer_portfolio and scorer_portfolio.already_open(cid):
                    continue
                candidates[(cid, side_str)] = wallets

            log.info(f"[WHALE] {len(candidates)} candidatos após dedup")

            # Ordena: mais wallets primeiro, depois endDate ascendente
            sorted_candidates = sorted(
                candidates.items(),
                key=lambda x: (-len(x[1]), pos_data[x[0]].get("endDate", "9999"))
            )

            new_trades = 0
            bought_cids: set[str] = set()

            for (cid, side_str), wallets in sorted_candidates:
                if not portfolio.can_trade():
                    log.info("[WHALE] can_trade=False — stop")
                    break

                # Blacklist
                title = pos_data[(cid, side_str)].get("title", "")
                if any(b.lower() in title.lower() for b in WHALE_QUESTION_BLACKLIST):
                    continue

                # Dedup — não comprar YES e NO do mesmo mercado
                if cid in bought_cids:
                    continue

                p         = pos_data[(cid, side_str)]
                cur_price = float(p.get("curPrice") or 0)
                end_str   = p.get("endDate", "")
                n         = len(wallets)

                # Constrói Market a partir dos dados da Data API
                try:
                    end_str_clean = end_str.rstrip("Z")
                    if "T" not in end_str_clean:
                        end_str_clean += "T23:59:59"
                    resolves_at = datetime.fromisoformat(end_str_clean).replace(tzinfo=timezone.utc)
                    hours_left  = (resolves_at - datetime.now(timezone.utc)).total_seconds() / 3600
                except Exception:
                    continue

                side = Side.YES if side_str == "YES" else Side.NO
                yes_price = cur_price if side == Side.YES else round(1 - cur_price, 4)
                no_price  = cur_price if side == Side.NO  else round(1 - cur_price, 4)

                market = Market(
                    condition_id     = cid,
                    question         = title,
                    yes_price        = yes_price,
                    no_price         = no_price,
                    volume_usdc      = float(p.get("initialValue") or 0),
                    liquidity_usdc   = 0.0,
                    spread           = 0.02,
                    resolves_at      = resolves_at,
                    hours_to_resolve = hours_left,
                )

                price = cur_price
                if n >= 4:
                    size_usdc = FULL_SIZE_USDC
                elif n >= 2:
                    size_usdc = HALF_SIZE_USDC
                else:
                    size_usdc = HALF_SIZE_USDC / 2

                sig = AgentSignal(
                    agent           = AgentName.WHALE_COPY,
                    market          = market,
                    side            = side,
                    confidence      = min(1.0, n / 4),
                    reason          = f"{n} wallet(s) {side_str} @ {price:.2f}",
                    suggested_price = price + 0.01,
                )
                decision = TradeDecision(
                    market            = market,
                    side              = side,
                    consensus_count   = n,
                    size_usdc         = size_usdc,
                    entry_price       = price + 0.01,
                    target_exit_price = round(price + 0.01 + 0.90 * (1.0 - (price + 0.01)), 4),
                    signals           = [sig],
                )

                if hours_left <= 0:
                    continue
                log.info(f"[WHALE] {side_str} {title[:45]} @ {price:.2f} [{n}w] {hours_left:.0f}h")
                try:
                    pos = executor.execute(decision)
                except Exception as ex:
                    log.error(f"[WHALE/EXEC-ERR] {ex}")
                    pos = None
                if pos:
                    portfolio.add_position(pos)
                    bought_cids.add(cid)
                    new_trades += 1
                    log.info(f"[WHALE/OK] {side_str} {title[:45]} @ {price:.2f}")
                else:
                    log.warning(f"[WHALE/FAIL] {side_str} {title[:45]}")

            if new_trades:
                log.info(f"[WHALE] {new_trades} novas posições abertas")

            if portfolio.positions:
                if not _exit_task_whale or _exit_task_whale.done():
                    _exit_task_whale = asyncio.create_task(_exit_background(portfolio, exit_manager, "WHALE"))

            console.print(f"[bold purple]{portfolio.summary()}")
            _write_dashboard(DATA_DIR / "dashboard_data_whale.json", _whale_cycle, len(wallet_map), new_trades, {"whale_copy": new_trades}, 0, [], portfolio, list(_log_buffer_whale))
            await reconcile_portfolio(portfolio, POLY_PROXY_ADDRESS, "WHALE")

        except Exception as e:
            log.error(f"[WHALE] Erro: {e}", exc_info=True)

        if _first_run:
            _first_run = False
        else:
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
    whale_executor = Executor(
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
    whale_exit  = ExitManager(whale_executor)

    await asyncio.gather(
        scorer_loop(executor, scorer_portfolio, scorer_exit, whale_portfolio),
        whale_loop(whale_executor, whale_portfolio, whale_exit, scorer_portfolio),
    )


if __name__ == "__main__":
    asyncio.run(main())
