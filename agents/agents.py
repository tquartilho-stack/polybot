from __future__ import annotations
import asyncio
import logging

import httpx

from data.models import Market, WalletProfile, AgentSignal, AgentName, Side

log = logging.getLogger(__name__)

POLY_DATA_API = "https://data-api.polymarket.com"


class BaseAgent:
    name: AgentName

    def analyze(self, markets: list[Market], wallets: list[WalletProfile] | None = None) -> list[AgentSignal]:
        raise NotImplementedError


class ArbitrageAgent(BaseAgent):
    name = AgentName.ARBITRAGE

    SCORE_TO_PROB = {
        (9.0, 10.1): 0.75,
        (7.5,  9.0): 0.65,
        (6.0,  7.5): 0.55,
    }

    def analyze(self, markets: list[Market], wallets=None) -> list[AgentSignal]:
        signals: list[AgentSignal] = []

        for m in markets:
            side = self._extract_side(m.score_reason)
            if not side:
                continue

            current_price = m.yes_price if side == Side.YES else m.no_price
            expected_prob = self._expected_prob(m.score)

            if expected_prob == 0 or current_price >= expected_prob:
                continue

            gap = expected_prob - current_price
            if gap < 0.06:
                continue

            confidence = min(gap / 0.25, 1.0)

            signals.append(AgentSignal(
                agent           = self.name,
                market          = m,
                side            = side,
                confidence      = confidence,
                reason          = f"Gap {gap:.0%} entre preço ({current_price:.2f}) e prob esperada ({expected_prob:.2f})",
                suggested_price = current_price + 0.01,
            ))

        log.info(f"ArbitrageAgent: {len(signals)} sinais")
        return signals

    def _extract_side(self, score_reason: str) -> Side | None:
        if score_reason.startswith("[YES]"):
            return Side.YES
        if score_reason.startswith("[NO]"):
            return Side.NO
        return None

    def _expected_prob(self, score: float) -> float:
        for (lo, hi), prob in self.SCORE_TO_PROB.items():
            if lo <= score < hi:
                return prob
        return 0


class ConvergenceAgent(BaseAgent):
    name = AgentName.CONVERGENCE

    MAX_HOURS = 24.0
    MIN_PRICE = 0.55
    MAX_PRICE = 0.88

    def analyze(self, markets: list[Market], wallets=None) -> list[AgentSignal]:
        signals: list[AgentSignal] = []

        for m in markets:
            if m.hours_to_resolve > self.MAX_HOURS:
                continue

            if self.MIN_PRICE <= m.yes_price <= self.MAX_PRICE:
                side          = Side.YES
                current_price = m.yes_price
            elif self.MIN_PRICE <= m.no_price <= self.MAX_PRICE:
                side          = Side.NO
                current_price = m.no_price
            else:
                continue

            time_factor  = 1.0 - (m.hours_to_resolve / self.MAX_HOURS)
            price_factor = (current_price - self.MIN_PRICE) / (self.MAX_PRICE - self.MIN_PRICE)
            confidence   = time_factor * 0.5 + price_factor * 0.5

            signals.append(AgentSignal(
                agent           = self.name,
                market          = m,
                side            = side,
                confidence      = confidence,
                reason          = f"Convergência: {m.hours_to_resolve:.1f}h para resolver, preço={current_price:.2f}",
                suggested_price = current_price + 0.01,
            ))

        log.info(f"ConvergenceAgent: {len(signals)} sinais")
        return signals


class WhaleCopyAgent(BaseAgent):
    name = AgentName.WHALE_COPY

    MIN_WHALE_COUNT = 2
    MAX_ENTRY_PRICE = 0.80

    def analyze(self, markets: list[Market], wallets: list[WalletProfile] | None = None) -> list[AgentSignal]:
        # Versão sync vazia — usar analyze_async no main
        return []

    async def analyze_async(self, markets: list[Market], wallets: list[WalletProfile] | None = None) -> list[AgentSignal]:
        if not wallets:
            log.warning("WhaleCopyAgent: sem wallets, a saltar")
            return []

        whale_positions = await self._fetch_all_positions(wallets)

        if not whale_positions:
            log.warning("WhaleCopyAgent: sem posições obtidas")
            return []

        scored_ids = {m.condition_id: m for m in markets}

        from collections import defaultdict
        whale_map: dict[str, dict] = defaultdict(lambda: {"YES": [], "NO": []})

        for pos in whale_positions:
            cid   = pos.get("conditionId", "")
            side  = pos.get("outcome", "").upper()
            price = float(pos.get("curPrice") or pos.get("avgPrice") or 0)
            if cid and side in ("YES", "NO"):
                whale_map[cid][side].append(price)

        signals: list[AgentSignal] = []

        for cid, sides in whale_map.items():
            if cid not in scored_ids:
                continue

            market = scored_ids[cid]

            for side_str, prices in sides.items():
                if len(prices) < self.MIN_WHALE_COUNT:
                    continue

                avg_entry = sum(prices) / len(prices)
                if avg_entry > self.MAX_ENTRY_PRICE:
                    continue

                side       = Side.YES if side_str == "YES" else Side.NO
                confidence = min(len(prices) / 5, 1.0)

                signals.append(AgentSignal(
                    agent           = self.name,
                    market          = market,
                    side            = side,
                    confidence      = confidence,
                    reason          = f"{len(prices)} whales em {side_str} @ avg {avg_entry:.2f}",
                    suggested_price = (market.yes_price if side == Side.YES else market.no_price) + 0.01,
                ))

        log.info(f"WhaleCopyAgent: {len(signals)} sinais de {len(whale_positions)} posições")
        return signals

    async def _fetch_all_positions(self, wallets: list[WalletProfile]) -> list[dict]:
        async with httpx.AsyncClient() as client:
            tasks = [self._fetch_positions(client, w.address) for w in wallets[:20]]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        all_positions: list[dict] = []
        for r in results:
            if isinstance(r, list):
                all_positions.extend(r)
        return all_positions

    async def _fetch_positions(self, client: httpx.AsyncClient, address: str) -> list[dict]:
        try:
            r = await client.get(
                f"{POLY_DATA_API}/positions",
                params={"user": address, "sizeThreshold": "0.01"},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            log.debug(f"Falha posições {address[:10]}…: {e}")
            return []
