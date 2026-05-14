"""
sync_portfolio.py — sincroniza portfolio_state.json com posicoes reais do Polymarket.
Guarda token_id (asset) para leitura correcta de precos via CLOB.
"""
from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

POLY_DATA_API = "https://data-api.polymarket.com"
CLOB_API      = "https://clob.polymarket.com"
PROXY_ADDRESS = os.getenv("POLY_PROXY_ADDRESS", "")

SCORER_STATE = Path("portfolio_state.json")
WHALE_STATE  = Path("portfolio_state_whale.json")


async def fetch_open_positions(address: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{POLY_DATA_API}/positions",
            params={"user": address, "sizeThreshold": "0.01"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return [p for p in data if float(p.get("size", 0)) > 0] if isinstance(data, list) else []


async def fetch_clob_price(token_id: str) -> float:
    """Lê preço actual via CLOB usando o token_id."""
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{CLOB_API}/last-trade-price", params={"token_id": token_id}, timeout=5)
            r.raise_for_status()
            return float(r.json().get("price", 0))
        except:
            try:
                r = await client.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=5)
                r.raise_for_status()
                return float(r.json().get("mid", 0))
            except:
                return 0.0


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"daily_count": {}, "positions": [], "history": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict):
    path.write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"  Guardado: {path}")


async def position_to_dict(p: dict) -> dict:
    cid     = p.get("conditionId", "")
    token_id = p.get("asset", "")  # token_id correcto
    outcome = p.get("outcome", "Yes").strip()
    if outcome.upper() not in ("YES", "NO"):
        outcome = "YES"
    else:
        outcome = outcome.upper()

    size    = float(p.get("size", 0))
    # Calcula entry price a partir do initialValue e size
    initial = float(p.get("initialValue") or 0)
    price   = round(initial / size, 6) if size > 0 else float(p.get("avgPrice") or 0.5)
    
    # Pega preco actual via CLOB (correcto para todos os mercados)
    cur = await fetch_clob_price(token_id) if token_id else float(p.get("curPrice") or price)
    if cur == 0:
        cur = float(p.get("curPrice") or price)

    title   = p.get("title", "")
    end_str = p.get("endDate", "").rstrip("Z")
    if end_str:
        if "T" not in end_str: end_str += "T23:59:59"
        resolves_at = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc)
    else:
        resolves_at = datetime.now(timezone.utc)

    hours_left = (resolves_at - datetime.now(timezone.utc)).total_seconds() / 3600
    target = round(price + 0.85 * (1.0 - price), 4)

    return {
        "trade_id":        f"sync_{cid[:8]}_{outcome[:2]}",
        "condition_id":    cid,
        "token_id":        token_id,   # guardado para leitura de precos
        "question":        title,
        "yes_price":       cur if outcome == "YES" else round(1 - cur, 4),
        "no_price":        cur if outcome == "NO"  else round(1 - cur, 4),
        "volume_usdc":     0.0,
        "liquidity_usdc":  0.0,
        "spread":          0.01,
        "resolves_at":     resolves_at.isoformat(),
        "hours_to_resolve":max(hours_left, 0),
        "side":            outcome,
        "size_usdc":       round(size * price, 2),
        "entry_price":     price,
        "entry_time":      datetime.now(timezone.utc).isoformat(),
        "target_exit":     target,
        "current_price":   cur,
        "peak_price":      cur,
        "volume_baseline": 0.0,
        "neg_risk":        p.get("negativeRisk", False),
    }


async def main():
    print(f"\nPolyBot Portfolio Sync")
    print(f"Proxy: {PROXY_ADDRESS}\n")

    if not PROXY_ADDRESS:
        print("ERRO: POLY_PROXY_ADDRESS nao configurado no .env")
        return

    print("A puxar posicoes reais do Polymarket...")
    real_positions = await fetch_open_positions(PROXY_ADDRESS)
    print(f"  {len(real_positions)} posicoes encontradas\n")

    if not real_positions:
        print("Sem posicoes abertas.")
        for path in [SCORER_STATE, WHALE_STATE]:
            state = load_state(path)
            state["positions"] = []
            save_state(path, state)
        return

    print("Posicoes no Polymarket (a verificar precos via CLOB):")
    new_positions = []
    for p in real_positions:
        pos = await position_to_dict(p)
        title   = pos["question"][:50]
        outcome = pos["side"]
        entry   = pos["entry_price"]
        cur     = pos["current_price"]
        size    = float(p.get("size", 0))
        value   = float(p.get("currentValue") or 0)
        pnl_pct = float(p.get("percentPnl") or 0)
        print(f"  [{outcome}] {title:<50} entry={entry:.4f} cur={cur:.4f} val=${value:.2f} ({pnl_pct:+.1f}%)")
        new_positions.append(pos)

    print()
    scorer_state = load_state(SCORER_STATE)
    action = input("Reset completo com estes dados? (s/n): ").strip().lower()

    if action == "s":
        scorer_state["positions"] = new_positions
        scorer_state["history"]   = scorer_state.get("history", [])

        whale_state = load_state(WHALE_STATE)
        whale_state["positions"] = []

        save_state(SCORER_STATE, scorer_state)
        save_state(WHALE_STATE,  whale_state)
        print(f"\n{len(new_positions)} posicoes reconstruidas com token_id e precos correctos.")
        print("Corre agora: python upload_portfolio.py")
    else:
        print("Sem alteracoes.")


if __name__ == "__main__":
    asyncio.run(main())
