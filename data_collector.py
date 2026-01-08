"""
Data collector for BTC 15-min market prices.

Collects price snapshots every second and sends to local API when market ends.
"""

import httpx
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import pytz
import asyncio

from config import DATA_COLLECTOR_API_URL

logger = logging.getLogger(__name__)


@dataclass
class PriceSnapshot:
    timestamp: datetime
    up_price: float
    down_price: float


@dataclass
class MarketData:
    condition_id: str
    question: str
    start_time: datetime
    end_time: datetime
    up_token_id: str
    down_token_id: str
    snapshots: List[PriceSnapshot] = field(default_factory=list)
    winner: Optional[str] = None


class DataCollector:
    """
    Collects price data during market and sends to API on market end.
    
    Usage:
        collector = DataCollector()
        
        # When new market starts
        collector.start_market(market_info, up_token, down_token, start_time, end_time)
        
        # Every iteration (will sample every second)
        collector.record_price(up_price, down_price)
        
        # When market ends
        await collector.save_market(winner='UP')
    """
    
    def __init__(self, api_url: str = DATA_COLLECTOR_API_URL):
        self.api_url = api_url
        self.current_market: Optional[MarketData] = None
        self.last_record_time: float = 0
        self.record_interval: float = 1.0  # Record every 1 second
        self.et_tz = pytz.timezone('America/New_York')
        
        # Async client for sending data
        self._client: Optional[httpx.AsyncClient] = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client
    
    async def close(self):
        """Close HTTP client"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
    
    def start_market(
        self,
        condition_id: str,
        question: str,
        up_token_id: str,
        down_token_id: str,
        start_time: datetime,
        end_time: datetime
    ):
        """
        Start collecting data for a new market.
        
        Clears any previous market data.
        """
        self.current_market = MarketData(
            condition_id=condition_id,
            question=question,
            start_time=start_time,
            end_time=end_time,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            snapshots=[]
        )
        self.last_record_time = 0
        
        # logger.info(f"[DataCollector] Started collecting: {question[:50]}...")
    
    def record_price(self, up_price: float, down_price: float) -> bool:
        """
        Record a price snapshot if enough time has passed.
        
        Returns True if a snapshot was recorded, False otherwise.
        """
        if self.current_market is None:
            return False
        
        now = time.time()
        
        # Only record once per interval
        if now - self.last_record_time < self.record_interval:
            return False
        
        # Create snapshot with current timestamp
        snapshot = PriceSnapshot(
            timestamp=datetime.now(self.et_tz),
            up_price=up_price,
            down_price=down_price
        )
        
        self.current_market.snapshots.append(snapshot)
        self.last_record_time = now
        
        return True
    
    async def save_market(self, winner: Optional[str] = None) -> bool:
        """
        Save collected market data to the API.
        
        Args:
            winner: 'UP', 'DOWN', or None
            
        Returns:
            True if saved successfully, False otherwise
        """
        if self.current_market is None:
            logger.warning("[DataCollector] No market data to save")
            return False
        
        if len(self.current_market.snapshots) == 0:
            logger.warning("[DataCollector] No price snapshots collected")
            return False
        
        self.current_market.winner = winner
        
        # Prepare payload
        payload = {
            "condition_id": self.current_market.condition_id,
            "question": self.current_market.question,
            "start_time": self.current_market.start_time.isoformat(),
            "end_time": self.current_market.end_time.isoformat(),
            "up_token_id": self.current_market.up_token_id,
            "down_token_id": self.current_market.down_token_id,
            "winner": self.current_market.winner,
            "snapshots": [
                {
                    "timestamp": s.timestamp.isoformat(),
                    "up_price": s.up_price,
                    "down_price": s.down_price
                }
                for s in self.current_market.snapshots
            ]
        }
        
        try:
            client = await self._get_client()
            response = await client.post(f"{self.api_url}/market", json=payload)
            
            if response.status_code == 200:
                result = response.json()
                logger.info(
                    f"[DataCollector] Saved market {self.current_market.condition_id[:10]}... "
                    f"with {result['snapshots_saved']} snapshots"
                )
                self.current_market = None
                return True
            else:
                logger.error(f"[DataCollector] API error: {response.status_code} - {response.text}")
                
        except httpx.ConnectError:
            logger.warning("[DataCollector] API server not available - data not saved")
        except Exception as e:
            logger.error(f"[DataCollector] Error saving market: {e}")
        
        return False
    
    def get_snapshot_count(self) -> int:
        """Get number of snapshots collected for current market"""
        if self.current_market is None:
            return 0
        return len(self.current_market.snapshots)
    
    def has_active_market(self) -> bool:
        """Check if currently collecting data for a market"""
        return self.current_market is not None


