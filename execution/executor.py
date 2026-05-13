from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone

from data.models import TradeDecision, OpenPosition, Side

log = logging.getLogger(__name__)

try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds, OrderArgsV2
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    log.warning("py_clob_client_v2 nao instalado. A correr em modo DRY RUN.")


class Executor:
    def __init__(
        self,
        private_key:    str,
        api_key:        str,
        api_secret:     str,
        api_passphrase: str,
        proxy_addr:     str,
        dry_run:        bool = False,
    ):
        self.dry_run    = dry_run or not CLOB_AVAILABLE
        self.proxy_addr = proxy_addr

        if not self.dry_run:
            creds = ApiCreds(
                api_key        = api_key,
                api_secret     = api_secret,
                api_passphrase = api_passphrase,
            )
            self.clob = ClobClient(
                host           = "https://clob.polymarket.com",
                chain_id       = 137,
                key            = private_key,
                creds          = creds,
                signature_type = 3,
                funder         = proxy_addr,
            )
            log.info("ClobClient V2 inicializado em modo LIVE")
        else:
            self.clob = None
            log.info("Executor em modo DRY RUN (sem execucao real)")

    def execute(self, decision: TradeDecision) -> OpenPosition | None:
        market   = decision.market
        side     = decision.side
        price    = decision.entry_price
        size     = max(round(decision.size_usdc / price, 2), 5.0)  # minimo 5 shares
        trade_id = str(uuid.uuid4())[:8]

        if self.dry_run:
            log.info(
                f"[DRY RUN] {trade_id} — {side.value} {size:.2f} shares "
                f"@ {price:.3f} — {market.question[:50]}"
            )
            return self._build_position(trade_id, decision, price)

        try:
            token_id = self._get_token_id(market.condition_id, side)
            if not token_id:
                log.error(f"Sem token_id para {market.condition_id} {side.value}")
                return None

            order = self.clob.create_and_post_order(OrderArgsV2(
                token_id = token_id,
                price    = price,
                size     = size,
                side     = "BUY",
            ))

            if order and order.get("success"):
                log.info(f"Ordem executada: {trade_id} orderID={order.get('orderID','')[:16]}...")
                return self._build_position(trade_id, decision, price)
            else:
                log.warning(f"Ordem nao executada: {order}")
                return None

        except Exception as e:
            log.error(f"Erro na execucao de {trade_id}: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run:
            return True
        try:
            self.clob.cancel_order(order_id)
            return True
        except Exception as e:
            log.error(f"Erro ao cancelar {order_id}: {e}")
            return False

    def _get_token_id(self, condition_id: str, side: Side) -> str | None:
        try:
            market_data = self.clob.get_market(condition_id)
            tokens      = market_data.get("tokens", [])
            # Tenta encontrar YES/NO explícito
            for t in tokens:
                if t.get("outcome", "").upper() == side.value:
                    return t["token_id"]
            # Mercados com nomes de equipas: YES=primeiro token, NO=segundo
            if tokens:
                idx = 0 if side == Side.YES else 1
                if idx < len(tokens):
                    return tokens[idx]["token_id"]
        except Exception as e:
            log.error(f"Erro ao obter token_id: {e}")
        return None

    def _build_position(self, trade_id: str, decision: TradeDecision, actual_price: float) -> OpenPosition:
        return OpenPosition(
            trade_id        = trade_id,
            market          = decision.market,
            side            = decision.side,
            size_usdc       = decision.size_usdc,
            entry_price     = actual_price,
            entry_time      = datetime.now(timezone.utc),
            target_exit     = decision.target_exit_price,
            current_price   = actual_price,
            peak_price      = actual_price,
            volume_baseline = decision.market.volume_usdc,
        )
