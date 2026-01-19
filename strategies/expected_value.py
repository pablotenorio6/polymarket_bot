"""
Expected Value Strategy

Strategy: Use historical probability tables to find mispriced tokens
and place limit orders when Expected Value is positive.

Logic:
1. Load historical probability tables (minutes_remaining × market_price → real_prob)
2. On each price update, lookup real probability for current state
3. If EV > threshold and sample size > minimum, place limit order
4. Use Kelly Criterion (1/4) for position sizing
5. Place limit orders slightly better than spread to avoid taker fees

Configuration:
- min_edge: Minimum edge percentage to trade (default: 5%)
- min_markets: Minimum unique markets in sample (default: 100)
- kelly_fraction: Fraction of Kelly to use (default: 0.25)
"""

import logging
import csv
from typing import Dict, Optional, Tuple
from datetime import datetime
from pathlib import Path
import pytz

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class ExpectedValueStrategy(BaseStrategy):
    """
    Trade based on historical probability edge.
    
    Uses lookup tables mapping (minutes_remaining, market_price) to real_prob.
    Places limit orders when expected value exceeds threshold.
    """
    
    name = "expected_value"
    description = "Trade mispriced tokens using historical probability tables"
    
    # Default strategy parameters (can be overridden)
    DEFAULT_MIN_EDGE_PCT = 5.0       # Minimum edge % to trade
    DEFAULT_MIN_UNIQUE_MARKETS = 100  # Minimum sample size
    DEFAULT_KELLY_FRACTION = 0.25     # 1/4 Kelly
    DEFAULT_MIN_MINUTES = 2           # Don't trade with < 3 min left
    
    # Order placement
    TICK_SIZE = 0.01          # Minimum price increment
    MIN_ORDER_SIZE = 5.0      # Polymarket minimum order size
    ORDER_COOLDOWN = 5.0      # Seconds between orders for same token
    
    def __init__(
        self,
        *args,
        min_edge_pct: float = None,
        min_unique_markets: int = None,
        kelly_fraction: float = None,
        min_minutes: int = None,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        
        # Strategy parameters (use defaults if not provided)
        self.min_edge_pct = min_edge_pct or self.DEFAULT_MIN_EDGE_PCT
        self.min_unique_markets = min_unique_markets or self.DEFAULT_MIN_UNIQUE_MARKETS
        self.kelly_fraction = kelly_fraction or self.DEFAULT_KELLY_FRACTION
        self.min_minutes = min_minutes or self.DEFAULT_MIN_MINUTES
        
        # Lookup tables: (minutes_remaining, market_price) -> row data
        self.up_table: Dict[Tuple[int, float], Dict] = {}
        self.down_table: Dict[Tuple[int, float], Dict] = {}
        
        # Track POSITION PER SIDE (for delta-neutral strategy)
        # We want to accumulate both UP and DOWN while staying roughly balanced
        self.position_up: float = 0.0    # Total $ in UP tokens
        self.position_down: float = 0.0  # Total $ in DOWN tokens
        
        # Maximum allowed imbalance between sides (as ratio)
        # 1.5 means one side can be at most 1.5x the other
        self.max_imbalance_ratio = 2.0
        
        # Track order history for logging/analysis
        self.order_history: list = []
        
        # Cooldown tracking (avoid spamming orders)
        self.last_order_time: Dict[str, str] = {}  # side -> timestamp
        
        # Market timing
        self.market_end_time: Optional[datetime] = None
    
    async def initialize(self) -> None:
        """Load probability tables from CSV files"""
        logger.info("Loading probability tables...")
        
        # Determine CSV paths based on market symbol
        symbol = self.market_symbol.lower()  # BTC -> btc
        base_path = Path(__file__).parent.parent  # Go up from strategies/
        
        up_csv = base_path / f"{symbol}_up_data_16_01.csv"
        down_csv = base_path / f"{symbol}_down_data_16_01.csv"
        
        # Load UP table
        if up_csv.exists():
            self.up_table = self._load_csv(up_csv)
            logger.info(f"Loaded UP table: {len(self.up_table)} entries")
        else:
            logger.warning(f"UP table not found: {up_csv}")
        
        # Load DOWN table
        if down_csv.exists():
            self.down_table = self._load_csv(down_csv)
            logger.info(f"Loaded DOWN table: {len(self.down_table)} entries")
        else:
            logger.warning(f"DOWN table not found: {down_csv}")
        
        if not self.up_table and not self.down_table:
            logger.error("No probability tables loaded! Strategy will not trade.")
        
        logger.info(f"Strategy params: min_edge={self.min_edge_pct}%, "
                   f"min_markets={self.min_unique_markets}, "
                   f"kelly={self.kelly_fraction}, min_minutes={self.min_minutes}")
    
    def _load_csv(self, path: Path) -> Dict[Tuple[int, float], Dict]:
        """Load CSV into lookup table"""
        table = {}
        
        with open(path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    minutes = int(row['minutes_remaining'])
                    price = float(row['market_price'])
                    
                    table[(minutes, price)] = {
                        'real_prob': float(row['real_prob']),
                        'samples': int(row['samples']),
                        'unique_markets': int(row['unique_markets']),
                        'edge_pct': float(row['edge_pct']),
                    }
                except (ValueError, KeyError) as e:
                    logger.debug(f"Skipping row: {e}")
        
        return table
    
    def _get_minutes_remaining(self, end_time: datetime) -> int:
        """Calculate minutes remaining until market close"""
        now = datetime.now(pytz.UTC)
        
        # Ensure end_time is timezone aware
        if end_time.tzinfo is None:
            end_time = pytz.UTC.localize(end_time)
        
        delta = end_time - now
        minutes = int(delta.total_seconds() / 60)
        
        # Clamp to valid range
        return max(0, min(14, minutes))
    
    def _round_price(self, price: float) -> float:
        """Round price to nearest tick size"""
        return round(price / self.TICK_SIZE) * self.TICK_SIZE
    
    def _lookup_probability(
        self,
        table: Dict,
        minutes: int,
        price: float
    ) -> Optional[Dict]:
        """
        Lookup real probability for given state.
        Returns dict with real_prob, edge_pct, unique_markets, or None.
        """
        rounded_price = self._round_price(price)
        key = (minutes, rounded_price)
        
        return table.get(key)
    
    def _calculate_kelly_size(
        self,
        real_prob: float,
        market_price: float,
        bankroll: float
    ) -> float:
        """
        Calculate position size using Kelly Criterion.
        
        Kelly formula for binary outcome:
        f* = (p * b - q) / b
        where:
            p = probability of winning
            q = probability of losing (1 - p)
            b = odds (payout ratio)
        
        For a binary market at price P:
            - If you buy at P and win, you get 1/P return
            - b = (1 - P) / P  (net profit per dollar risked)
        """
        if market_price <= 0 or market_price >= 1:
            return 0
        
        p = real_prob
        q = 1 - p
        b = (1 - market_price) / market_price  # Odds
        
        # Full Kelly
        kelly = (p * b - q) / b if b > 0 else 0
        
        # Clamp to reasonable range
        kelly = max(0, min(1, kelly))
        
        # Apply fraction (e.g., 1/4 Kelly)
        fractional_kelly = kelly * self.kelly_fraction
        
        # Calculate dollar amount
        size = bankroll * fractional_kelly
        
        return size
    
    async def on_new_market(self, market_data: Dict) -> None:
        """
        Called when new market detected.
        Store market end time for minutes calculation.
        """
        # Just mark as processed - we trade on price updates
        self.mark_market_processed(market_data['condition_id'])
        
        # Store end time
        if 'end_time' in market_data:
            self.market_end_time = market_data['end_time']
            logger.info(f"Market tracked: {market_data['question'][:40]}...")
    
    async def on_market_active(self, market_data: Dict) -> None:
        """Called when market becomes active"""
        if 'end_time' in market_data:
            self.market_end_time = market_data['end_time']
    
    async def on_price_update(
        self,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market: Dict
    ) -> None:
        """
        Main decision logic - check for trading opportunities.
        """
        if not self.market_end_time:
            # Try to get from market data
            end_date = market.get('endDate')
            if end_date:
                from dateutil import parser
                self.market_end_time = parser.parse(end_date)
            else:
                return
        
        # Calculate minutes remaining
        minutes = self._get_minutes_remaining(self.market_end_time)
        
        # Skip if market about to close - not enough time
        if minutes < self.min_minutes:
            return
        
        # Check UP token
        await self._check_opportunity(
            token_id=up_token_id,
            side='up',
            market_price=up_price,
            minutes=minutes,
            table=self.up_table
        )
        
        # Check DOWN token
        await self._check_opportunity(
            token_id=down_token_id,
            side='down',
            market_price=down_price,
            minutes=minutes,
            table=self.down_table
        )
    
    async def _check_opportunity(
        self,
        token_id: str,
        side: str,
        market_price: float,
        minutes: int,
        table: Dict
    ) -> None:
        """
        Check if there's a trading opportunity for this token.
        
        Delta-neutral strategy: accumulate both UP and DOWN positions
        when EV is positive, while maintaining rough balance between sides.
        """
        import time
        
        # Check cooldown (avoid spamming orders for same side)
        last_order = self.last_order_time.get(side, 0)
        if time.time() - last_order < self.ORDER_COOLDOWN:
            return
        
        # Lookup historical data
        data = self._lookup_probability(table, minutes, market_price)
        
        if not data:
            return
        
        real_prob = data['real_prob']
        edge_pct = data['edge_pct']
        unique_markets = data['unique_markets']
        
        # Check filters
        if unique_markets < self.min_unique_markets:
            return
        
        if edge_pct < self.min_edge_pct:
            return
        
        # Calculate OPTIMAL position size using Kelly
        optimal_kelly_size = self._calculate_kelly_size(
            real_prob=real_prob,
            market_price=market_price,
            bankroll=self.position_size
        )
        
        if optimal_kelly_size < self.MIN_ORDER_SIZE:
            return
        
        # Get current positions for both sides
        current_side_position = self.position_up if side == 'up' else self.position_down
        other_side_position = self.position_down if side == 'up' else self.position_up
        
        # Calculate how much we can add while respecting:
        # 1. Kelly limit for this side
        # 2. Imbalance limit between sides
        
        # Kelly limit: don't exceed optimal for this opportunity
        kelly_room = optimal_kelly_size - current_side_position
        
        # Imbalance limit: don't let this side get too far ahead of the other
        # If other side has $10, this side can have at most $10 * max_ratio = $20
        if other_side_position > 0:
            max_for_balance = other_side_position * self.max_imbalance_ratio
            balance_room = max_for_balance - current_side_position
        else:
            # If other side is 0, allow first position up to kelly size
            balance_room = optimal_kelly_size
        
        # Take the more restrictive limit
        available_size = min(kelly_room, balance_room)
        
        # Skip if no room to add
        if available_size < self.MIN_ORDER_SIZE:
            return
        
        # Order size is available room (capped at kelly)
        order_size = min(available_size, optimal_kelly_size)
        
        # Calculate limit price - be a MAKER (avoid taker fees)
        limit_price = self._round_price(market_price)
        
        # Don't place orders at extreme prices
        if limit_price <= 0.01 or limit_price >= 0.99:
            return
        
        # Calculate net exposure after this trade
        new_side_position = current_side_position + order_size
        net_exposure = (self.position_up + (order_size if side == 'up' else 0)) - \
                       (self.position_down + (order_size if side == 'down' else 0))
        
        # Log opportunity
        logger.info(
            f"OPPORTUNITY [{side.upper()}]: "
            f"price={market_price:.2f}, real_prob={real_prob:.3f}, "
            f"edge={edge_pct:.1f}%, min={minutes}"
        )
        logger.info(
            f"  Positions: UP=${self.position_up:.2f}, DOWN=${self.position_down:.2f}, "
            f"net_exposure=${net_exposure:.2f}"
        )
        
        # Place limit order
        order = self.trader.place_buy_order(
            token_id=token_id,
            side=side,
            price=limit_price,
            size=order_size,
            market_info=None,
            order_type="GTC"
        )
        
        if order:
            # Update position for this side
            if side == 'up':
                self.position_up += order_size
            else:
                self.position_down += order_size
            
            self.last_order_time[side] = time.time()
            
            # Calculate new net exposure
            net_exposure = self.position_up - self.position_down
            
            # Log order
            logger.info(
                f"  Order placed: {side.upper()} @ {limit_price:.2f}, "
                f"size=${order_size:.2f}"
            )
            logger.info(
                f"  New positions: UP=${self.position_up:.2f}, "
                f"DOWN=${self.position_down:.2f}, "
                f"NET=${net_exposure:+.2f}"
            )
            
            # Track in history
            self.order_history.append({
                'timestamp': datetime.now(pytz.UTC),
                'token_id': token_id,
                'side': side,
                'price': limit_price,
                'size': order_size,
                'edge_pct': edge_pct,
                'minutes': minutes,
                'position_up': self.position_up,
                'position_down': self.position_down,
                'net_exposure': net_exposure
            })
        else:
            logger.warning(f"  Failed to place {side.upper()} order")
    
    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        """
        Called when market ends.
        Calculate P&L and clear state for next market.
        """
        logger.info(f"=" * 50)
        logger.info(f"MARKET ENDED: winner={winner}")
        
        total_position = self.position_up + self.position_down
        net_exposure = self.position_up - self.position_down
        
        if total_position > 0:
            logger.info(f"  Final positions: UP=${self.position_up:.2f}, DOWN=${self.position_down:.2f}")
            logger.info(f"  Net exposure: ${net_exposure:+.2f}")
            logger.info(f"  Total orders: {len(self.order_history)}")
            
            if winner:
                # Calculate approximate P&L
                # Winner pays out ~1/price, loser pays 0
                # For simplicity, assume average entry price from history
                
                if winner.upper() == 'UP':
                    # UP wins: UP position profits, DOWN position loses
                    # Approximate: UP bought at ~0.5 avg -> pays 2x -> profit = position
                    # DOWN bought at ~0.5 avg -> pays 0 -> loss = position
                    pnl_estimate = self.position_up - self.position_down
                else:
                    # DOWN wins
                    pnl_estimate = self.position_down - self.position_up
                
                logger.info(f"  Estimated P&L: ${pnl_estimate:+.2f}")
                
                # More detailed P&L from order history
                if self.order_history:
                    total_cost = 0
                    total_payout = 0
                    for order in self.order_history:
                        cost = order['size']
                        total_cost += cost
                        if order['side'].upper() == winner.upper():
                            # Winner: payout = size / price
                            payout = order['size'] / order['price']
                            total_payout += payout
                    
                    actual_pnl = total_payout - total_cost
                    logger.info(f"  Actual P&L: ${actual_pnl:+.2f} (cost=${total_cost:.2f}, payout=${total_payout:.2f})")
        
        logger.info(f"=" * 50)
        
        # Clear state for next market
        self.position_up = 0.0
        self.position_down = 0.0
        self.order_history.clear()
        self.last_order_time.clear()
        self.market_end_time = None
    
    async def shutdown(self) -> None:
        """Clean up on shutdown"""
        logger.info(f"Shutting down {self.name} strategy")
        
        total_position = self.position_up + self.position_down
        if total_position > 0:
            net_exposure = self.position_up - self.position_down
            logger.info(f"Positions at shutdown: UP=${self.position_up:.2f}, DOWN=${self.position_down:.2f}")
            logger.info(f"Net exposure: ${net_exposure:+.2f}")
