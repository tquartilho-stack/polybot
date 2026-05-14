"""
upload_portfolio.py — envia portfolio_state.json para o Railway via HTTP.

Corre depois de sync_portfolio.py:
  python upload_portfolio.py
"""
import json, httpx
from pathlib import Path

RAILWAY_URL = "https://polybot-production-8ef4.up.railway.app"

scorer = Path("portfolio_state.json")
whale  = Path("portfolio_state_whale.json")

if scorer.exists():
    data = json.loads(scorer.read_text(encoding="utf-8"))
    r = httpx.post(f"{RAILWAY_URL}/upload-portfolio", json=data, timeout=15)
    print(f"Scorer: {r.json()}")

if whale.exists():
    data = json.loads(whale.read_text(encoding="utf-8"))
    r = httpx.post(f"{RAILWAY_URL}/upload-portfolio-whale", json=data, timeout=15)
    print(f"Whale: {r.json()}")
