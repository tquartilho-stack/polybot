"""
sync_portfolio.py — sincroniza portfolio_state.json com posições reais do Polymarket.

Corre localmente:
  python sync_portfolio.py
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


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"daily_count": {}, "positions": [], "history": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict):
    path.write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"  Guardado: {path}")


def position_to_dict(p: dict) -> dict:
    """Converte posição da API para formato do portfolio_state."""
    cid     = p.get("conditionId", "")
    outcome = p.get("outcome", "Yes").strip()
    # Normaliza outcome para YES/NO
    if outcome.upper() not in ("YES", "NO"):
        outcome = "YES"  # para outcomes como "LNG Esports", assume YES (primeiro token)
    else:
        outcome = outcome.upper()

    size    = float(p.get("size", 0))
    price   = float(p.get("avgPrice") or 0.5)
    cur     = float(p.get("curPrice") or price)
    title   = p.get("title", "")

    # Parse end date
    end_str = p.get("endDate", "")
    if end_str:
        end_str = end_str.rstrip("Z")
        if "T" not in end_str:
            end_str += "T23:59:59"
        resolves_at = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc)
    else:
        resolves_at = datetime.now(timezone.utc)

    hours_left = (resolves_at - datetime.now(timezone.utc)).total_seconds() / 3600

    return {
        "trade_id":        f"sync_{cid[:8]}_{outcome[:2]}",
        "condition_id":    cid,
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
        "target_exit":     round(cur + 0.85 * (1 - cur), 3),
        "current_price":   cur,
        "peak_price":      cur,
        "volume_baseline": 0.0,
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

    print("Posicoes no Polymarket:")
    for p in real_positions:
        title   = p.get("title", "")[:50]
        outcome = p.get("outcome", "?")
        size    = float(p.get("size", 0))
        price   = float(p.get("curPrice") or 0)
        value   = float(p.get("currentValue") or 0)
        pnl     = float(p.get("cashPnl") or 0)
        print(f"  [{outcome}] {title:<50} {size:.1f} shares @ {price:.3f} val=${value:.2f} pnl=${pnl:+.2f}")

    print()
    scorer_state = load_state(SCORER_STATE)
    whale_state  = load_state(WHALE_STATE)

    print(f"Scorer portfolio actual: {len(scorer_state.get('positions', []))} posicoes")
    print(f"Whale portfolio actual:  {len(whale_state.get('positions', []))} posicoes")
    print()

    action = input("O que fazer?\n  1 - Reset completo (tudo para scorer, whale a zero)\n  2 - Sair sem alterar\n\nEscolha: ").strip()

    if action == "1":
        new_positions = [position_to_dict(p) for p in real_positions]

        print("\nReconstruido:")
        for pos in new_positions:
            print(f"  [{pos['side']}] {pos['question'][:50]}")

        scorer_state["positions"] = new_positions
        whale_state["positions"]  = []

        save_state(SCORER_STATE, scorer_state)
        save_state(WHALE_STATE,  whale_state)

        print(f"\n{len(new_positions)} posicoes reconstruidas no scorer portfolio.")
        print("Whale portfolio limpo.")
        print("\nAgora faz redeploy no Railway para carregar os ficheiros actualizados.")
        print("(ou copia para /data via Railway CLI se tiveres acesso)")
    else:
        print("Sem alteracoes.")


if __name__ == "__main__":
    asyncio.run(main())
