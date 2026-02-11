"""
Data collector for BTC 15-min market prices.

Collects price snapshots every second and sends to local API when market ends.
Now includes real-time BTC price from Chainlink oracle.
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
    crypto_price: Optional[float] = None  # Crypto price from Polymarket RTDS


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
    start_crypto_price: Optional[float] = None  # Chainlink price at event start
    end_crypto_price: Optional[float] = None    # Chainlink price at event end


class DataCollector:
    """
    Collects price data during market and sends to API on market end.
    Now includes real-time BTC price from Chainlink oracle.
    
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
        self.record_interval: float = 0.5  # Record every 0.5 seconds
        self.et_tz = pytz.timezone('America/New_York')
        
        # Async client for sending data
        self._client: Optional[httpx.AsyncClient] = None
        
        # RTDS client for crypto price (set via set_rtds_client)
        self._rtds_client = None
        self._crypto_symbol = "BTC"  # Default symbol, set via set_rtds_client
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client
    
    async def close(self):
        """Close HTTP client"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
    
    def set_rtds_client(self, rtds_client, crypto_symbol: str = "BTC"):
        """
        Set the RTDS client for crypto price feeds.
        
        Args:
            rtds_client: RTDSCryptoPrices instance
            crypto_symbol: Crypto symbol to track (BTC, ETH, SOL)
        """
        self._rtds_client = rtds_client
        self._crypto_symbol = crypto_symbol.upper()
        logger.info(f"[DataCollector] RTDS {self._crypto_symbol} price feed connected")
    
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
        # Capture Chainlink price at event start
        start_crypto_price = None
        if self._rtds_client:
            start_crypto_price = self._rtds_client.get_price(self._crypto_symbol)

        self.current_market = MarketData(
            condition_id=condition_id,
            question=question,
            start_time=start_time,
            end_time=end_time,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            snapshots=[],
            start_crypto_price=round(start_crypto_price, 2) if start_crypto_price else None,
        )
        self.last_record_time = 0

        if start_crypto_price:
            logger.info(f"[DataCollector] Start Chainlink {self._crypto_symbol}: ${start_crypto_price:,.2f}")
    
    def record_price(self, up_price: float, down_price: float) -> bool:
        """
        Record a price snapshot if enough time has passed.
        Includes BTC price from Chainlink oracle.
        
        Returns True if a snapshot was recorded, False otherwise.
        """
        if self.current_market is None:
            return False
        
        now = time.time()
        
        # Only record once per interval
        if now - self.last_record_time < self.record_interval:
            return False
        
        # Get current crypto price from Polymarket RTDS
        crypto_price = None
        if self._rtds_client:
            crypto_price = self._rtds_client.get_price(self._crypto_symbol)
        
        # Create snapshot with current timestamp and crypto price
        # Round to 4 decimals for precision
        snapshot = PriceSnapshot(
            timestamp=datetime.now(self.et_tz),
            up_price=round(up_price, 4),
            down_price=round(down_price, 4),
            crypto_price=round(crypto_price, 2) if crypto_price else None
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

        # Capture Chainlink price at event end
        if self._rtds_client:
            end_price = self._rtds_client.get_price(self._crypto_symbol)
            if end_price:
                self.current_market.end_crypto_price = round(end_price, 2)
                logger.info(f"[DataCollector] End Chainlink {self._crypto_symbol}: ${end_price:,.2f}")

        # Prepare payload
        payload = {
            "condition_id": self.current_market.condition_id,
            "question": self.current_market.question,
            "start_time": self.current_market.start_time.isoformat(),
            "end_time": self.current_market.end_time.isoformat(),
            "up_token_id": self.current_market.up_token_id,
            "down_token_id": self.current_market.down_token_id,
            "winner": self.current_market.winner,
            "crypto_symbol": self._crypto_symbol,  # BTC, ETH, SOL
            "start_crypto_price": self.current_market.start_crypto_price,
            "end_crypto_price": self.current_market.end_crypto_price,
            "snapshots": [
                {
                    "timestamp": s.timestamp.isoformat(),
                    "up_price": round(s.up_price, 4),
                    "down_price": round(s.down_price, 4),
                    "crypto_price": s.crypto_price  # Chainlink price
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


