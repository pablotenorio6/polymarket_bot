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
            logger.info(f"WebSocket connected to {WS_MARKET_URL}")
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
            logger.info(f"Subscribed to {len(token_ids)} tokens: {[tid[:10] for tid in token_ids]}")
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
        
        logger.info("WebSocket closed")
    
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
    
    __slots__ = (
        'http_monitor', 'ws_monitor', 'use_websocket',
        'current_up_token', 'current_down_token'
    )
    
    def __init__(self, http_monitor):
        self.http_monitor = http_monitor
        self.ws_monitor = WebSocketPriceMonitor()
        self.use_websocket = True
        self.current_up_token: Optional[str] = None
        self.current_down_token: Optional[str] = None
    
    async def start(self):
        """Start WebSocket connection"""
        if await self.ws_monitor.connect():
            asyncio.create_task(self.ws_monitor.listen())
            return True
        return False
    
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

        # RECONNECT WebSocket for clean subscription state
        await self._reconnect_ws()

        # Subscribe to new tokens
        await self.ws_monitor.subscribe([up_token, down_token])
        
        # Quick wait for WebSocket prices (max 15 seconds)
        for _ in range(60):  # 30 x 0.5s = 15s max
            up_price = self.ws_monitor.get_price(up_token)
            down_price = self.ws_monitor.get_price(down_token)
            if up_price is not None and down_price is not None:
                logger.info(f"WS Ready: UP=${up_price:.4f}, DOWN=${down_price:.4f}")
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
                logger.info(f"HTTP Seed: UP=${up_price:.4f}, DOWN=${down_price:.4f}")
                logger.info(f"Cache keys: {list(self.ws_monitor.prices.keys())}")
            else:
                logger.warning(f"HTTP failed to return prices for tokens")
        
        logger.info("WebSocket subscribed to market tokens")
    
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
