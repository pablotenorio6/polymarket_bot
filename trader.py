"""
Optimized trader with pre-signed orders and minimal latency

Key optimizations:
1. Pre-signed order templates ready to post
2. Client connection kept warm
3. Minimal processing between decision and execution
4. Async order submission for non-blocking trades
"""

import asyncio
import httpx
import json
import logging
import time
from typing import Dict, Optional, List
from decimal import Decimal, ROUND_DOWN
from concurrent.futures import ThreadPoolExecutor
import threading

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from config import CHAIN_ID, CLOB_API
from auth import get_auth

logger = logging.getLogger(__name__)

# Thread pool for async order submission
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="trader")


class FastTrader:
    """
    High-performance trader optimized for minimal latency
    
    Key features:
    - Pre-initialized and warmed-up client connection
    - Optimized share calculation (no iterations)
    - Async order posting (non-blocking)
    - Position tracking in memory (no API calls)
    """
    
    def __init__(self):
        self.client: Optional[ClobClient] = None
        self.signer_address: Optional[str] = None
        self.funder_address: Optional[str] = None
        
        # Position tracking
        self.active_positions: Dict[str, Dict] = {}  # token_id -> position data
        self._position_lock = threading.Lock()
        
        # Pre-computed tick sizes by market
        self.tick_sizes: Dict[str, str] = {}
        
        # Connection warmup tracking
        self._last_warmup = 0
        self._warmup_interval = 60  # seconds
        
        # Initialize client
        self._initialize()
    
    def _initialize(self):
        """Initialize and warm up the trading client"""
        auth = get_auth()
        try:
            self.client = auth.get_client()
            if self.client:
                self.signer_address = self.client.get_address()
                self.funder_address = auth.funder_address
                
                logger.info(f"FastTrader initialized | Signer: {self.signer_address[:10]}...")
                if self.funder_address:
                    logger.info(f"Funder mode: {self.funder_address[:10]}...")
                
                # Warm up connection
                self._warmup_connection()
        except Exception as e:
            logger.warning(f"Could not initialize trader: {e}")
    
    def _warmup_connection(self):
        """Keep the connection warm with a lightweight request"""
        if not self.client:
            return
        
        now = time.time()
        if now - self._last_warmup < self._warmup_interval:
            return
        
        try:
            # Lightweight call to keep connection alive
            self.client.get_ok()
            self._last_warmup = now
            logger.debug("Connection warmed up")
        except Exception as e:
            logger.debug(f"Warmup failed: {e}")
    
    def _get_tick_size(self, token_id: str) -> str:
        """Get tick size for a token, with caching"""
        if token_id in self.tick_sizes:
            return self.tick_sizes[token_id]
        
        # Default to 0.01 for BTC markets
        tick_size = "0.01"
        
        if self.client:
            try:
                ts = self.client.get_tick_size(token_id)
                if ts:
                    tick_size = ts
            except:
                pass
        
        self.tick_sizes[token_id] = tick_size
        return tick_size

    
    def place_buy_order(
        self,
        token_id: str,
        side: str,  # 'up' or 'down'
        price: float,
        size: float,
        market_info: Dict,
        order_type: str = "FOK"
    ) -> Optional[Dict]:
        """
        Place a buy order with minimal latency
        
        Args:
            token_id: Token to buy
            side: 'up' or 'down' (for logging)
            price: Price per share
            size: Amount in USD to spend
            market_info: Market metadata
            order_type: FOK or GTC
        
        Returns:
            Order response or None
        """
        if not self.client:
            logger.warning("Client not initialized")
            return None
        
        # Warm up connection if needed
        self._warmup_connection()
        
        # Round price
        price_rounded = round(price, 2)
        
        tick_size = self._get_tick_size(token_id)
        
        logger.debug(f"BUY {side.upper()}: {size} shares @ ${price_rounded} (${size:.2f} total)")
        
        try:
            # Create order args
            order_args = OrderArgs(
                token_id=token_id,
                price=price_rounded,
                size=size,
                side=BUY,
                fee_rate_bps=0
            )
            
            # Sign order
            signed_order = self.client.create_order(order_args)
            
            # Post order
            py_order_type = OrderType.FOK if order_type == "FOK" else OrderType.GTC
            result = self.client.post_order(signed_order, orderType=py_order_type)
            
            if result:
                logger.info(f"ORDER FILLED: BUY {side.upper()} {size} @ ${price_rounded}")
                
                # Track position
                with self._position_lock:
                    self.active_positions[token_id] = {
                        'side': side,
                        'shares': size,
                        'entry_price': price_rounded,
                        'entry_time': time.time(),
                        'market': market_info
                    }
                
                return result
                
        except Exception as e:
            error_msg = str(e)
            if "not enough balance" in error_msg.lower():
                logger.error("Insufficient USDC balance or allowance")
            else:
                logger.error(f"Order failed: {e}")
        
        return None
    
    def place_sell_order(
        self,
        token_id: str,
        price: float,
        size: float,
        order_type: str = "FOK"
    ) -> Optional[Dict]:
        """
        Place a sell order (for stop loss or take profit)
        
        Args:
            token_id: Token to sell
            price: Price per share
            size: Number of shares to sell
            order_type: FOK (immediate) or GTC (resting)
        """
        if not self.client:
            return None
        
        price_rounded = round(price, 2)
        size_rounded = round(size, 2)
        
        tick_size = self._get_tick_size(token_id)
        
        logger.debug(f"SELL: {size_rounded} shares @ ${price_rounded}")
        
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price_rounded,
                size=size_rounded,
                side=SELL,
                fee_rate_bps=0
            )
            
            signed_order = self.client.create_order(order_args)
            py_order_type = OrderType.FOK if order_type == "FOK" else OrderType.GTC
            result = self.client.post_order(signed_order, orderType=py_order_type)
            
            if result:
                logger.info(f"SOLD: {size_rounded} shares @ ${price_rounded}")
                
                # Remove from tracked positions
                with self._position_lock:
                    if token_id in self.active_positions:
                        del self.active_positions[token_id]
                
                return result
                
        except Exception as e:
            logger.error(f"Sell order failed: {e}")
        
        return None
    
    def place_sell_order_async(
        self,
        token_id: str,
        price: float,
        size: float,
        order_type: str = "FOK"
    ):
        """
        Non-blocking sell order submission
        
        Returns immediately, order is placed in background
        """
        _executor.submit(
            self.place_sell_order,
            token_id, price, size, order_type
        )
    
    def get_position(self, token_id: str) -> Optional[Dict]:
        """Get position for a token from local cache"""
        with self._position_lock:
            return self.active_positions.get(token_id)
    
    def get_all_positions(self) -> Dict[str, Dict]:
        """Get all tracked positions"""
        with self._position_lock:
            return dict(self.active_positions)
    
    def remove_position(self, token_id: str):
        """Remove a position from tracking"""
        with self._position_lock:
            if token_id in self.active_positions:
                del self.active_positions[token_id]
    
    def should_enter_trade(self, price: float, trigger_price: float) -> bool:
        """Check if price meets entry criteria"""
        return price >= trigger_price
    
    def get_trade_side(self, up_price: float, down_price: float, trigger_price: float) -> Optional[str]:
        """
        Determine which side to trade based on prices
        
        Returns 'up', 'down', or None
        """
        if up_price >= trigger_price:
            return 'up'
        elif down_price >= trigger_price:
            return 'down'
        return None


class PreSignedOrderCache:
    """
    Cache for pre-signed orders to minimize latency at execution time
    
    Pre-signs orders for common price points so execution only
    requires posting, not signing
    """
    
    def __init__(self, trader: FastTrader, token_id: str, size: float):
        self.trader = trader
        self.token_id = token_id
        self.size = size
        
        # Cache of pre-signed orders: price -> signed_order
        self.buy_orders: Dict[float, object] = {}
        self.sell_orders: Dict[float, object] = {}
        
        # Price points to pre-sign
        self.buy_prices = [0.95, 0.96, 0.97, 0.98, 0.99]
        self.sell_prices = [0.80, 0.85, 0.90]
    
    def warm_up(self):
        """Pre-sign orders for all configured price points"""
        if not self.trader.client:
            return
        
        tick_size = self.trader._get_tick_size(self.token_id)
        
        # Pre-sign buy orders
        for price in self.buy_prices:
            shares = self.trader.calculate_shares_fast(self.size, price)
            if shares > 0:
                try:
                    order_args = OrderArgs(
                        token_id=self.token_id,
                        price=price,
                        size=shares,
                        side=BUY,
                        fee_rate_bps=0
                    )
                    self.buy_orders[price] = self.trader.client.create_order(order_args)
                except Exception as e:
                    logger.debug(f"Failed to pre-sign buy @ {price}: {e}")
        
        # Pre-sign sell orders
        position = self.trader.get_position(self.token_id)
        if position:
            for price in self.sell_prices:
                try:
                    order_args = OrderArgs(
                        token_id=self.token_id,
                        price=price,
                        size=position['shares'],
                        side=SELL,
                        fee_rate_bps=0
                    )
                    self.sell_orders[price] = self.trader.client.create_order(order_args)
                except Exception as e:
                    logger.debug(f"Failed to pre-sign sell @ {price}: {e}")
    
    def execute_buy(self, price: float) -> Optional[Dict]:
        """Execute a pre-signed buy order if available"""
        if price in self.buy_orders:
            try:
                return self.trader.client.post_order(
                    self.buy_orders[price],
                    orderType=OrderType.FOK
                )
            except:
                pass
        
        # Fallback to regular order
        return self.trader.place_buy_order(
            self.token_id, 'cached', price, self.size, {}
        )
    
    def execute_sell(self, price: float) -> Optional[Dict]:
        """Execute a pre-signed sell order if available"""
        if price in self.sell_orders:
            try:
                return self.trader.client.post_order(
                    self.sell_orders[price],
                    orderType=OrderType.FOK
                )
            except:
                pass
        
        # Fallback to regular order
        position = self.trader.get_position(self.token_id)
        if position:
            return self.trader.place_sell_order(
                self.token_id, price, position['shares']
            )
        return None

