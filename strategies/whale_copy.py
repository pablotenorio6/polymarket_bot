"""
Whale Copy Strategy - Copy trades from suspected market manipulators

This strategy monitors target wallet(s) for large buys in 15-minute crypto markets
and copies their position when detected.

Theory: Certain whales may manipulate the underlying crypto price to win
these short-term prediction markets. By copying their positions early,
we can profit from their manipulation.

Configuration:
    - Set WHALE_TARGET_WALLETS env var to configure which wallets to monitor
    - Use comma-separated addresses for multiple whales:
      WHALE_TARGET_WALLETS=0x123...,0x456...,0x789...
    - If not set, uses default wallets defined in the strategy

Usage: python main.py --market btc --mode trade --strategy whale_copy
"""

import logging
import asyncio
import httpx
import os
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from dataclasses import dataclass
import pytz

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


@dataclass
class WhaleActivity:
    """Represents detected whale activity"""
    timestamp: datetime
    market_slug: str
    side: str           # 'up' or 'down'
    outcome: str        # 'Up' or 'Down'
    total_buys: float   # Total $ bought
    total_sells: float  # Total $ sold
    token_id: str
    num_trades: int
    is_accumulating: bool  # True if buying > selling


class WhaleCopyStrategy(BaseStrategy):
    """
    Copy trades from suspected market manipulators.
    
    Monitors target wallet(s) for:
    - Large buys (>$X) in 15-minute crypto markets
    - Concentration in one direction (Up or Down)
    
    When detected, copies the position with configurable size.
    """
    
    name = "whale_copy"
    description = "Copy whale trades in 15min crypto markets"
    
    # This strategy doesn't need real-time price feeds or data collection
    # It only polls the whale's activity via HTTP
    requires_price_websocket = False
    requires_data_collector = False
    requires_rtds = False
    
    # ===== CONFIGURATION =====
    
    # Target wallets to monitor - can be set via WHALE_TARGET_WALLETS env var
    # Use comma-separated addresses for multiple whales
    # Example: WHALE_TARGET_WALLETS=0x123...,0x456...,0x789...
    DEFAULT_TARGET_WALLETS = [
        "0x506bce138df20695c03cd5a59a937499fb00b0fe",  # a4385
        "0xa5e83423126dbc6cdb34f10f37f5d27668ab95f5",  # hai15617
    ]
    
    # Crypto market identifiers (slugs containing these are crypto markets)
    CRYPTO_SLUGS = ['btc', 'eth', 'sol', 'xrp', 'bitcoin', 'ethereum', 'solana']
    # Detection thresholds
    MIN_WHALE_BUY = 500          # Minimum $ to consider a "whale buy"
    
    # Copy parameters (fixed size)
    COPY_SIZE = 50               # Fixed $ amount to copy per detection
    
    # Timing
    POLL_INTERVAL = 2            # Check whale activity every N seconds
    
    async def initialize(self) -> None:
        """Initialize strategy state"""
        logger.info(f"Initializing {self.name} strategy...")
        
        # Target wallet(s) to monitor - from env var or defaults
        env_wallets = os.environ.get('WHALE_TARGET_WALLETS', '')
        if env_wallets:
            self.target_wallets = [w.strip() for w in env_wallets.split(',') if w.strip()]
        else:
            self.target_wallets = self.DEFAULT_TARGET_WALLETS.copy()
        
        # Track our positions in MULTIPLE markets simultaneously
        # Dict keyed by market slug: {slug: {side, size, token_id, up_token_id, down_token_id, condition_id}}
        self.positions: Dict[str, Dict] = {}
        
        # Track logged positions to avoid duplicate "new position detected" logs
        self.logged_positions: set = set()  # (slug, direction) tuples we've already logged
        
        # Cooldown after selling to prevent buy/sell loops
        # Dict: slug -> datetime of last sell
        self.sell_cooldowns: Dict[str, datetime] = {}
        self.SELL_COOLDOWN_SECONDS = 10  # Wait 60s after selling before buying same market again
        
        # HTTP client for API calls
        self.http_client = httpx.AsyncClient(timeout=10.0)
        
        # Background task for monitoring
        self._monitor_task: Optional[asyncio.Task] = None
        
        logger.info(f"Whale copy: {len(self.target_wallets)} wallet(s), min ${self.MIN_WHALE_BUY}, copy ${self.COPY_SIZE}")
        
        # Start whale monitor immediately - it will find active markets on its own
        self._monitor_task = asyncio.create_task(self._whale_monitor_loop())
    
    async def on_new_market(self, market_data: Dict) -> None:
        """
        Called when new/future market detected.
        
        For whale_copy, we don't need to track individual markets -
        the monitor loop finds active markets automatically.
        """
        # Just mark as processed to avoid repeated calls
        condition_id = market_data['condition_id']
        self.mark_market_processed(condition_id)
    
    async def on_market_active(self, market_data: Dict) -> None:
        """
        Called when bot joins an already active market.
        
        For whale_copy, the monitor loop handles everything automatically.
        """
        pass
    
    async def _whale_monitor_loop(self) -> None:
        """Background loop to monitor whale positions in ANY crypto market"""
        logger.info("Whale monitor started")
        
        while True:
            try:
                # Get whale's current positions
                positions = await self._get_whale_positions()
                
                # Filter to positions in markets that are CURRENTLY ACTIVE
                active_positions = []
                for pos in positions:
                    title = pos.get('title', '')
                    end_date = pos.get('endDate', '')
                    
                    if self._is_market_active_now(title, end_date):
                        active_positions.append(pos)
                
                # Log NEW whale positions (once per position)
                self._log_new_positions(active_positions)
                
                # === COPY LOGIC ===
                # Try to copy ALL markets that meet threshold (multiple markets supported)
                if active_positions:
                    await self._try_copy_whale_positions(active_positions)
                
                # Check exit conditions for ALL our positions
                if self.positions:
                    await self._check_all_positions_exit(positions)
                
                await asyncio.sleep(self.POLL_INTERVAL)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Whale monitor error: {e}")
                await asyncio.sleep(self.POLL_INTERVAL)
    
    def _reset_position(self, slug: str) -> None:
        """Reset position tracking for a specific market"""
        if slug in self.positions:
            del self.positions[slug]
            logger.debug(f"Position reset for {slug}")
    
    def _is_crypto_market(self, slug: str) -> bool:
        """Check if slug is a crypto market (BTC, ETH, SOL, XRP)"""
        slug_lower = slug.lower()
        return any(crypto in slug_lower for crypto in self.CRYPTO_SLUGS)
    
    def _calculate_net_exposures(self, positions: List[Dict]) -> Dict[str, Dict]:
        """
        Calculate NET exposure per market by grouping UP and DOWN positions.
        
        For each market, calculates:
        - up_value: total value in UP positions
        - down_value: total value in DOWN positions  
        - net_value: SIGNED value (up - down). Positive = UP, Negative = DOWN
        - up_token_id, down_token_id: token IDs for each side
        - representative_pos: a position dict to get metadata (title, etc)
        
        Returns dict keyed by market slug.
        """
        market_exposures = {}
        
        for pos in positions:
            slug = pos.get('slug', '')
            outcome = pos.get('outcome', '').lower()  # 'up' or 'down'
            current_value = float(pos.get('currentValue', 0))
            total_bought = float(pos.get('totalBought', 0))
            token_id = pos.get('asset', '')
            
            if slug not in market_exposures:
                market_exposures[slug] = {
                    'up_value': 0.0,
                    'down_value': 0.0,
                    'up_bought': 0.0,
                    'down_bought': 0.0,
                    'up_token_id': None,
                    'down_token_id': None,
                    'up_price': 0.0,
                    'down_price': 0.0,
                    'representative_pos': pos,
                    'condition_id': pos.get('conditionId', ''),
                }
            
            m = market_exposures[slug]
            
            if outcome == 'up':
                m['up_value'] += current_value
                m['up_bought'] += total_bought
                m['up_token_id'] = token_id
                m['up_price'] = float(pos.get('curPrice', 0))
            elif outcome == 'down':
                m['down_value'] += current_value
                m['down_bought'] += total_bought
                m['down_token_id'] = token_id
                m['down_price'] = float(pos.get('curPrice', 0))
        
        # Calculate SIGNED net exposure for each market
        # Positive = UP conviction, Negative = DOWN conviction
        for slug, m in market_exposures.items():
            m['net_value'] = m['up_value'] - m['down_value']  # SIGNED, not abs()
            m['slug'] = slug
        
        return market_exposures
    
    def _is_market_active_now(self, title: str, end_date: str) -> bool:
        """
        Check if a market is currently in its active trading period.
        
        Parses the title to extract time window (e.g., "3:15-3:30AM ET")
        and checks if current time is within that window.
        
        Args:
            title: Market title like "Bitcoin Up or Down - January 18, 3:15-3:30AM ET"
            end_date: End date string like "2026-01-18"
        
        Returns:
            True if we're currently within the market's active period
        """
        import re
        
        try:
            et_tz = pytz.timezone('US/Eastern')
            now_et = datetime.now(et_tz)
            
            # Parse end_date for the market date
            # Handle different date formats
            if 'T' in end_date:
                # Full timestamp format: "2026-01-22T21:00:00Z"
                market_date = datetime.fromisoformat(end_date.replace('Z', '+00:00')).date()
            else:
                # Simple date format: "2026-01-22"
                market_date = datetime.strptime(end_date, "%Y-%m-%d").date()
            
            # Extract time range from title using multiple regex patterns
            # Format 1: "3:45PM-4:00PM" (both times have AM/PM)
            # Format 2: "3:15-3:30AM" (only end has AM/PM)
            # Format 3: "12AM" (hourly market)
            
            start_hour = None
            start_min = 0
            end_hour = None
            end_min = 0
            ampm = None
            
            # Try format: "H:MMAM/PM-H:MMAM/PM" (e.g., "3:45PM-4:00PM")
            full_pattern = r'(\d{1,2}):(\d{2})(AM|PM)-(\d{1,2}):(\d{2})(AM|PM)'
            full_match = re.search(full_pattern, title, re.IGNORECASE)
            
            if full_match:
                start_hour = int(full_match.group(1))
                start_min = int(full_match.group(2))
                start_ampm = full_match.group(3).upper()
                end_hour = int(full_match.group(4))
                end_min = int(full_match.group(5))
                end_ampm = full_match.group(6).upper()
                
                # Convert to 24h
                if start_ampm == 'PM' and start_hour != 12:
                    start_hour += 12
                elif start_ampm == 'AM' and start_hour == 12:
                    start_hour = 0
                if end_ampm == 'PM' and end_hour != 12:
                    end_hour += 12
                elif end_ampm == 'AM' and end_hour == 12:
                    end_hour = 0
            else:
                # Try format: "H:MM-H:MMAM/PM" (e.g., "3:15-3:30AM")
                range_pattern = r'(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})(AM|PM)'
                range_match = re.search(range_pattern, title, re.IGNORECASE)
                
                if range_match:
                    start_hour = int(range_match.group(1))
                    start_min = int(range_match.group(2))
                    end_hour = int(range_match.group(3))
                    end_min = int(range_match.group(4))
                    ampm = range_match.group(5).upper()
                    
                    # Convert to 24h (both use same AM/PM)
                    if ampm == 'PM' and start_hour != 12:
                        start_hour += 12
                    elif ampm == 'AM' and start_hour == 12:
                        start_hour = 0
                    if ampm == 'PM' and end_hour != 12:
                        end_hour += 12
                    elif ampm == 'AM' and end_hour == 12:
                        end_hour = 0
                else:
                    # Try hourly format: "12AM ET" or "3PM ET"
                    hourly_pattern = r'(\d{1,2})(AM|PM)\s+ET'
                    hourly_match = re.search(hourly_pattern, title, re.IGNORECASE)
                    
                    if hourly_match:
                        start_hour = int(hourly_match.group(1))
                        ampm = hourly_match.group(2).upper()
                        
                        if ampm == 'PM' and start_hour != 12:
                            start_hour += 12
                        elif ampm == 'AM' and start_hour == 12:
                            start_hour = 0
                        
                        # Hourly markets: full hour
                        end_hour = start_hour + 1
                        end_min = 0
                        start_min = 0
                    else:
                        # Can't parse - assume market IS active (safer)
                        logger.debug(f"Can't parse time from title: {title}")
                        return True
            
            if start_hour is None:
                return True  # Can't parse, assume active
            
            # Build datetime objects
            start_time = datetime(market_date.year, market_date.month, market_date.day, start_hour, start_min)
            end_time = datetime(market_date.year, market_date.month, market_date.day, end_hour, end_min)
            
            # Localize to ET
            start_time = et_tz.localize(start_time)
            end_time = et_tz.localize(end_time)
            
            # Check if current time is within window
            return start_time <= now_et < end_time
            
        except Exception as e:
            logger.warning(f"Error parsing market time: {e} | Title: {title} | end_date: {end_date}")
            # If we can't parse, assume market IS active (safer - don't reset positions)
            return True
    
    async def _get_whale_positions(self) -> List[Dict]:
        """
        Fetch positions from ALL monitored whales using /positions endpoint.
        
        Returns list of ACTIVE crypto positions (curPrice > 0) from all wallets.
        Each position includes 'wallet' field to identify which whale it belongs to.
        """
        all_active_crypto = []
        
        try:
            for wallet in self.target_wallets:
                url = f"https://data-api.polymarket.com/positions?user={wallet}"
                response = await self.http_client.get(url)
                
                if response.status_code != 200:
                    logger.debug(f"Failed to fetch positions for {wallet[:10]}...")
                    continue
                
                all_positions = response.json()
                
                # Filter to active crypto positions only
                for pos in all_positions:
                    slug = pos.get('slug', '')
                    cur_price = float(pos.get('curPrice', 0))
                    size = float(pos.get('size', 0))
                    
                    # Only active positions (price > 0) in crypto markets
                    if cur_price > 0 and size > 0 and self._is_crypto_market(slug):
                        # Add wallet identifier to position
                        pos['wallet'] = wallet
                        all_active_crypto.append(pos)
            
            return all_active_crypto
            
        except Exception as e:
            logger.error(f"Error fetching whale positions: {e}")
            return all_active_crypto
    
    async def _get_actual_balance_for_sell(self, token_id: str) -> float:
        """
        Get ACTUAL balance for selling.
        
        Note: Fees are paid in USDC, not shares (per Polymarket docs).
        Fallbacks use 3% safety margin to avoid "not enough balance" errors.
        
        Priority for sells:
        1. API query (most accurate - on-chain balance)
        2. Trader's active_positions (with 3% safety margin)
        3. Our tracked positions (with 3% safety margin)
        """
        # 1. Query API first - shows ACTUAL on-chain balance
        try:
            our_wallet = self.trader.funder_address or self.trader.signer_address
            if our_wallet:
                url = f"https://data-api.polymarket.com/positions?user={our_wallet}"
                response = await self.http_client.get(url)
                
                if response.status_code == 200:
                    positions = response.json()
                    for pos in positions:
                        if pos.get('asset') == token_id:
                            size = float(pos.get('size', 0))
                            if size > 0:
                                logger.info(f"[SELL BALANCE] {token_id[:10]}... = {size:.4f} (from API)")
                                return size
            
        except Exception as e:
            logger.warning(f"API position query failed: {e}")
        
        # 2. Fallback: use trader's tracked shares with 3% safety margin
        if hasattr(self.trader, 'active_positions') and token_id in self.trader.active_positions:
            tracked = self.trader.active_positions[token_id].get('shares', 0)
            if tracked > 0:
                safe_amount = tracked * 0.968  # 3% safety margin
                logger.warning(f"[SELL BALANCE] {token_id[:10]}... = {safe_amount:.2f} (from trader, -3%)")
                return safe_amount
        
        # 3. Last fallback: our tracked positions with 3% safety margin
        for slug, pos in self.positions.items():
            if pos.get('token_id') == token_id:
                size = pos.get('size', 0) * 0.968  # 3% safety margin
                logger.warning(f"[SELL BALANCE] {token_id[:10]}... = {size:.2f} (from tracked, -3%)")
                return size
        
        logger.debug(f"[SELL BALANCE] {token_id[:10]}... = 0 (not found)")
        return 0.0
    
    async def _get_our_token_balance(self, token_id: str) -> float:
        """
        Get our token balance for checking if we have a position.
        
        Note: For SELLING, use _get_actual_balance_for_sell() instead,
        as this function returns gross amounts that don't account for fees.
        
        Priority:
        1. Trader's active_positions (immediate)
        2. API query (accurate but may have delay)
        3. Our tracked size (fallback)
        """
        # 1. Check trader's local tracking
        if hasattr(self.trader, 'active_positions') and token_id in self.trader.active_positions:
            pos = self.trader.active_positions[token_id]
            shares = pos.get('shares', 0)
            if shares > 0:
                logger.debug(f"[BALANCE] {token_id[:10]}... = {shares:.2f} (from trader)")
                return shares
        
        # 2. Query API
        try:
            our_wallet = self.trader.funder_address or self.trader.signer_address
            if our_wallet:
                url = f"https://data-api.polymarket.com/positions?user={our_wallet}"
                response = await self.http_client.get(url)
                
                if response.status_code == 200:
                    positions = response.json()
                    for pos in positions:
                        if pos.get('asset') == token_id:
                            size = float(pos.get('size', 0))
                            if size > 0:
                                logger.debug(f"[BALANCE] {token_id[:10]}... = {size:.2f} (from API)")
                                return size
            
        except Exception as e:
            logger.debug(f"API position query failed: {e}")
        
        # 3. Fallback: check our tracked positions
        for slug, pos in self.positions.items():
            if pos.get('token_id') == token_id:
                size = pos.get('size', 0)
                logger.debug(f"[BALANCE] {token_id[:10]}... = {size:.2f} (from tracked)")
                return size
        
        logger.debug(f"[BALANCE] {token_id[:10]}... = 0 (not found)")
        return 0.0
    
    def _log_new_positions(self, active_positions: List[Dict]) -> None:
        """Log NEW whale positions in active markets (once per position)"""
        if not active_positions:
            return
        
        # Calculate NET exposures per market (signed)
        net_exposures = self._calculate_net_exposures(active_positions)
        
        for slug, exp in net_exposures.items():
            net_value = exp['net_value']  # SIGNED: positive=UP, negative=DOWN
            
            # Determine direction and check threshold
            if net_value >= self.MIN_WHALE_BUY:
                direction = 'up'
                display_value = net_value
            elif net_value <= -self.MIN_WHALE_BUY:
                direction = 'down'
                display_value = abs(net_value)
            else:
                # Below threshold, skip
                continue
            
            # Create unique key for this position
            position_key = (slug, direction)
            
            # Only log if we haven't seen this position before
            if position_key not in self.logged_positions:
                self.logged_positions.add(position_key)
                
                title = exp['representative_pos'].get('title', '')[:60]
                logger.info(
                    f"[WHALE] New position: {direction.upper()} ${display_value:,.0f} net | {title}"
                )
    
    async def _try_copy_whale_positions(self, active_positions: List[Dict]) -> None:
        """
        Try to copy whale positions in ALL active markets that meet threshold.
        
        Uses SIGNED net_value:
        - net_value >= +MIN_WHALE_BUY: buy UP
        - net_value <= -MIN_WHALE_BUY: buy DOWN
        - Otherwise: skip (no conviction)
        """
        # Calculate net exposures per market
        net_exposures = self._calculate_net_exposures(active_positions)
        
        for slug, exposure in net_exposures.items():
            # Skip markets where we already have a position
            if slug in self.positions:
                continue
            
            # Check if market is still active (not ended)
            rep_pos = exposure['representative_pos']
            title = rep_pos.get('title', '')
            end_date = rep_pos.get('endDate', '')
            if not self._is_market_active_now(title, end_date):
                continue  # Market ended, don't buy
            
            # Check cooldown - don't buy if we recently sold this market
            if slug in self.sell_cooldowns:
                cooldown_end = self.sell_cooldowns[slug] + timedelta(seconds=self.SELL_COOLDOWN_SECONDS)
                if datetime.now() < cooldown_end:
                    remaining = (cooldown_end - datetime.now()).seconds
                    logger.debug(f"[COOLDOWN] {slug}: {remaining}s remaining")
                    continue
                else:
                    # Cooldown expired, remove it
                    del self.sell_cooldowns[slug]
            
            net_value = exposure['net_value']  # SIGNED: positive=UP, negative=DOWN
            
            # Determine side based on signed net_value
            if net_value >= self.MIN_WHALE_BUY:
                side = 'up'
            elif net_value <= -self.MIN_WHALE_BUY:
                side = 'down'
            else:
                # No clear conviction, skip
                continue
            
            # Extract position details
            token_id = exposure['up_token_id'] if side == 'up' else exposure['down_token_id']
            cur_price = exposure['up_price'] if side == 'up' else exposure['down_price']
            
            # Validate token_id exists
            if not token_id:
                logger.warning(f"[SKIP] {slug}: No token_id for {side.upper()}")
                continue
            
            try:
                limit_price = min(0.9, cur_price + 0.05)
                
                market_info = {
                    'slug': slug,
                    'condition_id': exposure['condition_id'],
                    'up_token_id': exposure['up_token_id'],
                    'down_token_id': exposure['down_token_id'],
                }
                
                # Log whale position before buying
                net_value = exposure['net_value']
                whale_dir = 'UP' if net_value > 0 else 'DOWN'
                logger.info(f"[WHALE] {whale_dir} ${abs(net_value):,.0f} net | {title}")
                
                # Use FOK (Fill or Kill) with high price to ensure immediate fill
                market_price = 0.99  # High price to guarantee fill
                
                order = self.trader.place_buy_order(
                    token_id=token_id,
                    side=side,
                    price=market_price,
                    size=self.COPY_SIZE,
                    market_info=market_info,
                    order_type="FOK"  # Fill or Kill - immediate execution
                )
                
                if order:
                    # Get actual shares from trader's active_positions
                    actual_shares = self.COPY_SIZE  # default
                    if hasattr(self.trader, 'active_positions') and token_id in self.trader.active_positions:
                            actual_shares = self.trader.active_positions[token_id].get('shares', self.COPY_SIZE)
                    
                    logger.info(f"[BUY] {side.upper()} {actual_shares:.2f} @ market | {title}")
                    
                    self.positions[slug] = {
                        'side': side,
                        'size': actual_shares,
                        'token_id': token_id,
                        'up_token_id': exposure['up_token_id'],
                        'down_token_id': exposure['down_token_id'],
                        'condition_id': exposure['condition_id'],
                        'title': title,  # Store for market active check
                        'end_date': end_date,  # Store for market active check
                    }
                else:
                    logger.warning(f"[BUY FAILED] {side.upper()} {self.COPY_SIZE} | {title}")
                    
            except Exception as e:
                logger.error(f"BUY error ({slug}): {e}")
    
    async def _check_all_positions_exit(self, all_positions: List[Dict]) -> None:
        """
        Check exit conditions for ALL our positions.
        
        Uses SIGNED net_value:
        - Positive = whale bullish (UP)
        - Negative = whale bearish (DOWN)
        
        Exit conditions:
        - Market period ended: reset
        - Whale exited: sell
        - Conviction dropped: sell (net_value crossed threshold)
        """
        if not self.positions:
            return
        
        # Calculate net exposures for all markets
        net_exposures = self._calculate_net_exposures(all_positions)
        
        # Check each of our positions
        # Use list() to avoid "dictionary changed size during iteration"
        for slug in list(self.positions.keys()):
            pos = self.positions[slug]
            our_side = pos['side']
            
            # Check if market is still active using STORED values (not whale positions)
            title = pos.get('title', '')
            end_date = pos.get('end_date', '')
            
            if not self._is_market_active_now(title, end_date):
                logger.info(f"[END] {slug}: Market ended | {our_side.upper()} {pos['size']}")
                self._reset_position(slug)
                continue
            
            # Check whale's exposure in this market
            if slug not in net_exposures:
                # Whale has no positions - sell
                await self._sell_position(slug, "whale exited")
                continue
            
            exposure = net_exposures[slug]
            net_value = exposure['net_value']  # SIGNED: positive=UP, negative=DOWN
            
            # Unified exit logic using signed net_value:
            # - UP position: need net_value >= +threshold to hold
            # - DOWN position: need net_value <= -threshold to hold
            should_sell = False
            reason = ""
            
            if our_side == 'up':
                if net_value < self.MIN_WHALE_BUY:
                    should_sell = True
                    reason = f"net ${net_value:.0f} < +${self.MIN_WHALE_BUY}"
            else:  # our_side == 'down'
                if net_value > -self.MIN_WHALE_BUY:
                    should_sell = True
                    reason = f"net ${net_value:.0f} > -${self.MIN_WHALE_BUY}"
            
            if should_sell:
                # Log whale's current position before selling
                whale_direction = 'UP' if net_value > 0 else 'DOWN'
                title = exposure['representative_pos'].get('title', '')[:60]
                logger.info(f"[WHALE] Position changed: {whale_direction} ${abs(net_value):,.0f} net | {title}")
                
                await self._sell_position(slug, reason, apply_cooldown=False)
                # No cooldown - if whale now has conviction in opposite direction,
                # next loop will buy it
            
            # Otherwise: hold (do nothing)
    
    async def _sell_position(self, slug: str, reason: str, apply_cooldown: bool = True) -> None:
        """
        Sell our position in a specific market.
        
        Queries actual token balance before selling to account for fees.
        
        Args:
            slug: Market slug to sell
            reason: Reason for selling (for logging)
            apply_cooldown: If True, set cooldown to prevent immediate re-buy.
                           Set to False when selling due to direction change,
                           so next loop can buy the new direction.
        """
        if slug not in self.positions:
            return
        
        pos = self.positions[slug]
        token_id = pos['token_id']
        tracked_balance = pos.get('size', 0)
        
        # Get ACTUAL on-chain balance (API first, accounts for fees)
        actual_balance = await self._get_actual_balance_for_sell(token_id)
        
        # Use actual balance from API
        sell_size = actual_balance if actual_balance > 0 else (tracked_balance * 0.97)
        
        # Log for debugging
        logger.info(f"[SELL ATTEMPT] {pos['side'].upper()} | api_balance={actual_balance:.2f}, tracked={tracked_balance:.2f}, selling={sell_size:.2f}")
        
        if sell_size <= 0:
            logger.warning(f"[SELL SKIP] No balance for {pos['side'].upper()} | {slug[:30]}")
            return
        
        try:
            sell_order = self.trader.place_sell_order(
                token_id=token_id,
                price=0.01,  # Very low price to ensure fill (market sell)
                size=sell_size,
                order_type="GTC"  # GTC more reliable than FOK for sells
            )
            
            if sell_order:
                logger.info(f"[SELL] {pos['side'].upper()} {sell_size:.2f} | {slug[:30]} | {reason}")
                # Set cooldown only if requested (not for direction changes)
                if apply_cooldown:
                    self.sell_cooldowns[slug] = datetime.now()
                # Only reset position if sell succeeded
                self._reset_position(slug)
            else:
                # SELL FAILED - keep position tracked to prevent buying opposite side!
                logger.warning(f"[SELL FAILED] {pos['side'].upper()} {sell_size:.2f} | {slug[:30]} - keeping position")
                
        except Exception as e:
            logger.error(f"SELL error ({slug}): {e}")
            # Don't reset on error - we still hold the tokens
    
    
    async def _get_whale_activity(self) -> tuple[Optional[WhaleActivity], List[str]]:
        """
        Fetch and analyze recent activity from ALL monitored whales in crypto markets.
        
        Aggregates activity from all wallets.
        Returns (WhaleActivity, list of market slugs with activity).
        """
        # Aggregate activity from all wallets
        market_activity = {}
        now = datetime.now(pytz.UTC)
        
        try:
            for wallet in self.target_wallets:
                url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=100"
                response = await self.http_client.get(url)
                
                if response.status_code != 200:
                    continue
                
                activities = response.json()
                
                for act in activities:
                    slug = act.get('slug', '')
                    
                    # Only consider crypto markets (BTC, ETH, SOL, XRP)
                    if not self._is_crypto_market(slug):
                        continue
                    
                    # Only count TRADE type
                    if act.get('type') != 'TRADE':
                        continue
                    
                    # Initialize market entry
                    if slug not in market_activity:
                        market_activity[slug] = {
                            'up_buys': 0.0, 'up_sells': 0.0,
                            'down_buys': 0.0, 'down_sells': 0.0,
                            'up_token': None, 'down_token': None,
                            'trade_count': 0
                        }
                    
                    usdc = float(act.get('usdcSize', 0))
                    outcome = act.get('outcome', '')
                    side = act.get('side', '')
                    
                    m = market_activity[slug]
                    
                    if outcome == 'Up':
                        m['up_token'] = act.get('asset')
                        if side == 'BUY':
                            m['up_buys'] += usdc
                        else:
                            m['up_sells'] += usdc
                        m['trade_count'] += 1
                    elif outcome == 'Down':
                        m['down_token'] = act.get('asset')
                        if side == 'BUY':
                            m['down_buys'] += usdc
                        else:
                            m['down_sells'] += usdc
                        m['trade_count'] += 1
            
            # List of markets with activity (for logging)
            active_markets = list(market_activity.keys())
            
            # Find the most significant BUY activity (prioritize new positions)
            best_activity = None
            best_buy_volume = 0
            
            for slug, m in market_activity.items():
                # Check UP buys
                if m['up_buys'] >= self.MIN_WHALE_BUY:
                    if m['up_buys'] > best_buy_volume:
                        best_buy_volume = m['up_buys']
                        best_activity = WhaleActivity(
                            timestamp=now,
                            market_slug=slug,
                            side='up',
                            outcome='Up',
                            total_buys=m['up_buys'],
                            total_sells=m['up_sells'],
                            token_id=m['up_token'],
                            num_trades=m['trade_count'],
                            is_accumulating=m['up_buys'] > m['up_sells']
                        )
                
                # Check DOWN buys
                if m['down_buys'] >= self.MIN_WHALE_BUY:
                    if m['down_buys'] > best_buy_volume:
                        best_buy_volume = m['down_buys']
                        best_activity = WhaleActivity(
                            timestamp=now,
                            market_slug=slug,
                            side='down',
                            outcome='Down',
                            total_buys=m['down_buys'],
                            total_sells=m['down_sells'],
                            token_id=m['down_token'],
                            num_trades=m['trade_count'],
                            is_accumulating=m['down_buys'] > m['down_sells']
                        )
            
            # If we have a position, also check for sell signals in that market
            if self.copied_market_slug and self.copied_market_slug in market_activity:
                m = market_activity[self.copied_market_slug]
                if self.copied_side == 'up' and m['up_sells'] > 0:
                    # Whale is selling in our copied market
                    best_activity = WhaleActivity(
                        timestamp=now,
                        market_slug=self.copied_market_slug,
                        side='up',
                        outcome='Up',
                        total_buys=m['up_buys'],
                        total_sells=m['up_sells'],
                        token_id=m['up_token'],
                        num_trades=m['trade_count'],
                        is_accumulating=m['up_buys'] > m['up_sells']
                    )
                elif self.copied_side == 'down' and m['down_sells'] > 0:
                    best_activity = WhaleActivity(
                        timestamp=now,
                        market_slug=self.copied_market_slug,
                        side='down',
                        outcome='Down',
                        total_buys=m['down_buys'],
                        total_sells=m['down_sells'],
                        token_id=m['down_token'],
                        num_trades=m['trade_count'],
                        is_accumulating=m['down_buys'] > m['down_sells']
                    )
            
            return best_activity, active_markets
            
        except Exception as e:
            logger.error(f"Error fetching whale activity: {e}")
            return None, []
    
    async def _handle_whale_activity(self, activity: WhaleActivity) -> None:
        """
        Handle detected whale activity.
        
        Logic:
        - If we haven't copied yet AND whale is accumulating: BUY
        - If we have copied AND whale stops accumulating/sells: SELL
        - Once we've bought in a market, we can only sell (no more buys)
        """
        
        # CASE 1: We haven't copied yet - consider buying
        if self.copied_side is None:
            # Only buy if whale is accumulating (buying > selling)
            if not activity.is_accumulating:
                return
            
            # Only buy if whale has significant position
            if activity.total_buys < self.MIN_WHALE_BUY:
                return
            
            # BUY - copy the whale
            copy_size = self.COPY_SIZE
            
            logger.info("=" * 60)
            logger.info(f" WHALE DETECTED - COPYING!")
            logger.info(f"  Market: {activity.market_slug}")
            logger.info(f"  Direction: {activity.outcome}")
            logger.info(f"  Whale bought: ${activity.total_buys:,.0f} ({activity.num_trades} trades)")
            logger.info(f"  Copying: ${copy_size:.2f}")
            logger.info("=" * 60)
            
            try:
                # Aggressive price to ensure fill
                limit_price = 0.70
                
                order = self.place_buy_order(
                    token_id=activity.token_id,
                    side=activity.side,
                    price=limit_price,
                    size=copy_size,
                    order_type="FOK"
                )
                
                if order:
                    logger.info(f" BUY order placed: {activity.side.upper()} ${copy_size:.2f} @ {limit_price}")
                    
                    # Mark that we've copied - no more buys allowed until market ends
                    self.copied_side = activity.side
                    self.copied_size = copy_size
                    self.copied_token_id = activity.token_id
                    self.copied_market_slug = activity.market_slug
                    self.whale_last_buys = activity.total_buys
                else:
                    logger.warning(" Failed to place BUY order")
                    
            except Exception as e:
                logger.error(f"Error placing BUY order: {e}")
        
        # CASE 2: We already copied - monitor for exit signal
        else:
            # Only care about activity in the SAME MARKET we copied
            if activity.market_slug != self.copied_market_slug:
                return
            
            # Only care about activity in our copied direction
            if activity.side != self.copied_side:
                return
            
            # Check if whale is selling or stopped accumulating
            should_sell = False
            reason = ""
            
            # Whale is net selling
            if activity.total_sells > activity.total_buys:
                should_sell = True
                reason = f"whale selling (${activity.total_sells:,.0f} > ${activity.total_buys:,.0f})"
            
            # Whale stopped accumulating (buys dropped significantly)
            elif activity.total_buys < self.whale_last_buys * 0.5:
                should_sell = True
                reason = f"whale stopped accumulating (${activity.total_buys:,.0f} < ${self.whale_last_buys * 0.5:,.0f})"
            
            if should_sell and self.copied_size > 0:
                logger.info("=" * 60)
                logger.info(f" EXIT SIGNAL - SELLING!")
                logger.info(f"  Market: {self.copied_market_slug}")
                logger.info(f"  Reason: {reason}")
                logger.info(f"  Selling: {self.copied_side.upper()} ${self.copied_size:.2f}")
                logger.info("=" * 60)
                
                try:
                    # Sell our position
                    order = self.trader.place_market_sell_order(
                        token_id=self.copied_token_id,
                        size=self.copied_size
                    )
                    
                    if order:
                        logger.info(f" SELL order placed")
                        self.copied_size = 0.0
                    else:
                        logger.warning(" Failed to place SELL order")
                        
                except Exception as e:
                    logger.error(f"Error placing SELL order: {e}")
            
            # Update whale's last known position
            self.whale_last_buys = activity.total_buys
            
            # If we sold, reset position for next opportunity
            if self.copied_size <= 0:
                logger.info(f"Position closed - ready for next opportunity")
                self._reset_position()
    
    async def on_price_update(
        self,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market: Dict
    ) -> None:
        """
        Called on every price update.
        
        We don't react to price changes directly in this strategy -
        all logic is in the whale monitor loop.
        """
        # This strategy doesn't use price updates - everything is handled in _whale_monitor_loop
        pass
    
    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        """Called when a market ends (from main bot framework)"""
        market_slug = market_data.get('slug', '')
        
        # Check if we have a position in this market
        if market_slug in self.positions:
            pos = self.positions[market_slug]
            if winner:
                won = (winner.upper() == pos['side'].upper())
                result = "WON" if won else "LOST"
                logger.info(f"[RESULT] {result} | {pos['side'].upper()} {pos['size']} | {market_slug[:30]}")
            
            # Reset position for this market
            self._reset_position(market_slug)
    
    async def shutdown(self) -> None:
        """Clean up on shutdown"""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        
        await self.http_client.aclose()
        
        if self.positions:
            logger.info(f"[SHUTDOWN] Open positions: {len(self.positions)}")
            for slug, pos in self.positions.items():
                logger.info(f"  {pos['side'].upper()} {pos['size']} | {slug[:40]}")
