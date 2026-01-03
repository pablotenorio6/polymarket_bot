"""
Polymarket Trading Bot - Main Entry Point

Strategy: Buy the same side when it reaches 96 cents (momentum trading)
Buys at 97 cents with fill-or-kill order, expecting it to reach 99 cents
Uses stop loss for risk management
"""

import time
import logging
import sys
import os
from datetime import datetime
from typing import Dict

# Configure logging FIRST, before importing other modules
# This ensures all modules use the same logging configuration
from config import LOG_LEVEL, LOG_FILE

# Ensure logs directory exists
log_dir = os.path.dirname(LOG_FILE)
if log_dir:
    os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ],
    force=True  # Override any existing logging configuration
)

logger = logging.getLogger(__name__)

# Now import other modules (they will use the configured logging)
from config import (
    TRIGGER_PRICE, ORDER_PRICE, STOP_LOSS_PRICE,
    POLL_INTERVAL, MAX_POSITION_SIZE, ENABLE_TAKE_PROFIT, ENABLE_STOP_LOSS
)
from monitor import MarketMonitor
from trader import get_trader
from risk_manager import get_risk_manager
from auth import get_auth


class TradingBot:
    """
    Main trading bot that monitors markets and executes strategy
    """
    
    def __init__(self):
        self.monitor = MarketMonitor()
        self.trader = get_trader()
        self.risk_manager = get_risk_manager()
        self.running = False
        self.last_market_id = None  # Track market changes
    
    def initialize(self):
        """Initialize bot components"""
        # Check authentication
        auth = get_auth()
        if not auth.private_key:
            logger.error("POLYMARKET_PRIVATE_KEY not set! Trading DISABLED")
            return False
        
        # Initialize trader
        try:
            self.trader.initialize()
        except Exception as e:
            logger.error(f"Failed to initialize trader: {e}")
            return False
        
        logger.info(f"Bot ready | Trigger: ${TRIGGER_PRICE:.2f} | Entry: ${ORDER_PRICE:.2f} | Stop: ${STOP_LOSS_PRICE:.2f} | Size: ${MAX_POSITION_SIZE:.2f}")
        return True
    
    def run(self):
        """Main bot loop"""
        if not self.initialize():
            logger.warning("Running in MONITOR MODE only (no trading)")
        
        self.running = True
        logger.info("Monitoring BTC 15-min markets... (Ctrl+C to stop)")
        
        try:
            while self.running:
                self._trading_loop()
                time.sleep(POLL_INTERVAL)
                
        except KeyboardInterrupt:
            logger.info("\n Shutting down bot...")
            self.shutdown()
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            self.shutdown()
    
    def _trading_loop(self):
        """Single iteration of the trading loop"""
        try:
            # Update active markets
            markets = self.monitor.update_active_markets()
            
            if not markets:
                logger.debug("No active BTC 15-min markets found")
                return
            
            # Get current prices for all markets
            all_prices = self.monitor.get_all_market_prices()
            
            if not all_prices:
                logger.debug("No price data available")
                return
            
            # Check each market for trading opportunities
            for price_data in all_prices:
                self._check_market_opportunity(price_data)
            
            # Check stop losses and take profits for existing positions
            self._manage_positions(all_prices)
            
        except Exception as e:
            logger.error(f"Error in trading loop: {e}", exc_info=True)
    
    def _check_market_opportunity(self, price_data: Dict):
        """Check a single market for trading opportunity"""
        up_price = price_data['up_price']
        down_price = price_data['down_price']
        up_token = price_data['up_token_id']
        down_token = price_data['down_token_id']
        market = price_data['market']
        market_id = market.get('conditionId', market.get('id', ''))
        
        # Log only when entering a new market
        if market_id != self.last_market_id:
            self.last_market_id = market_id
            question = market.get('question', 'Unknown Market')
            logger.info(f"\n{'='*60}")
            logger.info(f"NEW MARKET: {question}")
            logger.info(f"{'='*60}")
        
        # Check if we can open new positions
        if not self.risk_manager.can_open_new_position():
            return
        
        # Strategy: If UP reaches TRIGGER_PRICE, buy UP at ORDER_PRICE
        if self.trader.should_enter_trade(up_price, TRIGGER_PRICE, up_token):
            logger.info(f"TRIGGER! UP at ${up_price:.3f} >= ${TRIGGER_PRICE:.2f} - Placing order...")
            self._execute_trade(up_token, "UP", ORDER_PRICE, market)
        
        # Strategy: If DOWN reaches TRIGGER_PRICE, buy DOWN at ORDER_PRICE
        elif self.trader.should_enter_trade(down_price, TRIGGER_PRICE, down_token):
            logger.info(f"TRIGGER! DOWN at ${down_price:.3f} >= ${TRIGGER_PRICE:.2f} - Placing order...")
            self._execute_trade(down_token, "DOWN", ORDER_PRICE, market)
    
    def _execute_trade(self, token_id: str, side: str, price: float, market: Dict):
        """Execute a trade and place automatic stop loss order"""
        try:
            # Check if trader is initialized
            if not self.trader.client:
                logger.warning("Trader not initialized - skipping trade")
                return
            
            # Place buy order
            order = self.trader.place_buy_order(
                token_id=token_id,
                side=side,
                price=price,
                size=MAX_POSITION_SIZE,
                market_info=market
            )
            
            if order:
                # Set stop loss via price monitoring
                # (Polymarket doesn't have native stop orders)
                if ENABLE_STOP_LOSS:
                    self.risk_manager.set_stop_loss(token_id, STOP_LOSS_PRICE)
                    logger.info(f"STOP LOSS monitoring active @ ${STOP_LOSS_PRICE:.2f}")
            else:
                logger.warning("Trade not filled (FOK rejected)")
                
        except Exception as e:
            logger.error(f"Error executing trade: {e}")
    
    def _manage_positions(self, all_prices: list):
        """Manage existing positions (stop loss, take profit)"""
        positions = self.trader.get_all_positions()
        if not positions:
            return
        
        # Build current price dict
        current_prices = {}
        for price_data in all_prices:
            current_prices[price_data['up_token_id']] = price_data['up_price']
            current_prices[price_data['down_token_id']] = price_data['down_price']
        
        # Check stop losses - this will execute sell if price drops to stop level
        # Also cleans up positions from resolved markets
        self.risk_manager.check_stop_losses(current_prices)
        
        # Log position status
        for token_id, position in positions.items():
            if token_id in current_prices:
                current_price = current_prices[token_id]
                unrealized_pnl = (current_price - position['entry_price']) * position['size']
                logger.debug(f"Holding {position['side']}: ${current_price:.3f} (P&L: ${unrealized_pnl:+.2f})")

    def shutdown(self):
        """Gracefully shutdown the bot"""
        self.running = False
        
        # Print final summary
        positions = self.trader.get_all_positions()
        if positions:
            logger.warning(f"OPEN POSITIONS: {len(positions)} - Close manually!")
        
        logger.info("Bot stopped")


def main():
    """Main entry point"""
    print("\n=== POLYMARKET BTC 15-MIN TRADING BOT ===\n")
    
    # Create and run bot
    bot = TradingBot()
    bot.run()


if __name__ == "__main__":
    main()

