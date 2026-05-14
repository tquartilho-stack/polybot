"""
data/models.py — modelos de dados partilhados por todo o sistema.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    YES = "YES"
    NO  = "NO"


class AgentName(str, Enum):
    ARBITRAGE   = "arbitrage"
    CONVERGENCE = "convergence"
    WHALE_COPY  = "whale_copy"


@dataclass
class Market:
    """Mercado do Polymarket."""
    condition_id:   str
    question:       str
    yes_price:      float          # 0-1, representa probabilidade implícita
    no_price:       float
    volume_usdc:    float
    liquidity_usdc: float
    spread:         float          # no_price - yes_price após normalizar
    resolves_at:    datetime
    hours_to_resolve: float
    # preenchido pelo scorer
    score:          float = 0.0
    score_reason:   str   = ""


@dataclass
class WalletProfile:
    """Perfil de uma whale wallet."""
    address:        str
    total_trades:   int
    win_rate:       float
    total_profit:   float
    avg_hold_hours: float
    early_exit_pct: float          # % vezes que sai antes de settlement
    preferred_sides: dict[str, float] = field(default_factory=dict)


@dataclass
class AgentSignal:
    """Sinal de um agente."""
    agent:          AgentName
    market:         Market
    side:           Side
    confidence:     float          # 0-1
    reason:         str
    suggested_price: float         # preço limite a colocar


@dataclass
class TradeDecision:
    """Decisão final do consensus filter."""
    market:         Market
    side:           Side
    size_usdc:      float
    signals:        list[AgentSignal]
    consensus_count: int
    entry_price:    float
    target_exit_price: float       # 85% do movimento esperado


@dataclass
class OpenPosition:
    """Posição aberta em carteira."""
    trade_id:       str
    market:         Market
    side:           Side
    size_usdc:      float
    entry_price:    float
    entry_time:     datetime
    target_exit:    float
    current_price:  float = 0.0
    peak_price:     float = 0.0
    volume_baseline: float = 0.0   # volume na hora de entrada
    token_id:        str   = ""    # token_id para leitura correcta de precos via CLOB


@dataclass
class TradeResult:
    """Resultado de um trade fechado."""
    trade_id:       str
    market_question: str
    side:           Side
    size_usdc:      float
    entry_price:    float
    exit_price:     float
    pnl_usdc:       float
    hold_hours:     float
    exit_reason:    str            # "target", "volume_spike", "settlement"
    closed_at:      datetime = field(default_factory=datetime.utcnow)
