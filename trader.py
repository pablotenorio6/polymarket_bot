"""
Trading logic for placing orders on Polymarket
"""

import logging
from typing import Dict, Optional
from decimal import Decimal
from config import ORDER_PRICE, MAX_POSITION_SIZE
from auth import get_auth

logger = logging.getLogger(__name__)


class PolymarketTrader:
    """
    Handles order placement and position management
    """
    
    def __init__(self):
        self.auth = get_auth()
        self.client = None
        self.active_positions = {}  # token_id -> position info
    
    def initialize(self):
        """Initialize the trading client"""
        try:
            self.client = self.auth.get_client()
            logger.info("Trader initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize trader: {e}")
            raise
    
    def place_buy_order(
        self,
        token_id: str,
        side: str,  # "UP" or "DOWN"
        price: float,
        size: float,
        market_info: Dict
    ) -> Optional[Dict]:
        """
        Place a fill-or-kill buy order
        
        Args:
            token_id: The token ID to buy
            side: "UP" or "DOWN"
            price: Price willing to pay (e.g., 0.97)
            size: Amount in USD to spend
            market_info: Market information dict
        
        Returns:
            Order result dict or None if failed
        """
        if not self.client:
            logger.error("Client not initialized. Call initialize() first.")
            return None
        
        try:
            logger.info(f"Attempting to BUY {side} at ${price:.3f} for ${size:.2f}")
            logger.info(f"Market: {market_info.get('question', 'Unknown')[:60]}...")
            
            # Calculate shares to buy
            # shares = size / price
            shares = size / price
            
            # Create order using py-clob-client
            order = self.client.create_order(
                token_id=token_id,
                price=price,
                size=shares,
                side="BUY",
                order_type="FOK"  # Fill or Kill
            )
            
            if order:
                logger.info(f"✓ Order placed successfully: {order}")
                
                # Track position
                self.active_positions[token_id] = {
                    'side': side,
                    'entry_price': price,
                    'size': shares,
                    'market': market_info,
                    'order_id': order.get('id'),
                    'timestamp': order.get('timestamp')
                }
                
                return order
            else:
                logger.warning("Order returned None (possibly not filled)")
                return None
                
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return None
    
    def place_sell_order(
        self,
        token_id: str,
        price: float,
        size: float
    ) -> Optional[Dict]:
        """
        Place a sell order to close position
        
        Args:
            token_id: The token ID to sell
            price: Price to sell at
            size: Number of shares to sell
        
        Returns:
            Order result dict or None if failed
        """
        if not self.client:
            logger.error("Client not initialized")
            return None
        
        try:
            logger.info(f"Attempting to SELL {size:.2f} shares at ${price:.3f}")
            
            order = self.client.create_order(
                token_id=token_id,
                price=price,
                size=size,
                side="SELL",
                order_type="FOK"
            )
            
            if order:
                logger.info(f"✓ Sell order placed: {order}")
                
                # Remove from active positions
                if token_id in self.active_positions:
                    position = self.active_positions.pop(token_id)
                    entry_price = position['entry_price']
                    pnl = (price - entry_price) * size
                    logger.info(f"Position closed. P&L: ${pnl:+.2f}")
                
                return order
            else:
                logger.warning("Sell order returned None")
                return None
                
        except Exception as e:
            logger.error(f"Error placing sell order: {e}")
            return None
    
    def get_position(self, token_id: str) -> Optional[Dict]:
        """Get info about an active position"""
        return self.active_positions.get(token_id)
    
    def has_position(self, token_id: str) -> bool:
        """Check if we have an open position for this token"""
        return token_id in self.active_positions
    
    def get_all_positions(self) -> Dict:
        """Get all active positions"""
        return self.active_positions.copy()
    
    def should_enter_trade(
        self,
        current_side_price: float,
        trigger_price: float,
        token_id: str
    ) -> bool:
        """
        Determine if we should enter a trade
        
        Logic: If this side reaches trigger_price (0.96),
        we buy THIS SAME side at ORDER_PRICE (0.97) expecting it to reach 0.99+
        
        Args:
            current_side_price: Current price of this side
            trigger_price: Price threshold to trigger trade (e.g., 0.96)
            token_id: Token we would buy
        
        Returns:
            True if we should enter trade
        """
        # Don't enter if we already have a position
        if self.has_position(token_id):
            return False
        
        # Check if this side has reached trigger price
        if current_side_price >= trigger_price:
            logger.info(f"🎯 TRIGGER: This side at ${current_side_price:.3f} >= ${trigger_price:.3f}")
            return True
        
        return False


# Singleton instance
_trader_instance: Optional[PolymarketTrader] = None


def get_trader() -> PolymarketTrader:
    """Get the singleton trader instance"""
    global _trader_instance
    if _trader_instance is None:
        _trader_instance = PolymarketTrader()
    return _trader_instance

