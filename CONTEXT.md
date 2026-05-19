# PolyBot — CONTEXT.md (actualizado 2026-05-19)

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
# Se não fizer redeploy automático:
git commit --allow-empty -m "force redeploy"
git push origin main
```

## Portfolios
- **Scorer:** `portfolio_state.json` no Volume `/data`
- **Whale:** `portfolio_state_whale.json` no Volume `/data`

## Proxy Addresses
- **Scorer:** `0x0F4902690951B760C451A8f9dc81D72871359E18`
- **Whale:** (ver logs — `positions?user=0x...&sizeThreshold=0.01`)

## Endpoints Úteis
```powershell
# Limpar histórico (remove BAD_IDS e BAD_QUESTIONS do disco + memória)
Invoke-RestMethod -Uri "https://polybot-production-8ef4.up.railway.app/clean-history" -Method POST

# Descarregar portfolio scorer
Invoke-WebRequest -Uri "https://polybot-production-8ef4.up.railway.app/download-portfolio" -OutFile "$env:TEMP\portfolio.json"

# Upload portfolio scorer
$body = Get-Content "$env:TEMP\portfolio.json" -Raw
Invoke-RestMethod -Uri "https://polybot-production-8ef4.up.railway.app/upload-portfolio" -Method POST -Body $body -ContentType "application/json"

# Ver dashboard data
Invoke-WebRequest -Uri "https://polybot-production-8ef4.up.railway.app/dashboard_data.json" | Select-Object -ExpandProperty Content | ConvertFrom-Json | Select-Object total_trades, total_pnl, win_rate

# Force redeploy
git commit --allow-empty -m "force redeploy"
git push origin main
```

## Fixes Aplicados (histórico completo)

### exit_manager.py
- monitor_loop dinâmico (recebe portfolio, não lista estática)
- **volume_spike removido completamente** (2026-05-19)
- **settlement por tempo removido** — settlement só quando `current_price >= 0.95` (mercado resolveu YES) ou `current_price <= 0.05` com posição NO (2026-05-19)
- aggressive sell floor: `max(exit_price * 0.95, entry_price + 0.01)` — nunca vende abaixo do entry
- erro "not enough balance" → warning (não crash)

### reconcile.py
- Adiciona posições em falta ao scorer
- Não adiciona posições com `curPrice==0 and redeemable==True`
- Não adiciona posições com `endDate` passado há >1h e `curPrice < 0.02`
- Não adiciona posições com `curPrice >= 0.95` (já resolvidas, aguardam redemption)
- PnL correcto: curPrice>=0.95 → exit 1.0; curPrice<=0.05 → exit 0.0
- target = price + 0.90*(1-price)
- **BLACKLISTED_CONDITIONS** ao nível de módulo — bloqueia condition_ids específicas em `_build_missing_position`
- **BLACKLISTED_PREFIXES** — bloqueia por prefixo de condition_id
- Blacklist actual:
  - `0x4f60e49a9c6265c2567eedbf183500f8f2f10cd81b1468e4c5c4c1bf6f5c74ae` (CS GamerLegion NaVi)
  - prefixo `0x9046b0` (CS BetBoom NaVi)

### server.py
- `GET /download-portfolio` — descarrega `portfolio_state.json`
- `GET /download-portfolio-whale` — descarrega `portfolio_state_whale.json`
- `POST /clean-history` — remove entradas por trade_id (BAD_IDS) e market_question (BAD_QUESTIONS) do disco E recarrega memória
- `register_portfolio(name, portfolio)` — registo global de portfolios em memória
- BAD_IDS: `sync_0x3dbf1d_YE`, `sync_0x7382a5_YE`, `sync_0xc6ddb1_YE`, `sync_0x69f9e1_YE`, `sync_0x4f60e4_YE`
- BAD_QUESTIONS: `GamerLegion vs Natus Vincere`, `BetBoom Team vs Natus Vincere`

### main_combined.py
- scorer_loop recebe whale_portfolio como argumento opcional
- whale_loop recebe scorer_portfolio como argumento opcional
- `_has_opposite_side()` helper para bloquear lados opostos
- `_exit_background` passa portfolio directamente ao monitor_loop
- reconcile background scorer cada 5min em asyncio.create_task
- `register_portfolio("scorer", ...)` e `register_portfolio("whale", ...)` após init
- `_real_positions` dict global — preenchido pelo reconcile com posições reais da Poly
- `_write_dashboard` filtra BAD_IDS e BAD_QUESTIONS do histórico
- `open_positions` no dashboard usa posições reais da Poly quando disponíveis

### dashboard_writer.py
- Filtro BAD_QUESTIONS na linha de trades

### market_scorer.py
- HAIKU_THRESHOLD = 3.0 (era 4.0)

### wallet_scanner.py
- Combina leaderboard PNL MONTH + PNL ALL (deduplicado)
- limit=50 cada, até ~90 wallets únicas

## Loops Detectados e Corrigidos
Padrão: reconcile re-adiciona posição já resolvida → exit_manager fecha → re-adiciona → loop
- **Eurovision/CS (loop original)** — ~812 entradas falsas — fix: filtro endDate expirado
- **CS GamerLegion NaVi** — ~32 entradas por ciclo — fix: blacklist condition_id
- **CS BetBoom NaVi** — ~46 entradas — fix: blacklist prefixo `0x9046b0`

## Problemas Resolvidos ✅
1. Dashboard loop (-10k PnL falso) — reconcile re-adicionava posições já resolvidas
2. Exit com prejuízo em volume_spike/settlement — saía independentemente do preço
3. Exit manager estático — não detectava novas posições
4. Dedup cross-bot (compras duplicadas scorer+whale)
5. Whale bot 0 candidatos — posições com curPrice=0 eram contadas
6. Custo catastrófico whale (1120 mercados ao Haiku)
7. Loop infinito Eurovision/CS — posições expiradas com preço residual re-adicionadas
8. Loop CS GamerLegion — condition_id blacklisted
9. Loop CS BetBoom — prefix blacklisted
10. Settlement prematuro — vendia durante jogos em curso quando mins_left < 15
11. Volume spike removido — lógica incorrecta (volume pode ser compras na nossa direcção)

## Estado Actual (2026-05-19)
- Bot a correr em LIVE com scorer + whale
- Scorer: ~55 trades no histórico, posições abertas sincronizadas com Poly
- Railway: auto-deploy por vezes não dispara — usar `git commit --allow-empty` para forçar
- Backup whale original em: `C:\Users\tquar\polybot\backup_whale_original\`
- **Próxima alteração planeada:** whale passa a copiar directamente 4 wallets fixas:
  - `0x204f72f35326db932158cba6adff0b9a1da95e14`
  - `0x9495425feeb0c250accb89275c97587011b19a27`
  - `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`
  - `0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a`
  - Lógica: half_size por defeito, full_size se scorer também tem sinal no mesmo mercado
