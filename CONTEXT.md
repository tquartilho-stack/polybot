# PolyBot — CONTEXT.md (actualizado 2026-05-17)

## Projecto
Bot de trading Polymarket. Stack: Python 3.11/asyncio, Railway EU West, Claude Sonnet 4.6 + Haiku 4.5.
- Repo: `tquartilho-stack/polybot` → `https://github.com/tquartilho-stack/polybot`
- Dashboard: `https://polybot-production-8ef4.up.railway.app`
- Saldo actual: ~$130 USDC na Polygon

## Arquitectura
```
main_combined.py       # Único processo, scorer + whale asyncio paralelo
config.py              # Configuração central
reconcile.py           # Sincroniza portfolio com Polymarket
portfolio.py           # Persistência /data/ com JSON
execution/executor.py  # py-clob-client-v2, signature_type=3
execution/exit_manager.py  # Monitor dinâmico de posições
scoring/market_scorer.py   # 2 estágios: Haiku pre-filtro + Sonnet scoring
agents/agents.py       # Arb, Conv, WhaleCopy
consensus.py           # 2/3 threshold
data/wallet_scanner.py # Leaderboard PNL MONTH + ALL combinados
data/fetcher.py        # get_filtered_markets()
```

## Config Actual (config.py)
```python
EXIT_PROFIT_TARGET  = 0.90
FULL_SIZE_USDC      = 20
HALF_SIZE_USDC      = 10
MAX_OPEN_POSITIONS  = 100
MAX_DAILY_TRADES    = 100
MIN_LIQUIDITY_USDC  = 500
MAX_SPREAD_PCT      = 0.12
MAX_HOURS_TO_RESOLVE= 336
TOP_N_WALLETS       = 100
CLAUDE_MODEL        = "claude-sonnet-4-6"
CLAUDE_HAIKU_MODEL  = "claude-haiku-4-5-20251001"
WALLET_CACHE_TTL    = 6*3600
```

## Deploy Workflow (PowerShell)
```powershell
Move-Item C:\Users\tquar\Downloads\FICHEIRO.py C:\Users\tquar\polybot\PASTA\ -Force
git add FICHEIRO.py
git commit -m "mensagem"
git push origin main   # Railway redeploy automático
```

## Portfolios
- **Scorer:** `portfolio_state.json` no Volume `/data`
- **Whale:** `portfolio_state_whale.json` no Volume `/data`

## Fixes Aplicados (histórico completo)

### exit_manager.py
- monitor_loop dinâmico (recebe portfolio, não lista estática)
- volume_spike só sai se `current_price > pos.entry_price`
- settlement só sai se `current_price > pos.entry_price`
- erro "not enough balance" → warning (não crash)

### reconcile.py
- Adiciona posições em falta ao scorer
- Não adiciona posições com `curPrice==0 and redeemable==True`
- Não adiciona posições com `endDate` passado há >1h e `curPrice < 0.02` (fix loop Eurovision/CS)
- PnL correcto: curPrice>=0.95 → exit 1.0; curPrice<=0.05 → exit 0.0
- target = price + 0.90*(1-price)

### main_combined.py
- scorer_loop recebe whale_portfolio como argumento opcional
- whale_loop recebe scorer_portfolio como argumento opcional
- _has_opposite_side() helper para bloquear lados opostos
- _exit_background passa portfolio directamente ao monitor_loop
- reconcile background scorer cada 5min em asyncio.create_task
- whale: delay 30s antes de reconcile após compras
- whale: aguarda cache scorer, não faz score próprio

### market_scorer.py
- HAIKU_THRESHOLD = 3.0 (era 4.0)

### wallet_scanner.py
- Combina leaderboard PNL MONTH + PNL ALL (deduplicado)
- limit=50 cada, até ~90 wallets únicas

## Problemas Resolvidos ✅
1. Dashboard loop (-10k PnL falso) — reconcile re-adicionava posições já resolvidas
2. Exit com prejuízo em volume_spike/settlement — saía independentemente do preço
3. Exit manager estático — não detectava novas posições
4. Dedup cross-bot (compras duplicadas scorer+whale)
5. Whale bot 0 candidatos — posições com curPrice=0 eram contadas
6. Custo catastrófico whale (1120 mercados ao Haiku)
7. Loop infinito Eurovision/CS — posições expiradas com preço residual (0.001) re-adicionadas

## Pendente / A Investigar
- Brighton sell às ~15h 2026-05-17 com prejuízo (-$0.46 de -$10.05 para +$9.59) — suspeita de volume_spike mas não confirmado nos logs (logs disponíveis só a partir das 16:38)
- PnL histórico corrompido pelo loop do dashboard — aceite como perda de precisão

## Estado Actual
- Bot a correr em LIVE com scorer + whale
- Último push: fix exit_manager + reconcile (2026-05-17 ~18h PT)
- Saldo real na Poly: ~$130 USDC
