"""
Risk management and stop loss logic
"""

import logging
from typing import Dict, List, Optional
from config import STOP_LOSS_PRICE, ENABLE_STOP_LOSS, MAX_CONCURRENT_POSITIONS
from trader import get_trader
from monitor import MarketMonitor

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Manages risk including stop losses and position limits
    """
    
    def __init__(self):
        self.trader = get_trader()
        self.monitor = MarketMonitor()
        self.stop_losses = {}  # token_id -> stop loss price
    
    def set_stop_loss(self, token_id: str, stop_price: float):
        """Set a stop loss for a position"""
        self.stop_losses[token_id] = stop_price
        logger.info(f"Stop loss set for {token_id[:10]}... at ${stop_price:.3f}")
    
    def check_stop_losses(self, current_prices: Dict[str, float]):
        """
        Check if any positions have hit their stop loss
        
        Args:
            current_prices: Dict of token_id -> current_price
        """
        if not ENABLE_STOP_LOSS:
            return
        
        positions = self.trader.get_all_positions()
        
        for token_id, position in positions.items():
            if token_id not in self.stop_losses:
                # Set default stop loss if not set
                self.set_stop_loss(token_id, STOP_LOSS_PRICE)
                continue
            
            current_price = current_prices.get(token_id)
            if current_price is None:
                logger.warning(f"No price data for {token_id[:10]}...")
                continue
            
            stop_price = self.stop_losses[token_id]
            
            # Check if stop loss triggered
            if current_price <= stop_price:
                logger.warning(f"⚠️  STOP LOSS TRIGGERED for {position['side']}")
                logger.warning(f"   Current: ${current_price:.3f} <= Stop: ${stop_price:.3f}")
                
                # Execute stop loss
                self._execute_stop_loss(token_id, position, current_price)
    
    def _execute_stop_loss(self, token_id: str, position: Dict, current_price: float):
        """Execute a stop loss sell order"""
        try:
            size = position['size']
            entry_price = position['entry_price']
            
            logger.info(f"Executing stop loss sell for {size:.2f} shares")
            
            # Place sell order at current market price (or slightly below)
            sell_price = max(current_price - 0.01, 0.01)  # Don't sell below 0.01
            
            order = self.trader.place_sell_order(
                token_id=token_id,
                price=sell_price,
                size=size
            )
            
            if order:
                loss = (sell_price - entry_price) * size
                logger.warning(f"❌ Stop loss executed. Loss: ${loss:.2f}")
                
                # Remove stop loss
                if token_id in self.stop_losses:
                    del self.stop_losses[token_id]
            else:
                logger.error("Failed to execute stop loss order!")
                
        except Exception as e:
            logger.error(f"Error executing stop loss: {e}")
    
    def check_take_profit(
        self,
        token_id: str,
        current_price: float,
        take_profit_price: float = 0.99
    ) -> bool:
        """
        Check if position has reached take profit target (OPTIONAL)
        
        Note: In prediction markets, you can hold until resolution to get $1.00 per share.
        Take profit is only useful if you want to lock in gains early.
        
        Args:
            token_id: Token ID
            current_price: Current price
            take_profit_price: Price to take profit (default 0.99)
        
        Returns:
            True if take profit executed
        """
        position = self.trader.get_position(token_id)
        if not position:
            return False
        
        if current_price >= take_profit_price:
            logger.info(f"🎯 TAKE PROFIT OPPORTUNITY at ${current_price:.3f}")
            logger.info(f"   (Can also hold to resolution for $1.00)")
            
            # Execute sell
            order = self.trader.place_sell_order(
                token_id=token_id,
                price=current_price,
                size=position['size']
            )
            
            if order:
                profit = (current_price - position['entry_price']) * position['size']
                logger.info(f"✅ Take profit executed. Profit: ${profit:+.2f}")
                
                # Remove stop loss
                if token_id in self.stop_losses:
                    del self.stop_losses[token_id]
                
                return True
        
        return False
    
    def can_open_new_position(self) -> bool:
        """Check if we can open a new position based on limits"""
        current_positions = len(self.trader.get_all_positions())
        
        if current_positions >= MAX_CONCURRENT_POSITIONS:
            logger.debug(f"Max positions reached ({current_positions}/{MAX_CONCURRENT_POSITIONS})")
            return False
        
        return True
    
    def get_position_summary(self) -> str:
        """Get a summary of all positions and their P&L"""
        positions = self.trader.get_all_positions()
        
        if not positions:
            return "No open positions"
        
        summary = f"\n{'='*60}\n"
        summary += f"OPEN POSITIONS ({len(positions)}):\n"
        summary += f"{'='*60}\n"
        
        for token_id, pos in positions.items():
            side = pos['side']
            entry = pos['entry_price']
            size = pos['size']
            stop = self.stop_losses.get(token_id, 'Not set')
            
            summary += f"\n  {side} | Entry: ${entry:.3f} | Size: {size:.2f} shares\n"
            summary += f"  Stop Loss: ${stop:.3f}\n" if isinstance(stop, float) else f"  Stop Loss: {stop}\n"
            summary += f"  Market: {pos['market'].get('question', '')[:50]}...\n"
        
        summary += f"{'='*60}\n"
        return summary


# Singleton instance
_risk_manager_instance: Optional[RiskManager] = None


def get_risk_manager() -> RiskManager:
    """Get the singleton risk manager instance"""
    global _risk_manager_instance
    if _risk_manager_instance is None:
        _risk_manager_instance = RiskManager()
    return _risk_manager_instance

