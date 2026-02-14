"""
Post-Close Sniper Strategy

Pure directional bet based on Chainlink oracle price delta.

Resolution rule (for a 2:00-2:15 market):
  - Init price:  Chainlink report at start_time (exact boundary)
  - Final price: Chainlink report at end_time   (exact boundary)
  - UP wins if final >= init, DOWN wins if final < init (ties → UP)

Key insight: end_price[market N] == start_price[market N+1] because Chainlink
reports at consecutive boundaries use the same report for the shared boundary.

Strategy flow:
1. On market active → query Chainlink REST API at start_time for definitive start price.
2. Pre-sign UP + DOWN orders.
3. Sleep until end_time + delay → query REST API at end_time for definitive end price
   → compare → POST winner.
4. Carry end price forward as next market's start price.

Uses Chainlink Data Streams REST API for both trigger timing and definitive prices.
No dependency on RTDS WS.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Optional
from datetime import datetime

import httpx
import pytz
from eth_abi import decode as abi_decode

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

# Configuration
SNIPE_SIZE_USD = 10         # $ per order
MAX_PRICE = 0.99            # Market order price cap (CLOB max is 0.99)
MAX_RETRIES = 3             # Retries if POST fails
GRACE_SECONDS = 30          # Seconds post-close for retries
REST_POLL_INTERVAL_MS = 200  # ms between REST API polls after boundary
REST_POLL_TIMEOUT_S = 15    # Max seconds to poll before giving up

# Chainlink DS REST API
CL_API_BASE = os.environ.get("CHAINLINK_API_BASE", "https://api.dataengine.chain.link")

# Feed IDs (same as chainlink_ds.py)
_FEED_IDS = {
    "BTC": "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8",
    "ETH": "0x000362205e10b3a147d02792eccee483dca6c7b44ecce7012cb8c6e0b68b3ae9",
    "SOL": "0x0003b778d3f6b2ac4991302b89cb313f99a42467d6c9c5f96f57c29c0d2bc24f",
}

# V3 report ABI
_OUTER_ABI = ["bytes32[3]", "bytes", "bytes32[]", "bytes32[]", "bytes32"]
_V3_ABI = [
    "bytes32", "uint32", "uint32", "uint192", "uint192",
    "uint32", "int192", "int192", "int192",
]


def _cl_auth_headers(user_id: str, secret: str, cf_client_id: str, method: str, path: str) -> dict:
    """Build HMAC auth headers for Chainlink Data Streams REST API."""
    timestamp = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(b"").hexdigest()
    sign_string = f"{method} {path} {body_hash} {user_id} {timestamp}"
    signature = hmac.new(secret.encode(), sign_string.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": user_id,
        "X-Authorization-Timestamp": timestamp,
        "X-Authorization-Signature-SHA256": signature,
        "CF-Access-Client-Id": cf_client_id,
        "CF-Access-Client-Secret": secret,
    }


def _decode_v3_price(hex_payload: str) -> Optional[float]:
    """Decode V3 report and return benchmarkPrice."""
    try:
        raw = bytes.fromhex(hex_payload.removeprefix("0x"))
        outer = abi_decode(_OUTER_ABI, raw)
        v3 = abi_decode(_V3_ABI, outer[1])
        return v3[6] / 10**18  # benchmarkPrice
    except Exception as e:
        logger.warning(f"[Sniper] V3 decode error: {e}")
        return None


@dataclass
class MarketState:
    """Tracks state for a single market snipe."""
    condition_id: str
    question: str
    up_token_id: str
    down_token_id: str
    start_time: datetime
    end_time: datetime
    start_time_unix: int = 0    # start_time as epoch seconds
    end_time_unix: int = 0      # end_time as epoch seconds
    start_chainlink_price: Optional[float] = None
    end_chainlink_price: Optional[float] = None
    end_price_finalized: bool = False
    presigned_up: Optional[object] = None
    presigned_down: Optional[object] = None
    order_sent: bool = False
    order_result: Optional[Dict] = None
    order_side: Optional[str] = None
    retries: int = 0
    snipe_task: Optional[object] = None  # asyncio.Task for scheduled snipe


class PostCloseSniperStrategy(BaseStrategy):
    """
    Post-close sniper: directional bet based on Chainlink start/end delta.

    Uses Chainlink DS REST API for definitive prices at exact boundaries.
    Schedules snipe via asyncio.sleep (no RTDS dependency).
    """

    name = "postclose_sniper"
    description = "Directional snipe using Chainlink start/end price delta"

    requires_price_websocket = True
    requires_data_collector = False
    requires_rtds = False

    post_close_grace_seconds = GRACE_SECONDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.market_states: Dict[str, MarketState] = {}
        # Carried forward from previous market's end price
        self.carried_price: Optional[float] = None
        # HTTP client for Chainlink REST API
        self._cl_client: Optional[httpx.AsyncClient] = None
        # Chainlink credentials
        self._cl_user = os.environ.get("CHAINLINK_USERNAME", "")
        self._cl_cf_id = os.environ.get("CHAINLINK_API_KEY", "")
        self._cl_secret = os.environ.get("CHAINLINK_PASSWORD", "")
        self._cl_available = bool(self._cl_user and self._cl_cf_id and self._cl_secret)
        # Feed ID for our market symbol
        self._feed_id: Optional[str] = None

    async def initialize(self) -> None:
        logger.info("[Sniper] Initializing post-close sniper")

        self._feed_id = _FEED_IDS.get(self.market_symbol.upper())
        if not self._feed_id:
            logger.error(f"[Sniper] No Chainlink feed ID for {self.market_symbol}")
            return

        if not self._cl_available:
            logger.error(
                "[Sniper] Chainlink REST API credentials missing "
                "(CHAINLINK_USERNAME, CHAINLINK_API_KEY, CHAINLINK_PASSWORD). "
                "Cannot get definitive boundary prices."
            )
            return

        self._cl_client = httpx.AsyncClient(http2=True, timeout=10.0)
        logger.info("[Sniper] Chainlink REST API client ready")

    # ==================== Chainlink REST API ====================

    async def _query_chainlink_price(self, timestamp_unix: int) -> Optional[float]:
        """
        Query Chainlink DS REST API for the BTC price at a specific unix timestamp.

        Returns the benchmarkPrice from the V3 report, or None on failure.
        The API returns the report at or before the given timestamp.
        """
        if not self._cl_client or not self._cl_available or not self._feed_id:
            return None

        path = f"/api/v1/reports?feedID={self._feed_id}&timestamp={timestamp_unix}"
        url = f"{CL_API_BASE}{path}"
        headers = _cl_auth_headers(self._cl_user, self._cl_secret, self._cl_cf_id, "GET", path)

        try:
            resp = await self._cl_client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"[Sniper] CL REST {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            report = data.get("report") or (data.get("reports", [None])[0] if data.get("reports") else None)
            if not report:
                logger.warning(f"[Sniper] CL REST: no report in response")
                return None

            full_hex = report.get("fullReport")
            if not full_hex:
                return None

            price = _decode_v3_price(full_hex)
            if price:
                logger.info(f"[Sniper] CL REST price at {timestamp_unix}: ${price:,.2f}")
            return price

        except Exception as e:
            logger.warning(f"[Sniper] CL REST error: {e}")
            return None

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
            start_time_unix=int(start_time.timestamp()),
            end_time_unix=int(end_time.timestamp()),
        )
        self.market_states[condition_id] = state
        self.mark_market_processed(condition_id)

        logger.info(
            f"[Sniper] Registered: {state.question[:60]} | "
            f"start={start_time} end={end_time}"
        )

    async def on_market_active(self, market_data: Dict) -> None:
        """Get definitive start price from CL REST API, pre-sign orders, schedule snipe."""
        condition_id = market_data['condition_id']
        state = self.market_states.get(condition_id)
        if not state:
            return

        # Option 1: Use carried price from previous market's end (already queried via REST)
        if self.carried_price is not None:
            state.start_chainlink_price = self.carried_price
            logger.info(
                f"[Sniper] Start price (carried): ${self.carried_price:,.2f}"
            )
        else:
            # Option 2: Query REST API at start boundary
            price = await self._query_chainlink_price(state.start_time_unix)
            if price:
                state.start_chainlink_price = price
                logger.info(f"[Sniper] Start price (REST API): ${price:,.2f}")

        if state.start_chainlink_price is not None:
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
            logger.info("[Sniper] No start price available - will only carry end price")

        # Schedule async task to sleep until end_time and execute snipe
        state.snipe_task = asyncio.create_task(self._wait_and_snipe(state))

    async def _wait_and_snipe(self, state: MarketState) -> None:
        """Sleep until end_time, then poll REST API until report is available."""
        try:
            now = datetime.now(pytz.UTC)
            wait_s = (state.end_time - now).total_seconds()
            if wait_s > 0:
                logger.info(f"[Sniper] Waiting {wait_s:.1f}s until end_time boundary")
                await asyncio.sleep(wait_s)

            # Poll REST API at boundary until report appears
            t0 = time.perf_counter()
            attempts = 0
            while (time.perf_counter() - t0) < REST_POLL_TIMEOUT_S:
                attempts += 1
                end_price = await self._query_chainlink_price(state.end_time_unix)
                if end_price:
                    latency_ms = (time.perf_counter() - t0) * 1000
                    state.end_chainlink_price = end_price
                    logger.info(
                        f"[Sniper] Got end price after {attempts} polls "
                        f"({latency_ms:.0f}ms from boundary)"
                    )
                    break
                await asyncio.sleep(REST_POLL_INTERVAL_MS / 1000)

            if state.end_chainlink_price is None:
                logger.error(
                    f"[Sniper] No end price after {REST_POLL_TIMEOUT_S}s "
                    f"({attempts} polls)"
                )
                return

            state.end_price_finalized = True
            self.carried_price = state.end_chainlink_price

            # No start price → only carry forward
            if state.start_chainlink_price is None:
                logger.info(
                    f"[Sniper] No start price, skipping. "
                    f"End=${state.end_chainlink_price:,.2f} → carried"
                )
                return

            delta = state.end_chainlink_price - state.start_chainlink_price
            logger.info(
                f"[Sniper] End price (REST): ${state.end_chainlink_price:,.2f} "
                f"(delta: ${delta:+.2f})"
            )

            self._execute_snipe(state)

        except asyncio.CancelledError:
            logger.info(f"[Sniper] Snipe task cancelled for {state.condition_id[:16]}")
        except Exception as e:
            logger.error(f"[Sniper] Snipe task error: {e}", exc_info=True)

    async def on_price_update(
        self,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market: Dict
    ) -> None:
        """Hot path: only used for order retries if first attempt failed."""
        condition_id = market.get('conditionId')
        if not condition_id:
            return

        state = self.market_states.get(condition_id)
        if not state or state.order_sent or not state.end_price_finalized:
            return

        # Retry execute if previous attempt failed
        if state.start_chainlink_price is not None:
            self._execute_snipe(state)

    def _execute_snipe(self, state: MarketState) -> None:
        """Compare start vs end Chainlink price and POST pre-signed order."""
        if state.order_sent or state.end_chainlink_price is None:
            return

        # Determine direction (Polymarket resolves UP on >=, i.e. ties go UP)
        if state.end_chainlink_price >= state.start_chainlink_price:
            side = "UP"
            signed_order = state.presigned_up
        else:
            side = "DOWN"
            signed_order = state.presigned_down

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
            f"start=${state.start_chainlink_price or 0:,.2f} "
            f"end=${state.end_chainlink_price or 0:,.2f} "
            f"delta={delta_str} | "
            f"bet={state.order_side or 'NONE'} winner={winner}{correct} | "
            f"sent={state.order_sent} retries={state.retries}"
        )

    async def shutdown(self) -> None:
        # Cancel pending snipe tasks
        for state in self.market_states.values():
            if state.snipe_task and not state.snipe_task.done():
                state.snipe_task.cancel()

        sent = sum(1 for s in self.market_states.values() if s.order_sent and s.order_side)
        total = len(self.market_states)
        logger.info(f"[Sniper] Shutdown: {sent}/{total} markets sniped")

        if self._cl_client:
            await self._cl_client.aclose()
