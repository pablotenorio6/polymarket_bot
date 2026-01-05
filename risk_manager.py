"""
Optimized risk management for fast trading bot

Key differences from original RiskManager:
1. Dependency injection - trader passed as parameter
2. No unnecessary imports (trader, monitor)
3. Lighter weight initialization
4. Thread-safe position access
"""

import logging
from typing import Dict, Optional, TYPE_CHECKING

from config import STOP_LOSS_PRICE, ENABLE_STOP_LOSS, MAX_CONCURRENT_POSITIONS

# Type checking only import to avoid circular deps
if TYPE_CHECKING:
    from trader import FastTrader

logger = logging.getLogger(__name__)

# After this many cycles without price data, consider market resolved
NO_PRICE_THRESHOLD = 10


class FastRiskManager:
    """
    Lightweight risk manager for use with FastTrader
    
    Features:
    - No external dependencies (trader injected)
    - Stop loss via price monitoring
    - Position limit enforcement
    - Resolved market detection
    """
    
    def __init__(self, trader: 'FastTrader'):
        """
        Initialize with a trader instance
        
        Args:
            trader: FastTrader instance to use for order execution
        """
        self.trader = trader
        self.stop_losses: Dict[str, float] = {}  # token_id -> stop price
        self.no_price_count: Dict[str, int] = {}  # Track consecutive no-price cycles
    
    def set_stop_loss(self, token_id: str, stop_price: float):
        """Set a stop loss price for a position"""
        self.stop_losses[token_id] = stop_price
        logger.debug(f"Stop loss set for {token_id[:10]}... at ${stop_price:.3f}")
    
    def remove_stop_loss(self, token_id: str):
        """Remove a stop loss"""
        if token_id in self.stop_losses:
            del self.stop_losses[token_id]
        if token_id in self.no_price_count:
            del self.no_price_count[token_id]
    
    def check_stop_losses(self, current_prices: Dict[str, float]):
        """
        Check all positions against their stop losses
        
        Args:
            current_prices: Dict of token_id -> current_price
        """
        if not ENABLE_STOP_LOSS:
            return
        
        positions = self.trader.get_all_positions()
        
        # Process each position
        tokens_to_process = list(positions.keys())
        
        for token_id in tokens_to_process:
            position = positions.get(token_id)
            if not position:
                continue
            
            # Ensure stop loss is set
            if token_id not in self.stop_losses:
                self.set_stop_loss(token_id, STOP_LOSS_PRICE)
            
            current_price = current_prices.get(token_id)
            
            if current_price is None:
                self._handle_no_price(token_id, position)
                continue
            
            # Reset counter on valid price
            self.no_price_count[token_id] = 0
            
            # Check stop loss trigger
            stop_price = self.stop_losses[token_id]
            
            if current_price <= stop_price:
                self._execute_stop_loss(token_id, position, current_price)
    
    def _handle_no_price(self, token_id: str, position: Dict):
        """Handle case when no price data is available"""
        self.no_price_count[token_id] = self.no_price_count.get(token_id, 0) + 1
        
        if self.no_price_count[token_id] >= NO_PRICE_THRESHOLD:
            # Market likely resolved
            logger.info(f"Market resolved for {position['side']} position - cleaning up")
            self._cleanup_resolved_position(token_id)
        elif self.no_price_count[token_id] == 1:
            logger.debug(f"No price data for {token_id[:10]}...")
    
    def _execute_stop_loss(self, token_id: str, position: Dict, current_price: float):
        """Execute a stop loss market sell order using PRE-SIGNED order if available"""
        try:
            shares = position.get('shares', position.get('size', 0))
            entry_price = position['entry_price']
            side = position['side']
            
            logger.warning(f"STOP LOSS TRIGGERED for {side.upper()} @ ${current_price:.3f}")
            
            # Try PRE-SIGNED order first (FAST PATH)
            order = self.trader.execute_presigned_stop_loss(token_id)
            
            if order:
                # Estimate loss based on current price
                est_loss = (current_price - entry_price) * shares
                logger.warning(f"STOP LOSS EXECUTED | Est. Loss: ${est_loss:.2f}")
                self.remove_stop_loss(token_id)
            else:
                logger.error("Stop loss order failed - will retry next cycle")
                
        except Exception as e:
            logger.error(f"Error executing stop loss: {e}")
    
    def _cleanup_resolved_position(self, token_id: str):
        """Clean up position from resolved market"""
        try:
            # Remove from trader's active positions
            self.trader.remove_position(token_id)
            logger.info("  -> Redeem winnings from Polymarket or wait for auto-redeem")
            
            # Clean up our tracking
            self.remove_stop_loss(token_id)
            
        except Exception as e:
            logger.error(f"Error cleaning up resolved position: {e}")
    
    def check_take_profit(
        self,
        token_id: str,
        current_price: float,
        take_profit_price: float = 0.99
    ) -> bool:
        """
        Check if position has reached take profit target
        
        Note: Usually better to hold until resolution for $1.00/share
        """
        position = self.trader.get_position(token_id)
        if not position:
            return False
        
        if current_price >= take_profit_price:
            shares = position.get('shares', position.get('size', 0))
            
            logger.info(f"TAKE PROFIT at ${current_price:.3f}")
            
            order = self.trader.place_sell_order(
                token_id=token_id,
                price=current_price,
                size=shares,
                order_type="FOK"
            )
            
            if order:
                profit = (current_price - position['entry_price']) * shares
                logger.info(f"TAKE PROFIT EXECUTED | Profit: ${profit:+.2f}")
                self.remove_stop_loss(token_id)
                return True
        
        return False
    
    def can_open_new_position(self) -> bool:
        """Check if we can open a new position"""
        current_positions = len(self.trader.get_all_positions())
        
        if current_positions >= MAX_CONCURRENT_POSITIONS:
            logger.debug(f"Max positions ({current_positions}/{MAX_CONCURRENT_POSITIONS})")
            return False
        
        return True
    
    def get_position_summary(self) -> str:
        """Get summary of all positions"""
        positions = self.trader.get_all_positions()
        
        if not positions:
            return ""
        
        lines = [f"Positions ({len(positions)}):"]
        
        for token_id, pos in positions.items():
            side = pos['side']
            entry = pos['entry_price']
            shares = pos.get('shares', pos.get('size', 0))
            stop = self.stop_losses.get(token_id, STOP_LOSS_PRICE)
            
            lines.append(f"  {side.upper()} {shares:.2f}@${entry:.2f} | Stop: ${stop:.2f}")
        
        return " | ".join(lines)

