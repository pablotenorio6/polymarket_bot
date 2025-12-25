"""
Polymarket Trading Bot - Main Entry Point

Strategy: Buy the same side when it reaches 96 cents (momentum trading)
Buys at 97 cents with fill-or-kill order, expecting it to reach 99 cents
Uses stop loss for risk management
"""

import time
import logging
import sys
from datetime import datetime
from typing import Dict

# Configure logging FIRST, before importing other modules
# This ensures all modules use the same logging configuration
from config import LOG_LEVEL, LOG_FILE

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
    
    def initialize(self):
        """Initialize bot components"""
        logger.info("="*80)
        logger.info("POLYMARKET TRADING BOT - INITIALIZING")
        logger.info("="*80)
        
        # Check authentication
        auth = get_auth()
        if not auth.private_key:
            logger.error("   POLYMARKET_PRIVATE_KEY not set!")
            logger.error("   Set environment variable: POLYMARKET_PRIVATE_KEY=your_private_key")
            logger.error("   Trading will be DISABLED (monitor mode only)")
            return False
        
        # Initialize trader
        try:
            self.trader.initialize()
        except Exception as e:
            logger.error(f"Failed to initialize trader: {e}")
            logger.error("   Bot will run in MONITOR MODE only (no trading)")
            return False
        
        logger.info("   Bot initialized successfully")
        logger.info(f"  Strategy: Buy same side when it reaches ${TRIGGER_PRICE:.2f}")
        logger.info(f"  Order Price: ${ORDER_PRICE:.2f} (Fill or Kill)")
        logger.info(f"  Stop Loss: ${STOP_LOSS_PRICE:.2f}")
        logger.info(f"  Take Profit: {'Enabled ($0.99)' if ENABLE_TAKE_PROFIT else 'Disabled (Hold to resolution)'}")
        logger.info(f"  Position Size: ${MAX_POSITION_SIZE:.2f}")
        logger.info("="*80)
        return True
    
    def run(self):
        """Main bot loop"""
        if not self.initialize():
            logger.warning("Running in MONITOR MODE only (no trading)")
        
        self.running = True
        logger.info(" Bot started. Press Ctrl+C to stop.")
        
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
            
            logger.info(f"\n{'='*80}")
            logger.info(f"Monitoring {len(all_prices)} active markets | {datetime.now().strftime('%H:%M:%S')}")
            logger.info(f"{'='*80}")
            
            # Check each market for trading opportunities
            for price_data in all_prices:
                self._check_market_opportunity(price_data)
            
            # Check stop losses and take profits for existing positions
            self._manage_positions(all_prices)
            
            # Print position summary if we have any
            positions = self.trader.get_all_positions()
            if positions:
                logger.info(self.risk_manager.get_position_summary())
            
        except Exception as e:
            logger.error(f"Error in trading loop: {e}", exc_info=True)
    
    def _check_market_opportunity(self, price_data: Dict):
        """Check a single market for trading opportunity"""
        up_price = price_data['up_price']
        down_price = price_data['down_price']
        up_token = price_data['up_token_id']
        down_token = price_data['down_token_id']
        market = price_data['market']
        
        question = market.get('question', '')[:60]
        logger.info(f"  {question}...")
        logger.info(f"    UP: ${up_price:.3f} | DOWN: ${down_price:.3f}")
        
        # Check if we can open new positions
        if not self.risk_manager.can_open_new_position():
            return
        
        # Strategy: If UP reaches TRIGGER_PRICE, buy UP at ORDER_PRICE
        if self.trader.should_enter_trade(up_price, TRIGGER_PRICE, up_token):
            logger.info(f"  UP side at ${up_price:.3f} - Buying UP side!")
            self._execute_trade(up_token, "UP", ORDER_PRICE, market)
        
        # Strategy: If DOWN reaches TRIGGER_PRICE, buy DOWN at ORDER_PRICE
        elif self.trader.should_enter_trade(down_price, TRIGGER_PRICE, down_token):
            logger.info(f"  DOWN side at ${down_price:.3f} - Buying DOWN side!")
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
                logger.info(f"  Trade executed successfully!")
                
                # Get the position to know how many shares we bought
                position = self.trader.get_position(token_id)
                if position and ENABLE_STOP_LOSS:
                    # Place automatic stop loss order (GTC - stays on exchange)
                    logger.info(f"  Placing automatic stop loss order...")
                    stop_loss_order = self.trader.place_stop_loss_order(
                        token_id=token_id,
                        stop_loss_price=STOP_LOSS_PRICE,
                        size=position['size']
                    )
                    
                    if stop_loss_order:
                        logger.info(f"  STOP LOSS order active at ${STOP_LOSS_PRICE:.3f}")
                    else:
                        logger.warning("  Failed to place stop loss order - will use price monitoring instead")
                        # Fallback to old method of monitoring price
                        self.risk_manager.set_stop_loss(token_id, STOP_LOSS_PRICE)
                else:
                    logger.info("  Stop loss disabled or position not found")
            else:
                logger.warning("   Trade failed or not filled (FOK)")
                
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
        
        
        # Just log position status
        for token_id, position in positions.items():
            if token_id in current_prices:
                current_price = current_prices[token_id]
                unrealized_pnl = (current_price - position['entry_price']) * position['size']
                logger.debug(f"  Holding {position['side']}: ${current_price:.3f} (Unrealized P&L: ${unrealized_pnl:+.2f})")

    def shutdown(self):
        """Gracefully shutdown the bot"""
        self.running = False
        
        # Print final summary
        positions = self.trader.get_all_positions()
        if positions:
            logger.info("\n  OPEN POSITIONS AT SHUTDOWN:")
            logger.info(self.risk_manager.get_position_summary())
            logger.warning("Please manually close these positions!")
        
        logger.info("Bot stopped.")


def main():
    """Main entry point"""
    print("""
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║          POLYMARKET TRADING BOT - BTC 15-MIN MARKETS          ║
║                                                               ║
║  Strategy: Buy same side when it reaches trigger price       ║
║  Entry: Fill or Kill order                                    ║
║  Exit: Hold to market resolution ($1.00 if win)              ║
║  Protection: Optional stop loss                               ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    # Create and run bot
    bot = TradingBot()
    bot.run()


if __name__ == "__main__":
    main()

