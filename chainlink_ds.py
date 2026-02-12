"""
Chainlink Data Streams direct WebSocket client.

Connects directly to wss://ws.dataengine.chain.link for real-time BTC/ETH/SOL
prices, bypassing the Polymarket RTDS relay. Uses HMAC-SHA256 auth and decodes
V3 reports in-house via eth_abi.

Duck-type compatible with RTDSCryptoPrices — drop-in replacement.

Credentials (.env):
    CHAINLINK_API_KEY   — Authorization header + HMAC signing component
    CHAINLINK_PASSWORD  — HMAC-SHA256 secret
"""

import asyncio
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

import websockets
from eth_abi import decode
from websockets.exceptions import ConnectionClosed

try:
    import orjson as _json
except ImportError:
    import json as _json  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# --- Feed IDs (Data Streams v3) ---
FEED_IDS = {
    "BTC": "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8",
    "ETH": "0x000362205e10b3a147d02792eccee483dca6c7b44ecce7012cb8c6e0b68b3ae9",
    "SOL": "0x0003b778d3f6b2ac4991302b89cb313f99a42467d6c9c5f96f57c29c0d2bc24f",
}

# Reverse lookup: feedId hex (no 0x, lowercase) -> symbol
_FEED_TO_SYMBOL: Dict[str, str] = {v[2:].lower(): k for k, v in FEED_IDS.items()}

WS_BASE = "wss://ws.dataengine.chain.link"
WS_PATH = "/api/v1/ws"

# V3 report ABI types
_OUTER_ABI = ["bytes32[3]", "bytes", "bytes32[]", "bytes32[]", "bytes32"]
_V3_ABI = [
    "bytes32",   # feedId
    "uint32",    # validFromTimestamp
    "uint32",    # observationsTimestamp
    "uint192",   # nativeFee
    "uint192",   # linkFee
    "uint32",    # expiresAt
    "int192",    # benchmarkPrice
    "int192",    # bid
    "int192",    # ask
]


def _build_auth_headers(
    user_id: str, secret: str, cf_client_id: str, method: str, path: str
) -> dict:
    """
    Build authentication headers for Chainlink Data Streams.

    Chainlink mainnet sits behind Cloudflare Access, so we need two layers:
    - HMAC-SHA256 (user_id + secret) for the Data Streams backend
    - CF-Access-Client-Id / CF-Access-Client-Secret for the Cloudflare proxy

    Env mapping:
        CHAINLINK_USERNAME  -> user_id   (Authorization header + HMAC component)
        CHAINLINK_API_KEY   -> cf_client_id (CF-Access-Client-Id)
        CHAINLINK_PASSWORD  -> secret    (HMAC key + CF-Access-Client-Secret)
    """
    timestamp = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(b"").hexdigest()

    sign_string = f"{method} {path} {body_hash} {user_id} {timestamp}"
    signature = hmac.new(
        secret.encode(), sign_string.encode(), hashlib.sha256
    ).hexdigest()

    return {
        "Authorization": user_id,
        "X-Authorization-Timestamp": timestamp,
        "X-Authorization-Signature-SHA256": signature,
        "CF-Access-Client-Id": cf_client_id,
        "CF-Access-Client-Secret": secret,
    }


def _decode_report(hex_payload: str) -> Optional[Tuple[str, float, int]]:
    """
    Decode a Chainlink V3 report hex string.

    Returns (symbol, price, timestamp_ms) or None on failure.
    """
    try:
        raw = bytes.fromhex(hex_payload.removeprefix("0x"))

        # Stage 1: outer envelope
        outer = decode(_OUTER_ABI, raw)
        report_blob = outer[1]  # bytes field = actual report data

        # Stage 2: V3 report payload
        v3 = decode(_V3_ABI, report_blob)
        feed_id_hex = v3[0].hex()  # bytes32 -> hex string
        observations_ts = v3[2]    # uint32, Unix seconds
        benchmark_price = v3[6]    # int192, scaled by 1e18

        symbol = _FEED_TO_SYMBOL.get(feed_id_hex)
        if symbol is None:
            logger.debug(f"[ChainlinkDS] Unknown feedId: {feed_id_hex[:16]}...")
            return None

        price = benchmark_price / 10**18
        timestamp_ms = observations_ts * 1000

        return symbol, price, timestamp_ms

    except Exception as e:
        logger.debug(f"[ChainlinkDS] Report decode error: {e}")
        return None


class ChainlinkDataStreams:
    """
    Direct Chainlink Data Streams WebSocket client.

    Duck-type compatible with RTDSCryptoPrices.
    """

    def __init__(self):
        self.ws = None
        self.connected = False
        self.running = False

        # Price cache (same structure as RTDSCryptoPrices)
        self.prices: Dict[str, float] = {}
        self.last_update: Dict[str, datetime] = {}
        self.timestamps: Dict[str, int] = {}  # symbol -> timestamp_ms

        # Callback for price updates
        self.on_price_update: Optional[Callable[[str, float], None]] = None

        # Reconnection settings
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 30.0

        # Credentials (see _build_auth_headers docstring for mapping)
        self._user_id = os.environ.get("CHAINLINK_USERNAME", "")
        self._cf_client_id = os.environ.get("CHAINLINK_API_KEY", "")
        self._secret = os.environ.get("CHAINLINK_PASSWORD", "")

    async def start(self) -> bool:
        """Connect and start listening. Returns True on success."""
        if not self._user_id or not self._cf_client_id or not self._secret:
            logger.error("[ChainlinkDS] Missing CHAINLINK_USERNAME, CHAINLINK_API_KEY, or CHAINLINK_PASSWORD")
            return False

        if await self._connect():
            asyncio.create_task(self._listen())
            return True
        return False

    async def _connect(self) -> bool:
        """Establish authenticated WebSocket connection."""
        try:
            feed_csv = ",".join(FEED_IDS.values())
            path = f"{WS_PATH}?feedIDs={feed_csv}"
            url = f"{WS_BASE}{path}"

            headers = _build_auth_headers(
                self._user_id, self._secret, self._cf_client_id, "GET", path
            )

            self.ws = await websockets.connect(
                url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
            self.connected = True
            self.reconnect_delay = 1.0
            logger.info(f"[ChainlinkDS] Connected to {WS_BASE}")
            return True

        except Exception as e:
            logger.error(f"[ChainlinkDS] Connection failed: {e}")
            self.connected = False
            return False

    async def _listen(self):
        """Main receive loop with auto-reconnect."""
        self.running = True

        while self.running and self.connected:
            try:
                message = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                self._handle_message(message)

            except asyncio.TimeoutError:
                try:
                    pong = await self.ws.ping()
                    await asyncio.wait_for(pong, timeout=10)
                except Exception:
                    logger.warning("[ChainlinkDS] Ping timeout, reconnecting...")
                    await self._reconnect()

            except ConnectionClosed:
                logger.warning("[ChainlinkDS] Connection closed")
                if self.running:
                    await self._reconnect()

            except Exception as e:
                logger.error(f"[ChainlinkDS] Listen error: {e}")
                if self.running:
                    await self._reconnect()

    def _handle_message(self, raw_message):
        """Parse incoming WS message and decode report."""
        try:
            data = _json.loads(raw_message)
        except Exception:
            logger.debug(f"[ChainlinkDS] Invalid message: {str(raw_message)[:100]}")
            return

        # Data Streams sends {"report": {"fullReport": "0x..."}, ...}
        report = data.get("report")
        if not report:
            return

        full_report_hex = report.get("fullReport")
        if not full_report_hex:
            return

        result = _decode_report(full_report_hex)
        if result is None:
            return

        symbol, price, timestamp_ms = result
        self._update_price(symbol, price, timestamp_ms)

    def _update_price(self, symbol: str, price: float, timestamp_ms: int):
        """Update price cache and trigger callback."""
        self.prices[symbol] = price
        self.last_update[symbol] = datetime.now()
        self.timestamps[symbol] = timestamp_ms

        if self.on_price_update:
            self.on_price_update(symbol, price)

        logger.debug(f"[ChainlinkDS] {symbol}: ${price:,.2f} (ts={timestamp_ms})")

    async def _reconnect(self):
        """Reconnect with exponential backoff."""
        self.connected = False

        while self.running:
            logger.info(f"[ChainlinkDS] Reconnecting in {self.reconnect_delay}s...")
            await asyncio.sleep(self.reconnect_delay)

            if await self._connect():
                return

            self.reconnect_delay = min(
                self.reconnect_delay * 2, self.max_reconnect_delay
            )

    async def close(self):
        """Close the WebSocket connection."""
        self.running = False
        self.connected = False

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        logger.info("[ChainlinkDS] Closed")

    # --- Public getters (identical signatures to RTDSCryptoPrices) ---

    def get_price(self, symbol: str = "BTC") -> Optional[float]:
        """Get current price for a symbol."""
        return self.prices.get(symbol.upper())

    def get_price_with_ts(self, symbol: str = "BTC") -> Optional[Tuple[float, int]]:
        """Get (price, chainlink_timestamp_ms) or None."""
        sym = symbol.upper()
        price = self.prices.get(sym)
        ts = self.timestamps.get(sym)
        if price is not None and ts is not None:
            return (price, ts)
        return None

    def get_btc_price(self) -> Optional[float]:
        """Get current BTC price."""
        return self.get_price("BTC")

    def get_all_prices(self) -> Dict[str, float]:
        """Get all cached prices."""
        return dict(self.prices)
