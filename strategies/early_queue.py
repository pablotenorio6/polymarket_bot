"""
Early Queue Strategy

Strategy: Place GTC (Good Till Cancelled) orders on BOTH UP and DOWN
outcomes as soon as a market is detected, BEFORE it becomes active.

Rationale:
- Polymarket orderbook uses FIFO (First In, First Out) for price ties
- By placing orders 24h before market start, we get queue priority
- Both outcomes get orders since we can't predict direction
- One will fill, one will cancel at resolution

Configuration:
- entry_price: Price for limit orders (default: 0.01 = lowest possible)
- position_size: USD amount per order
"""

import logging
from typing import Dict, Optional
from datetime import datetime
import pytz

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class EarlyQueueStrategy(BaseStrategy):
    """
    Place early limit orders for maximum FIFO priority.
    
    Lifecycle:
    1. initialize(): Recover existing orders from Polymarket API
    2. on_new_market(): Place UP + DOWN orders immediately
    3. on_price_update(): Just record data (orders already placed)
    4. on_market_end(): Orders resolve automatically
    """
    
    name = "early_queue"
    description = "Place GTC orders at market creation for FIFO priority"
    
    async def initialize(self) -> None:
        """Recover state from open orders on Polymarket"""
        logger.info("Recovering orders state from Polymarket...")
        
        try:
            # Get token IDs with open orders
            open_token_ids = self.trader.get_open_order_token_ids()
            
            if not open_token_ids:
                logger.info("No existing open orders found - starting fresh")
                return
            
            logger.info(f"Found {len(open_token_ids)} tokens with open orders")
            
            # Scan future markets to map token_ids -> condition_ids
            future_markets = await self.monitor.get_future_markets(hours_ahead=24)
            
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
            self.processed_markets.update(recovered_markets)
            
            logger.info(f"Recovered {len(recovered_markets)} markets with existing orders")
            logger.info(f"Total markets tracked: {len(self.processed_markets)}")
            
        except Exception as e:
            logger.error(f"Error recovering orders state: {e}")
            logger.info("Continuing with empty state - may place duplicate orders")
    
    async def on_new_market(self, market_data: Dict) -> None:
        """
        Place both UP and DOWN limit orders as soon as market is detected.
        This happens BEFORE the market becomes active, securing FIFO priority.
        """
        condition_id = market_data['condition_id']
        
        # Skip if already processed
        if self.is_market_processed(condition_id):
            return
        
        up_token = market_data['up_token_id']
        down_token = market_data['down_token_id']
        start_time = market_data['start_time']
        market = market_data['market']
        
        # Format start time for logging
        et_tz = pytz.timezone('America/New_York')
        start_et = start_time.astimezone(et_tz)
        time_until = start_time - datetime.now(pytz.UTC)
        hours_until = time_until.total_seconds() / 3600
        
        # Get current crypto price at detection
        crypto_price = self.get_crypto_price()
        
        logger.info(f"NEW MARKET DETECTED: {market_data['question'][:50]}...")
        logger.info(f"  Starts: {start_et.strftime('%Y-%m-%d %H:%M')} ET ({hours_until:.1f}h from now)")
        if crypto_price:
            logger.info(f"  {self.market_symbol} price at detection: ${crypto_price:,.2f}")
        logger.info(f"  Placing orders @ ${self.entry_price:.3f}...")
        
        # Place UP order
        up_order = self.trader.place_buy_order(
            token_id=up_token,
            side='up',
            price=self.entry_price,
            size=self.position_size,
            market_info=market,
            order_type="GTC"
        )
        
        if up_order:
            logger.info(f"  UP order placed - Size: ${self.position_size}")
        else:
            logger.warning(f" Failed to place UP order")
        
        # Place DOWN order
        down_order = self.trader.place_buy_order(
            token_id=down_token,
            side='down',
            price=self.entry_price,
            size=self.position_size,
            market_info=market,
            order_type="GTC"
        )
        
        if down_order:
            logger.info(f"  DOWN order placed - Size: ${self.position_size}")
        else:
            logger.warning(f" Failed to place DOWN order")
        
        # Mark market as processed (even if orders failed, to avoid retry spam)
        self.mark_market_processed(condition_id)
        
        if up_order and down_order:
            logger.info(f"  Both orders queued - MAXIMUM PRIORITY secured!")
        
        # Log total markets with orders
        logger.info(f"  Total markets with orders: {len(self.processed_markets)}")
    
    async def on_price_update(
        self,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market: Dict
    ) -> None:
        """
        Called on each price update.
        
        For early_queue strategy, we just record data.
        Orders are already placed and will resolve automatically.
        """
        # Data recording is handled by the bot
        # No action needed - orders are already in queue
        pass
    
    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        """
        Called when market ends.
        
        For early_queue, positions resolve automatically.
        We could log the outcome here for analysis.
        """
        condition_id = market_data.get('condition_id', 'unknown')[:10]
        question = market_data.get('question', 'Unknown')[:40]
        
        if winner:
            logger.info(f"Market ended: {question}... Winner: {winner}")
        else:
            logger.info(f"Market ended: {question}... (winner unknown)")
