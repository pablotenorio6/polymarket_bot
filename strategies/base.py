"""
Base strategy class - defines the interface for all trading strategies

Strategies receive events from the bot and decide what actions to take.
The bot handles all infrastructure: market monitoring, WebSocket connections,
order execution, data collection.

Strategy lifecycle:
1. initialize() - Called once at startup
2. on_new_market() - Called when a new future market is detected
3. on_market_active() - Called when a market becomes active (trading starts)
4. on_price_update() - Called on each price update (real-time)
5. on_market_end() - Called when market ends (before resolution)
6. shutdown() - Called on bot shutdown
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, Set, TYPE_CHECKING
from datetime import datetime
import logging

if TYPE_CHECKING:
    from trader import FastTrader
    from monitor import FastMarketMonitor
    from ws_monitor import HybridPriceMonitor
    from data_collector import DataCollector
    from rtds_crypto_prices import RTDSCryptoPrices

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    Abstract base class for trading strategies.
    
    Subclasses must implement the abstract methods to define
    their specific trading logic.
    
    Attributes:
        trader: FastTrader instance for placing orders
        monitor: FastMarketMonitor for market data
        ws_monitor: HybridPriceMonitor for real-time prices
        data_collector: DataCollector for recording data
        rtds_client: RTDSCryptoPrices for crypto prices
        market_symbol: Crypto symbol (BTC, ETH, SOL)
        entry_price: Price for limit orders
        position_size: Size of orders in USD
    """
    
    name: str = "base"  # Override in subclasses
    description: str = "Base strategy - do not use directly"
    
    # Resource requirements - override in subclasses to disable
    requires_price_websocket: bool = True   # Real-time token prices via WebSocket
    requires_data_collector: bool = True    # Send market data to API
    requires_rtds: bool = True              # Real-time crypto prices (BTC/ETH/SOL)
    
    def __init__(
        self,
        trader: "FastTrader",
        monitor: "FastMarketMonitor",
        ws_monitor: "HybridPriceMonitor",
        data_collector: "DataCollector",
        rtds_client: Optional["RTDSCryptoPrices"],
        market_symbol: str,
        entry_price: float,
        position_size: float,
    ):
        self.trader = trader
        self.monitor = monitor
        self.ws_monitor = ws_monitor
        self.data_collector = data_collector
        self.rtds_client = rtds_client
        self.market_symbol = market_symbol
        self.entry_price = entry_price
        self.position_size = position_size
        
        # Track which markets have been processed
        self.processed_markets: Set[str] = set()
    
    async def initialize(self) -> None:
        """
        Called once at bot startup.
        
        Override to perform any initialization:
        - Recover state from API
        - Load historical data
        - Set up strategy-specific resources
        """
        pass
    
    @abstractmethod
    async def on_new_market(self, market_data: Dict) -> None:
        """
        Called when a new FUTURE market is detected (before it becomes active).
        
        This is the earliest opportunity to place orders on a market.
        
        Args:
            market_data: Dict containing:
                - condition_id: Unique market identifier
                - question: Market question/title
                - up_token_id: Token ID for UP outcome
                - down_token_id: Token ID for DOWN outcome
                - start_time: When market becomes active (datetime)
                - end_time: When market closes (datetime)
                - market: Raw market data from Polymarket
        """
        pass
    
    async def on_market_active(self, market_data: Dict) -> None:
        """
        Called when a market becomes active (trading officially starts).
        
        Override if you need to take action at market start.
        
        Args:
            market_data: Same structure as on_new_market
        """
        pass
    
    @abstractmethod
    async def on_price_update(
        self,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market: Dict
    ) -> None:
        """
        Called on each price update from WebSocket/HTTP.
        
        This is your main decision point for reactive strategies.
        Called approximately every 10-100ms.
        
        Args:
            up_price: Current UP token price (0.01-0.99)
            down_price: Current DOWN token price (0.01-0.99)
            up_token_id: Token ID for UP
            down_token_id: Token ID for DOWN
            market: Raw market data
        """
        pass
    
    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        """
        Called when a market ends (before resolution).
        
        Override if you need to take action before market resolves.
        
        Args:
            market_data: Market information
            winner: Predicted winner based on final prices ('UP', 'DOWN', or None)
        """
        pass
    
    async def shutdown(self) -> None:
        """
        Called on bot shutdown.
        
        Override to clean up strategy-specific resources.
        """
        pass
    
    # ==================== Helper Methods ====================
    
    def get_crypto_price(self) -> Optional[float]:
        """Get current crypto price from RTDS"""
        if self.rtds_client:
            return self.rtds_client.get_price(self.market_symbol)
        return None
    
    def place_buy_order(
        self,
        token_id: str,
        side: str,
        price: Optional[float] = None,
        size: Optional[float] = None,
        order_type: str = "GTC"
    ) -> Optional[Dict]:
        """
        Helper to place a buy order.
        
        Args:
            token_id: Token to buy
            side: 'up' or 'down' (for logging)
            price: Order price (defaults to self.entry_price)
            size: Order size (defaults to self.position_size)
            order_type: 'GTC' (Good Till Cancelled) or 'FOK' (Fill or Kill)
        
        Returns:
            Order result or None if failed
        """
        return self.trader.place_buy_order(
            token_id=token_id,
            side=side,
            price=price or self.entry_price,
            size=size or self.position_size,
            market_info=None,
            order_type=order_type
        )
    
    def is_market_processed(self, condition_id: str) -> bool:
        """Check if we've already processed this market"""
        return condition_id in self.processed_markets
    
    def mark_market_processed(self, condition_id: str) -> None:
        """Mark a market as processed"""
        self.processed_markets.add(condition_id)
