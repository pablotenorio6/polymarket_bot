"""
Optimized market monitoring with async requests and WebSocket support

Key optimizations:
1. Async HTTP requests for parallel fetching
2. Batch midpoint requests (/midprices instead of /midpoint)
3. Connection pooling via httpx
4. Market metadata caching
5. Persistent connections for async usage
6. Fresh connections for sync wrapper (event loop safety)
"""

import asyncio
import httpx
import json
import re
import logging
import time
from typing import List, Dict, Optional, Set
from datetime import datetime, timedelta
from dateutil import parser
import pytz

from config import GAMMA_API, CLOB_API, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

# Connection pool configuration
POOL_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=50,
    keepalive_expiry=30.0
)


class PersistentAsyncClient:
    """
    Async HTTP client with persistent connections for use in a single event loop.
    Use this when running in main_fast.py (pure async context).
    """
    
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
    
    async def get(self) -> httpx.AsyncClient:
        """Get or create persistent client"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=POOL_LIMITS,
                timeout=httpx.Timeout(REQUEST_TIMEOUT),
                http2=True
            )
        return self._client
    
    async def close(self):
        """Close the client"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


class FastMarketMonitor:
    """
    High-performance market monitor with async operations
    
    Optimizations:
    - Async HTTP with connection pooling
    - Batch API calls where possible
    - Aggressive caching of static data
    - Parallel price fetching
    
    Usage modes:
    - Persistent (use_persistent_client=True): For pure async contexts like main_fast.py
    - Fresh (use_persistent_client=False): For sync wrappers, creates new client each call
    """
    
    def __init__(self, use_persistent_client: bool = False):
        self.et_tz = pytz.timezone('America/New_York')
        
        # Client mode
        self.use_persistent_client = use_persistent_client
        self._persistent_client: Optional[PersistentAsyncClient] = None
        if use_persistent_client:
            self._persistent_client = PersistentAsyncClient()
        
        # Cache for market metadata (doesn't change often)
        self.market_cache: Dict[str, Dict] = {}  # condition_id -> market data
        self.token_cache: Dict[str, Dict] = {}   # slug -> token IDs
        
        # Active markets tracking
        self.active_markets: List[Dict] = []
        self.current_market_end_time: Optional[datetime] = None
        
        # Price cache (updated each cycle)
        self.price_cache: Dict[str, float] = {}  # token_id -> price
        self.last_price_update: Optional[datetime] = None
        
        # Market slugs to monitor (expandable for ETH, SOL, etc.)
        self.market_prefixes = [
            "btc-updown-15m-",
            # Future: "eth-updown-15m-", "sol-updown-15m-"
        ]
    
    async def close(self):
        """Clean up resources"""
        if self._persistent_client:
            await self._persistent_client.close()
    
    def _generate_current_slugs(self) -> List[str]:
        """Generate slugs for current and adjacent 15-min periods"""
        now_et = datetime.now(self.et_tz)
        current_minute = now_et.minute
        rounded_minute = (current_minute // 15) * 15
        current_rounded = now_et.replace(minute=rounded_minute, second=0, microsecond=0)
        
        slugs = []
        for prefix in self.market_prefixes:
            for i in range(-1, 3):  # Check -1, 0, +1, +2 periods
                time_offset = current_rounded + timedelta(minutes=15 * i)
                timestamp = int(time_offset.timestamp())
                slugs.append(f"{prefix}{timestamp}")
        
        return slugs
    
    async def _fetch_event_by_slug(self, client: httpx.AsyncClient, slug: str) -> Optional[Dict]:
        """Fetch a single event by slug"""
        try:
            response = await client.get(
                f"{GAMMA_API}/events",
                params={"slug": slug}
            )
            
            if response.status_code == 200:
                events = response.json()
                if events and len(events) > 0:
                    return events[0]
        except Exception as e:
            logger.debug(f"Error fetching {slug}: {e}")
        
        return None
    
    async def _make_requests(self, request_fn):
        """
        Execute requests using appropriate client mode.
        For persistent mode: reuse connection
        For fresh mode: create new client each time
        """
        if self.use_persistent_client and self._persistent_client:
            client = await self._persistent_client.get()
            return await request_fn(client)
        else:
            async with httpx.AsyncClient(
                limits=POOL_LIMITS,
                timeout=httpx.Timeout(REQUEST_TIMEOUT)
            ) as client:
                return await request_fn(client)
    
    async def get_active_markets(self) -> List[Dict]:
        """
        Fetch active 15-min markets using parallel async requests
        
        Optimizations:
        - Parallel fetching of all potential time periods
        - Caching of market metadata
        """
        # Check if current market is still active
        if self.current_market_end_time:
            now_et = datetime.now(self.et_tz)
            if now_et < self.current_market_end_time:
                logger.debug(f"Using cached market until {self.current_market_end_time.strftime('%H:%M:%S')}")
                return self.active_markets
        
        slugs = self._generate_current_slugs()
        
        async def fetch_all(client):
            tasks = [self._fetch_event_by_slug(client, slug) for slug in slugs]
            return await asyncio.gather(*tasks, return_exceptions=True)
        
        results = await self._make_requests(fetch_all)
        
        active_markets = []
        now_utc = datetime.now(pytz.UTC)
        
        for event in results:
            if event is None or isinstance(event, Exception):
                continue
            
            if not event.get('active') or event.get('closed'):
                continue
            
            # Check if currently active
            start_dt = parser.parse(event.get('startTime', ''))
            end_dt = parser.parse(event.get('endDate', ''))
            
            if start_dt <= now_utc <= end_dt:
                markets = event.get('markets', [])
                if markets:
                    market = markets[0]
                    active_markets.append(market)
                    
                    # Cache the market metadata
                    condition_id = market.get('conditionId')
                    if condition_id:
                        self.market_cache[condition_id] = market
                    
                    # Update end time tracking
                    self.current_market_end_time = end_dt.astimezone(self.et_tz)
        
        self.active_markets = active_markets
        
        if active_markets:
            logger.debug(f"Found {len(active_markets)} active markets")
        
        return active_markets
    
    async def get_prices_batch(self, token_ids: List[str]) -> Dict[str, float]:
        """
        Fetch multiple prices in a single batch request
        
        Uses /midprices endpoint for efficiency (1 request vs N requests)
        """
        if not token_ids:
            return {}
        
        async def fetch_batch(client):
            response = await client.get(
                f"{CLOB_API}/midprices",
                params={"token_ids": ",".join(token_ids)}
            )
            
            if response.status_code == 200:
                data = response.json()
                prices = {}
                
                # Parse response (format may vary)
                if isinstance(data, dict):
                    for token_id, mid in data.items():
                        if mid is not None:
                            prices[token_id] = float(mid) if isinstance(mid, str) else mid
                elif isinstance(data, list):
                    for item in data:
                        token_id = item.get('token_id') or item.get('tokenId')
                        mid = item.get('mid') or item.get('midpoint')
                        if token_id and mid:
                            prices[token_id] = float(mid)
                
                return prices
            return None
        
        try:
            prices = await self._make_requests(fetch_batch)
            if prices:
                self.price_cache.update(prices)
                self.last_price_update = datetime.now()
                return prices
                
        except Exception as e:
            logger.debug(f"Batch prices failed, using individual: {e}")
        
        # Fallback to individual requests if batch fails
        return await self._get_prices_individual(token_ids)
    
    async def _get_prices_individual(self, token_ids: List[str]) -> Dict[str, float]:
        """Fallback: fetch prices individually in parallel"""
        
        async def fetch_all(client):
            async def fetch_one(token_id: str) -> tuple:
                try:
                    response = await client.get(
                        f"{CLOB_API}/midpoint",
                        params={"token_id": token_id}
                    )
                    if response.status_code == 200:
                        data = response.json()
                        mid = data.get('mid') if isinstance(data, dict) else data
                        return (token_id, float(mid) if mid else None)
                except:
                    pass
                return (token_id, None)
            
            return await asyncio.gather(*[fetch_one(tid) for tid in token_ids])
        
        results = await self._make_requests(fetch_all)
        
        prices = {}
        for token_id, price in results:
            if price is not None:
                prices[token_id] = price
                self.price_cache[token_id] = price
        
        self.last_price_update = datetime.now()
        return prices
    
    async def get_all_market_prices(self) -> List[Dict]:
        """
        Get current prices for all active markets
        
        Returns list of dicts with up/down prices and token IDs
        """
        markets = await self.get_active_markets()
        
        if not markets:
            return []
        
        # Collect all token IDs
        token_ids = []
        token_map = {}  # token_id -> (market, outcome)
        
        for market in markets:
            clob_tokens = market.get('clobTokenIds', [])
            if isinstance(clob_tokens, str):
                clob_tokens = json.loads(clob_tokens)
            
            outcomes = market.get('outcomes', [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            if len(clob_tokens) >= 2:
                up_idx = 0 if outcomes[0].lower() == 'up' else 1
                down_idx = 1 - up_idx
                
                up_token = clob_tokens[up_idx]
                down_token = clob_tokens[down_idx]
                
                token_ids.extend([up_token, down_token])
                token_map[up_token] = (market, 'up')
                token_map[down_token] = (market, 'down')
        
        # Fetch all prices in batch
        prices = await self.get_prices_batch(token_ids)
        
        # Build result
        result = []
        processed_markets = set()
        
        for token_id, (market, side) in token_map.items():
            market_id = market.get('conditionId')
            if market_id in processed_markets:
                continue
            
            clob_tokens = market.get('clobTokenIds', [])
            if isinstance(clob_tokens, str):
                clob_tokens = json.loads(clob_tokens)
            
            outcomes = market.get('outcomes', [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            up_idx = 0 if outcomes[0].lower() == 'up' else 1
            down_idx = 1 - up_idx
            
            up_token = clob_tokens[up_idx]
            down_token = clob_tokens[down_idx]
            
            up_price = prices.get(up_token, 0.5)
            down_price = prices.get(down_token, 0.5)
            
            result.append({
                'up_price': up_price,
                'down_price': down_price,
                'up_token_id': up_token,
                'down_token_id': down_token,
                'market': market
            })
            
            processed_markets.add(market_id)
        
        return result
    
    def parse_token_ids(self, market: Dict) -> tuple:
        """Parse token IDs from market data (cached)"""
        condition_id = market.get('conditionId')
        
        if condition_id in self.token_cache:
            cached = self.token_cache[condition_id]
            return cached['up_token'], cached['down_token']
        
        clob_tokens = market.get('clobTokenIds', [])
        if isinstance(clob_tokens, str):
            clob_tokens = json.loads(clob_tokens)
        
        outcomes = market.get('outcomes', [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        
        if len(clob_tokens) >= 2:
            up_idx = 0 if outcomes[0].lower() == 'up' else 1
            down_idx = 1 - up_idx
            
            up_token = clob_tokens[up_idx]
            down_token = clob_tokens[down_idx]
            
            self.token_cache[condition_id] = {
                'up_token': up_token,
                'down_token': down_token
            }
            
            return up_token, down_token
        
        return None, None


# Synchronous wrapper for compatibility with existing code
class FastMarketMonitorSync:
    """
    Synchronous wrapper around FastMarketMonitor
    
    Maintains the same interface as the original MarketMonitor
    for drop-in replacement
    """
    
    def __init__(self):
        self._async_monitor = FastMarketMonitor()
    
    def _run_async(self, coro):
        """Run async coroutine synchronously - creates fresh event loop each time"""
        # Always use asyncio.run() for clean event loop management
        # This avoids issues with httpx client being bound to old loops
        return asyncio.run(coro)
    
    def update_active_markets(self) -> List[Dict]:
        """Update and return active markets"""
        return self._run_async(self._async_monitor.get_active_markets())
    
    def get_all_market_prices(self) -> List[Dict]:
        """Get prices for all active markets"""
        return self._run_async(self._async_monitor.get_all_market_prices())
    
    def get_current_prices(self, market: Dict) -> Optional[Dict]:
        """Get prices for a specific market"""
        up_token, down_token = self._async_monitor.parse_token_ids(market)
        if not up_token or not down_token:
            return None
        
        prices = self._run_async(
            self._async_monitor.get_prices_batch([up_token, down_token])
        )
        
        return {
            'up_price': prices.get(up_token, 0.5),
            'down_price': prices.get(down_token, 0.5),
            'up_token_id': up_token,
            'down_token_id': down_token,
            'market': market
        }
    
    def close(self):
        """Clean up resources"""
        try:
            asyncio.run(self._async_monitor.close())
        except Exception:
            pass


# For backwards compatibility
def get_fast_monitor() -> FastMarketMonitorSync:
    """Get an instance of the fast monitor"""
    return FastMarketMonitorSync()

