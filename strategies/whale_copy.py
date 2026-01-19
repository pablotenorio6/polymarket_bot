"""
Whale Copy Strategy - Copy trades from suspected market manipulators

This strategy monitors a target wallet for large buys in 15-minute crypto markets
and copies their position when detected.

Theory: Certain whales may manipulate the underlying crypto price to win
these short-term prediction markets. By copying their positions early,
we can profit from their manipulation.

Usage: python main.py --market btc --mode trade --strategy whale_copy
"""

import logging
import asyncio
import httpx
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
    
    # Target wallet to monitor (a4385, other is hai15617)
    DEFAULT_TARGET_WALLET = "0x506bce138df20695c03cd5a59a937499fb00b0fe"
    
    # Crypto market identifiers (slugs containing these are crypto markets)
    CRYPTO_SLUGS = ['btc', 'eth', 'sol', 'xrp', 'bitcoin', 'ethereum', 'solana']
    
    # Detection thresholds
    MIN_WHALE_BUY = 500          # Minimum $ to consider a "whale buy"
    
    # Copy parameters (fixed size)
    COPY_SIZE = 50               # Fixed $ amount to copy per detection
    
    # Timing
    POLL_INTERVAL = 3            # Check whale activity every N seconds
    
    async def initialize(self) -> None:
        """Initialize strategy state"""
        logger.info(f"Initializing {self.name} strategy...")
        
        # Target wallet(s) to monitor
        self.target_wallets = [self.DEFAULT_TARGET_WALLET]
        
        # Track our position in current market
        # Once we copy, we can only SELL (not buy again)
        self.copied_side: Optional[str] = None      # 'up' or 'down' or None
        self.copied_size: float = 0.0               # $ amount we copied
        self.copied_token_id: Optional[str] = None
        self.copied_market_slug: Optional[str] = None  # slug of market we copied in
        
        # Track attempted copies to avoid spam
        self.attempted_markets: set = set()  # slugs we've already tried to copy
        
        # Track whale's last known position for comparison
        self.whale_last_buys: float = 0.0
        
        # Current market info
        self.current_market_slug: Optional[str] = None
        self.current_up_token: Optional[str] = None
        self.current_down_token: Optional[str] = None
        self.market_end_time: Optional[datetime] = None
        
        # HTTP client for API calls
        self.http_client = httpx.AsyncClient(timeout=10.0)
        
        # Background task for monitoring
        self._monitor_task: Optional[asyncio.Task] = None
        
        logger.info(f"  Monitoring wallet: {self.DEFAULT_TARGET_WALLET}")
        logger.info(f"  Min whale buy: ${self.MIN_WHALE_BUY}")
        logger.info(f"  Copy size: ${self.COPY_SIZE}")
        
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
        logger.info("Starting whale monitor loop...")
        logger.info(f"  Monitoring ALL crypto markets (BTC, ETH, SOL, XRP - any timeframe)")
        logger.info(f"  Using /positions endpoint for real-time position tracking")
        
        # Counter for periodic status log
        poll_count = 0
        
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
                
                # Log positions every ~30 seconds (10 polls at 3s interval)
                poll_count += 1
                if poll_count % 10 == 1 or poll_count == 1:
                    await self._log_whale_positions(positions, active_positions)
                
                # === COPY LOGIC ===
                # Only if we haven't copied yet
                if self.copied_side is None and active_positions:
                    await self._try_copy_whale_position(active_positions)
                
                # If we have a position, check if market is still active
                if self.copied_side is not None:
                    await self._check_position_exit(positions)
                
                await asyncio.sleep(self.POLL_INTERVAL)
                
            except asyncio.CancelledError:
                logger.info("Whale monitor cancelled")
                break
            except Exception as e:
                logger.error(f"Error in whale monitor: {e}")
                await asyncio.sleep(self.POLL_INTERVAL)
    
    def _reset_position(self) -> None:
        """Reset position tracking for next market opportunity"""
        self.copied_side = None
        self.copied_size = 0.0
        self.copied_token_id = None
        self.copied_market_slug = None
        self.whale_last_buys = 0.0
    
    def _is_crypto_market(self, slug: str) -> bool:
        """Check if slug is a crypto market (BTC, ETH, SOL, XRP)"""
        slug_lower = slug.lower()
        return any(crypto in slug_lower for crypto in self.CRYPTO_SLUGS)
    
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
            market_date = datetime.strptime(end_date, "%Y-%m-%d").date()
            
            # Extract time range from title using regex
            # Patterns like "3:15-3:30AM", "11:00-11:15PM", "12:00AM" (for midnight markets)
            time_pattern = r'(\d{1,2}):?(\d{2})?-?(\d{1,2})?:?(\d{2})?(AM|PM)'
            match = re.search(time_pattern, title, re.IGNORECASE)
            
            if not match:
                # Also try hourly market pattern: "January 18, 12AM ET"
                hourly_pattern = r'(\d{1,2})(AM|PM)\s+ET'
                hourly_match = re.search(hourly_pattern, title, re.IGNORECASE)
                
                if hourly_match:
                    hour = int(hourly_match.group(1))
                    ampm = hourly_match.group(2).upper()
                    
                    if ampm == 'PM' and hour != 12:
                        hour += 12
                    elif ampm == 'AM' and hour == 12:
                        hour = 0
                    
                    # Hourly markets are active for the full hour
                    start_time = datetime(market_date.year, market_date.month, market_date.day, hour, 0)
                    end_time = start_time + timedelta(hours=1)
                    
                    start_time = et_tz.localize(start_time)
                    end_time = et_tz.localize(end_time)
                    
                    return start_time <= now_et < end_time
                
                # Can't parse - assume not active
                return False
            
            # Parse start time
            start_hour = int(match.group(1))
            start_min = int(match.group(2)) if match.group(2) else 0
            ampm = match.group(5).upper()
            
            # Parse end time if present (for 15-min markets)
            if match.group(3):
                end_hour = int(match.group(3))
                end_min = int(match.group(4)) if match.group(4) else 0
            else:
                # Single time - assume 15 min market
                end_hour = start_hour
                end_min = start_min + 15
                if end_min >= 60:
                    end_min -= 60
                    end_hour += 1
            
            # Convert to 24h format
            if ampm == 'PM' and start_hour != 12:
                start_hour += 12
            elif ampm == 'AM' and start_hour == 12:
                start_hour = 0
            
            if ampm == 'PM' and end_hour != 12:
                end_hour += 12
            elif ampm == 'AM' and end_hour == 12:
                end_hour = 0
            
            # Build datetime objects
            start_time = datetime(market_date.year, market_date.month, market_date.day, start_hour, start_min)
            end_time = datetime(market_date.year, market_date.month, market_date.day, end_hour, end_min)
            
            # Localize to ET
            start_time = et_tz.localize(start_time)
            end_time = et_tz.localize(end_time)
            
            # Check if current time is within window
            return start_time <= now_et < end_time
            
        except Exception as e:
            logger.debug(f"Error parsing market time: {e} | Title: {title}")
            return False
    
    async def _get_whale_positions(self) -> List[Dict]:
        """
        Fetch whale's current positions using /positions endpoint.
        
        Returns list of ACTIVE crypto positions (curPrice > 0).
        """
        try:
            for wallet in self.target_wallets:
                url = f"https://data-api.polymarket.com/positions?user={wallet}"
                response = await self.http_client.get(url)
                
                if response.status_code != 200:
                    continue
                
                all_positions = response.json()
                
                # Filter to active crypto positions only
                active_crypto = []
                for pos in all_positions:
                    slug = pos.get('slug', '')
                    cur_price = float(pos.get('curPrice', 0))
                    size = float(pos.get('size', 0))
                    
                    # Only active positions (price > 0) in crypto markets
                    if cur_price > 0 and size > 0 and self._is_crypto_market(slug):
                        active_crypto.append(pos)
                
                return active_crypto
            
            return []
            
        except Exception as e:
            logger.error(f"Error fetching whale positions: {e}")
            return []
    
    async def _log_whale_positions(self, positions: List[Dict], active_positions: List[Dict] = None) -> None:
        """Log whale's positions in ACTIVE markets only"""
        active_positions = active_positions or []
        
        if not active_positions:
            logger.info("Whale has no positions in active markets")
            return
        
        logger.info("=" * 70)
        logger.info(f"WHALE ACTIVE POSITIONS ({len(active_positions)} in active markets)")
        logger.info("=" * 70)
        
        # Group by crypto
        by_crypto = {}
        for pos in active_positions:
            slug = pos.get('slug', '')
            for crypto in self.CRYPTO_SLUGS:
                if crypto in slug.lower():
                    if crypto not in by_crypto:
                        by_crypto[crypto] = []
                    by_crypto[crypto].append(pos)
                    break
        
        total_value = 0
        for crypto, crypto_positions in sorted(by_crypto.items()):
            logger.info(f"\n{crypto.upper()}:")
            for pos in crypto_positions:
                title = pos.get('title', '')[:50]
                outcome = pos.get('outcome', '')
                size = float(pos.get('size', 0))
                cur_price = float(pos.get('curPrice', 0))
                current_value = float(pos.get('currentValue', 0))
                total_bought = float(pos.get('totalBought', 0))
                
                total_value += current_value
                
                logger.info(
                    f"  {outcome:5} | Size: {size:>10.2f} | "
                    f"Price: {cur_price:.2f} | "
                    f"Value: ${current_value:>8.2f} | "
                    f"Bought: ${total_bought:>10.2f}"
                )
                logger.info(f"         {title}")
        
        logger.info("-" * 70)
        logger.info(f"TOTAL ACTIVE VALUE: ${total_value:,.2f}")
        
        # Show our position if any
        if self.copied_side:
            logger.info(f"OUR POSITION: {self.copied_side.upper()} ${self.copied_size:.2f} in {self.copied_market_slug}")
        
        logger.info("=" * 70)
    
    async def _try_copy_whale_position(self, active_positions: List[Dict]) -> None:
        """
        Try to copy the whale's position in an active market.
        
        Rules:
        - Only copy if whale has $MIN_WHALE_BUY or more in the position
        - Only copy ONCE per market (even if order fails)
        - Use fixed COPY_SIZE with GTC order
        """
        # Find the largest position that meets threshold
        best_pos = None
        best_value = 0
        
        for pos in active_positions:
            slug = pos.get('slug', '')
            total_bought = float(pos.get('totalBought', 0))
            current_value = float(pos.get('currentValue', 0))
            
            # Skip markets we've already attempted
            if slug in self.attempted_markets:
                continue
            
            # Check if whale's position is significant
            if total_bought >= self.MIN_WHALE_BUY and current_value > best_value:
                best_pos = pos
                best_value = current_value
        
        if not best_pos:
            return
        
        # Extract position details
        slug = best_pos.get('slug', '')
        outcome = best_pos.get('outcome', '')  # 'Up' or 'Down'
        token_id = best_pos.get('asset', '')
        total_bought = float(best_pos.get('totalBought', 0))
        title = best_pos.get('title', '')
        cur_price = float(best_pos.get('curPrice', 0))
        
        side = outcome.lower()  # 'up' or 'down'
        
        # Mark this market as attempted (even before trying)
        self.attempted_markets.add(slug)
        
        logger.info("=" * 60)
        logger.info(f">>> WHALE DETECTED - COPYING!")
        logger.info(f"  Market: {title}")
        logger.info(f"  Direction: {outcome}")
        logger.info(f"  Whale invested: ${total_bought:,.0f}")
        logger.info(f"  Current price: {cur_price:.2f}")
        logger.info(f"  Copying: ${self.COPY_SIZE:.2f} (GTC order)")
        logger.info("=" * 60)
        
        try:
            # Use current price + small buffer for GTC order
            # GTC will stay in orderbook if not immediately filled
            limit_price = min(0.9, cur_price + 0.05)
            
            # Build minimal market_info for the trader
            market_info = {
                'slug': slug,
                'condition_id': best_pos.get('conditionId', ''),
                'up_token_id': token_id if side == 'up' else best_pos.get('oppositeAsset', ''),
                'down_token_id': token_id if side == 'down' else best_pos.get('oppositeAsset', ''),
            }
            
            order = self.trader.place_buy_order(
                token_id=token_id,
                side=side,
                price=limit_price,
                size=self.COPY_SIZE,
                market_info=market_info,
                order_type="GTC"  # Good Till Cancelled - stays in orderbook
            )
            
            if order:
                logger.info(f"[OK] BUY order PLACED: {outcome} ${self.COPY_SIZE:.2f} @ {limit_price}")
                
                # Mark that we've copied - no more buys allowed
                self.copied_side = side
                self.copied_size = self.COPY_SIZE
                self.copied_token_id = token_id
                self.copied_market_slug = slug
                self.whale_last_buys = total_bought
            else:
                logger.warning("[FAIL] BUY order failed")
                
        except Exception as e:
            logger.error(f"Error placing copy order: {e}")
    
    async def _check_position_exit(self, all_positions: List[Dict]) -> None:
        """
        Check if we should exit our position.
        
        Exit conditions:
        - The market is no longer in active period (ended)
        - Whale sold their position (not in active positions anymore)
        """
        if not self.copied_side or not self.copied_market_slug:
            return
        
        # Find our copied market in whale's current positions
        whale_still_holds = False
        market_still_active = False
        
        for pos in all_positions:
            if pos.get('slug') == self.copied_market_slug and pos.get('outcome', '').lower() == self.copied_side:
                whale_still_holds = True
                
                # Check if market is still active
                title = pos.get('title', '')
                end_date = pos.get('endDate', '')
                market_still_active = self._is_market_active_now(title, end_date)
                break
        
        # If market period ended, log once and reset
        if not market_still_active and self.copied_size > 0:
            logger.info("=" * 60)
            logger.info(f"MARKET PERIOD ENDED: {self.copied_market_slug}")
            logger.info(f"  Our position: {self.copied_side.upper()} ${self.copied_size:.2f}")
            logger.info(f"  Waiting for resolution...")
            logger.info("=" * 60)
            # Reset position - we'll wait for resolution
            self._reset_position()
            return
        
        # If whale no longer holds position (but market still active), consider selling
        if not whale_still_holds and self.copied_size > 0 and market_still_active:
            logger.info("=" * 60)
            logger.info(f"[!] WHALE EXITED - Consider selling")
            logger.info(f"  Market: {self.copied_market_slug}")
            logger.info(f"  Our position: {self.copied_side.upper()} ${self.copied_size:.2f}")
            logger.info("=" * 60)
            
            # Optional: Auto-sell when whale exits
            # For now, just log - user can enable auto-sell if desired
            # await self._sell_position("whale exited")
    
    async def _get_whale_activity(self) -> tuple[Optional[WhaleActivity], List[str]]:
        """
        Fetch and analyze recent whale activity in ANY crypto market.
        
        Returns (WhaleActivity, list of market slugs with activity).
        """
        try:
            # Fetch recent activity for target wallet
            for wallet in self.target_wallets:
                url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=100"
                response = await self.http_client.get(url)
                
                if response.status_code != 200:
                    continue
                
                activities = response.json()
                now = datetime.now(pytz.UTC)
                
                # Group activity by market slug (any crypto market)
                market_activity = {}
                
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
            
            return None, []
            
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
        # Update token IDs if needed
        if not self.current_up_token:
            self.current_up_token = up_token_id
        if not self.current_down_token:
            self.current_down_token = down_token_id
    
    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        """Called when a market ends (from main bot framework)"""
        # Log if we had a position in this specific market
        market_slug = market_data.get('slug', self.current_market_slug)
        
        if self.copied_market_slug == market_slug and self.copied_size > 0:
            logger.info("=" * 60)
            logger.info(f"MARKET ENDED: {market_slug}")
            logger.info(f"  Position: {self.copied_side.upper()} ${self.copied_size:.2f}")
            
            if winner:
                won = (winner.upper() == self.copied_side.upper())
                result = "WON" if won else "LOST"
                logger.info(f"  Result: {result}")
            
            logger.info("=" * 60)
            
            # Reset position for next opportunity
            self._reset_position()
        
        # Reset current market tracking
        self.current_market_slug = None
        self.current_up_token = None
        self.current_down_token = None
        self.market_end_time = None
    
    async def shutdown(self) -> None:
        """Clean up on shutdown"""
        logger.info(f"Shutting down {self.name} strategy...")
        
        # Cancel monitor
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        
        # Close HTTP client
        await self.http_client.aclose()
        
        # Log final position
        if self.copied_side and self.copied_size > 0:
            logger.info(f"Final position in {self.copied_market_slug}: {self.copied_side.upper()} ${self.copied_size:.2f}")
