"""
Chainlink BTC/USD Price Feed Reader

Uses Web3 to read the latest BTC price from Chainlink's price feed on Polygon.
"""

import logging
import time
from typing import Optional
from web3 import Web3
from functools import lru_cache

logger = logging.getLogger(__name__)

# Chainlink BTC/USD Price Feed on Polygon Mainnet
# See: https://docs.chain.link/data-feeds/price-feeds/addresses?network=polygon
CHAINLINK_BTC_USD_POLYGON = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

# Polygon RPC endpoints (public, free)
POLYGON_RPC_URLS = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.network",
    "https://polygon.llamarpc.com",
    "https://polygon-mainnet.public.blastapi.io",
]

# Chainlink Aggregator V3 Interface ABI (minimal)
AGGREGATOR_V3_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "description",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    }
]


class ChainlinkPriceFeed:
    """
    Reads BTC/USD price from Chainlink oracle on Polygon.
    
    Features:
    - Automatic RPC failover
    - Caching to reduce RPC calls
    - Thread-safe
    """
    
    def __init__(self, cache_ttl: float = 1.0):
        """
        Initialize the price feed reader.
        
        Args:
            cache_ttl: Cache time-to-live in seconds (default 1s)
        """
        self.cache_ttl = cache_ttl
        self._cached_price: Optional[float] = None
        self._cache_time: float = 0
        self._web3: Optional[Web3] = None
        self._contract = None
        self._decimals: Optional[int] = None
        self._rpc_index: int = 0
        
        # Initialize connection
        self._connect()
    
    def _connect(self) -> bool:
        """
        Connect to Polygon RPC with failover support.
        
        Returns:
            True if connected successfully
        """
        for i in range(len(POLYGON_RPC_URLS)):
            rpc_url = POLYGON_RPC_URLS[(self._rpc_index + i) % len(POLYGON_RPC_URLS)]
            try:
                self._web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 5}))
                
                if self._web3.is_connected():
                    # Create contract instance
                    self._contract = self._web3.eth.contract(
                        address=Web3.to_checksum_address(CHAINLINK_BTC_USD_POLYGON),
                        abi=AGGREGATOR_V3_ABI
                    )
                    
                    # Get decimals (usually 8 for BTC/USD)
                    self._decimals = self._contract.functions.decimals().call()
                    
                    logger.info(f"[Chainlink] Connected to Polygon RPC: {rpc_url[:30]}...")
                    self._rpc_index = (self._rpc_index + i) % len(POLYGON_RPC_URLS)
                    return True
                    
            except Exception as e:
                logger.debug(f"[Chainlink] RPC {rpc_url} failed: {e}")
                continue
        
        logger.error("[Chainlink] All Polygon RPC endpoints failed")
        return False
    
    def get_btc_price(self) -> Optional[float]:
        """
        Get the current BTC/USD price from Chainlink.
        
        Returns:
            BTC price in USD, or None if unavailable
        """
        # Check cache first
        now = time.time()
        if self._cached_price is not None and (now - self._cache_time) < self.cache_ttl:
            return self._cached_price
        
        # Fetch fresh price
        try:
            if not self._contract:
                if not self._connect():
                    return self._cached_price  # Return stale cache if reconnect fails
            
            # Call latestRoundData()
            # Returns: (roundId, answer, startedAt, updatedAt, answeredInRound)
            round_data = self._contract.functions.latestRoundData().call()
            
            # answer is the price with 8 decimals (for BTC/USD)
            raw_price = round_data[1]
            decimals = self._decimals or 8
            
            # Convert to float
            price = raw_price / (10 ** decimals)
            
            # Update cache
            self._cached_price = price
            self._cache_time = now
            
            return price
            
        except Exception as e:
            logger.warning(f"[Chainlink] Error fetching BTC price: {e}")
            
            # Try reconnecting
            self._contract = None
            if self._connect():
                # Retry once
                try:
                    round_data = self._contract.functions.latestRoundData().call()
                    raw_price = round_data[1]
                    decimals = self._decimals or 8
                    price = raw_price / (10 ** decimals)
                    self._cached_price = price
                    self._cache_time = now
                    return price
                except:
                    pass
            
            return self._cached_price  # Return stale cache
    
    def get_btc_price_with_metadata(self) -> Optional[dict]:
        """
        Get BTC price with additional metadata from Chainlink.
        
        Returns:
            Dict with price, updatedAt timestamp, and roundId
        """
        try:
            if not self._contract:
                if not self._connect():
                    return None
            
            round_data = self._contract.functions.latestRoundData().call()
            
            round_id = round_data[0]
            raw_price = round_data[1]
            updated_at = round_data[3]
            
            decimals = self._decimals or 8
            price = raw_price / (10 ** decimals)
            
            return {
                "price": price,
                "updated_at": updated_at,
                "round_id": round_id
            }
            
        except Exception as e:
            logger.warning(f"[Chainlink] Error fetching BTC metadata: {e}")
            return None


# Global singleton for performance
_price_feed: Optional[ChainlinkPriceFeed] = None


def get_btc_price() -> Optional[float]:
    """
    Convenience function to get BTC price using global singleton.
    
    Returns:
        BTC price in USD, or None if unavailable
    """
    global _price_feed
    
    if _price_feed is None:
        _price_feed = ChainlinkPriceFeed(cache_ttl=1.0)
    
    return _price_feed.get_btc_price()


def get_price_feed() -> ChainlinkPriceFeed:
    """
    Get the global price feed instance.
    
    Returns:
        ChainlinkPriceFeed singleton
    """
    global _price_feed
    
    if _price_feed is None:
        _price_feed = ChainlinkPriceFeed(cache_ttl=1.0)
    
    return _price_feed
