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
            # Step 1: Create and sign the order
            from py_clob_client.order_builder.constants import BUY
            from py_clob_client.clob_types import OrderArgs, OrderType
            
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=shares,
                side=BUY,
                fee_rate_bps=0,  # Fee rate in basis points
                nonce=0  # Will be auto-generated if 0
            )
            
            # Create and sign the order
            signed_order = self.client.create_order(order_args)
            
            # Step 2: Post the order to the exchange with FOK time in force
            order = self.client.post_order(signed_order, orderType=OrderType.FOK)
            
            if order:
                logger.info(f"Order placed successfully: {order}")
                
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
        size: float,
        order_type: str = "FOK"
    ) -> Optional[Dict]:
        """
        Place a sell order to close position
        
        Args:
            token_id: The token ID to sell
            price: Price to sell at
            size: Number of shares to sell
            order_type: Order type - "FOK" (Fill or Kill) or "GTC" (Good Till Canceled)
        
        Returns:
            Order result dict or None if failed
        """
        if not self.client:
            logger.error("Client not initialized")
            return None
        
        try:
            logger.info(f"Attempting to SELL {size:.2f} shares at ${price:.3f} ({order_type})")
            
            from py_clob_client.order_builder.constants import SELL
            from py_clob_client.clob_types import OrderArgs, OrderType
            
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=SELL,
                fee_rate_bps=0,
                nonce=0
            )
            signed_order = self.client.create_order(order_args)

            # Select order type
            ot = OrderType.GTC if order_type == "GTC" else OrderType.FOK
            order = self.client.post_order(signed_order, orderType=ot)

            if order:
                logger.info(f"Sell order placed: {order}")
                
                # Remove from active positions only if it's a FOK order (immediate)
                # GTC orders (stop loss) don't remove the position yet
                if order_type == "FOK" and token_id in self.active_positions:
                    position = self.active_positions.pop(token_id)
                    entry_price = position['entry_price']
                    pnl = (price - entry_price) * size
                    logger.info(f"Position closed. P&L: ${pnl:+.2f}")
                
                return order
            else:
                logger.warning(f"Sell order returned None ({order_type})")
                return None
                
        except Exception as e:
            logger.error(f"Error placing sell order: {e}")
            return None
    
    def place_stop_loss_order(
        self,
        token_id: str,
        stop_loss_price: float,
        size: float
    ) -> Optional[Dict]:
        """
        Place a Good-Till-Canceled (GTC) sell order as a stop loss
        
        This order will remain active on the exchange and automatically execute
        if the price drops to the stop loss level.
        
        Args:
            token_id: The token ID to sell
            stop_loss_price: Price at which to sell (stop loss level)
            size: Number of shares to sell
        
        Returns:
            Order result dict or None if failed
        """
        logger.info(f"Placing STOP LOSS order at ${stop_loss_price:.3f} for {size:.2f} shares")
        
        # Use GTC (Good Till Canceled) so the order stays on the book
        result = self.place_sell_order(
            token_id=token_id,
            price=stop_loss_price,
            size=size,
            order_type="GTC"
        )
        
        if result:
            logger.info(f"STOP LOSS order placed successfully - will execute if price drops to ${stop_loss_price:.3f}")
            
            # Store stop loss order ID in the position
            if token_id in self.active_positions:
                self.active_positions[token_id]['stop_loss_order_id'] = result.get('id')
                self.active_positions[token_id]['stop_loss_price'] = stop_loss_price
        
        return result
    
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
            logger.info(f"TRIGGER: This side at ${current_side_price:.3f} >= ${trigger_price:.3f}")
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

