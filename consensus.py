"""
consensus.py — filtro de consenso entre os 3 agentes.

Regras:
  2+ agentes concordam no mesmo mercado + side → FULL SIZE
  1  agente sozinho                            → HALF SIZE
  0  agentes concordam                         → NO TRADE

"Concordar" = mesmo condition_id + mesmo side.
O target de saída é calculado aqui com base no preço de entrada.
"""
from __future__ import annotations
import logging
from collections import defaultdict

from config import FULL_SIZE_USDC, HALF_SIZE_USDC, CONSENSUS_THRESHOLD
from data.models import AgentSignal, TradeDecision, Side

log = logging.getLogger(__name__)


def build_consensus(all_signals: list[AgentSignal]) -> list[TradeDecision]:
    """
    Agrega sinais de todos os agentes e devolve decisões de trade.
    """
    # Agrupa por (condition_id, side)
    groups: dict[tuple[str, Side], list[AgentSignal]] = defaultdict(list)
    for sig in all_signals:
        key = (sig.market.condition_id, sig.side)
        groups[key].append(sig)

    decisions: list[TradeDecision] = []

    for (condition_id, side), signals in groups.items():
        n = len(signals)
        if n == 0:
            continue

        market        = signals[0].market
        avg_confidence= sum(s.confidence for s in signals) / n
        entry_price   = max(s.suggested_price for s in signals)  # mais conservador

        if side == Side.YES:
            current = market.yes_price
        else:
            current = market.no_price

        # Target de saída: 90% do movimento até 1.0
        # Ex: preço actual 0.60 → move até 1.0 → 90% desse move = 0.60 + 0.90*(0.40) = 0.96
        target_exit = entry_price + 0.90 * (1.0 - entry_price)

        if n >= CONSENSUS_THRESHOLD:
            size = FULL_SIZE_USDC
        else:
            size = HALF_SIZE_USDC

        decisions.append(TradeDecision(
            market          = market,
            side            = side,
            size_usdc       = size,
            signals         = signals,
            consensus_count = n,
            entry_price     = entry_price,
            target_exit_price = target_exit,
        ))

    # Ordena por consenso + confiança média
    decisions.sort(
        key=lambda d: (d.consensus_count, sum(s.confidence for s in d.signals)),
        reverse=True,
    )

    log.info(
        f"Consensus: {len(all_signals)} sinais → "
        f"{sum(1 for d in decisions if d.consensus_count >= CONSENSUS_THRESHOLD)} full size, "
        f"{sum(1 for d in decisions if d.consensus_count < CONSENSUS_THRESHOLD)} half size"
    )
    return decisions
