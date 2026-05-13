import asyncio, httpx
from datetime import datetime, timezone

async def test():
    async with httpx.AsyncClient() as c:
        r = await c.get('https://gamma-api.polymarket.com/markets', params={'active':'true','closed':'false','limit':'10'}, timeout=20)
        markets = r.json()
    now = datetime.now(timezone.utc)
    for m in markets:
        liq    = float(m.get('liquidity', 0))
        spread = float(m.get('spread', 1))
        outcomes = m.get('outcomes', [])
        end = m.get('endDate') or m.get('endDateIso', '')
        end = end.rstrip('Z')
        if 'T' not in end:
            end += 'T23:59:59'
        resolves_at = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
        hours = (resolves_at - now).total_seconds() / 3600
        print(f"outcomes={len(outcomes)} liq={liq:.0f} spread={spread:.3f} hours={hours:.0f} q={m['question'][:45]}")

asyncio.run(test())