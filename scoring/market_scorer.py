"""
scoring/market_scorer.py — usa Claude para pontuar mercados.

Pipeline de dois estágios:
  1. Haiku pre-filtro: descarta mercados óbvios (score < 3) a baixo custo
  2. Sonnet scoring fino: analisa só os sobreviventes com profundidade
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Any

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from data.models import Market, Side

log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

HAIKU_MODEL    = "claude-haiku-4-5-20251001"
BATCH_SIZE     = 50   # mercados por chamada ao Haiku (pre-filtro)
SONNET_BATCH   = 20   # mercados por chamada ao Sonnet (scoring fino)
HAIKU_THRESHOLD = 5.0  # score mínimo para passar ao Sonnet


# ── System prompts ────────────────────────────────────────────────────────────

PREFILTER_PROMPT = """És um filtro rápido de mercados de predição do Polymarket.
Estes mercados já passaram filtros de liquidez e timing. A tua função é apenas descartar os trivialmente sem edge.

Devolve EXCLUSIVAMENTE um array JSON sem markdown:
[{"condition_id": "...", "score": 6.5, "keep": true}]

score: 0-10. keep: false APENAS se:
- Preço YES entre 0.02-0.05 ou 0.95-0.98 (mercado já decidido)
- Pergunta trivialmente óbvia sem incerteza real

Em caso de dúvida, keep: true."""

SYSTEM_PROMPT = """És um analista de mercados de predição especializado no Polymarket.
Recebes uma lista de mercados binários (YES/NO) com os seus dados e tens de os pontuar.

Para cada mercado avalia:
1. GAP — a probabilidade implícita (yes_price) está desalinhada com a realidade?
   Ex: mercado a 0.35 YES mas evidência forte sugere 0.55+ → gap de 20pts
2. DEPTH — liquidez suficiente para entrar e sair sem grande slippage?
3. TIMING — resolução próxima = menos tempo exposto a risco.

Devolve EXCLUSIVAMENTE um array JSON válido, sem markdown, sem texto extra.
Formato de cada objecto:
{
  "condition_id": "...",
  "score": 7.5,
  "side": "YES",
  "reason": "Mercado subestima probabilidade com base em precedente histórico claro."
}

score: 0-10 (só puntua acima de 6 se houver edge real)
side: "YES", "NO", ou "SKIP" (se não houver edge claro)
reason: máximo 15 palavras"""


# ── Utils ─────────────────────────────────────────────────────────────────────

def _market_to_dict(m: Market) -> dict[str, Any]:
    return {
        "condition_id":     m.condition_id,
        "question":         m.question,
        "yes_price":        round(m.yes_price, 3),
        "no_price":         round(m.no_price, 3),
        "liquidity_usdc":   round(m.liquidity_usdc, 0),
        "volume_usdc":      round(m.volume_usdc, 0),
        "hours_to_resolve": round(m.hours_to_resolve, 1),
        "spread":           round(m.spread, 4),
    }


def _parse_json(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


# ── Stage 1: Haiku pre-filtro ─────────────────────────────────────────────────

def _prefilter_batch(markets: list[Market]) -> list[Market]:
    """Usa Haiku para descartar mercados sem edge óbvio."""
    payload = json.dumps([_market_to_dict(m) for m in markets], ensure_ascii=False)

    message = client.messages.create(
        model      = HAIKU_MODEL,
        max_tokens = 2000,
        system     = PREFILTER_PROMPT,
        messages   = [{"role": "user", "content": payload}],
    )

    results = _parse_json(message.content[0].text)
    keep_ids = {r["condition_id"] for r in results if r.get("keep", False) and r.get("score", 0) >= HAIKU_THRESHOLD}
    kept = [m for m in markets if m.condition_id in keep_ids]
    log.info(f"[HAIKU] {len(markets)} → {len(kept)} mercados após pre-filtro")
    return kept


def prefilter_markets(markets: list[Market]) -> list[Market]:
    """Corre pre-filtro Haiku em batches."""
    survivors = []
    for i in range(0, len(markets), BATCH_SIZE):
        batch = markets[i : i + BATCH_SIZE]
        try:
            survivors.extend(_prefilter_batch(batch))
        except Exception as e:
            log.error(f"Erro no pre-filtro batch {i//BATCH_SIZE}: {e}")
            survivors.extend(batch)  # em caso de erro, passa tudo ao Sonnet
    return survivors


# ── Stage 2: Sonnet scoring fino ──────────────────────────────────────────────

def _score_batch(markets: list[Market]) -> list[dict]:
    """Chama Sonnet para pontuar um batch de mercados."""
    payload = json.dumps([_market_to_dict(m) for m in markets], ensure_ascii=False)

    message = client.messages.create(
        model      = CLAUDE_MODEL,
        max_tokens = 2000,
        system     = SYSTEM_PROMPT,
        messages   = [{"role": "user", "content": payload}],
    )

    return _parse_json(message.content[0].text)


def score_markets(markets: list[Market], min_score: float = 6.0) -> list[Market]:
    """
    Pipeline de dois estágios:
    1. Haiku descarta mercados óbvios sem edge
    2. Sonnet faz scoring fino dos sobreviventes
    """
    # Stage 1: pre-filtro Haiku
    candidates = prefilter_markets(markets)
    log.info(f"[SCORER] Pre-filtro: {len(markets)} → {len(candidates)} candidatos para Sonnet")

    if not candidates:
        return []

    # Stage 2: Sonnet scoring fino
    scored: list[Market] = []

    for i in range(0, len(candidates), SONNET_BATCH):
        batch = candidates[i : i + SONNET_BATCH]
        try:
            results = _score_batch(batch)
        except Exception as e:
            log.error(f"Erro no batch Sonnet {i//SONNET_BATCH}: {e}")
            continue

        result_map = {r["condition_id"]: r for r in results}
        for m in batch:
            r = result_map.get(m.condition_id)
            if not r:
                continue
            m.score        = r.get("score", 0)
            m.score_reason = r.get("reason", "")
            side           = r.get("side", "SKIP")

            if m.score >= min_score and side != "SKIP":
                m.score_reason = f"[{side}] {m.score_reason}"
                scored.append(m)

    scored.sort(key=lambda m: m.score, reverse=True)
    log.info(f"[SCORER] Sonnet: {len(candidates)} → {len(scored)} mercados com edge")
    return scored


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from data.fetcher import get_filtered_markets

    logging.basicConfig(level=logging.INFO)
    markets = asyncio.run(get_filtered_markets())
    print(f"Mercados após filtro API: {len(markets)}")

    top = score_markets(markets)
    print(f"\nTop mercados após scorer ({len(top)}):\n")
    for m in top[:8]:
        print(f"  [{m.score:.1f}] {m.question[:55]:<55}  {m.score_reason[:50]}")
