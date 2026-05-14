# PolyBot — Contexto Completo do Projecto

## Visão Geral

Sistema automatizado de trading no Polymarket. Inspirado num post viral de Twitter sobre um bot com 3 agentes e consenso. O post tinha métricas fictícias ($200→$14.300 em 27 dias) mas arquitectura legítima. Construímos a versão honesta.

**Stack:** Python 3.11 / asyncio / Railway EU West / Claude Sonnet 4.6 + Haiku 4.5
**Repo:** https://github.com/tquartilho-stack/polybot
**Dashboard:** https://polybot-production-8ef4.up.railway.app
**Saldo inicial:** ~$160 USDC na Polygon

---

## Arquitectura

### Entry Point
```
main_combined.py  — único processo, corre scorer + whale em paralelo como asyncio tasks
Procfile: web: python main_combined.py
```

### Estrutura de Ficheiros
```
polybot/
├── main_combined.py       # Bot combinado (scorer + whale em asyncio paralelo)
├── config.py              # Parâmetros globais
├── consensus.py           # Filtro consenso (target = entry + 0.85*(1-entry))
├── portfolio.py           # Persistência posições + histórico em /data/
├── reconcile.py           # Reconcilia portfolio com Polymarket após cada ciclo
├── server.py              # HTTP server porta 8080 + endpoints de controlo
├── dashboard.html         # Dashboard tabs scorer/whale/comparar + botões por tab
├── dashboard_writer.py    # Escreve /data/dashboard_data.json
├── sync_portfolio.py      # Sync portfolio local com Polymarket (corre no PC)
├── upload_portfolio.py    # Envia portfolio_state.json para Railway via HTTP
├── data/
│   ├── models.py          # Market, OpenPosition (token_id field), TradeResult, Side
│   ├── fetcher.py         # get_filtered_markets() → Gamma API
│   └── wallet_scanner.py  # get_top_wallets() → Data API leaderboard
├── scoring/
│   └── market_scorer.py   # 2 estágios: Haiku pre-filtro + Sonnet scoring fino
├── agents/agents.py       # ArbitrageAgent, ConvergenceAgent, WhaleCopyAgent
├── execution/
│   ├── executor.py        # py-clob-client-v2, signature_type=3, BUY
│   └── exit_manager.py    # Sem timeout, preços via CLOB token_id, SELL market order
```

### Railway
- 1 serviço `polybot` EU West → `main_combined.py`
- 1 Volume `/data` → persiste tudo entre deploys
- URL: polybot-production-8ef4.up.railway.app

---

## Fluxo dos Bots

### Scorer-First
1. Gamma API 250 mercados → filtros → ~80
2. **Haiku pre-filtro** batches 50 → ~30-50 candidatos
3. **Sonnet scoring fino** batches 20 → ~5 com edge
4. 3 agentes → consensus → BUY via CLOB V2
5. Exit manager background sem timeout → preços via CLOB token_id

### Whale-First
1. Top 50 wallets → candidatos onde 2+ whales estão no mesmo lado
2. **Usa cache de scoring do scorer** se < 25 min → zero chamadas Claude
3. Se cache expirado → Sonnet score só candidatos whale (~5-15 mercados)
4. Mesmo executor/exit manager

---

## Credenciais

```
ANTHROPIC_API_KEY=sk-ant-...
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_API_PASSPHRASE=...
POLY_PRIVATE_KEY=0x...
POLY_PROXY_ADDRESS=0x0F4902690951B760C451A8f9dc81D72871359E18
DRY_RUN=false
```

---

## Decisões Técnicas Críticas

**CLOB V2:** Polymarket migrou Abril 2026. Usar `py-clob-client-v2`. `signature_type=3` com `funder=POLY_PROXY_ADDRESS`.

**Geoblock:** Railway US West/East bloqueados. Usar EU West.

**Preços negRisk:** Mercados negativeRisk (Eurovision, GTA VI events) não têm preços correctos via Gamma API `outcomePrices[]`. Sempre usar `token_id` (campo `asset` da Data API) → CLOB `/last-trade-price`.

**Exit sem timeout:** Exit manager corre indefinidamente. Triggers: target (85%), volume_spike (3x), settlement (<15min). Sem stop-loss.

**Ordens SELL:** `create_and_post_market_order` para execução imediata. Nunca ordens limite ao preço de mercado — ficam pendentes sem match.

**No-balance flag:** Quando erro `not enough balance`, ambos os bots pausam ciclos até uma posição fechar.

**Target:** `entry_price + 0.85 * (1.0 - entry_price)` — calculado sobre entry, não preço actual.

**Reconciliação:** Após cada ciclo, vai à Data API e remove posições fantasma, actualiza token_id e current_price.

---

## Dashboard Controlo

| Endpoint | Acção |
|----------|-------|
| POST /start | Inicia scorer |
| POST /stop | Para scorer |
| POST /pause | Pausa scorer |
| POST /resume | Retoma scorer |
| POST /start-whale | Inicia whale |
| POST /stop-whale | Para whale |
| POST /upload-portfolio | Envia portfolio_state.json |
| POST /upload-portfolio-whale | Envia portfolio_state_whale.json |

**Ficheiros de controlo no Volume /data:** STARTED, STARTED_WHALE, PAUSE, PAUSE_WHALE

**Deploy workflow correcto:**
1. Clicar Stop no dashboard → aguardar "standby" nos logs
2. Fazer push → Railway redeploy automático
3. Clicar Start quando pronto

---

## Sync Manual Portfolio

Quando dashboard ≠ Polymarket:
```powershell
cd C:\Users\tquar\polybot
venv\Scripts\activate
python sync_portfolio.py      # reconstrói com posições reais + token_id + preços CLOB
python upload_portfolio.py    # envia para Railway
```

---

## Custo Claude Estimado

| | Antes | Depois optimizações |
|-|-------|---------------------|
| Por ciclo | $0.19 | $0.06 |
| Por dia | $9-16 | ~$3 |

Optimizações feitas: cache partilhado scorer↔whale + Haiku pre-filtro.

---

## Config Principal

```python
MAX_MARKETS_TO_SCORE = 250
FULL_SIZE_USDC       = 20.0
HALF_SIZE_USDC       = 10.0
MAX_OPEN_POSITIONS   = 20
RUN_INTERVAL_MINS    = 30
CLAUDE_MODEL         = "claude-sonnet-4-20250514"
CLAUDE_HAIKU_MODEL   = "claude-haiku-4-5-20251001"
EXIT_PROFIT_TARGET   = 0.85
VOLUME_SPIKE_MULT    = 3.0
POLL_INTERVAL_SECS   = 60
```

---

## Problemas Conhecidos

1. Austria YES 0.1¢ — 869 shares, $0.43 valor, -75%. Sem stop-loss, vai a zero.
2. Sem stop-loss implementado — posições em perda ficam abertas indefinidamente.
3. PnL no dashboard calculado com preços actuais, não preços reais das SELLs executadas.
4. Serviço `delightful-vision` no Railway — whale separado, já não necessário.

---

## Ambiente Local

```
Windows 11 / PowerShell
C:\Users\tquar\polybot\
venv\Scripts\activate
```

```powershell
# Push standard
git add .
git commit -m "mensagem"
git push origin main
# Railway redeploy automático
```
