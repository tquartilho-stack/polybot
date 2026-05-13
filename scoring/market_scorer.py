"""
scoring/market_scorer.py — usa Claude para pontuar mercados.

Claude recebe batches de mercados pré-filtrados e devolve:
  - score 0-10
  - razão em 1 linha
  - side recomendado (YES/NO/SKIP)

Ao contrário do que o post no Twitter implica, Claude não faz I/O de dados —
é o motor de raciocínio sobre dados já puxados pelas APIs.
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

BATCH_SIZE = 20  # mercados por chamada ao Claude


# ── System prompt ────────────────────────────────────────────────────────────

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


# ── Scoring ──────────────────────────────────────────────────────────────────

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


def _score_batch(markets: list[Market]) -> list[dict]:
    """Chama Claude para pontuar um batch de mercados."""
    payload = json.dumps([_market_to_dict(m) for m in markets], ensure_ascii=False)

    message = client.messages.create(
        model      = CLAUDE_MODEL,
        max_tokens = 2000,
        system     = SYSTEM_PROMPT,
        messages   = [{"role": "user", "content": payload}],
    )

    raw = message.content[0].text.strip()
    # Remove possíveis backticks se o modelo se enganar
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def score_markets(markets: list[Market], min_score: float = 6.0) -> list[Market]:
    """
    Pega lista de mercados, envia em batches ao Claude, aplica scores.
    Devolve apenas os mercados com score >= min_score e side != SKIP.
    """
    scored: list[Market] = []

    for i in range(0, len(markets), BATCH_SIZE):
        batch = markets[i : i + BATCH_SIZE]
        try:
            results = _score_batch(batch)
        except Exception as e:
            log.error(f"Erro no batch {i//BATCH_SIZE}: {e}")
            continue

        result_map = {r["condition_id"]: r for r in results}
        for m in batch:
            r = result_map.get(m.condition_id)
            if not r:
                continue
            m.score       = r.get("score", 0)
            m.score_reason= r.get("reason", "")
            side          = r.get("side", "SKIP")

            if m.score >= min_score and side != "SKIP":
                # Guardamos o side recomendado no score_reason para os agentes lerem
                m.score_reason = f"[{side}] {m.score_reason}"
                scored.append(m)

    scored.sort(key=lambda m: m.score, reverse=True)
    log.info(f"Scorer: {len(markets)} → {len(scored)} mercados com edge")
    return scored


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from data.fetcher import get_filtered_markets

    logging.basicConfig(level=logging.INFO)
    markets = asyncio.run(get_filtered_markets())
    print(f"Mercados após filtro API: {len(markets)}")

    top = score_markets(markets)
    print(f"\nTop mercados após Claude scorer ({len(top)}):\n")
    for m in top[:8]:
        print(f"  [{m.score:.1f}] {m.question[:55]:<55}  {m.score_reason[:50]}")
