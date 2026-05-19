# PolyBot — CONTEXT.md (actualizado 2026-05-19 ~16h)

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
agents/agents.py       # Arb, Conv, WhaleCopy (WhaleCopy já não usado pelo whale_loop)
consensus.py           # 2/3 threshold
data/wallet_scanner.py # Leaderboard PNL MONTH + ALL (usado só pelo scorer)
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
git push origin main
# Se não fizer redeploy automático:
git commit --allow-empty -m "force redeploy"
git push origin main
```

## Portfolios
- Scorer: portfolio_state.json no Volume /data
- Whale: portfolio_state_whale.json no Volume /data

## Proxy Addresses
- Scorer: 0x0F4902690951B760C451A8f9dc81D72871359E18
- Whale: ver logs (positions?user=0x...&sizeThreshold=0.01)

## Endpoints Úteis
```powershell
Invoke-RestMethod -Uri "https://polybot-production-8ef4.up.railway.app/clean-history" -Method POST
Invoke-WebRequest -Uri "https://polybot-production-8ef4.up.railway.app/download-portfolio" -OutFile "$env:TEMP\portfolio.json"
$p = Invoke-WebRequest -Uri "https://polybot-production-8ef4.up.railway.app/download-portfolio" | Select-Object -ExpandProperty Content | ConvertFrom-Json
$p.history | Group-Object market_question | Sort-Object Count -Descending | Select-Object -First 10 Name, Count
git commit --allow-empty -m "force redeploy" && git push origin main
```

## Lógica Actual

### Scorer
- Ciclo ~30min, Haiku pre-filtro + Sonnet scoring
- Arb + Conv + WhaleCopy agents → consensus → compra
- Exit: target (90% lucro) ou settlement (curPrice >= 0.95)

### Whale — copy trader directo (NOVO 2026-05-19)
- Ciclo ~30min
- Busca posições das 4 wallets fixas
- HALF_SIZE ($10) por defeito
- FULL_SIZE ($20) se scorer também tem posição no mesmo mercado
- Ignora curPrice >= 0.95
- Sem Claude, sem leaderboard

### Wallets Whale (fixas)
```
0x204f72f35326db932158cba6adff0b9a1da95e14
0x9495425feeb0c250accb89275c97587011b19a27
0x2005d16a84ceefa912d4e380cd32e7ff827875ea
0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a
```

## Fixes Aplicados

### exit_manager.py
- volume_spike removido
- settlement por tempo removido — só curPrice >= 0.95 (YES) ou <= 0.05 (NO)
- aggressive sell floor: max(exit_price * 0.95, entry_price + 0.01)

### reconcile.py
- Não adiciona curPrice >= 0.95
- Não adiciona endDate expirado >1h e curPrice < 0.02
- BLACKLISTED_CONDITIONS: 0x4f60e49a... (CS GamerLegion NaVi)
- BLACKLISTED_PREFIXES: 0x9046b0 (CS BetBoom NaVi)

### server.py /clean-history
- BAD_IDS: sync_0x3dbf1d_YE, sync_0x7382a5_YE, sync_0xc6ddb1_YE, sync_0x69f9e1_YE, sync_0x4f60e4_YE
- BAD_QUESTIONS: GamerLegion vs Natus Vincere, BetBoom Team vs Natus Vincere
- Recarrega memória após limpeza

### main_combined.py
- _real_positions dict global com posições reais da Poly
- _write_dashboard filtra BAD_IDS e BAD_QUESTIONS
- open_positions no dashboard usa dados reais da Poly
- whale_loop reescrito como copy trader

## Loops Detectados e Corrigidos
- Eurovision/CS: ~812 entradas — fix endDate expirado
- CS GamerLegion NaVi: ~32/ciclo — fix blacklist condition_id
- CS BetBoom NaVi: ~46 entradas — fix blacklist prefixo 0x9046b0

## Backup
- Whale original: C:\Users\tquar\polybot\backup_whale_original\

## Estado (2026-05-19 ~16h)
- Scorer: a correr, ~55 trades histórico
- Whale: nova lógica copy trader deploiada (aguardar primeiro ciclo)
- Se aparecerem novos loops: adicionar condition_id a BLACKLISTED_CONDITIONS ou prefixo a BLACKLISTED_PREFIXES em reconcile.py, e market_question a BAD_QUESTIONS em server.py
