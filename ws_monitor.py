"""
WebSocket-based real-time price monitor for Polymarket

Optimized for minimal CPU usage:
- orjson for fast JSON parsing (5-10x faster than stdlib)
- Minimal object creation
- Direct dictionary access
- No unnecessary async/await
"""

import asyncio
import logging
import time
from typing import Dict, Optional, Callable, List
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

# Use orjson for faster JSON parsing (falls back to json if not available)
try:
    import orjson
    def json_loads(s): return orjson.loads(s)
    def json_dumps(d): return orjson.dumps(d).decode('utf-8')
except ImportError:
    import json
    json_loads = json.loads
    json_dumps = json.dumps

logger = logging.getLogger(__name__)

# Polymarket WebSocket endpoint
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Pre-compute constant strings for faster comparison
EVENT_TYPE_KEY = 'event_type'
LAST_TRADE_PRICE = 'last_trade_price'
ASSET_ID_KEY = 'asset_id'
PRICE_KEY = 'price'


class WebSocketPriceMonitor:
    """
    Real-time price monitor using Polymarket WebSocket.
    Optimized for minimal CPU overhead.
    """
    
    __slots__ = (
        'ws', 'prices', 'subscribed_tokens', 'connected', 'running',
        'on_price_update', 'message_count', 'last_update',
        'reconnect_delay', 'max_reconnect_delay'
    )
    
    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.prices: Dict[str, float] = {}
        self.subscribed_tokens: List[str] = []
        self.connected = False
        self.running = False
        self.on_price_update: Optional[Callable[[str, float], None]] = None
        self.message_count = 0
        self.last_update: Optional[datetime] = None
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 30.0
    
    async def connect(self):
        """Establish WebSocket connection"""
        try:
            self.ws = await websockets.connect(
                WS_MARKET_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            )
            self.connected = True
            self.reconnect_delay = 1.0
            # logger.info(f"WebSocket connected to {WS_MARKET_URL}")
            return True
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            self.connected = False
            return False
    
    async def subscribe(self, token_ids: List[str]):
        """Subscribe to price updates for specific tokens"""
        if not self.ws or not self.connected:
            logger.warning("Cannot subscribe: WebSocket not connected")
            return False

        try:
            message = {
                "auth": {},
                "type": "market",
                "assets_ids": token_ids
            }
            await self.ws.send(json_dumps(message))
            self.subscribed_tokens = token_ids
            # logger.info(f"Subscribed to {len(token_ids)} tokens: {[tid[:10] for tid in token_ids]}")
            await asyncio.sleep(2)
            return True
        except Exception as e:
            logger.error(f"Subscription failed: {e}")
            return False
    
    async def unsubscribe(self, token_ids: List[str]):
        """Unsubscribe from tokens"""
        if not self.ws or not self.connected:
            return
        
        try:
            # Make a copy to avoid modifying list while iterating
            tokens_to_remove = list(token_ids)
            
            message = {"assets_ids": tokens_to_remove, "operation": "unsubscribe"}
            await self.ws.send(json_dumps(message))
            
            # Clear local state
            for tid in tokens_to_remove:
                self.prices.pop(tid, None)
                self.subscribed_tokens.remove(tid)

            
        except Exception as e:
            logger.debug(f"Unsubscribe error: {e}")
    
    async def listen(self):
        """Listen for incoming messages"""
        if not self.ws:
            return
        
        self.running = True
        
        while self.running and self.connected:
            try:
                message = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                self._handle_message(message)  # Sync, no await needed
                
            except asyncio.TimeoutError:
                try:
                    pong = await self.ws.ping()
                    await asyncio.wait_for(pong, timeout=10)
                except:
                    logger.warning("Ping timeout, reconnecting...")
                    await self._reconnect()
                    
            except ConnectionClosed:
                logger.warning("WebSocket connection closed")
                if self.running:
                    await self._reconnect()
                    
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                if self.running:
                    await self._reconnect()
    
    def _handle_message(self, raw_message: str):
        """
        Process incoming WebSocket message.
        SYNC function - no async overhead for CPU-bound parsing.
        """
        try:
            data = json_loads(raw_message)
            self.message_count += 1
            
            # Fast path: check for list or dict
            if isinstance(data, list):
                for item in data:
                    self._process_event(item)
            elif isinstance(data, dict):
                self._process_event(data)
                
        except Exception:
            # Silently ignore parse errors (very rare)
            pass
    
    def _process_event(self, event: dict):
        """
        Process a single event from WebSocket.
        SYNC function - optimized for minimal overhead.
        Only processes events for subscribed tokens.
        """
        # Fast check: only process last_trade_price events
        if event.get(EVENT_TYPE_KEY) != LAST_TRADE_PRICE:
            return
        
        asset_id = event.get(ASSET_ID_KEY)
        price_str = event.get(PRICE_KEY)
        
        if not asset_id or not price_str:
            return
        
        # IMPORTANT: Only process tokens we're subscribed to
        if asset_id not in self.subscribed_tokens:
            return
        
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return
        
        # Update price
        self.prices[asset_id] = price
        self.last_update = datetime.now()

        # Callback if registered
        if self.on_price_update:
            self.on_price_update(asset_id, price)
    
    async def _reconnect(self):
        """Attempt to reconnect with exponential backoff"""
        self.connected = False
        
        while self.running:
            logger.info(f"Reconnecting in {self.reconnect_delay}s...")
            await asyncio.sleep(self.reconnect_delay)
            
            if await self.connect():
                if self.subscribed_tokens:
                    await self.subscribe(self.subscribed_tokens)
                return
            
            self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
    
    async def close(self):
        """Close WebSocket connection"""
        self.running = False
        self.connected = False
        
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None
        
        # logger.info("WebSocket closed")
    
    def get_price(self, token_id: str) -> Optional[float]:
        """Get current price for a token (instant, no API call)"""
        return self.prices.get(token_id)
    
    def get_prices(self, token_ids: List[str]) -> Dict[str, float]:
        """Get prices for multiple tokens (instant, no API call)"""
        prices = self.prices
        return {tid: prices[tid] for tid in token_ids if tid in prices}


class HybridPriceMonitor:
    """
    Hybrid monitor that uses WebSocket for real-time updates
    with HTTP fallback for initial data and recovery.
    """
    
    STALENESS_THRESHOLD = 30.0  # seconds without price update → force reconnect

    __slots__ = (
        'http_monitor', 'ws_monitor', 'use_websocket',
        'current_up_token', 'current_down_token',
        '_last_price_ts', '_reconnecting'
    )

    def __init__(self, http_monitor):
        self.http_monitor = http_monitor
        self.ws_monitor = WebSocketPriceMonitor()
        self.use_websocket = True
        self.current_up_token: Optional[str] = None
        self.current_down_token: Optional[str] = None
        self._last_price_ts: float = 0.0  # monotonic clock of last price update
        self._reconnecting: bool = False
    
    async def start(self):
        """Start WebSocket connection"""
        if await self.ws_monitor.connect():
            # Hook callback to track last price receive time
            original_cb = self.ws_monitor.on_price_update
            def _track_ts(asset_id, price):
                self._last_price_ts = time.monotonic()
                if original_cb:
                    original_cb(asset_id, price)
            self.ws_monitor.on_price_update = _track_ts
            asyncio.create_task(self.ws_monitor.listen())
            return True
        return False

    async def check_staleness(self) -> bool:
        """
        Check if WS price data is stale (>30s since last update).
        Forces reconnection if stale. Returns True if reconnection was triggered.
        """
        if self._reconnecting or not self._last_price_ts:
            return False

        elapsed = time.monotonic() - self._last_price_ts
        if elapsed < self.STALENESS_THRESHOLD:
            return False

        logger.warning(
            f"[WS STALE] No price update for {elapsed:.0f}s — forcing reconnection"
        )
        self._reconnecting = True
        try:
            await self._reconnect_ws()
            if self.current_up_token and self.current_down_token:
                await self.ws_monitor.subscribe(
                    [self.current_up_token, self.current_down_token]
                )
            self._last_price_ts = time.monotonic()  # reset after reconnect
            logger.info("[WS STALE] Reconnected successfully")
        except Exception as e:
            logger.error(f"[WS STALE] Reconnection failed: {e}")
        finally:
            self._reconnecting = False
        return True
    
    async def _reconnect_ws(self):
        """Reconnect WebSocket with fresh state"""
        # Close existing connection
        await self.ws_monitor.close()
        
        # Clear all state
        self.ws_monitor.prices.clear()
        self.ws_monitor.subscribed_tokens = []
        
        # Reconnect
        if await self.ws_monitor.connect():
            asyncio.create_task(self.ws_monitor.listen())
            return True
        return False
    
    async def subscribe_to_market(self, up_token: str, down_token: str):
        """Subscribe to price updates for a market"""
        self.current_up_token = up_token
        self.current_down_token = down_token
        self._last_price_ts = time.monotonic()  # reset staleness timer

        # RECONNECT WebSocket for clean subscription state
        await self._reconnect_ws()

        # Subscribe to new tokens
        await self.ws_monitor.subscribe([up_token, down_token])
        
        # Quick wait for WebSocket prices (max 15 seconds)
        for _ in range(60):  # 30 x 0.5s = 15s max
            up_price = self.ws_monitor.get_price(up_token)
            down_price = self.ws_monitor.get_price(down_token)
            if up_price is not None and down_price is not None:
                # logger.info(f"WS Ready: UP=${up_price:.4f}, DOWN=${down_price:.4f}")
                # logger.info(f"Cache keys: {list(self.ws_monitor.prices.keys())}")
                return
            await asyncio.sleep(0.5)

        # No WS prices - seed from HTTP
        logger.info("No WS trade prices yet, fetching via HTTP...")
        http_prices = await self.http_monitor.get_prices_batch([up_token, down_token])
        
        if http_prices:
            up_price = http_prices.get(up_token)
            down_price = http_prices.get(down_token)
            
            if up_price is not None and down_price is not None:
                self.ws_monitor.prices[up_token] = up_price
                self.ws_monitor.prices[down_token] = down_price
                self.ws_monitor.last_update = datetime.now()
                # logger.info(f"HTTP Seed: UP=${up_price:.4f}, DOWN=${down_price:.4f}")
                # logger.info(f"Cache keys: {list(self.ws_monitor.prices.keys())}")
            else:
                logger.warning(f"HTTP failed to return prices for tokens")
        
        # logger.info("WebSocket subscribed to market tokens")
    
    def get_prices(self) -> Optional[Dict[str, float]]:
        """Get current prices (instant from memory)"""
        up_token = self.current_up_token
        down_token = self.current_down_token
        
        if not up_token or not down_token:
            return None

        prices = self.ws_monitor.prices
        up_price = prices.get(up_token)
        down_price = prices.get(down_token)

        if up_price is None or down_price is None:
            return None

        return {up_token: up_price, down_token: down_price}
    
    async def get_prices_with_fallback(self) -> Optional[Dict[str, float]]:
        """Get prices with HTTP fallback if WebSocket data unavailable"""
        prices = self.get_prices()
        if prices:
            return prices
        
        if self.current_up_token and self.current_down_token:
            return await self.http_monitor.get_prices_batch([
                self.current_up_token,
                self.current_down_token
            ])
        
        return None
    
    async def close(self):
        """Close connections"""
        await self.ws_monitor.close()


# WebSocket endpoint for user channel
WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class WebSocketUserFillsTracker:
    """
    Real-time tracker for user order fills using Polymarket WebSocket.
    
    This provides instant updates when orders are filled, much faster than
    polling the data API (which has ~30s delay).
    
    Usage:
        tracker = WebSocketUserFillsTracker(clob_client)
        await tracker.start()
        # After starting, positions are updated in real-time
        balance = tracker.get_balance(token_id)
    """
    
    __slots__ = (
        'client', 'ws', 'positions', 'connected', 'running',
        'reconnect_delay', 'max_reconnect_delay', 'condition_ids', 'on_fill'
    )
    
    def __init__(self, clob_client):
        """
        Initialize with a ClobClient that has API credentials set.
        
        Args:
            clob_client: Authenticated py_clob_client.ClobClient instance
        """
        self.client = clob_client
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.positions: Dict[str, float] = {}  # token_id -> shares
        self.connected = False
        self.running = False
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 30.0
        self.condition_ids: List[str] = []
        # callback(token_id, side, size) - size is TOTAL for the order (multi-level fills aggregated)
        self.on_fill: Optional[Callable[[str, str, float], None]] = None
    
    def _get_auth_message(self) -> Optional[Dict]:
        """Get authentication message using client's API credentials"""
        try:
            # Access the client's API credentials
            creds = self.client.creds
            if not creds:
                logger.error("No API credentials available on client")
                return None
            
            # Auth fields as per Polymarket docs (lowercase)
            return {
                "apiKey": creds.api_key,
                "secret": creds.api_secret,
                "passphrase": creds.api_passphrase
            }
        except Exception as e:
            logger.error(f"Failed to get auth credentials: {e}")
            return None
    
    async def connect(self) -> bool:
        """Establish WebSocket connection"""
        try:
            self.ws = await websockets.connect(
                WS_USER_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            )
            self.connected = True
            self.reconnect_delay = 1.0
            logger.info(f"User WebSocket connected")
            return True
        except Exception as e:
            logger.error(f"User WebSocket connection failed: {e}")
            self.connected = False
            return False
    
    async def subscribe(self, condition_ids: List[str]) -> bool:
        """
        Subscribe to user fills for specific markets.
        
        Args:
            condition_ids: List of market condition IDs to monitor
        """
        if not self.ws or not self.connected:
            logger.warning("Cannot subscribe: User WebSocket not connected")
            return False
        
        auth = self._get_auth_message()
        if not auth:
            logger.error("Cannot subscribe: No authentication")
            return False
        
        try:
            message = {
                "auth": auth,
                "type": "USER",  # Uppercase as per Polymarket docs
                "markets": condition_ids
            }
            await self.ws.send(json_dumps(message))
            self.condition_ids = condition_ids
            logger.info(f"Subscribed to user fills for {len(condition_ids)} markets")
            return True
        except Exception as e:
            logger.error(f"User subscribe failed: {e}")
            return False
    
    async def listen(self):
        """Listen for fill events and update positions"""
        self.running = True
        
        while self.running:
            try:
                if not self.ws or not self.connected:
                    await self._reconnect()
                    continue
                
                message = await self.ws.recv()
                self._process_message(message)
                
            except ConnectionClosed:
                logger.warning("User WebSocket connection closed")
                self.connected = False
                if self.running:
                    await self._reconnect()
            except ConnectionClosedError:
                logger.warning("User WebSocket connection closed with error")
                self.connected = False
                if self.running:
                    await self._reconnect()
            except Exception as e:
                logger.error(f"User WebSocket error: {e}")
                await asyncio.sleep(1)
    
    def _process_message(self, raw_message: str):
        """Process incoming fill/order messages"""
        # Ignore empty messages (can happen during reconnect)
        if not raw_message or raw_message.strip() == '':
            return
        
        try:
            data = json_loads(raw_message)
            
            # Handle array of events
            events = data if isinstance(data, list) else [data]
            
            for event in events:
                event_type = event.get('event_type')
                
                if event_type == 'trade':
                    self._handle_trade(event)
                elif event_type == 'order':
                    self._handle_order(event)
                    
        except Exception as e:
            # Only log if it's not an empty/ping message
            if len(raw_message) > 10:
                logger.warning(f"Failed to parse user message: {e}")
    
    def _handle_trade(self, trade: Dict):
        """
        Handle trade fill event - update our position tracking.
        
        Trade statuses:
        - MATCHED: Order matched, trade pending (FIRST - use this)
        - MINED: Transaction mined (duplicate)
        - CONFIRMED: Transaction confirmed (duplicate)
        - FAILED: Trade failed
        
        We only process MATCHED to avoid duplicates (same trade sends 3 messages).
        """
        status = trade.get('status', '')
        
        # Only process MATCHED (first message) to avoid duplicates
        if status != 'MATCHED':
            return
        
        asset_id = trade.get('asset_id')
        side = trade.get('side', '').upper()
        size = float(trade.get('size', 0))
        
        # Log all trade fields once to understand the data structure
        logger.debug(f"[WS TRADE RAW] {list(trade.keys())}")
        
        if not asset_id or not side or size <= 0:
            return
        
        # Update position based on trade side
        current = self.positions.get(asset_id, 0)
        
        # Log fee info if available (to check if WS includes net amounts)
        fee = trade.get('fee', trade.get('maker_fee', trade.get('taker_fee')))
        fee_info = f" | fee={fee}" if fee else ""
        
        if side == 'BUY':
            self.positions[asset_id] = current + size
            # logger.warning(f"[WS FILL] BUY {size:.4f} of {asset_id[:10]}...{fee_info} | total: {self.positions[asset_id]:.4f}")
        elif side == 'SELL':
            self.positions[asset_id] = max(0, current - size)
            # logger.warning(f"[WS FILL] SELL {size:.4f} of {asset_id[:10]}...{fee_info} | total: {self.positions[asset_id]:.4f}")
        
        # Callback for external handling
        if self.on_fill:
            try:
                self.on_fill(asset_id, side, size)
            except Exception as e:
                logger.debug(f"Fill callback error: {e}")
    
    def _handle_order(self, order: Dict):
        """Handle order placement/update/cancellation events"""
        order_type = order.get('type', '')
        
        # Log order events for debugging
        if order_type == 'PLACEMENT':
            logger.debug(f"[WS ORDER] Placed: {order.get('side')} {order.get('original_size')} @ {order.get('price')}")
        elif order_type == 'CANCELLATION':
            logger.debug(f"[WS ORDER] Cancelled: {order.get('id', '')[:20]}...")
    
    async def _reconnect(self):
        """Attempt to reconnect with exponential backoff"""
        self.connected = False
        
        while self.running:
            logger.info(f"User WebSocket reconnecting in {self.reconnect_delay}s...")
            await asyncio.sleep(self.reconnect_delay)
            
            if await self.connect():
                if self.condition_ids:
                    await self.subscribe(self.condition_ids)
                return
            
            self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
    
    async def start(self, condition_ids: List[str] = None) -> bool:
        """
        Start the WebSocket tracker.
        
        Args:
            condition_ids: Optional list of market condition IDs to subscribe to
        """
        if not await self.connect():
            return False
        
        if condition_ids:
            if not await self.subscribe(condition_ids):
                return False
        
        # Start listening in background
        asyncio.create_task(self.listen())
        return True
    
    async def close(self):
        """Close WebSocket connection"""
        self.running = False
        self.connected = False
        
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None
        
        logger.info("User WebSocket closed")
    
    def get_balance(self, token_id: str) -> float:
        """
        Get current balance for a token (instant, no API call).
        
        Returns tracked position size, or 0 if not tracked.
        """
        return self.positions.get(token_id, 0)
    
    def set_initial_balance(self, token_id: str, balance: float):
        """
        Set initial balance for a token (e.g., from API query at startup).
        
        This allows syncing with existing positions before WebSocket
        starts tracking new fills.
        """
        self.positions[token_id] = balance
    
    async def add_condition_id(self, condition_id: str):
        """Add a new condition ID and reconnect WebSocket for clean state"""
        if condition_id not in self.condition_ids:
            self.condition_ids.append(condition_id)
        
        # Force reconnect for clean subscription state
        logger.warning(f"[WS USER] Adding market {condition_id[:16]}... - reconnecting")
        self.connected = False
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None
        
        # Reconnect and subscribe to all markets
        if await self.connect():
            await self.subscribe(self.condition_ids)
            logger.warning(f"[WS USER] Reconnected, tracking {len(self.condition_ids)} markets")
