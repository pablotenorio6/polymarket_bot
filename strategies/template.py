"""
Template for new strategies - copy this file and modify

Example: python main.py --market btc --mode trade --strategy my_strategy
"""

import logging
from typing import Dict, Optional
from datetime import datetime
import pytz

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class MyNewStrategy(BaseStrategy):
    """
    Your strategy description here.
    
    Explain:
    - What triggers orders
    - When orders are placed
    - Position management rules
    """
    
    name = "my_strategy"
    description = "Brief description of strategy"
    
    async def initialize(self) -> None:
        """
        Called once at startup.
        
        Use for:
        - Recovering state from API
        - Loading historical data
        - Setting up strategy-specific variables
        """
        logger.info(f"Initializing {self.name} strategy...")
        
        # Example: track your own state
        self.pending_orders = {}
        self.last_trade_time = None
    
    async def on_new_market(self, market_data: Dict) -> None:
        """
        Called when a NEW FUTURE market is detected (before it starts).
        
        market_data contains:
            - condition_id: Unique market ID
            - question: Market title
            - up_token_id: Token for UP outcome
            - down_token_id: Token for DOWN outcome  
            - start_time: When market starts (datetime, UTC)
            - end_time: When market ends (datetime, UTC)
            - market: Raw Polymarket market object
        """
        condition_id = market_data['condition_id']
        
        # Skip if already processed
        if self.is_market_processed(condition_id):
            return
        
        # === YOUR LOGIC HERE ===
        # Example: Place orders only if crypto price is in certain range
        crypto_price = self.get_crypto_price()
        
        if crypto_price:
            logger.info(f"New market detected. {self.market_symbol}: ${crypto_price:,.2f}")
        
        # Example: Place an order
        # order = self.place_buy_order(
        #     token_id=market_data['up_token_id'],
        #     side='up',
        #     price=0.01,  # or self.entry_price
        #     size=50,     # or self.position_size
        #     order_type="GTC"  # or "FOK"
        # )
        
        # Mark as processed to avoid duplicate processing
        self.mark_market_processed(condition_id)
    
    async def on_price_update(
        self,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market: Dict
    ) -> None:
        """
        Called on EVERY price update (10-100ms interval).
        
        This is your main decision point for reactive strategies.
        Keep this FAST - heavy computation will slow down the bot.
        
        Args:
            up_price: Current UP token price (0.01 to 0.99)
            down_price: Current DOWN token price (0.01 to 0.99)
            up_token_id: Token ID for UP
            down_token_id: Token ID for DOWN
            market: Raw market data
        """
        # === YOUR LOGIC HERE ===
        
        # Example: Trigger when price crosses threshold
        # if up_price >= 0.05 and not self.has_position:
        #     order = self.place_buy_order(
        #         token_id=up_token_id,
        #         side='up',
        #         price=up_price,
        #         order_type="FOK"  # Fill or Kill for immediate execution
        #     )
        #     if order:
        #         self.has_position = True
        
        # Example: Log crypto price alongside market price
        # crypto_price = self.get_crypto_price()
        # if crypto_price:
        #     logger.debug(f"UP: {up_price:.3f} | {self.market_symbol}: ${crypto_price:,.0f}")
        
        pass
    
    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        """
        Called when a market ends (before resolution).
        
        Args:
            market_data: Market information (condition_id, question)
            winner: Predicted winner based on final prices ('UP', 'DOWN', or None)
        """
        logger.info(f"Market ended: {market_data.get('question', '')[:40]}... Winner: {winner}")
        
        # === YOUR CLEANUP LOGIC HERE ===
        # Example: Reset state for next market
        # self.has_position = False
    
    async def shutdown(self) -> None:
        """Called on bot shutdown. Clean up resources."""
        logger.info(f"Shutting down {self.name} strategy...")
        # Cancel any pending orders, save state, etc.
