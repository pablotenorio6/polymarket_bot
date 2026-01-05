"""
WebSocket-based real-time price monitor for Polymarket

Benefits over HTTP polling:
- Latency: ~5-10ms vs ~100-150ms
- No polling overhead
- Prices update instantly when they change
"""

import asyncio
import json
import logging
from typing import Dict, Optional, Callable, List
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

logger = logging.getLogger(__name__)

# Polymarket WebSocket endpoints
# Try the CLOB WebSocket again with different subscription format
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class WebSocketPriceMonitor:
    """
    Real-time price monitor using Polymarket WebSocket.
    
    Maintains current prices in memory, updated via WebSocket push.
    No HTTP calls needed for price checks after initial connection.
    """
    
    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.prices: Dict[str, float] = {}  # token_id -> price
        self.subscribed_tokens: List[str] = []
        self.connected = False
        self.running = False
        
        # Callbacks
        self.on_price_update: Optional[Callable[[str, float], None]] = None
        
        # Stats
        self.message_count = 0
        self.last_update: Optional[datetime] = None
        
        # Reconnection settings
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
            self.reconnect_delay = 1.0  # Reset on successful connect
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
            # Build subscription message - CLOB format
            # According to docs, it uses auth + type + assets_ids
            message = {
                "auth": {},  # Empty auth for now (no authentication needed for market data)
                "type": "market",
                "assets_ids": token_ids
            }

            # logger.info(f"Sending RTDS subscription: {message}")
            await self.ws.send(json.dumps(message))
            self.subscribed_tokens = token_ids
            logger.info(f"Subscribed to {len(token_ids)} tokens: {[tid[:10] for tid in token_ids]}")

            # Wait a bit and check if we got initial prices
            await asyncio.sleep(2)
            # logger.info(f"After subscription: {len(self.prices)} prices in cache")
            # for tid, price in self.prices.items():
            #     logger.info(f"  {tid[:10]}... = ${price:.4f}")

            return True

        except Exception as e:
            logger.error(f"Subscription failed: {e}")
            return False
    
    async def unsubscribe(self, token_ids: List[str]):
        """Unsubscribe from tokens"""
        if not self.ws or not self.connected:
            return
        
        try:
            message = {
                "assets_ids": token_ids,
                "operation": "unsubscribe"
            }
            await self.ws.send(json.dumps(message))
            
            # Remove from subscribed list
            for tid in token_ids:
                if tid in self.subscribed_tokens:
                    self.subscribed_tokens.remove(tid)
                if tid in self.prices:
                    del self.prices[tid]
                    
        except Exception as e:
            logger.debug(f"Unsubscribe error: {e}")
    
    async def listen(self):
        """Listen for incoming messages"""
        if not self.ws:
            return
        
        self.running = True
        
        while self.running and self.connected:
            try:
                message = await asyncio.wait_for(
                    self.ws.recv(),
                    timeout=30.0
                )
                await self._handle_message(message)
                
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
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
    
    async def _handle_message(self, raw_message: str):
        """Process incoming WebSocket message"""
        try:
            data = json.loads(raw_message)
            self.message_count += 1

            # Only log errors, keep trade events minimal
            if "error" in raw_message.lower():
                logger.warning(f"WS Error: {raw_message[:200]}...")

            # Handle different message types
            # Price change event
            if isinstance(data, list):
                for item in data:
                    await self._process_event(item)
            elif isinstance(data, dict):
                await self._process_event(data)

        except json.JSONDecodeError:
            logger.debug(f"Invalid JSON: {raw_message[:100]}")
        except Exception as e:
            logger.debug(f"Message handling error: {e}")
    
    async def _process_event(self, event: dict):
        """Process a single event from WebSocket"""
        # Handle Polymarket CLOB market channel format
        # Format: {"market":"...", "price_changes":[{"asset_id":"...", "price":"...", ...}]}

        # Check for last_trade_price (ACTUAL TRADE EXECUTION)
        if event.get('event_type') == 'last_trade_price':
            asset_id = event.get('asset_id')
            price_str = event.get('price')

            if asset_id and price_str:
                price = float(price_str)
                old_price = self.prices.get(asset_id)

                # Only log if price actually changed
                if old_price != price:
                    self.prices[asset_id] = price
                    self.last_update = datetime.now()

                    # Callback if price changed (only for actual trades)
                    if self.on_price_update:
                        self.on_price_update(asset_id, price)

                    # Log price changes (reduced verbosity)
                    # logger.info(f"TRADE: {asset_id[:8]}... = ${price:.4f}" + (f" ({price - old_price:+.4f})" if old_price else ""))
                else:
                    # Just update timestamp for unchanged prices
                    self.last_update = datetime.now()

    
    async def _reconnect(self):
        """Attempt to reconnect with exponential backoff"""
        self.connected = False
        
        while self.running:
            logger.info(f"Reconnecting in {self.reconnect_delay}s...")
            await asyncio.sleep(self.reconnect_delay)
            
            if await self.connect():
                # Resubscribe to tokens
                if self.subscribed_tokens:
                    await self.subscribe(self.subscribed_tokens)
                return
            
            # Exponential backoff
            self.reconnect_delay = min(
                self.reconnect_delay * 2,
                self.max_reconnect_delay
            )
    
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
        return {tid: self.prices[tid] for tid in token_ids if tid in self.prices}


class HybridPriceMonitor:
    """
    Hybrid monitor that uses WebSocket for real-time updates
    with HTTP fallback for initial data and recovery.
    """
    
    def __init__(self, http_monitor):
        """
        Args:
            http_monitor: FastMarketMonitor instance for HTTP fallback
        """
        self.http_monitor = http_monitor
        self.ws_monitor = WebSocketPriceMonitor()
        self.use_websocket = True
        
        # Current market state
        self.current_up_token: Optional[str] = None
        self.current_down_token: Optional[str] = None
    
    async def start(self):
        """Start WebSocket connection"""
        if await self.ws_monitor.connect():
            # Start listening in background
            asyncio.create_task(self.ws_monitor.listen())
            return True
        return False
    
    async def subscribe_to_market(self, up_token: str, down_token: str):
        """Subscribe to price updates for a market"""
        self.current_up_token = up_token
        self.current_down_token = down_token

        # Unsubscribe from old tokens if any
        if self.ws_monitor.subscribed_tokens:
            await self.ws_monitor.unsubscribe(self.ws_monitor.subscribed_tokens)

        # Subscribe to new tokens
        await self.ws_monitor.subscribe([up_token, down_token])
        logger.debug(f"Subscribed to tokens: UP={up_token[:8]}..., DOWN={down_token[:8]}...")

        # Quick wait for WebSocket prices (max 3 seconds)
        max_wait = 3.0
        waited = 0
        while waited < max_wait:
            if self.ws_monitor.get_price(up_token) is not None and self.ws_monitor.get_price(down_token) is not None:
                up_price = self.ws_monitor.get_price(up_token)
                down_price = self.ws_monitor.get_price(down_token)
                logger.info(f"WS Ready: UP=${up_price:.4f}, DOWN=${down_price:.4f}")
                return
            await asyncio.sleep(0.5)
            waited += 0.5

        # No WS prices yet - fetch initial prices via HTTP and seed the cache
        logger.info("No WS trade prices yet, fetching via HTTP...")
        http_prices = await self.http_monitor.get_prices_batch([up_token, down_token])
        if http_prices:
            up_price = http_prices.get(up_token)
            down_price = http_prices.get(down_token)
            if up_price is not None and down_price is not None:
                # Seed the WS cache with HTTP prices so fast loop can run
                self.ws_monitor.prices[up_token] = up_price
                self.ws_monitor.prices[down_token] = down_price
                self.ws_monitor.last_update = datetime.now()
                logger.info(f"HTTP Seed: UP=${up_price:.4f}, DOWN=${down_price:.4f}")
            else:
                logger.warning("HTTP fallback returned incomplete prices")
    
    def get_prices(self) -> Optional[Dict[str, float]]:
        """
        Get current prices (instant from memory).
        Returns None if prices not available from WebSocket.
        """
        if not self.current_up_token or not self.current_down_token:
            return None

        up_price = self.ws_monitor.get_price(self.current_up_token)
        down_price = self.ws_monitor.get_price(self.current_down_token)

        # Only return prices if BOTH are available from WebSocket
        if up_price is None or down_price is None:
            return None

        return {
            self.current_up_token: up_price,
            self.current_down_token: down_price
        }
    
    async def get_prices_with_fallback(self) -> Optional[Dict[str, float]]:
        """
        Get prices with HTTP fallback if WebSocket data unavailable.
        """
        # Try WebSocket first (instant)
        prices = self.get_prices()
        if prices:
            return prices
        
        # Fallback to HTTP
        if self.current_up_token and self.current_down_token:
            logger.debug("WebSocket prices unavailable, using HTTP fallback")
            return await self.http_monitor.get_prices_batch([
                self.current_up_token,
                self.current_down_token
            ])
        
        return None
    
    async def close(self):
        """Close connections"""
        await self.ws_monitor.close()

