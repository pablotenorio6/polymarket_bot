"""
Polymarket Real-Time Data Stream (RTDS) for Chainlink Crypto Prices.

Connects to wss://ws-live-data.polymarket.com for real-time BTC/ETH/SOL prices
from the Chainlink oracle (the actual resolution source for Polymarket markets).
See: https://docs.polymarket.com/developers/RTDS/RTDS-crypto-prices#chainlink-source-crypto_prices_chainlink
"""

import asyncio
import json
import logging
import time
from typing import Dict, Optional, Callable
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# Polymarket RTDS WebSocket
RTDS_URL = "wss://ws-live-data.polymarket.com"


class RTDSCryptoPrices:
    """
    Real-time Chainlink crypto prices from Polymarket RTDS.

    Provides BTC, ETH, SOL prices from the Chainlink oracle (~1s updates).
    Chainlink is the actual resolution source for Polymarket crypto markets.
    Symbol format: btc/usd, eth/usd, sol/usd
    """
    
    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self.running = False
        
        # Price cache
        self.prices: Dict[str, float] = {}
        self.last_update: Dict[str, datetime] = {}
        
        # Callback for price updates
        self.on_price_update: Optional[Callable[[str, float], None]] = None
        
        # Reconnection settings
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 30.0
        self.ping_interval = 5.0  # Send ping every 5 seconds as per docs
    
    async def connect(self) -> bool:
        """Connect to RTDS WebSocket"""
        try:
            self.ws = await websockets.connect(
                RTDS_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            )
            self.connected = True
            self.reconnect_delay = 1.0
            logger.info(f"[RTDS] Connected to {RTDS_URL}")
            return True
        except Exception as e:
            logger.error(f"[RTDS] Connection failed: {e}")
            self.connected = False
            return False
    
    async def subscribe_crypto_prices(self):
        """Subscribe to crypto price updates"""
        if not self.ws or not self.connected:
            logger.warning("[RTDS] Cannot subscribe: not connected")
            return False
        
        try:
            # Subscribe to Chainlink crypto prices topic
            subscribe_msg = {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": "crypto_prices_chainlink",
                        "type": "*"
                    }
                ]
            }

            await self.ws.send(json.dumps(subscribe_msg))
            logger.info("[RTDS] Subscribed to crypto_prices_chainlink")
            return True
            
        except Exception as e:
            logger.error(f"[RTDS] Subscription failed: {e}")
            return False
    
    async def start(self) -> bool:
        """Start the RTDS connection and subscription"""
        if await self.connect():
            await self.subscribe_crypto_prices()
            # Start listener in background
            asyncio.create_task(self._listen())
            # Start ping task
            asyncio.create_task(self._ping_loop())
            return True
        return False
    
    async def _ping_loop(self):
        """Send periodic pings to maintain connection"""
        while self.running and self.connected:
            try:
                if self.ws:
                    await self.ws.ping()
                await asyncio.sleep(self.ping_interval)
            except Exception:
                break
    
    async def _listen(self):
        """Listen for incoming messages"""
        if not self.ws:
            return
        
        self.running = True
        
        while self.running and self.connected:
            try:
                message = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                self._handle_message(message)
                
            except asyncio.TimeoutError:
                # Send ping to check connection
                try:
                    pong = await self.ws.ping()
                    await asyncio.wait_for(pong, timeout=10)
                except:
                    logger.warning("[RTDS] Ping timeout, reconnecting...")
                    await self._reconnect()
                    
            except ConnectionClosed:
                logger.warning("[RTDS] Connection closed")
                if self.running:
                    await self._reconnect()
                    
            except Exception as e:
                logger.error(f"[RTDS] Error: {e}")
                if self.running:
                    await self._reconnect()
    
    def _handle_message(self, raw_message: str):
        """
        Process incoming RTDS Chainlink message.

        Message format:
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "timestamp": 1753314064237,
            "payload": {
                "symbol": "btc/usd",
                "timestamp": 1753314064213,
                "value": 95077.56
            }
        }
        """
        try:
            if not raw_message:
                return

            data = json.loads(raw_message)

            topic = data.get('topic')
            payload = data.get('payload', {})

            if topic == 'crypto_prices_chainlink':
                self._process_price_update(payload)

        except json.JSONDecodeError:
            logger.debug(f"[RTDS] Invalid JSON: {raw_message[:100]}")
        except Exception as e:
            logger.debug(f"[RTDS] Error processing message: {e}")
    
    def _process_price_update(self, payload: Dict):
        """
        Process Chainlink crypto price update from RTDS.

        Expected format:
        {
            "symbol": "btc/usd",
            "timestamp": 1753314064213,
            "value": 95077.56
        }
        """
        try:
            if not isinstance(payload, dict):
                return

            # Get symbol and normalize (btc/usd -> BTC)
            symbol = payload.get('symbol', '')
            if '/' in symbol:
                symbol = symbol.split('/')[0].upper()  # btc/usd -> BTC
            else:
                symbol = symbol.upper()

            # Get price from 'value' field
            price = payload.get('value')
            if price is not None and symbol:
                self._update_price(symbol, float(price))

        except Exception as e:
            logger.debug(f"[RTDS] Error processing price: {e}")
    
    def _update_price(self, symbol: str, price: float):
        """Update price cache and trigger callback"""
        self.prices[symbol] = price
        self.last_update[symbol] = datetime.now()
        
        if self.on_price_update:
            self.on_price_update(symbol, price)
        
        logger.debug(f"[RTDS] {symbol}: ${price:,.2f}")
    
    async def _reconnect(self):
        """Reconnect with exponential backoff"""
        self.connected = False
        
        while self.running:
            logger.info(f"[RTDS] Reconnecting in {self.reconnect_delay}s...")
            await asyncio.sleep(self.reconnect_delay)
            
            if await self.connect():
                await self.subscribe_crypto_prices()
                return
            
            self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
    
    async def close(self):
        """Close the WebSocket connection"""
        self.running = False
        self.connected = False
        
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None
        
        logger.info("[RTDS] Closed")
    
    def get_price(self, symbol: str = "BTC") -> Optional[float]:
        """
        Get current price for a symbol.
        
        Args:
            symbol: Crypto symbol (BTC, ETH, SOL)
            
        Returns:
            Price or None if not available
        """
        return self.prices.get(symbol.upper())
    
    def get_btc_price(self) -> Optional[float]:
        """Get current BTC price"""
        return self.get_price("BTC")
    
    def get_all_prices(self) -> Dict[str, float]:
        """Get all cached prices"""
        return dict(self.prices)


# Global singleton
_rtds_client: Optional[RTDSCryptoPrices] = None


async def get_rtds_client() -> RTDSCryptoPrices:
    """Get or create global RTDS client"""
    global _rtds_client
    
    if _rtds_client is None:
        _rtds_client = RTDSCryptoPrices()
        await _rtds_client.start()
    
    return _rtds_client


async def close_rtds_client():
    """Close global RTDS client"""
    global _rtds_client
    
    if _rtds_client:
        await _rtds_client.close()
        _rtds_client = None
