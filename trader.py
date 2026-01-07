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
from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs
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
    - PRE-SIGNED ORDERS for instant execution
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
        
        # === PRE-SIGNED ORDERS CACHE ===
        # Buy orders: (token_id, price) -> signed_order
        self.presigned_buys: Dict[tuple, object] = {}
        # Sell orders: token_id -> signed_order (for stop loss)
        self.presigned_sells: Dict[str, object] = {}
        # Track which market we have pre-signed orders for
        self.presigned_market_id: Optional[str] = None
        
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

    # =========================================
    # LIVE ORDER MONITORING
    # =========================================
    
    def _monitor_live_order(self, order_id: str, order_type: str, start_time: float):
        """
        Monitor a LIVE order until it's resolved (MATCHED/CANCELED).
        Runs in background thread to track orderbook time.
        """
        check_interval = 0.2  # Check every 200ms
        max_wait = 100  # Max 60 seconds monitoring
        
        while True:
            elapsed = time.time() - start_time
            
            if elapsed > max_wait:
                logger.warning(f"ORDER MONITOR TIMEOUT: {order_id[:16]}... still LIVE after {elapsed:.1f}s")
                break
            
            try:
                # Query order status
                url = f"{CLOB_API}/data/order/{order_id}"
                response = httpx.get(url, timeout=5.0)
                
                if response.status_code == 200:
                    data = response.json()
                    status = data.get('status', 'UNKNOWN')
                    size_matched = data.get('size_matched', '0')
                    
                    if status == 'MATCHED':
                        logger.info(f"ORDER FILLED: {order_type} {order_id[:16]}... - TIME IN ORDERBOOK: {elapsed:.2f}s")
                        break
                    elif status == 'CANCELED':
                        logger.warning(f"ORDER CANCELED: {order_type} {order_id[:16]}... after {elapsed:.2f}s")
                        break
                    elif status != 'LIVE':
                        logger.info(f"ORDER STATUS CHANGED: {order_type} {order_id[:16]}... -> {status} after {elapsed:.2f}s")
                        break
                    # Still LIVE, continue monitoring
                    
            except Exception as e:
                logger.debug(f"Error checking order status: {e}")
            
            time.sleep(check_interval)
    
    # =========================================
    # PRE-SIGNED ORDERS (Low Latency Trading)
    # =========================================
    
    def presign_buy_orders(
        self,
        up_token_id: str,
        down_token_id: str,
        price: float,
        size: float,
        market_id: str
    ):
        """
        Pre-sign buy orders for both UP and DOWN outcomes.
        Call this when a new market is detected to have orders ready.
        
        Args:
            up_token_id: Token ID for UP outcome
            down_token_id: Token ID for DOWN outcome  
            price: Entry price (e.g. 0.97)
            size: Position size in shares
            market_id: Market identifier to track freshness
        """
        if not self.client:
            return
        
        # Clear old pre-signed orders if market changed
        if market_id != self.presigned_market_id:
            self.presigned_buys.clear()
            self.presigned_sells.clear()
            self.presigned_market_id = market_id
        
        price_rounded = round(price, 2)
        
        # Pre-sign UP buy order
        try:
            up_args = OrderArgs(
                token_id=up_token_id,
                price=price_rounded,
                size=size,
                side=BUY,
                fee_rate_bps=0
            )
            self.presigned_buys[(up_token_id, price_rounded)] = self.client.create_order(up_args)
            logger.debug(f"Pre-signed BUY UP @ ${price_rounded}")
        except Exception as e:
            logger.debug(f"Failed to pre-sign UP buy: {e}")
        
        # Pre-sign DOWN buy order
        try:
            down_args = OrderArgs(
                token_id=down_token_id,
                price=price_rounded,
                size=size,
                side=BUY,
                fee_rate_bps=0
            )
            self.presigned_buys[(down_token_id, price_rounded)] = self.client.create_order(down_args)
            logger.debug(f"Pre-signed BUY DOWN @ ${price_rounded}")
        except Exception as e:
            logger.debug(f"Failed to pre-sign DOWN buy: {e}")
    
    def presign_stop_loss(self, token_id: str, shares: float):
        """
        Pre-sign a sell order for stop loss at minimum price.
        Uses price=0.01 to ensure immediate execution against any bid.
        Call this immediately after a buy order fills.
        
        Args:
            token_id: Token to sell
            shares: Number of shares to sell
        """
        if not self.client:
            return
        
        try:
            # Pre-sign sell order at MINIMUM PRICE (0.01)
            # This ensures immediate execution - will fill at best available bid
            order_args = OrderArgs(
                token_id=token_id,
                price=0.01,  # Minimum price = instant fill at best bid
                size=round(shares, 2),
                side=SELL,
                fee_rate_bps=0
            )
            self.presigned_sells[token_id] = self.client.create_market_order(order_args)
            # logger.debug(f"Pre-signed STOP LOSS @ $0.01 (instant fill) for {token_id[:10]}...")
        except Exception as e:
            logger.debug(f"Failed to pre-sign stop loss: {e}")
    
    def execute_presigned_buy(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        market_info: Dict,
        order_type: str = "FOK"
    ) -> Optional[Dict]:
        """
        Execute a pre-signed buy order if available, otherwise create new.
        
        Returns:
            Order response or None
        """
        price_rounded = round(price, 2)
        key = (token_id, price_rounded)
        
        # Try pre-signed order first (FAST PATH)
        if key in self.presigned_buys:
            try:
                # START PROFILING: Measure execution latency from trigger detection
                trigger_start_time = time.perf_counter()
                
                py_order_type = OrderType.FOK if order_type == "FOK" else OrderType.GTC
                result = self.client.post_order(self.presigned_buys[key], orderType=py_order_type)
                
                # Log API response fields
                if result:
                    success = result.get('success', 'N/A')
                    status = result.get('status', 'N/A')
                    error_msg = result.get('errorMsg', '')
                    order_id = result.get('orderID', 'N/A')
                    logger.info(f"PRE-SIGNED BUY RESPONSE: success={success}, status={status}, orderID={order_id[:16] if order_id != 'N/A' else 'N/A'}...")
                    if error_msg:
                        logger.warning(f"Order errorMsg: {error_msg}")
                    
                    logger.info(f"ORDER FILLED (pre-signed): BUY {side.upper()} {size} @ ${price_rounded}")

                    # END PROFILING: Log execution latency
                    trigger_end_time = time.perf_counter()
                    execution_latency = (trigger_end_time - trigger_start_time) * 1000
                    logger.info(f"ORDER FILLED EXECUTION LATENCY: {execution_latency:.2f}ms")

                    # Track position
                    with self._position_lock:
                        self.active_positions[token_id] = {
                            'side': side,
                            'shares': size,
                            'entry_price': price_rounded,
                            'entry_time': time.time(),
                            'market': market_info
                        }
                    
                    # Remove used pre-signed order
                    del self.presigned_buys[key]
                    
                    # IMMEDIATELY pre-sign stop loss for this position
                    self.presign_stop_loss(token_id, size)
                    
                    return result
                    
            except Exception as e:
                logger.debug(f"Pre-signed buy failed, falling back: {e}")
                # Remove failed pre-signed order
                del self.presigned_buys[key]
        
        # SLOW PATH: Create and post new order
        return self.place_buy_order(token_id, side, price, size, market_info, order_type)
    
    def execute_presigned_stop_loss(self, token_id: str) -> Optional[Dict]:
        """
        Execute a pre-signed stop loss if available.
        
        Returns:
            Order response or None
        """
        logging.info(token_id)
        logging.info(self.presigned_sells)
        
        # Try pre-signed order first (FAST PATH)
        if token_id in self.presigned_sells:
            try:
                # FOK = Fill or Kill - execute immediately at best available price or fail
                result = self.client.post_order(self.presigned_sells[token_id], OrderType.FOK)
                
                # Log API response fields
                if result:
                    success = result.get('success', 'N/A')
                    status = result.get('status', 'N/A')
                    error_msg = result.get('errorMsg', '')
                    order_id = result.get('orderID', 'N/A')
                    logger.info(f"PRE-SIGNED STOP LOSS RESPONSE: success={success}, status={status}, orderID={order_id[:16] if order_id != 'N/A' else 'N/A'}...")
                    if error_msg:
                        logger.warning(f"Order errorMsg: {error_msg}")
                    
                    position = self.active_positions.get(token_id, {})
                    shares = position.get('shares', 0)
                    
                    # If order is LIVE, monitor until resolved
                    if status == 'LIVE' and order_id and order_id != 'N/A':
                        logger.warning(f"STOP LOSS IS LIVE (in orderbook) - monitoring until filled...")
                        _executor.submit(self._monitor_live_order, order_id, 'stop_loss', time.time())
                    else:
                        logger.info(f"STOP LOSS EXECUTED (pre-signed): {shares} shares")
                    
                    # Remove from tracked positions
                    with self._position_lock:
                        if token_id in self.active_positions:
                            del self.active_positions[token_id]
                    
                    # Remove used pre-signed order
                    del self.presigned_sells[token_id]
                    
                    return result
                    
            except Exception as e:
                logger.error(f"Pre-signed stop loss failed, falling back: {e}")
                del self.presigned_sells[token_id]
        
        # SLOW PATH: Get position info and create new order
        position = self.get_position(token_id)
        if position:
            return self.place_market_sell_order(token_id, position.get('shares', 0))
        
        return None
    
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
            
            # Log API response fields
            if result:
                success = result.get('success', 'N/A')
                status = result.get('status', 'N/A')
                error_msg = result.get('errorMsg', '')
                order_id = result.get('orderID', 'N/A')
                logger.info(f"BUY ORDER RESPONSE: success={success}, status={status}, orderID={order_id[:16] if order_id != 'N/A' else 'N/A'}...")
                if error_msg:
                    logger.warning(f"Order errorMsg: {error_msg}")
                
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
            
            # Log API response fields
            if result:
                success = result.get('success', 'N/A')
                status = result.get('status', 'N/A')
                error_msg = result.get('errorMsg', '')
                order_id = result.get('orderID', 'N/A')
                logger.info(f"SELL ORDER RESPONSE: success={success}, status={status}, orderID={order_id[:16] if order_id != 'N/A' else 'N/A'}...")
                if error_msg:
                    logger.warning(f"Order errorMsg: {error_msg}")
                
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
    
    def place_market_sell_order(
        self,
        token_id: str,
        size: float
    ) -> Optional[Dict]:
        """
        Place a market sell order (immediate execution at best available price)
        
        Used for stop loss to guarantee execution regardless of price.
        
        Args:
            token_id: Token to sell
            size: Number of shares to sell
        """
        if not self.client:
            return None
        
        size_rounded = round(size, 2)
        
        logger.debug(f"MARKET SELL: {size_rounded} shares")
        
        try:
            # MarketOrderArgs: amount is in USD for buys, shares for sells
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=size_rounded,
                side=SELL,
                fee_rate_bps=0
            )
            
            # Step 1: Create and sign the market order
            signed_order = self.client.create_market_order(order_args)
            
            if not signed_order:
                logger.error("Failed to create market order")
                return None
            
            # Step 2: Post the signed order to the exchange
            result = self.client.post_order(signed_order, OrderType.GTC)
            
            # Log API response fields
            if result:
                success = result.get('success', 'N/A')
                status = result.get('status', 'N/A')
                error_msg = result.get('errorMsg', '')
                order_id = result.get('orderID', 'N/A')
                logger.info(f"MARKET SELL RESPONSE: success={success}, status={status}, orderID={order_id[:16] if order_id != 'N/A' else 'N/A'}...")
                if error_msg:
                    logger.warning(f"Order errorMsg: {error_msg}")
                
                logger.info(f"MARKET SELL EXECUTED: {size_rounded} shares")
                
                # Remove from tracked positions
                with self._position_lock:
                    if token_id in self.active_positions:
                        del self.active_positions[token_id]
                
                return result
            else:
                logger.warning(f"Market sell order returned empty result")
                
        except Exception as e:
            error_msg = str(e)
            if "couldn't be fully filled" in error_msg:
                logger.warning(f"Market sell failed: No liquidity available")
            else:
                logger.error(f"Market sell order failed: {e}")
        
        return None
    
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



