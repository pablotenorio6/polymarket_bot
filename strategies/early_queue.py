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
- cancel_before_end_seconds: Cancel unfilled orders N seconds before market end (default: 10)
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
    3. on_price_update(): Monitor time and cancel unfilled orders before market end
    4. on_market_end(): Orders resolve automatically
    """
    
    name = "early_queue"
    description = "Place GTC orders at market creation for FIFO priority"
    
    # Configuration: seconds before market end to cancel unfilled orders
    cancel_before_end_seconds = 10
    
    async def initialize(self) -> None:
        """Recover state from open orders on Polymarket"""
        logger.info("Recovering orders state from Polymarket...")
        
        # Track active markets with their end times and token IDs
        # condition_id -> {end_time, up_token_id, down_token_id, orders_cancelled, has_up_order, has_down_order}
        self.active_market_info: Dict[str, Dict] = {}
        
        # Track which tokens have open orders (for checking missing orders)
        self.tokens_with_orders: set = set()
        
        try:
            # Get token IDs with open orders
            open_token_ids = self.trader.get_open_order_token_ids()
            self.tokens_with_orders = set(open_token_ids) if open_token_ids else set()
            
            if not open_token_ids:
                logger.info("No existing open orders found - starting fresh")
                return
            
            logger.info(f"Found {len(open_token_ids)} tokens with open orders")
            
            # Scan future markets to map token_ids -> condition_ids
            future_markets = await self.monitor.get_future_markets(hours_ahead=24)
            
            # Also get current active markets
            current_markets = await self.monitor.get_all_market_prices()
            
            # Build token -> condition_id mapping AND store market info
            token_to_condition = {}
            market_info_map = {}  # condition_id -> {end_time, up_token_id, down_token_id}
            
            for market_data in future_markets:
                condition_id = market_data['condition_id']
                token_to_condition[market_data['up_token_id']] = condition_id
                token_to_condition[market_data['down_token_id']] = condition_id
                market_info_map[condition_id] = {
                    'end_time': market_data['end_time'],
                    'up_token_id': market_data['up_token_id'],
                    'down_token_id': market_data['down_token_id']
                }
            
            for price_data in current_markets:
                condition_id = price_data['market'].get('conditionId')
                if condition_id:
                    token_to_condition[price_data['up_token_id']] = condition_id
                    token_to_condition[price_data['down_token_id']] = condition_id
                    # Get end_time from market data if available
                    end_date = price_data['market'].get('endDate')
                    if end_date and condition_id not in market_info_map:
                        from dateutil import parser
                        end_time = parser.parse(end_date)
                        market_info_map[condition_id] = {
                            'end_time': end_time,
                            'up_token_id': price_data['up_token_id'],
                            'down_token_id': price_data['down_token_id']
                        }
            
            # Find condition_ids for our open orders and reconstruct active_market_info
            # Track which specific orders (UP/DOWN) exist for each market
            recovered_markets = set()
            markets_with_both = 0
            markets_missing_orders = 0
            
            for token_id in open_token_ids:
                condition_id = token_to_condition.get(token_id)
                if condition_id:
                    recovered_markets.add(condition_id)
                    # Reconstruct active_market_info for cancellation monitoring
                    if condition_id in market_info_map:
                        if condition_id not in self.active_market_info:
                            self.active_market_info[condition_id] = {
                                'end_time': market_info_map[condition_id]['end_time'],
                                'up_token_id': market_info_map[condition_id]['up_token_id'],
                                'down_token_id': market_info_map[condition_id]['down_token_id'],
                                'orders_cancelled': False,
                                'has_up_order': False,
                                'has_down_order': False
                            }
                        
                        # Mark which order exists
                        if token_id == market_info_map[condition_id]['up_token_id']:
                            self.active_market_info[condition_id]['has_up_order'] = True
                        elif token_id == market_info_map[condition_id]['down_token_id']:
                            self.active_market_info[condition_id]['has_down_order'] = True
            
            # Count markets with both orders vs missing orders
            for condition_id, info in self.active_market_info.items():
                if info.get('has_up_order') and info.get('has_down_order'):
                    markets_with_both += 1
                    # Only mark as fully processed if BOTH orders exist
                    self.processed_markets.add(condition_id)
                else:
                    markets_missing_orders += 1
                    missing = []
                    if not info.get('has_up_order'):
                        missing.append('UP')
                    if not info.get('has_down_order'):
                        missing.append('DOWN')
                    logger.warning(f"Market {condition_id[:8]}... missing orders: {', '.join(missing)}")
            
            logger.info(f"Recovered {len(recovered_markets)} markets with existing orders")
            logger.info(f"  - {markets_with_both} markets have BOTH orders (fully processed)")
            logger.info(f"  - {markets_missing_orders} markets missing orders (will place on next scan)")
            logger.info(f"Reconstructed {len(self.active_market_info)} markets for cancellation monitoring")
            
        except Exception as e:
            logger.error(f"Error recovering orders state: {e}")
            logger.info("Continuing with empty state - may place duplicate orders")
    
    async def on_new_market(self, market_data: Dict) -> None:
        """
        Place both UP and DOWN limit orders as soon as market is detected.
        This happens BEFORE the market becomes active, securing FIFO priority.
        
        Also handles placing missing orders if only one side exists.
        """
        condition_id = market_data['condition_id']
        up_token = market_data['up_token_id']
        down_token = market_data['down_token_id']
        start_time = market_data['start_time']
        market = market_data['market']
        
        # Check if we already have orders for this market
        existing_info = self.active_market_info.get(condition_id, {})
        has_up = existing_info.get('has_up_order', False) or up_token in self.tokens_with_orders
        has_down = existing_info.get('has_down_order', False) or down_token in self.tokens_with_orders
        
        # Skip if both orders already exist
        if has_up and has_down:
            if not self.is_market_processed(condition_id):
                self.mark_market_processed(condition_id)
            return
        
        # Determine what we need to place
        need_up = not has_up
        need_down = not has_down
        is_recovery = has_up or has_down  # True if we're placing missing order(s)
        
        # Format start time for logging
        et_tz = pytz.timezone('America/New_York')
        start_et = start_time.astimezone(et_tz)
        time_until = start_time - datetime.now(pytz.UTC)
        hours_until = time_until.total_seconds() / 3600
        
        # Get current crypto price at detection
        crypto_price = self.get_crypto_price()
        
        if is_recovery:
            missing = []
            if need_up:
                missing.append('UP')
            if need_down:
                missing.append('DOWN')
            logger.info(f"PLACING MISSING ORDER(S): {market_data['question'][:50]}...")
            logger.info(f"  Missing: {', '.join(missing)}")
        else:
            logger.info(f"NEW MARKET DETECTED: {market_data['question'][:50]}...")
        
        logger.info(f"  Starts: {start_et.strftime('%Y-%m-%d %H:%M')} ET ({hours_until:.1f}h from now)")
        if crypto_price:
            logger.info(f"  {self.market_symbol} price at detection: ${crypto_price:,.2f}")
        logger.info(f"  Placing orders @ ${self.entry_price:.3f}...")
        
        up_order = None
        down_order = None
        
        # Place UP order if needed
        if need_up:
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
                self.tokens_with_orders.add(up_token)
                has_up = True
            else:
                logger.warning(f"  Failed to place UP order")
        else:
            logger.info(f"  UP order already exists - skipping")
        
        # Place DOWN order if needed
        if need_down:
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
                self.tokens_with_orders.add(down_token)
                has_down = True
            else:
                logger.warning(f"  Failed to place DOWN order")
        else:
            logger.info(f"  DOWN order already exists - skipping")
        
        # Only mark as fully processed if BOTH orders now exist
        if has_up and has_down:
            self.mark_market_processed(condition_id)
            logger.info(f"  Both orders queued - MAXIMUM PRIORITY secured!")
        else:
            # Don't mark as processed - will retry missing order on next scan
            missing = []
            if not has_up:
                missing.append('UP')
            if not has_down:
                missing.append('DOWN')
            logger.warning(f"  Still missing: {', '.join(missing)} - will retry on next scan")
        
        # Track/update this market for cancellation monitoring
        self.active_market_info[condition_id] = {
            'end_time': market_data['end_time'],
            'up_token_id': up_token,
            'down_token_id': down_token,
            'orders_cancelled': False,
            'has_up_order': has_up,
            'has_down_order': has_down
        }
        
        # Log total markets with orders
        logger.info(f"  Total fully processed markets: {len(self.processed_markets)}")
    
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
        
        For early_queue strategy:
        - Monitor time remaining until market end
        - Cancel unfilled limit orders if less than N seconds remain
        """
        # Find the active market by token IDs
        condition_id = market.get('conditionId')
        
        if not condition_id or condition_id not in self.active_market_info:
            return
        
        market_info = self.active_market_info[condition_id]
        
        # Skip if we already cancelled orders for this market
        if market_info.get('orders_cancelled', False):
            return
        
        # Check time remaining until market end
        end_time = market_info['end_time']
        now = datetime.now(pytz.UTC)
        time_remaining = (end_time - now).total_seconds()
        
        # Cancel unfilled orders if less than N seconds remain
        if time_remaining <= self.cancel_before_end_seconds and time_remaining > 0:
            logger.info(f"[EarlyQueue] {time_remaining:.1f}s until market end - cancelling unfilled orders...")
            
            cancelled = self._cancel_unfilled_orders(up_token_id, down_token_id)
            
            if cancelled:
                logger.info(f"[EarlyQueue] Successfully cancelled {cancelled} unfilled order(s)")
            else:
                logger.info(f"[EarlyQueue] No unfilled orders to cancel (may have been filled)")
            
            # Mark as cancelled so we don't try again
            market_info['orders_cancelled'] = True
    
    def _cancel_unfilled_orders(self, up_token_id: str, down_token_id: str) -> int:
        """
        Cancel any unfilled (LIVE) orders for the given tokens.
        
        Returns:
            Number of orders cancelled
        """
        if not self.trader or not self.trader.client:
            logger.warning("[EarlyQueue] Trader not available for cancellation")
            return 0
        
        cancelled_count = 0
        
        try:
            # Get all open orders
            open_orders = self.trader.get_open_orders()
            
            # Filter orders for our tokens
            orders_to_cancel = []
            for order in open_orders:
                token_id = order.get('token_id')
                if token_id in (up_token_id, down_token_id):
                    order_id = order.get('order_id')
                    if order_id:
                        orders_to_cancel.append(order_id)
            
            if not orders_to_cancel:
                return 0
            
            # Cancel the orders
            logger.info(f"[EarlyQueue] Cancelling {len(orders_to_cancel)} order(s)...")
            
            if len(orders_to_cancel) == 1:
                result = self.trader.client.cancel(orders_to_cancel[0])
            else:
                result = self.trader.client.cancel_orders(orders_to_cancel)
            
            if result:
                cancelled = result.get('canceled', [])
                not_cancelled = result.get('not_canceled', {})
                
                cancelled_count = len(cancelled)
                
                if not_cancelled:
                    logger.warning(f"[EarlyQueue] Some orders not cancelled: {not_cancelled}")
            
        except Exception as e:
            logger.error(f"[EarlyQueue] Error cancelling orders: {e}")
        
        return cancelled_count
    
    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        """
        Called when market ends.
        
        For early_queue, positions resolve automatically.
        We could log the outcome here for analysis.
        """
        condition_id = market_data.get('condition_id', 'unknown')
        question = market_data.get('question', 'Unknown')[:40]
        
        if winner:
            logger.info(f"Market ended: {question}... Winner: {winner}")
        else:
            logger.info(f"Market ended: {question}... (winner unknown)")
        
        # Clean up active market tracking
        if condition_id in self.active_market_info:
            del self.active_market_info[condition_id]
