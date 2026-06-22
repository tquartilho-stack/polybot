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
from server                 import start_server, is_paused, is_started, is_paused_whale, is_started_whale, is_paused_crypto, is_started_crypto
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


# ── WHALE LOOP v3 — live copy trader (1 wallet, 10s polling) ─────────────────

WHALE_COPY_WALLETS = [
    "0x9f2fe025f84839ca81dd8e0338892605702d2ca8",
    "0xb91aeb5accc33a5f9a8615b8ed6b2d352e913987",
    "0x9703676286b93c2eca71ca96e8757104519a69c2",
    "0x84dbb7103982e3617704a2ed7d5b39691952aeeb",
]
WHALE_POLL_SECS   = 10
WHALE_BET_USDC    = 5.0   # tamanho fixo por bet

async def whale_loop(executor, portfolio, exit_manager, scorer_portfolio=None):
    global _whale_cycle, _exit_task_whale

    import httpx
    from data.models import Side, Market, AgentSignal, AgentName
    from consensus import TradeDecision

    POLY_DATA_API = "https://data-api.polymarket.com"

    log.info(f"[WHALE] Live copy trader iniciado — {len(WHALE_COPY_WALLETS)} wallets poll={WHALE_POLL_SECS}s")

    if portfolio.positions:
        log.info(f"[WHALE] {len(portfolio.positions)} posições carregadas")
        _exit_task_whale = asyncio.create_task(_exit_background(portfolio, exit_manager, "WHALE"))

    copied_event_slugs: set[str] = set()
    log.info(f"[WHALE] Live copy — verifica hoje ET + não aberto na Poly")

    while True:
        if not is_started_whale() or is_paused_whale():
            copied_event_slugs.clear()
            await asyncio.sleep(10)
            continue

        await asyncio.sleep(WHALE_POLL_SECS)

        try:
            async with httpx.AsyncClient() as client:
                results = await asyncio.gather(*[
                    client.get(f"{POLY_DATA_API}/trades", params={"user": w, "limit": 50}, timeout=10)
                    for w in WHALE_COPY_WALLETS
                ], return_exceptions=True)
                trades = []
                for res in results:
                    if not isinstance(res, Exception) and res.is_success:
                        d = res.json()
                        if isinstance(d, list):
                            trades.extend(d)

            buys = [t for t in trades if t.get("side","").upper() == "BUY"]
            log.info(f"[WHALE] poll: {len(trades)} trades, {len(buys)} BUYs")
            if not buys:
                continue

            _whale_cycle += 1

            from reconcile import fetch_real_positions
            _real = await fetch_real_positions(POLY_PROXY_ADDRESS)
            _real_slugs = {p.get("eventSlug","") for p in _real if p.get("eventSlug","") and float(p.get("curPrice",0)) > 0.02}

            async with httpx.AsyncClient() as client:
                for t in buys:
                    cid       = t.get("conditionId","")
                    outcome   = t.get("outcome","")
                    price     = float(t.get("price", 0))
                    title     = t.get("title","")
                    # outcome no /trades é o nome do jogador/side — normaliza
                    outcome_idx = t.get("outcomeIndex", -1)
                    side_str = "YES" if outcome_idx == 0 else "NO" if outcome_idx == 1 else ""

                    if not cid or not side_str or price <= 0:
                        continue

                    # Filtro 1: price 0.05-0.95
                    if price < 0.05 or price > 0.95:
                        log.info(f"[WHALE] SKIP price={price:.2f}: {title[:40]}")
                        continue

                    # Filtro 2: World Cup (título ou eventSlug "fifwc")
                    _ev_slug_check = t.get("eventSlug","")
                    if "world cup" in title.lower() or "fifa" in title.lower() or _ev_slug_check.startswith("fifwc"):
                        log.info(f"[WHALE] SKIP world cup: {title[:40]}")
                        continue

                    # Filtro 3: endDate > 7 dias (via CLOB) — sem data, rejeita
                    try:
                        from datetime import timedelta, date as _date
                        _clob_r = await client.get(f"https://clob.polymarket.com/markets/{cid}", timeout=5)
                        if not _clob_r.is_success:
                            log.info(f"[WHALE] SKIP sem CLOB: {title[:40]}")
                            continue
                        _end_raw = _clob_r.json().get("end_date_iso","")
                        if not _end_raw:
                            log.info(f"[WHALE] SKIP sem data: {title[:40]}")
                            continue
                        _end_date = _date.fromisoformat(_end_raw[:10])
                        _cutoff = (datetime.now(timezone.utc) + timedelta(hours=48)).date()
                        if _end_date > _cutoff:
                            log.info(f"[WHALE] SKIP >7d {_end_raw[:10]}: {title[:40]}")
                            continue
                    except Exception as _ce:
                        log.info(f"[WHALE] SKIP CLOB err {cid[:10]}: {_ce}")
                        continue

                    # Filtro 4: já tens na Poly? (por eventSlug)
                    event_slug = t.get("eventSlug","")
                    if event_slug and event_slug in _real_slugs:
                        log.info(f"[WHALE] SKIP já na Poly: {title[:40]}")
                        continue

                    # Filtro 5: dedup por eventSlug neste ciclo
                    if event_slug and event_slug in copied_event_slugs:
                        log.info(f"[WHALE] SKIP dedup: {title[:40]}")
                        continue

                    log.info(f"[WHALE] PRE-COPY: {side_str} {title[:40]} ({outcome[:20]}) @ {price:.2f}")
                    if not portfolio.can_trade():
                        log.info("[WHALE] can_trade=False — stop")
                        break

                    side = Side.YES if side_str == "YES" else Side.NO

                    # Busca endDate via posições da wallet
                    end_str = t.get("endDate","")
                    try:
                        from datetime import timedelta
                        if end_str:
                            end_clean = end_str.rstrip("Z")
                            if "T" not in end_clean:
                                end_clean += "T23:59:59"
                            resolves_at = datetime.fromisoformat(end_clean).replace(tzinfo=timezone.utc)
                        else:
                            resolves_at = datetime.now(timezone.utc) + timedelta(hours=24)
                        hours_left = (resolves_at - datetime.now(timezone.utc)).total_seconds() / 3600
                        if hours_left <= 0:
                            log.info(f"[WHALE] SKIP expirado: {title[:40]}")
                            continue
                    except Exception:
                        resolves_at = datetime.now(timezone.utc) + timedelta(hours=24)
                        hours_left  = 24

                    yes_price = price if side == Side.YES else round(1 - price, 4)
                    no_price  = price if side == Side.NO  else round(1 - price, 4)

                    market = Market(
                        condition_id     = cid,
                        question         = title,
                        yes_price        = yes_price,
                        no_price         = no_price,
                        volume_usdc      = 0.0,
                        liquidity_usdc   = 0.0,
                        spread           = 0.02,
                        resolves_at      = resolves_at,
                        hours_to_resolve = hours_left,
                    )

                    sig = AgentSignal(
                        agent           = AgentName.WHALE_COPY,
                        market          = market,
                        side            = side,
                        confidence      = 1.0,
                        reason          = f"live copy {outcome[:20]} @ {price:.2f}",
                        suggested_price = price + 0.01,
                    )
                    decision = TradeDecision(
                        market            = market,
                        side              = side,
                        consensus_count   = 1,
                        size_usdc         = WHALE_BET_USDC,
                        entry_price       = price + 0.01,
                        target_exit_price = round(price + 0.01 + 0.90 * (1.0 - (price + 0.01)), 4),
                        signals           = [sig],
                    )

                    log.info(f"[WHALE] COPY {outcome} {title[:45]} @ {price:.2f} {hours_left:.0f}h")
                    try:
                        pos = executor.execute(decision)
                    except Exception as ex:
                        log.error(f"[WHALE/ERR] {ex}")
                        pos = None

                    if pos:
                        portfolio.add_position(pos)
                        if event_slug:
                            copied_event_slugs.add(event_slug)
                            _real_slugs.add(event_slug)
                        log.info(f"[WHALE/OK] {side_str} {title[:45]} ({outcome[:15]}) @ {price:.2f}")
                    else:
                        log.warning(f"[WHALE/FAIL] {side_str} {title[:45]}")

            if portfolio.positions:
                if not _exit_task_whale or _exit_task_whale.done():
                    _exit_task_whale = asyncio.create_task(_exit_background(portfolio, exit_manager, "WHALE"))

            _write_dashboard(DATA_DIR / "dashboard_data_whale.json", _whale_cycle, 1, 0, {"whale_copy": 0}, 0, [], portfolio, list(_log_buffer_whale))
            await reconcile_portfolio(portfolio, POLY_PROXY_ADDRESS, "WHALE")

        except Exception as e:
            log.error(f"[WHALE] Erro: {e}", exc_info=True)


# ── CRYPTO LOOP (fast copy trader — 2s polling, accepting_orders filter) ─────────

CRYPTO_COPY_WALLET = "0xb55fa1296e6ec55d0ce53d93b9237389f11764d4"
CRYPTO_POLL_SECS   = 2
CRYPTO_BET_USDC    = 5.0

async def crypto_loop(executor, portfolio, exit_manager):
    global _exit_task_whale  # reuse exit task pattern

    import httpx
    from data.models import Side, Market, AgentSignal, AgentName
    from consensus import TradeDecision

    POLY_DATA_API = "https://data-api.polymarket.com"
    CLOB_API_URL  = "https://clob.polymarket.com"

    _cycle = 0
    copied_event_slugs: set[str] = set()
    _exit_task = None

    log.info(f"[CRYPTO] Live copy trader iniciado — wallet {CRYPTO_COPY_WALLET[:10]}... poll={CRYPTO_POLL_SECS}s")

    if portfolio.positions:
        log.info(f"[CRYPTO] {len(portfolio.positions)} posições carregadas")
        _exit_task = asyncio.create_task(_exit_background(portfolio, exit_manager, "CRYPTO"))

    while True:
        if not is_started_crypto() or is_paused_crypto():
            copied_event_slugs.clear()
            await asyncio.sleep(2)
            continue

        await asyncio.sleep(CRYPTO_POLL_SECS)

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{POLY_DATA_API}/trades",
                    params={"user": CRYPTO_COPY_WALLET, "limit": 50},
                    timeout=8,
                )
                if not r.is_success:
                    continue
                trades = r.json() if isinstance(r.json(), list) else []

            buys = [t for t in trades if t.get("side","").upper() == "BUY"]
            if not buys:
                continue

            _cycle += 1

            from reconcile import fetch_real_positions
            _real = await fetch_real_positions(POLY_PROXY_ADDRESS)
            _real_cids = {p.get("conditionId","") for p in _real if float(p.get("curPrice",0)) > 0.02}

            async with httpx.AsyncClient() as client:
                for t in buys:
                    cid       = t.get("conditionId","")
                    price     = float(t.get("price", 0))
                    title     = t.get("title","")
                    event_slug = t.get("eventSlug","")
                    outcome_idx = t.get("outcomeIndex", -1)
                    side_str = "YES" if outcome_idx == 0 else "NO" if outcome_idx == 1 else ""

                    if not cid or not side_str or price <= 0:
                        continue
                    if price < 0.05 or price > 0.95:
                        continue
                    if cid in _real_cids:
                        continue
                    if event_slug and event_slug in copied_event_slugs:
                        continue
                    if portfolio.already_open(cid):
                        continue

                    # Filtro: mercado ainda aceita ordens (via CLOB)
                    try:
                        _clob_r = await client.get(f"{CLOB_API_URL}/markets/{cid}", timeout=3)
                        if not _clob_r.is_success:
                            continue
                        _clob = _clob_r.json()
                        if not _clob.get("accepting_orders", False):
                            log.info(f"[CRYPTO] SKIP not accepting: {title[:40]}")
                            continue
                    except Exception:
                        continue

                    if not portfolio.can_trade():
                        log.info("[CRYPTO] can_trade=False — stop")
                        break

                    side = Side.YES if side_str == "YES" else Side.NO

                    from datetime import timedelta
                    resolves_at = datetime.now(timezone.utc) + timedelta(hours=1)
                    market = Market(
                        condition_id     = cid,
                        question         = title,
                        yes_price        = price if side == Side.YES else round(1-price,4),
                        no_price         = price if side == Side.NO  else round(1-price,4),
                        volume_usdc      = 0.0,
                        liquidity_usdc   = 0.0,
                        spread           = 0.02,
                        resolves_at      = resolves_at,
                        hours_to_resolve = 1.0,
                    )

                    sig = AgentSignal(
                        agent           = AgentName.WHALE_COPY,
                        market          = market,
                        side            = side,
                        confidence      = 1.0,
                        reason          = f"crypto copy @ {price:.2f}",
                        suggested_price = price + 0.01,
                    )
                    decision = TradeDecision(
                        market            = market,
                        side              = side,
                        consensus_count   = 1,
                        size_usdc         = CRYPTO_BET_USDC,
                        entry_price       = price + 0.01,
                        target_exit_price = round(price + 0.01 + 0.90 * (1.0 - (price + 0.01)), 4),
                        signals           = [sig],
                    )

                    log.info(f"[CRYPTO] {side_str} {title[:45]} @ {price:.2f}")
                    try:
                        pos = executor.execute(decision)
                    except Exception as ex:
                        log.error(f"[CRYPTO/ERR] {ex}")
                        pos = None

                    if pos:
                        portfolio.add_position(pos)
                        if event_slug:
                            copied_event_slugs.add(event_slug)
                        log.info(f"[CRYPTO/OK] {side_str} {title[:45]} @ {price:.2f}")
                    else:
                        log.warning(f"[CRYPTO/FAIL] {side_str} {title[:45]}")

            if portfolio.positions:
                if not _exit_task or _exit_task.done():
                    _exit_task = asyncio.create_task(_exit_background(portfolio, exit_manager, "CRYPTO"))

            await reconcile_portfolio(portfolio, POLY_PROXY_ADDRESS, "CRYPTO")

        except Exception as e:
            log.error(f"[CRYPTO] Erro: {e}", exc_info=True)


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

    crypto_portfolio = Portfolio(max_open=MAX_OPEN_POSITIONS, max_daily=MAX_DAILY_TRADES,
                                  state_file=DATA_DIR / "portfolio_state_crypto.json")
    crypto_executor = Executor(
        private_key    = POLY_PRIVATE_KEY,
        api_key        = POLY_API_KEY,
        api_secret     = POLY_API_SECRET,
        api_passphrase = POLY_API_PASSPHRASE,
        proxy_addr     = POLY_PROXY_ADDRESS,
        dry_run        = DRY_RUN,
    )
    crypto_exit = ExitManager(crypto_executor)

    await asyncio.gather(
        scorer_loop(executor, scorer_portfolio, scorer_exit, whale_portfolio),
        whale_loop(executor, whale_portfolio, whale_exit, scorer_portfolio),
        crypto_loop(crypto_executor, crypto_portfolio, crypto_exit),
    )


if __name__ == "__main__":
    asyncio.run(main())
