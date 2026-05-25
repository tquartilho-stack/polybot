"""
config.py — centraliza toda a configuração do sistema.
Cria um .env com as tuas variáveis antes de correr.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── APIs ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
POLY_API_KEY        = os.getenv("POLY_API_KEY", "")        # CLOB API key
POLY_API_SECRET     = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")
POLY_PRIVATE_KEY    = os.getenv("POLY_PRIVATE_KEY", "")    # Wallet private key
POLY_PROXY_ADDRESS  = os.getenv("POLY_PROXY_ADDRESS", "")  # Proxy wallet address

# ── Endpoints ───────────────────────────────────────────────────────────────
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
GRAPH_API   = "https://api.thegraph.com/subgraphs/name/polymarket/matic-markets"

# ── Wallet scanner ──────────────────────────────────────────────────────────
MIN_TRADES          = 100          # mínimo de trades para considerar uma wallet
MIN_WIN_RATE        = 0.70         # 70% win rate
TOP_N_WALLETS       = 100           # top wallets a trackear
WALLET_LOOKBACK_DAYS= 90           # dias de histórico a analisar

# ── Market scorer ───────────────────────────────────────────────────────────
MAX_MARKETS_TO_SCORE= 1000         # mercados a puxar da API antes de filtrar
MIN_LIQUIDITY_USDC  = 50         # liquidez mínima em USDC
MAX_SPREAD_PCT      = 0.12         # spread máximo (8 cents num mercado 0-1)
MIN_HOURS_TO_RESOLVE= 1            # mercados com resolução > 2h
MAX_HOURS_TO_RESOLVE= 48          # e < 90h são os mais interessantes

# ── Consensus & sizing ──────────────────────────────────────────────────────
FULL_SIZE_USDC      = 20         # tamanho de posição completa
HALF_SIZE_USDC      = 10         # tamanho quando só 1 agente concorda
CONSENSUS_THRESHOLD = 2            # mínimo de agentes para full size

# ── Exit manager ────────────────────────────────────────────────────────────
EXIT_PROFIT_TARGET  = 0.90         # sai a 85% do movimento esperado
VOLUME_SPIKE_MULT   = 3.0          # sai se volume fizer 3x em pouco tempo
POLL_INTERVAL_SECS  = 60           # frequência de verificação de posições abertas

# ── Loop principal ──────────────────────────────────────────────────────────
RUN_INTERVAL_MINS   = 30           # corre o bot de 30 em 30 minutos
MAX_OPEN_POSITIONS  = 100           # máximo de posições abertas simultâneas
MAX_DAILY_TRADES    = 100         # travão diário

CLAUDE_MODEL        = "claude-sonnet-4-6"
CLAUDE_HAIKU_MODEL  = "claude-haiku-4-5-20251001"
