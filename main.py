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
from datetime import datetime

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
    - Async main loop with configurable poll interval
    - Non-blocking price fetching (batch requests)
    - Immediate trade execution on trigger
    - Background stop loss monitoring
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
        
        # Performance metrics
        self.loop_count = 0
        self.total_latency = 0.0
        
    async def run(self):
        """Main async trading loop"""
        self.running = True
        logger.info("Starting trading loop...")
        
        while self.running:
            loop_start = time.perf_counter()
            
            try:
                await self._trading_iteration()
                
            except Exception as e:
                logger.error(f"Error in trading loop: {e}", exc_info=True)
            
            # Track performance
            latency = time.perf_counter() - loop_start
            self.loop_count += 1
            self.total_latency += latency
            
            if self.loop_count % 100 == 0:
                avg_latency = self.total_latency / self.loop_count
                logger.debug(f"Avg loop latency: {avg_latency*1000:.1f}ms")
            
            # Wait for next iteration
            sleep_time = max(0, POLL_INTERVAL - latency)
            await asyncio.sleep(sleep_time)
        
        await self.shutdown()
    
    async def _trading_iteration(self):
        """Single iteration of the trading loop"""
        
        # 1. Get active markets
        markets = await self.monitor.get_active_markets()
        
        if not markets:
            return
        
        # 2. Fetch all prices in batch (single API call)
        prices = await self.monitor.get_all_market_prices()
        
        if not prices:
            return
        
        # 3. Check each market for trading opportunities
        for price_data in prices:
            await self._check_opportunity(price_data)
        
        # 4. Check stop losses (non-blocking)
        self._check_stop_losses(prices)
        
        # 5. Periodic redeem check
        await self._periodic_redeem()
    
    async def _check_opportunity(self, price_data: Dict):
        """Check for trading opportunity and execute if found"""
        up_price = price_data['up_price']
        down_price = price_data['down_price']
        market = price_data['market']
        market_id = market.get('conditionId', '')[:10]
        
        # Log new market and reset attempts
        if market_id != self.last_market_id:
            question = market.get('question', 'Unknown')[:50]
            logger.info(f"NEW MARKET: {question}...")
            self.last_market_id = market_id
            self.market_attempts.clear()  # Reset for new market
        
        # Check if we already have a position in this market
        up_token = price_data['up_token_id']
        down_token = price_data['down_token_id']
        
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
            # TRIGGER HIT - Execute immediately
            token_id = up_token if trade_side == 'up' else down_token
            current_price = up_price if trade_side == 'up' else down_price
            
            logger.info(f"TRIGGER: {trade_side.upper()} @ ${current_price:.4f} (attempt {attempts + 1}/{MAX_ATTEMPTS_PER_MARKET})")
            
            # Increment attempt counter BEFORE placing order
            self.market_attempts[market_id] = attempts + 1
            
            # Execute trade at fixed entry price
            order = self.trader.place_buy_order(
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
    
    def _check_stop_losses(self, prices: list):
        """Check and execute stop losses if needed"""
        if not ENABLE_STOP_LOSS:
            return
        
        # Build price dict
        current_prices = {}
        for p in prices:
            current_prices[p['up_token_id']] = p['up_price']
            current_prices[p['down_token_id']] = p['down_price']
        
        # Check stops (this will execute sells if triggered)
        self.risk_manager.check_stop_losses(current_prices)
        
        # Log position status if we have any
        positions = self.trader.get_all_positions()
        if positions:
            summary = self.risk_manager.get_position_summary()
            if summary:
                logger.debug(summary)
    
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

