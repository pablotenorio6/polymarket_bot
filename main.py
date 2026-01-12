"""
Optimized main trading bot with async operations

Performance optimizations:
1. Async market monitoring with batch price fetching
2. Non-blocking order execution
3. Minimal processing between price update and trade decision
4. Efficient position management
"""

import asyncio
import signal
import sys
import logging
import time
from typing import Dict, Optional, Set
from datetime import datetime, timedelta
import pytz

# Configure logging first
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('trading_bot.log')
    ]
)

logger = logging.getLogger(__name__)

# Reduce noise from other libraries
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('py_clob_client').setLevel(logging.WARNING)

from config import (
    ENTRY_PRICE, MAX_POSITION_SIZE, POLL_INTERVAL
)


from monitor import FastMarketMonitor
from trader import FastTrader
from risk_manager import FastRiskManager
from redeem import RedeemManager
from ws_monitor import HybridPriceMonitor
from data_collector import DataCollector


class FastTradingBot:
    """
    High-performance async trading bot
    
    Architecture:
    - FAST LOOP: When market locked, only fetch 2 token prices
    - SLOW LOOP: Market discovery and redeem (every 15 min)
    - Pre-signed orders for instant execution
    """
    
    def __init__(self):
        logger.info("=" * 50)
        logger.info("POLYMARKET FAST TRADING BOT")
        logger.info("=" * 50)
        logger.info(f"Strategy: BTC 15-min Up/Down - MAXIMUM PRIORITY")
        logger.info(f"Scan future markets & place orders immediately @ ${ENTRY_PRICE:.2f}")
        logger.info(f"Hold until resolution | Size: ${MAX_POSITION_SIZE}")
        logger.info("=" * 50)
        
        # === ORDERS TRACKING ===
        # Set of condition_ids where we've already placed orders
        self.orders_placed_markets: Set[str] = set()
        
        # Future market scanning config
        self.future_scan_hours = 24  # Scan 24 hours ahead
        self.last_future_scan = 0
        self.future_scan_interval = 60  # Scan every 60 seconds
        
        # Core components (use persistent client for best performance)
        self.monitor = FastMarketMonitor(use_persistent_client=True)
        self.trader = FastTrader()
        self.risk_manager = FastRiskManager(self.trader)  # Inject trader
        self.redeem_manager = RedeemManager()
        
        # WebSocket price monitor (real-time, low latency)
        self.ws_monitor = HybridPriceMonitor(self.monitor)
        self.use_websocket = True  # Can disable for fallback to HTTP polling
        
        # Data collector for price history
        self.data_collector = DataCollector()
        
        # State tracking
        self.running = False
        self.last_market_id: Optional[str] = None
        self.last_redeem_check = 0
        self.redeem_interval = 3600  # 1 hour
        
        # Track attempts per market (avoid infinite retry loops)
        self.market_attempts: Dict[str, int] = {}
        
        # === LOCKED MARKET STATE (for fast loop) ===
        self.locked_market: Optional[Dict] = None  # Current market data
        self.locked_up_token: Optional[str] = None
        self.locked_down_token: Optional[str] = None
        self.market_end_time: Optional[datetime] = None
        
        # Performance metrics
        self.loop_count = 0
        self.total_latency = 0.0
        
    async def run(self):
        """Main async trading loop with FAST PATH optimization"""
        self.running = True
        
        # Start WebSocket connection for real-time prices
        if self.use_websocket:
            if await self.ws_monitor.start():
                logger.info("WebSocket connected - real-time prices enabled")
            else:
                logger.warning("WebSocket failed, falling back to HTTP polling")
                self.use_websocket = False
        
        # Recover state from open orders (survives restarts)
        await self._recover_orders_state()
        
        logger.info("Starting trading loop...")
        
        while self.running:
            loop_start = time.perf_counter()
            
            try:
                # === TASK 1: Scan for NEW future markets and place orders ===
                await self._scan_and_place_future_orders()
                
                # === TASK 2: Monitor current active market for price data ===
                # Check if we need to find/refresh current market
                if self._needs_market_refresh():
                    await asyncio.sleep(POLL_INTERVAL)
                    await self._refresh_market()
                
                # FAST PATH: Only fetch prices for locked tokens
                if self.locked_market:
                    await self._fast_iteration()
                
            except Exception as e:
                logger.error(f"Error in trading loop: {e}", exc_info=True)
            
            # Track performance
            latency = time.perf_counter() - loop_start
            self.loop_count += 1
            self.total_latency += latency
            
            # Removed continuous loop stats - only execution latency when orders trigger
            # if self.loop_count % 5000 == 0:
            #     # Small sleep to prevent CPU hogging
            await asyncio.sleep(0.0001)  # 0.1ms sleep

            # if self.loop_count % 10000 == 0:
            #     avg_latency = self.total_latency / self.loop_count
            #     logger.info(f"Loop stats: {self.loop_count} iterations, avg {avg_latency*1000:.1f}ms")

        await self.shutdown()
    
    def _needs_market_refresh(self) -> bool:
        """Check if we need to find a new market"""
        # No market locked yet
        if not self.locked_market:
            return True
        
        # Market expired - compare in ET timezone
        if self.market_end_time:
            et_tz = pytz.timezone('America/New_York')
            now_et = datetime.now(et_tz)
            
            # Ensure market_end_time is timezone-aware
            if self.market_end_time.tzinfo is None:
                market_end_et = et_tz.localize(self.market_end_time)
            else:
                market_end_et = self.market_end_time.astimezone(et_tz)
            
            if now_et >= market_end_et:
                logger.info(f"Market expired (now: {now_et.strftime('%H:%M:%S')} ET >= end: {market_end_et.strftime('%H:%M:%S')} ET)")
                # Mark for saving - will be saved in _refresh_market
                self._market_expired = True
                return True
        
        return False
    
    async def _refresh_market(self):
        """SLOW PATH: Find new market and set up (runs every ~15 min)"""
        # Save previous market data if it expired
        if getattr(self, '_market_expired', False) and self.data_collector.has_active_market():
            # Determine winner based on last known prices
            winner = None
            if self.locked_up_token and self.locked_down_token:
                prices = self.ws_monitor.get_prices()
                if prices:
                    up_price = prices.get(self.locked_up_token, 0)
                    down_price = prices.get(self.locked_down_token, 0)
                    if up_price > 0.5:
                        winner = 'UP'
                    elif down_price > 0.5:
                        winner = 'DOWN'
            
            await self.data_collector.save_market(winner=winner)
            self._market_expired = False
        
        # Clear locked state
        self.locked_market = None
        self.locked_up_token = None
        self.locked_down_token = None
        
        # Find active markets
        prices = await self.monitor.get_all_market_prices()
        
        if not prices:
            return
        
        # Lock onto first active market
        price_data = prices[0]
        market = price_data['market']
        market_id = market.get('conditionId', '')[:10]
        
        self.locked_market = market
        self.locked_up_token = price_data['up_token_id']
        self.locked_down_token = price_data['down_token_id']
        
        # Calculate market end time (keep timezone info!)
        et_tz = pytz.timezone('America/New_York')
        if self.monitor.current_market_end_time:
            # Keep the timezone-aware datetime from monitor
            self.market_end_time = self.monitor.current_market_end_time
        else:
            # Fallback: 15 min from now in ET
            self.market_end_time = datetime.now(et_tz) + timedelta(minutes=15)
        
        # Log new market
        if market_id != self.last_market_id:
            question = market.get('question', 'Unknown')[:50]
            logger.info(f"NEW MARKET: {question}...")
            end_time_et = self.market_end_time.astimezone(et_tz) if self.market_end_time.tzinfo else et_tz.localize(self.market_end_time)
            logger.info(f"  Ends: {end_time_et.strftime('%H:%M:%S')} ET")
            self.last_market_id = market_id
            self.market_attempts.clear()
            
            # Subscribe to WebSocket for real-time price updates
            if self.use_websocket:
                await self.ws_monitor.subscribe_to_market(
                    self.locked_up_token,
                    self.locked_down_token
                )
            
            # Start collecting price data for this market
            self.data_collector.start_market(
                condition_id=market.get('conditionId', ''),
                question=market.get('question', 'Unknown'),
                up_token_id=self.locked_up_token,
                down_token_id=self.locked_down_token,
                start_time=datetime.now(et_tz),
                end_time=self.market_end_time
            )
            
            # Orders are now placed by _scan_and_place_future_orders
            # when the market is first detected (before it becomes active)
        
        # Periodic redeem (only on slow path)
        await self._periodic_redeem()
    
    async def _fast_iteration(self):
        """
        FAST PATH: Minimal latency price check and execution.
        Uses WebSocket for instant prices (no HTTP call).
        """
        # === START PROFILING - Total cycle time ===
        t0 = time.perf_counter()

        # Get prices - WebSocket (instant) or HTTP (fallback)
        if self.use_websocket:
            # INSTANT: Read from memory (no network call!)
            prices = self.ws_monitor.get_prices()
            if not prices:
                prices = await self.ws_monitor.get_prices_with_fallback()
        else:
            # HTTP polling fallback
            prices = await self.monitor.get_prices_batch([
                self.locked_up_token,
                self.locked_down_token
            ])

        if not prices:
            return

        up_price = prices.get(self.locked_up_token)
        down_price = prices.get(self.locked_down_token)

        # Skip if no valid prices
        if up_price is None or down_price is None:
            return

        # Record price snapshot (every 1 second)
        self.data_collector.record_price(up_price, down_price)

        # Build price data for compatibility
        price_data = {
            'up_price': up_price,
            'down_price': down_price,
            'up_token_id': self.locked_up_token,
            'down_token_id': self.locked_down_token,
            'market': self.locked_market
        }

        # Orders already placed at market start - just collecting data
        # No trigger logic needed - hold until market resolution
    
    async def _scan_and_place_future_orders(self):
        """
        Scan for new future markets and place orders immediately.
        This ensures MAXIMUM FIFO priority by placing orders as soon as
        markets are created (up to 24h before they become active).
        """
        now = time.time()
        
        # Only scan periodically to respect rate limits
        if now - self.last_future_scan < self.future_scan_interval:
            return
        
        self.last_future_scan = now
        
        try:
            # Get all future markets (up to 24h ahead)
            future_markets = await self.monitor.get_future_markets(self.future_scan_hours)
            
            if not future_markets:
                return
            
            # Find markets where we haven't placed orders yet
            new_markets = []
            for market_data in future_markets:
                condition_id = market_data['condition_id']
                if condition_id and condition_id not in self.orders_placed_markets:
                    new_markets.append(market_data)
            
            if new_markets:
                logger.info(f"Found {len(new_markets)} NEW future markets without orders")
                
                # Place orders on each new market
                for market_data in new_markets:
                    await self._place_orders_on_market(market_data)
                    # Small delay between markets to avoid rate limits
                    await asyncio.sleep(0.5)
                    
        except Exception as e:
            logger.error(f"Error scanning future markets: {e}")
    
    async def _place_orders_on_market(self, market_data: Dict):
        """
        Place both UP and DOWN limit orders on a market.
        Called for both current and future markets.
        """
        condition_id = market_data['condition_id']
        up_token = market_data['up_token_id']
        down_token = market_data['down_token_id']
        start_time = market_data['start_time']
        market = market_data['market']
        
        # Format start time for logging
        et_tz = pytz.timezone('America/New_York')
        start_et = start_time.astimezone(et_tz)
        time_until = start_time - datetime.now(pytz.UTC)
        hours_until = time_until.total_seconds() / 3600
        
        logger.info(f"NEW MARKET DETECTED: {market_data['question'][:50]}...")
        logger.info(f"  Starts: {start_et.strftime('%Y-%m-%d %H:%M')} ET ({hours_until:.1f}h from now)")
        logger.info(f"  Placing orders @ ${ENTRY_PRICE:.2f}...")
        
        # Place UP order
        up_order = self.trader.place_buy_order(
            token_id=up_token,
            side='up',
            price=ENTRY_PRICE,
            size=MAX_POSITION_SIZE,
            market_info=market,
            order_type="GTC"
        )
        
        if up_order:
            logger.info(f"  UP order placed - Size: {MAX_POSITION_SIZE}")
        else:
            logger.warning(f"  Failed to place UP order")
        
        # Place DOWN order
        down_order = self.trader.place_buy_order(
            token_id=down_token,
            side='down',
            price=ENTRY_PRICE,
            size=MAX_POSITION_SIZE,
            market_info=market,
            order_type="GTC"
        )
        
        if down_order:
            logger.info(f"  DOWN order placed - Size: {MAX_POSITION_SIZE}")
        else:
            logger.warning(f"  Failed to place DOWN order")
        
        # Mark market as processed (even if orders failed, to avoid retry spam)
        self.orders_placed_markets.add(condition_id)
        
        if up_order and down_order:
            logger.info(f"  Both orders queued - MAXIMUM PRIORITY secured!")
        
        # Log total markets with orders
        logger.info(f"  Total markets with orders: {len(self.orders_placed_markets)}")
    
    async def _recover_orders_state(self):
        """
        Recover state from Polymarket on startup.
        Checks which markets already have open orders to avoid duplicates.
        
        This survives app restarts by querying the actual orderbook state.
        """
        logger.info("Recovering orders state from Polymarket...")
        
        try:
            # Get token IDs with open orders
            open_token_ids = self.trader.get_open_order_token_ids()
            
            if not open_token_ids:
                logger.info("No existing open orders found - starting fresh")
                return
            
            logger.info(f"Found {len(open_token_ids)} tokens with open orders")
            
            # Scan future markets to map token_ids -> condition_ids
            future_markets = await self.monitor.get_future_markets(self.future_scan_hours)
            
            # Also get current active markets
            current_markets = await self.monitor.get_all_market_prices()
            
            # Build token -> condition_id mapping
            token_to_condition = {}
            
            for market_data in future_markets:
                condition_id = market_data['condition_id']
                token_to_condition[market_data['up_token_id']] = condition_id
                token_to_condition[market_data['down_token_id']] = condition_id
            
            for price_data in current_markets:
                condition_id = price_data['market'].get('conditionId')
                if condition_id:
                    token_to_condition[price_data['up_token_id']] = condition_id
                    token_to_condition[price_data['down_token_id']] = condition_id
            
            # Find condition_ids for our open orders
            recovered_markets = set()
            for token_id in open_token_ids:
                condition_id = token_to_condition.get(token_id)
                if condition_id:
                    recovered_markets.add(condition_id)
            
            # Add to our tracking set
            self.orders_placed_markets.update(recovered_markets)
            
            logger.info(f"Recovered {len(recovered_markets)} markets with existing orders")
            logger.info(f"Total markets tracked: {len(self.orders_placed_markets)}")
            
        except Exception as e:
            logger.error(f"Error recovering orders state: {e}")
            logger.info("Continuing with empty state - may place duplicate orders")
    
    async def _periodic_redeem(self):
        """Periodically check for redeemable positions"""
        now = time.time()
        
        if now - self.last_redeem_check > self.redeem_interval:
            self.last_redeem_check = now
            
            # Run redeem in background to not block trading
            try:
                self.redeem_manager.check_and_redeem()
            except Exception as e:
                logger.debug(f"Redeem check failed: {e}")
    
    async def shutdown(self):
        """Clean up resources"""
        logger.info("Shutting down...")
        
        # Save any pending market data
        if self.data_collector.has_active_market():
            logger.info("Saving pending market data...")
            await self.data_collector.save_market()
        
        await self.data_collector.close()
        
        # Close WebSocket connection
        if self.use_websocket:
            await self.ws_monitor.close()
        
        await self.monitor.close()
        
        positions = self.trader.get_all_positions()
        if positions:
            logger.warning(f"{len(positions)} open positions - close manually on Polymarket")
        
        logger.info("Bot stopped")


async def main():
    """Entry point with signal handling"""
    bot = FastTradingBot()
    
    # Handle shutdown signals
    loop = asyncio.get_running_loop()
    
    def signal_handler():
        logger.info("Shutdown signal received")
        bot.running = False
    
    if sys.platform != 'win32':
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
    
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")

