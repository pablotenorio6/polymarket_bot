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
from typing import Dict, Optional
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
    TRIGGER_PRICE, ENTRY_PRICE, MAX_POSITION_SIZE,
    STOP_LOSS_PRICE, ENABLE_STOP_LOSS,
    POLL_INTERVAL, MAX_ATTEMPTS_PER_MARKET
)


from monitor import FastMarketMonitor
from trader import FastTrader
from risk_manager import FastRiskManager
from redeem import RedeemManager


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
        logger.info(f"Strategy: BTC 15-min Up/Down")
        logger.info(f"Trigger: ${TRIGGER_PRICE:.2f} | Entry: ${ENTRY_PRICE:.2f}")
        logger.info(f"Stop Loss: ${STOP_LOSS_PRICE:.2f} | Size: ${MAX_POSITION_SIZE}")
        logger.info("=" * 50)
        
        # Core components (use persistent client for best performance)
        self.monitor = FastMarketMonitor(use_persistent_client=True)
        self.trader = FastTrader()
        self.risk_manager = FastRiskManager(self.trader)  # Inject trader
        self.redeem_manager = RedeemManager()
        
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
        logger.info("Starting trading loop...")
        
        while self.running:
            loop_start = time.perf_counter()
            
            try:
                # Check if we need to find/refresh market (SLOW PATH)
                if self._needs_market_refresh():
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
            
            if self.loop_count % 500 == 0:
                avg_latency = self.total_latency / self.loop_count
                logger.info(f"Loop stats: {self.loop_count} iterations, avg {avg_latency*1000:.1f}ms")
            
            # Wait for next iteration
            sleep_time = max(0, POLL_INTERVAL - latency)
            await asyncio.sleep(sleep_time)
        
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
                return True
        
        return False
    
    async def _refresh_market(self):
        """SLOW PATH: Find new market and set up (runs every ~15 min)"""
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
            
            # PRE-SIGN orders for instant execution
            self.trader.presign_buy_orders(
                up_token_id=self.locked_up_token,
                down_token_id=self.locked_down_token,
                price=ENTRY_PRICE,
                size=MAX_POSITION_SIZE,
                market_id=market_id
            )
            logger.debug("Pre-signed orders ready")
        
        # Periodic redeem (only on slow path)
        await self._periodic_redeem()
    
    async def _fast_iteration(self):
        """
        FAST PATH: Minimal latency price check and execution.
        Only fetches prices for the 2 locked tokens.
        """
        # Fetch ONLY the 2 tokens we care about (single API call)
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
        
        # Build price data for compatibility
        price_data = {
            'up_price': up_price,
            'down_price': down_price,
            'up_token_id': self.locked_up_token,
            'down_token_id': self.locked_down_token,
            'market': self.locked_market
        }
        
        # Check for trading opportunity
        await self._check_opportunity_fast(price_data)
        
        # Check stop losses
        current_prices = {
            self.locked_up_token: up_price,
            self.locked_down_token: down_price
        }
        self.risk_manager.check_stop_losses(current_prices)
    
    async def _check_opportunity_fast(self, price_data: Dict):
        """
        FAST PATH: Check for trading opportunity with minimal overhead.
        Market is already locked, no discovery needed.
        """
        up_price = price_data['up_price']
        down_price = price_data['down_price']
        up_token = price_data['up_token_id']
        down_token = price_data['down_token_id']
        market = price_data['market']
        market_id = market.get('conditionId', '')[:10]
        
        # Check if we already have a position
        existing_positions = self.trader.get_all_positions()
        if up_token in existing_positions or down_token in existing_positions:
            return  # Already have position
        
        # Skip if we exceeded max attempts for this market
        attempts = self.market_attempts.get(market_id, 0)
        if attempts >= MAX_ATTEMPTS_PER_MARKET:
            return
        
        # Check trigger conditions
        trade_side = self.trader.get_trade_side(up_price, down_price, TRIGGER_PRICE)
        
        if trade_side:
            # TRIGGER HIT - Execute immediately using PRE-SIGNED order
            token_id = up_token if trade_side == 'up' else down_token
            current_price = up_price if trade_side == 'up' else down_price
            
            logger.info(f"TRIGGER: {trade_side.upper()} @ ${current_price:.4f} (attempt {attempts + 1}/{MAX_ATTEMPTS_PER_MARKET})")
            
            # Increment attempt counter BEFORE placing order
            self.market_attempts[market_id] = attempts + 1
            
            # Execute trade using PRE-SIGNED order (FAST PATH)
            order = self.trader.execute_presigned_buy(
                token_id=token_id,
                side=trade_side,
                price=ENTRY_PRICE,
                size=MAX_POSITION_SIZE,
                market_info=market,
                order_type="FOK"
            )
            
            if order:
                if ENABLE_STOP_LOSS:
                    self.risk_manager.set_stop_loss(token_id, STOP_LOSS_PRICE)
                    logger.info(f"STOP LOSS set @ ${STOP_LOSS_PRICE:.2f}")
                # Reset attempts on success (filled position)
                self.market_attempts[market_id] = MAX_ATTEMPTS_PER_MARKET
    
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

