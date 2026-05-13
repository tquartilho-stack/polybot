from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone

import httpx

from config import (
    GAMMA_API, CLOB_API,
    MAX_MARKETS_TO_SCORE, MIN_LIQUIDITY_USDC,
    MAX_SPREAD_PCT, MIN_HOURS_TO_RESOLVE, MAX_HOURS_TO_RESOLVE,
)
from data.models import Market


async def fetch_active_markets(client: httpx.AsyncClient) -> list[dict]:
    params = {
        "active":    "true",
        "closed":    "false",
        "limit":     MAX_MARKETS_TO_SCORE,
        "order":     "volume24hr",
        "ascending": "false",
    }
    r = await client.get(f"{GAMMA_API}/markets", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


async def fetch_market_by_id(client: httpx.AsyncClient, condition_id: str):
    r = await client.get(
        f"{GAMMA_API}/markets",
        params={"conditionIds": condition_id},
        timeout=10,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    return data[0] if isinstance(data, list) else data


async def fetch_mid_price(client: httpx.AsyncClient, token_id: str) -> float:
    r = await client.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=10)
    r.raise_for_status()
    return float(r.json().get("mid", 0))


def parse_and_filter(raw_markets: list[dict]) -> list[Market]:
    now = datetime.now(timezone.utc)
    markets: list[Market] = []

    for m in raw_markets:
        try:
            outcomes_raw = m.get("outcomes", "[]")
            if isinstance(outcomes_raw, str):
                outcomes_raw = json.loads(outcomes_raw)
            if len(outcomes_raw) != 2:
                continue

            prices_raw = m.get("outcomePrices", '["0.5", "0.5"]')
            if isinstance(prices_raw, str):
                prices_raw = json.loads(prices_raw)
            yes_price = float(prices_raw[0])
            no_price  = float(prices_raw[1])

            liquidity = float(m.get("liquidity", 0))
            volume    = float(m.get("volume", 0))
            volume24h = float(m.get("volume24hr", 0))

            if liquidity < MIN_LIQUIDITY_USDC:
                continue

            spread = float(m.get("spread", 1))
            if spread > MAX_SPREAD_PCT:
                continue

            end_date_str = m.get("endDate") or m.get("endDateIso")
            if not end_date_str:
                continue
            end_date_str = end_date_str.rstrip("Z")
            if "T" not in end_date_str:
                end_date_str += "T23:59:59"
            resolves_at = datetime.fromisoformat(end_date_str).replace(tzinfo=timezone.utc)
            hours_left  = (resolves_at - now).total_seconds() / 3600

            if not (MIN_HOURS_TO_RESOLVE <= hours_left <= MAX_HOURS_TO_RESOLVE):
                continue

            markets.append(Market(
                condition_id     = m["conditionId"],
                question         = m.get("question", ""),
                yes_price        = yes_price,
                no_price         = no_price,
                volume_usdc      = volume24h,  # usa volume24h como baseline para volume spike
                liquidity_usdc   = liquidity,
                spread           = spread,
                resolves_at      = resolves_at,
                hours_to_resolve = hours_left,
            ))

        except (KeyError, ValueError, TypeError):
            continue

    return markets


async def get_filtered_markets() -> list[Market]:
    async with httpx.AsyncClient() as client:
        raw = await fetch_active_markets(client)
        markets = parse_and_filter(raw)
        return markets


if __name__ == "__main__":
    markets = asyncio.run(get_filtered_markets())
    print(f"Mercados após filtros: {len(markets)}")
    for m in markets[:5]:
        print(f"  {m.question[:60]:<60} YES={m.yes_price:.2f}  hrs={m.hours_to_resolve:.1f}")
