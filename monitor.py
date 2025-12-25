"""
Market monitoring for Bitcoin Up or Down 15-minute markets
"""

import requests
import json
import re
import logging
import time
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from dateutil import parser
import pytz

from config import GAMMA_API, CLOB_API, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


class MarketMonitor:
    """
    Monitors active Bitcoin 15-minute markets and tracks their prices
    """
    
    def __init__(self):
        self.active_markets = []
        self.price_cache = {}  # token_id -> latest price
        self.last_update = None
        self.current_market = None  # Currently active market
        self.current_market_end_time = None  # When current market ends
        self.et_tz = pytz.timezone('America/New_York')
    
    def get_active_btc_15min_markets(self) -> List[Dict]:
        """
        Fetch currently active Bitcoin Up or Down 15-minute markets that are happening NOW
        
        Uses a strategy of generating event slugs based on current timestamp,
        since these markets are part of a recurring series.
        """
        try:
            # Get current time in ET timezone
            et_tz = pytz.timezone('America/New_York')
            now_et = datetime.now(et_tz)
            
            # Round to nearest 15-minute interval
            current_minute = now_et.minute
            rounded_minute = (current_minute // 15) * 15
            current_rounded = now_et.replace(minute=rounded_minute, second=0, microsecond=0)
            
            # Generate slugs for current and adjacent periods (-1, 0, +1, +2)
            # to catch markets that might overlap
            timestamps_to_check = []
            for i in range(-1, 3):
                time_offset = current_rounded + timedelta(minutes=15 * i)
                timestamp = int(time_offset.timestamp())
                slug = f"btc-updown-15m-{timestamp}"
                timestamps_to_check.append((slug, time_offset))
            
            logger.debug(f"Checking {len(timestamps_to_check)} potential time periods...")
            
            active_markets = []
            
            for slug, period_time in timestamps_to_check:
                try:
                    # Get event by slug
                    response = requests.get(
                        f"{GAMMA_API}/events",
                        params={"slug": slug},
                        timeout=REQUEST_TIMEOUT
                    )
                    
                    if response.status_code == 200:
                        events = response.json()
                        
                        if events and len(events) > 0:
                            event = events[0]
                            
                            # Check if event is active and not closed
                            if event.get('active') and not event.get('closed'):
                                # Check if event is happening NOW
                                event_start = event.get('startTime')
                                event_end = event.get('endDate')
                                
                                if event_start and event_end:
                                    start_dt = parser.parse(event_start)
                                    end_dt = parser.parse(event_end)
                                    now_utc = datetime.now(pytz.UTC)
                                    
                                    # Only include if currently active
                                    if start_dt <= now_utc <= end_dt:
                                        # Get markets from event
                                        markets = event.get('markets', [])
                                        if markets:
                                            market = markets[0]
                                            logger.debug(f"Found active market: {market.get('question', 'N/A')}")
                                            active_markets.append(market)
                
                except Exception as e:
                    logger.debug(f"Error checking period {slug}: {e}")
                    continue
            
            logger.info(f"Found {len(active_markets)} BTC 15-min markets ACTIVE NOW")
            return active_markets
            
        except Exception as e:
            logger.error(f"Error fetching active markets: {e}")
            return []
    
    def _is_btc_15min_market(self, market: Dict) -> bool:
        """Check if market is a Bitcoin Up or Down 15-minute market that is happening NOW"""
        question = market.get('question', '').lower()
        
        # Check if it's a Bitcoin Up or Down market
        if not ('bitcoin' in question and 'up' in question and 'down' in question):
            return False
        
        # Must be active and have order book
        if not market.get('enableOrderBook'):
            return False
        
        # Check if it's 15 minutes duration
        if not self._is_15_minute_duration(market.get('question', '')):
            return False
        
        # Check if market is happening NOW (not future)
        return self._is_market_active_now(market.get('question', ''))
    
    def _is_15_minute_duration(self, question: str) -> bool:
        """
        Parse question to check if market duration is 15 minutes.
        Supports both formats:
        - "3:00PM-3:15PM" (AM/PM after each)
        - "3:00-3:15PM" (AM/PM only at end)
        """
        # Try first format: AM/PM after each time
        time_pattern1 = r'(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)'
        match = re.search(time_pattern1, question)
        
        if match:
            start_hour, start_min, start_period, end_hour, end_min, end_period = match.groups()
        else:
            # Try second format: AM/PM only at end
            time_pattern2 = r'(\d+):(\d+)-(\d+):(\d+)(AM|PM)'
            match = re.search(time_pattern2, question)
            
            if match:
                start_hour, start_min, end_hour, end_min, period = match.groups()
                start_period = period
                end_period = period
            else:
                return False
        
        # Convert to minutes
        start_total = (int(start_hour) % 12) * 60 + int(start_min)
        if start_period == 'PM' and int(start_hour) != 12:
            start_total += 12 * 60
        
        end_total = (int(end_hour) % 12) * 60 + int(end_min)
        if end_period == 'PM' and int(end_hour) != 12:
            end_total += 12 * 60
        
        duration = end_total - start_total
        if duration < 0:
            duration += 24 * 60
        
        return duration == 15
    
    def _is_market_active_now(self, question: str) -> bool:
        """
        Check if market is currently active (happening now).
        
        Returns True only if current time is between start and end time.
        Supports two formats:
        - "December 25, 3:00PM-3:15PM ET"
        - "December 25, 3:00-3:15PM ET" (AM/PM only at end)
        """
        try:
            # Try first format: AM/PM after each time
            date_time_pattern1 = r'(\w+ \d+),?\s+(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)\s+ET'
            match = re.search(date_time_pattern1, question)
            
            if match:
                date_str, start_hour, start_min, start_period, end_hour, end_min, end_period = match.groups()
            else:
                # Try second format: AM/PM only at end
                date_time_pattern2 = r'(\w+ \d+),?\s+(\d+):(\d+)-(\d+):(\d+)(AM|PM)\s+ET'
                match = re.search(date_time_pattern2, question)
                
                if match:
                    date_str, start_hour, start_min, end_hour, end_min, period = match.groups()
                    # Both times share the same AM/PM
                    start_period = period
                    end_period = period
                else:
                    logger.debug(f"Could not parse date/time from: {question}")
                    return False
            
            # Get current year (markets typically don't specify year)
            current_year = datetime.now().year
            
            # Parse the date with current year
            date_with_year = f"{date_str} {current_year}"
            market_date = parser.parse(date_with_year)
            
            # ET timezone
            et_tz = pytz.timezone('America/New_York')
            
            # Build start and end times
            start_hour_24 = int(start_hour) % 12
            if start_period == 'PM' and int(start_hour) != 12:
                start_hour_24 += 12
            elif start_period == 'AM' and int(start_hour) == 12:
                start_hour_24 = 0
            
            end_hour_24 = int(end_hour) % 12
            if end_period == 'PM' and int(end_hour) != 12:
                end_hour_24 += 12
            elif end_period == 'AM' and int(end_hour) == 12:
                end_hour_24 = 0
            
            # Create start and end datetime objects in ET
            start_time = et_tz.localize(datetime(
                market_date.year,
                market_date.month,
                market_date.day,
                start_hour_24,
                int(start_min)
            ))
            
            end_time = et_tz.localize(datetime(
                market_date.year,
                market_date.month,
                market_date.day,
                end_hour_24,
                int(end_min)
            ))
            
            # Get current time in ET
            now_et = datetime.now(et_tz)
            
            # Check if market is happening now
            is_active = start_time <= now_et <= end_time
            
            if is_active:
                logger.debug(f"Market ACTIVE NOW: {question}")
                logger.debug(f"  Start: {start_time}, End: {end_time}, Now: {now_et}")
            else:
                logger.debug(f"Market not active: {question}")
                logger.debug(f"  Start: {start_time}, End: {end_time}, Now: {now_et}")
            
            return is_active
            
        except Exception as e:
            logger.debug(f"Error parsing market time for '{question}': {e}")
            return False
    
    def get_current_prices(self, market: Dict) -> Optional[Dict]:
        """
        Get current prices for a market using CLOB midpoint endpoint
        
        The CLOB /midpoint endpoint provides more accurate real-time prices
        than outcomePrices (which can be stale) or raw order book (which has wide spreads).
        
        Returns: {
            'up_price': float,
            'down_price': float,
            'up_token_id': str,
            'down_token_id': str,
            'buy_price': float,  # Price to BUY the side (best ask)
            'sell_price': float  # Price to SELL the side (best bid)
        }
        """
        try:
            # Parse token IDs
            token_ids = market.get('clobTokenIds', [])
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            
            if len(token_ids) < 2:
                return None
            
            # Get outcomes
            outcomes = market.get('outcomes', [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            # Determine which is UP and which is DOWN
            up_idx = 0 if outcomes[0].lower() == 'up' else 1
            down_idx = 1 - up_idx
            
            up_token_id = token_ids[up_idx]
            down_token_id = token_ids[down_idx]
            
            # Get midpoint prices from CLOB API
            up_price = self._get_clob_midpoint(up_token_id)
            down_price = self._get_clob_midpoint(down_token_id)
            
            if up_price is None or down_price is None:
                # Fallback to outcomePrices if CLOB midpoint fails
                logger.debug("CLOB midpoint unavailable, falling back to outcomePrices")
                outcome_prices = market.get('outcomePrices', [])
                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)
                
                if len(outcome_prices) < 2:
                    return None
                
                up_price = float(outcome_prices[up_idx])
                down_price = float(outcome_prices[down_idx])
            
            # Get order book for buy/sell prices
            order_book = self.get_order_book(up_token_id)
            buy_price = order_book['best_ask'] if order_book else None
            sell_price = order_book['best_bid'] if order_book else None
            
            return {
                'up_price': up_price,
                'down_price': down_price,
                'up_token_id': up_token_id,
                'down_token_id': down_token_id,
                'market': market,
                'buy_price': buy_price,   # Best ask (price to buy at)
                'sell_price': sell_price  # Best bid (price to sell at)
            }
            
        except Exception as e:
            logger.error(f"Error getting prices for market: {e}")
            return None
    
    def _get_clob_midpoint(self, token_id: str) -> Optional[float]:
        """
        Get midpoint price from CLOB API
        
        This provides more accurate real-time prices than raw order book.
        """
        try:
            url = f"{CLOB_API}/midpoint"
            params = {"token_id": token_id}
            
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            
            mid = data.get('mid')
            if mid is not None:
                return float(mid)
            
            return None
            
        except Exception as e:
            logger.debug(f"Error getting CLOB midpoint for {token_id[:10]}...: {e}")
            return None
    
    def get_order_book(self, token_id: str) -> Optional[Dict]:
        """
        Get the order book for a specific token
        
        Returns: {
            'bids': [{'price': str, 'size': str}, ...],
            'asks': [{'price': str, 'size': str}, ...],
            'best_bid': float,
            'best_ask': float,
            'spread': float
        }
        """
        try:
            url = f"{CLOB_API}/book"
            params = {"token_id": token_id}
            
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            
            bids = data.get('bids', [])
            asks = data.get('asks', [])
            
            if not bids or not asks:
                return None
            
            best_bid = float(bids[0]['price']) if bids else 0
            best_ask = float(asks[0]['price']) if asks else 1
            spread = best_ask - best_bid
            
            return {
                'bids': bids,
                'asks': asks,
                'best_bid': best_bid,
                'best_ask': best_ask,
                'spread': spread
            }
            
        except Exception as e:
            logger.debug(f"Error getting order book for {token_id[:10]}...: {e}")
            return None
    
    def _extract_market_end_time(self, market: Dict) -> Optional[datetime]:
        """
        Extract the end time of a market from its question
        
        Example: "Bitcoin Up or Down - December 25, 4:45PM-5:00PM ET"
        Returns: datetime(2024, 12, 25, 17, 0, 0, tzinfo=ET)
        """
        try:
            question = market.get('question', '')
            
            # Regex to extract date and end time
            # Handles both "4:45PM-5:00PM ET" and "4:45-5:00PM ET"
            date_time_pattern = r'(\w+ \d+),?\s+\d+:\d+(?:AM|PM)?-(\d+):(\d+)(AM|PM)'
            match = re.search(date_time_pattern, question)
            
            if not match:
                logger.debug(f"Could not parse end time from: {question}")
                return None
            
            date_str, end_hour, end_min, end_period = match.groups()
            
            # Get current year
            current_year = datetime.now().year
            full_date_str = f"{date_str} {current_year}"
            
            # Parse the date
            market_date = parser.parse(full_date_str).date()
            
            # Convert end time to 24-hour format
            end_hour_24 = int(end_hour) % 12
            if end_period == 'PM' and int(end_hour) != 12:
                end_hour_24 += 12
            elif end_period == 'AM' and int(end_hour) == 12:
                end_hour_24 = 0
            
            # Create end datetime in ET
            end_time = self.et_tz.localize(datetime(
                market_date.year,
                market_date.month,
                market_date.day,
                end_hour_24,
                int(end_min),
                0
            ))
            
            return end_time
            
        except Exception as e:
            logger.debug(f"Error extracting end time from '{question}': {e}")
            return None
    
    def _is_current_market_still_active(self) -> bool:
        """Check if the currently tracked market is still active"""
        if not self.current_market or not self.current_market_end_time:
            return False
        
        now_et = datetime.now(self.et_tz)
        
        # Market is active until it actually ends (no buffer needed)
        # We'll search for the next market once this one expires
        return now_et < self.current_market_end_time
    
    def update_active_markets(self):
        """
        Refresh the list of active markets (optimized)
        
        Only searches for new markets if:
        - No current market, OR
        - Current market has ended
        
        This avoids unnecessary API calls during the 15-minute window.
        """
        # Check if we can reuse current market
        if self._is_current_market_still_active():
            logger.debug(f"Current market still active until {self.current_market_end_time.strftime('%I:%M:%S%p ET')}, skipping search")
            self.active_markets = [self.current_market]
            return self.active_markets
        
        # Current market ended or doesn't exist, search for new one
        if self.current_market:
            logger.info(f"Current market ended at {self.current_market_end_time.strftime('%I:%M:%S%p ET')}, searching for new market...")
        
        self.active_markets = self.get_active_btc_15min_markets()
        self.last_update = datetime.now()
        
        # Update current market tracking
        if self.active_markets:
            self.current_market = self.active_markets[0]
            self.current_market_end_time = self._extract_market_end_time(self.current_market)
            
            if self.current_market_end_time:
                logger.info(f"Locked onto market: {self.current_market.get('question')}")
                logger.info(f"  Will monitor until: {self.current_market_end_time.strftime('%I:%M:%S%p ET')}")
        else:
            self.current_market = None
            self.current_market_end_time = None
        
        return self.active_markets
    
    def get_all_market_prices(self) -> List[Dict]:
        """
        Get current prices for all active markets
        
        Returns list of price dicts with market info
        """
        # Ensure we have active markets (will reuse current if still valid)
        self.update_active_markets()
        
        if not self.active_markets:
            return []
        
        prices = []
        for market in self.active_markets:
            price_data = self.get_current_prices(market)
            if price_data:
                prices.append(price_data)
        
        return prices

