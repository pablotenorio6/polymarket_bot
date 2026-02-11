"""
Post-Close Sniper Strategy (Simplified)

Pure directional bet based on Chainlink oracle price delta.

Resolution rule (for a 2:00-2:15 market):
  - Init price:  last Chainlink update with ts <= start_time (2:00:00.000)
  - Final price: last Chainlink update with ts <= end_time   (2:15:00.000)
  - UP wins if final > init, DOWN wins if final < init

Key insight: end_price[market N] == start_price[market N+1] because markets
are consecutive. So we only track end prices and carry forward.

Strategy flow:
1. First market = calibration: track end price only, no bet.
2. End price finalized → carry forward as start price for next market → pre-sign.
3. Next market end price finalized → compare with carried start → POST winner.
4. Repeat.

Requires RTDS Chainlink feed for oracle prices.
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional
from datetime import datetime

import pytz

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

# Configuration
SNIPE_SIZE_USD = 10         # $ per order
MAX_PRICE = 0.99             # Market order price cap (CLOB max is 0.99)
MAX_RETRIES = 3             # Retries if POST fails
GRACE_SECONDS = 30          # Seconds post-close for retries
PRE_END_TRACK_S = 10        # Start tracking Chainlink price N seconds before end_time


@dataclass
class MarketState:
    """Tracks state for a single market snipe."""
    condition_id: str
    question: str
    up_token_id: str
    down_token_id: str
    start_time: datetime
    end_time: datetime
    end_time_ms: int = 0        # end_time as epoch ms (for Chainlink ts comparison)
    start_chainlink_price: Optional[float] = None
    start_chainlink_ts: Optional[int] = None
    end_chainlink_price: Optional[float] = None
    end_chainlink_ts: Optional[int] = None
    end_price_finalized: bool = False
    presigned_up: Optional[object] = None
    presigned_down: Optional[object] = None
    order_sent: bool = False
    order_result: Optional[Dict] = None
    order_side: Optional[str] = None
    retries: int = 0


class PostCloseSniperStrategy(BaseStrategy):
    """
    Post-close sniper: directional bet based on Chainlink start/end delta.

    First market is calibration (no bet). After that, each market's end price
    carries forward as the next market's start price.
    """

    name = "postclose_sniper"
    description = "Directional snipe using Chainlink start/end price delta"

    requires_price_websocket = True
    requires_data_collector = False
    requires_rtds = True

    post_close_grace_seconds = GRACE_SECONDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.market_states: Dict[str, MarketState] = {}
        # Carried forward from previous market's end price
        self.carried_price: Optional[float] = None
        self.carried_ts: Optional[int] = None

    async def initialize(self) -> None:
        logger.info("[Sniper] Initializing post-close sniper")
        if not (self.rtds_client and self.rtds_client.connected):
            logger.warning("[Sniper] RTDS not connected - strategy will not work")

    # ==================== Lifecycle ====================

    async def on_new_market(self, market_data: Dict) -> None:
        condition_id = market_data['condition_id']
        if self.is_market_processed(condition_id):
            return

        start_time = market_data['start_time']
        end_time = market_data['end_time']

        state = MarketState(
            condition_id=condition_id,
            question=market_data.get('question', ''),
            up_token_id=market_data['up_token_id'],
            down_token_id=market_data['down_token_id'],
            start_time=start_time,
            end_time=end_time,
            end_time_ms=int(end_time.timestamp() * 1000),
        )
        self.market_states[condition_id] = state
        self.mark_market_processed(condition_id)

        logger.info(
            f"[Sniper] Registered: {state.question[:60]} | "
            f"start={start_time} end={end_time}"
        )

    async def on_market_active(self, market_data: Dict) -> None:
        """Assign carried start price and pre-sign orders."""
        condition_id = market_data['condition_id']
        state = self.market_states.get(condition_id)
        if not state:
            return

        if self.carried_price is not None:
            state.start_chainlink_price = self.carried_price
            state.start_chainlink_ts = self.carried_ts
            logger.info(
                f"[Sniper] Start price (carried): ${self.carried_price:,.2f} "
                f"(ts={self.carried_ts})"
            )
            # Pre-sign market buy orders for both sides
            state.presigned_up = self.trader.presign_market_buy(
                state.up_token_id, MAX_PRICE, SNIPE_SIZE_USD
            )
            state.presigned_down = self.trader.presign_market_buy(
                state.down_token_id, MAX_PRICE, SNIPE_SIZE_USD
            )
            up_ok = "OK" if state.presigned_up else "FAIL"
            down_ok = "OK" if state.presigned_down else "FAIL"
            logger.info(f"[Sniper] Pre-signed orders: UP={up_ok}, DOWN={down_ok}")
        else:
            logger.info("[Sniper] No carried price yet - calibration market (no bet)")

    async def on_price_update(
        self,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market: Dict
    ) -> None:
        """
        Hot path (~10ms). Two jobs:
        1. Track end price: last Chainlink with ts <= end_time_ms (last N seconds)
        2. When end price finalized → carry forward + execute (if start price exists)
        """
        condition_id = market.get('conditionId')
        if not condition_id:
            return

        state = self.market_states.get(condition_id)
        if not state or state.order_sent:
            return

        # If end price already finalized but order pending → retry execute
        if state.end_price_finalized:
            if state.start_chainlink_price is not None:
                self._execute_snipe(state)
            return

        # Only start tracking in the last N seconds before end_time
        now = datetime.now(pytz.UTC)
        seconds_to_end = (state.end_time - now).total_seconds()
        if seconds_to_end > PRE_END_TRACK_S:
            return

        # Read current RTDS Chainlink price + oracle timestamp
        if not self.rtds_client:
            return
        result = self.rtds_client.get_price_with_ts(self.market_symbol)
        if not result:
            return
        price, chainlink_ts = result

        if chainlink_ts <= state.end_time_ms:
            # Rolling update: keep latest Chainlink price before end_time
            state.end_chainlink_price = price
            state.end_chainlink_ts = chainlink_ts
            return

        # First Chainlink update past end_time → end price finalized
        state.end_price_finalized = True
        if state.end_chainlink_price is None:
            # Edge case: first Chainlink update we see is already past end_time
            state.end_chainlink_price = price
            state.end_chainlink_ts = chainlink_ts
            logger.warning(
                f"[Sniper] No pre-end Chainlink seen, using post-end: "
                f"${price:,.2f} (ts={chainlink_ts})"
            )

        # Carry forward for next market
        self.carried_price = state.end_chainlink_price
        self.carried_ts = state.end_chainlink_ts

        # Calibration market (no start price) → just log and skip
        if state.start_chainlink_price is None:
            logger.info(
                f"[Sniper] Calibration done: end=${state.end_chainlink_price:,.2f} "
                f"(ts={state.end_chainlink_ts}) → carried as next start"
            )
            return

        delta = state.end_chainlink_price - state.start_chainlink_price
        logger.info(
            f"[Sniper] End price finalized: ${state.end_chainlink_price:,.2f} "
            f"(ts={state.end_chainlink_ts}, delta: ${delta:+.2f})"
        )

        # Execute immediately
        self._execute_snipe(state)

    def _execute_snipe(self, state: MarketState) -> None:
        """Compare start vs end Chainlink price and POST pre-signed order."""
        if state.order_sent or state.end_chainlink_price is None:
            return

        # Determine direction
        if state.end_chainlink_price > state.start_chainlink_price:
            side = "UP"
            signed_order = state.presigned_up
        elif state.end_chainlink_price < state.start_chainlink_price:
            side = "DOWN"
            signed_order = state.presigned_down
        else:
            state.order_sent = True
            logger.info("[Sniper] Start == End price, skipping")
            return

        if not signed_order:
            logger.warning(f"[Sniper] No pre-signed order for {side}")
            state.order_sent = True
            return

        if state.retries >= MAX_RETRIES:
            logger.warning(f"[Sniper] Max retries reached for {state.condition_id[:16]}")
            state.order_sent = True
            return

        # POST pre-signed order (fast path: ~20-50ms)
        t0 = time.perf_counter()
        result = self.trader.post_presigned_order(signed_order, "FAK")
        latency_ms = (time.perf_counter() - t0) * 1000

        if result and result.get('success'):
            state.order_sent = True
            state.order_result = result
            state.order_side = side
            order_id = result.get('orderID', 'N/A')
            logger.info(
                f"[Sniper] FILLED {side} ${SNIPE_SIZE_USD} in {latency_ms:.1f}ms "
                f"| orderID={order_id[:16] if order_id != 'N/A' else 'N/A'}"
            )
        else:
            state.retries += 1
            error_msg = result.get('errorMsg', 'unknown') if result else 'no response'
            logger.warning(
                f"[Sniper] Retry {state.retries}/{MAX_RETRIES}: {error_msg} ({latency_ms:.1f}ms)"
            )
            # Re-sign for next attempt (nonce/timestamp may expire)
            if side == "UP":
                state.presigned_up = self.trader.presign_market_buy(
                    state.up_token_id, MAX_PRICE, SNIPE_SIZE_USD
                )
            else:
                state.presigned_down = self.trader.presign_market_buy(
                    state.down_token_id, MAX_PRICE, SNIPE_SIZE_USD
                )

    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        condition_id = market_data.get('condition_id')
        state = self.market_states.get(condition_id) if condition_id else None
        if not state:
            return

        delta_str = "N/A"
        if state.start_chainlink_price and state.end_chainlink_price:
            delta = state.end_chainlink_price - state.start_chainlink_price
            delta_str = f"${delta:+.2f}"

        correct = ""
        if state.order_side and winner:
            correct = " CORRECT" if state.order_side == winner else " WRONG"

        logger.info(
            f"[Sniper] Market end: "
            f"start=${state.start_chainlink_price or 0:,.2f} (ts={state.start_chainlink_ts}) "
            f"end=${state.end_chainlink_price or 0:,.2f} (ts={state.end_chainlink_ts}) "
            f"delta={delta_str} | "
            f"bet={state.order_side or 'NONE'} winner={winner}{correct} | "
            f"sent={state.order_sent} retries={state.retries}"
        )

    async def shutdown(self) -> None:
        sent = sum(1 for s in self.market_states.values() if s.order_sent and s.order_side)
        total = len(self.market_states)
        logger.info(f"[Sniper] Shutdown: {sent}/{total} markets sniped")
