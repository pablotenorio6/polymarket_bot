"""
Whale Frontrun Strategy (v2)

Monitors Binance BTCUSDT for micro-breakouts and frontruns them on Polymarket 
HOURLY Bitcoin markets (btc-1h). 

IMPORTANT: This strategy is designed for HOURLY markets which have NO TRADING FEES.
Use with: python main.py --market btc-1h --strategy whale_frontrun

Architecture:
1. Binance @bookTicker (primary): Best bid/ask for OBI and price
2. Binance @aggTrade (confirmation): Volume filter to avoid spoofs
3. Polymarket orderbook (local cache): Via REST, get best prices
4. Order type: FOK (Fill or Kill) with aggressive price (no fees on hourly markets!)

Signal Logic:
- OBI (Order Book Imbalance) > threshold indicates directional pressure
- Price deviation from EMA indicates momentum
- Volume confirmation filters out spoofs/fakes

Configuration via environment variables:
- BREAKOUT_OBI_THRESHOLD: OBI threshold for signal (default: 0.6)
- BREAKOUT_DEVIATION_PCT: Price deviation from EMA % (default: 0.02)
- BREAKOUT_MIN_VOLUME: Min BTC volume in 1s (default: 1.0)
- BREAKOUT_COPY_SIZE: Shares to buy (default: 10)
- BREAKOUT_PRICE_SLIPPAGE: Extra price % to add for aggressive order (default: 0.03 = 3%)
"""

import os
import asyncio
import logging
from typing import Dict, Optional, Set
from datetime import datetime

from strategies.base import BaseStrategy
from binance_ws import BinanceBreakoutDetector, BreakoutSignal

logger = logging.getLogger(__name__)


class WhaleFrontrunStrategy(BaseStrategy):
    """
    Frontrun Binance micro-breakouts on Polymarket.
    
    When Binance shows:
    - OBI (Order Book Imbalance) indicating strong directional pressure
    - Price deviating from EMA (momentum)
    - Volume confirmation (not a spoof)
    
    Then:
    - BUY breakout → buy UP token on Polymarket
    - SELL breakout → buy DOWN token on Polymarket
    
    Uses IOC orders with aggressive pricing to ensure fill or cancel.
    """
    
    name = "whale_frontrun"
    description = "Frontrun Binance micro-breakouts on Polymarket"
    
    # Resource requirements
    requires_price_websocket = False   # We use our own Binance WS
    requires_data_collector = False
    requires_rtds = False
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Breakout detection thresholds from environment
        self.OBI_THRESHOLD = float(os.getenv("BREAKOUT_OBI_THRESHOLD", "0.6"))
        self.DEVIATION_PCT = float(os.getenv("BREAKOUT_DEVIATION_PCT", "0.02"))
        self.MIN_VOLUME = float(os.getenv("BREAKOUT_MIN_VOLUME", "1.0"))
        
        # Trading parameters
        self.COPY_SIZE = float(os.getenv("BREAKOUT_COPY_SIZE", "2"))  # Shares to buy
        self.PRICE_SLIPPAGE = float(os.getenv("BREAKOUT_PRICE_SLIPPAGE", "0.01"))  # 1% extra for IOC
        
        # Current market state
        self.current_up_token: Optional[str] = None
        self.current_down_token: Optional[str] = None
        self.current_market: Optional[Dict] = None
        self.market_active = False
        
        # Binance breakout detector
        self.binance_detector: Optional[BinanceBreakoutDetector] = None
        self._binance_task: Optional[asyncio.Task] = None
        
        # Track orders to avoid duplicates
        self.pending_orders: Set[str] = set()  # token_id
        
        # Cooldown per direction to avoid rapid-fire
        self.last_up_order_time: float = 0
        self.last_down_order_time: float = 0
        self.direction_cooldown_seconds = 10.0  # 10s cooldown per direction
        
        # Stats
        self.breakouts_received = 0
        self.orders_placed = 0
        self.orders_filled = 0
    
    async def initialize(self) -> None:
        """Start Binance breakout detector"""
        logger.info(f"Initializing {self.name} strategy...")
        logger.info(f"  OBI threshold: {self.OBI_THRESHOLD}")
        logger.info(f"  Deviation threshold: {self.DEVIATION_PCT}%")
        logger.info(f"  Min volume (1s): {self.MIN_VOLUME} BTC")
        logger.info(f"  Copy size: {self.COPY_SIZE} shares")
        logger.info(f"  Price slippage: {self.PRICE_SLIPPAGE*100:.1f}%")
        
        # Create Binance detector with callback
        self.binance_detector = BinanceBreakoutDetector(
            obi_threshold=self.OBI_THRESHOLD,
            deviation_threshold_pct=self.DEVIATION_PCT,
            min_volume_1s=self.MIN_VOLUME,
            on_breakout_detected=self._on_breakout_detected_sync
        )
        
        # Start Binance detector in background
        self._binance_task = asyncio.create_task(self._run_binance_detector())
        
        logger.info(f"[{self.name}] Strategy initialized")
    
    async def _run_binance_detector(self):
        """Background task for Binance breakout detector"""
        try:
            await self.binance_detector.start()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[BINANCE] Detector error: {e}")
    
    def _on_breakout_detected_sync(self, signal: BreakoutSignal):
        """
        Callback when breakout detected (called from Binance WS loop).
        Schedules async handler on event loop.
        """
        self.breakouts_received += 1
        
        # Schedule async handler
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._handle_breakout(signal))
        except Exception as e:
            logger.error(f"Failed to schedule breakout handler: {e}")
    
    async def _handle_breakout(self, signal: BreakoutSignal):
        """Handle detected breakout - place order on Polymarket"""
        if not self.market_active:
            logger.debug(f"[{self.name}] Breakout detected but no active market")
            return
        
        if not self.current_up_token or not self.current_down_token:
            logger.debug(f"[{self.name}] Breakout detected but tokens not set")
            return
        
        # Determine which token to buy and check cooldown
        now = datetime.now().timestamp()
        
        if signal.direction == 'UP':
            token_id = self.current_up_token
            side = 'UP'
            if now - self.last_up_order_time < self.direction_cooldown_seconds:
                logger.debug(f"[{self.name}] UP cooldown active, skipping")
                return
        else:
            token_id = self.current_down_token
            side = 'DOWN'
            if now - self.last_down_order_time < self.direction_cooldown_seconds:
                logger.debug(f"[{self.name}] DOWN cooldown active, skipping")
                return
        
        # Check if we already have pending order for this token
        if token_id in self.pending_orders:
            logger.debug(f"[{self.name}] Already have pending order for {side}")
            return
        
        # Get best ask price from Polymarket
        best_ask = await self._get_best_ask(token_id)
        if best_ask is None or best_ask <= 0:
            logger.warning(f"[{self.name}] Could not get best ask for {side}")
            return
        
        # Calculate aggressive price (add slippage for IOC)
        # If best ask is 0.50, with 3% slippage we bid 0.53
        aggressive_price = min(best_ask * (1 + self.PRICE_SLIPPAGE), 0.99)
        aggressive_price = round(aggressive_price, 2)
        
        logger.info(
            f"[FRONTRUN] {side} | Breakout: mid=${signal.mid_price:,.2f} OBI={signal.obi:+.2f} | "
            f"Polymarket: ask=${best_ask:.2f} → order=${aggressive_price:.2f}"
        )
        
        self.pending_orders.add(token_id)
        
        # Update cooldown BEFORE placing order
        if side == 'UP':
            self.last_up_order_time = now
        else:
            self.last_down_order_time = now
        
        try:
            # Place IOC order with aggressive price
            # IOC = Immediate or Cancel - fills what it can, cancels rest
            order_result = self.trader.place_buy_order(
                token_id=token_id,
                side=side.lower(),
                price=aggressive_price,
                size=self.COPY_SIZE,
                market_info=self.current_market,
                order_type="FOK"  # FOK acts as IOC for full size - fill all or cancel
            )
            
            if order_result:
                self.orders_placed += 1
                success = order_result.get('success', False)
                status = order_result.get('status', 'unknown')
                
                if success and status == 'matched':
                    self.orders_filled += 1
                    logger.info(
                        f"[FRONTRUN] FILLED: {side} {self.COPY_SIZE} @ ${aggressive_price:.2f} | "
                        f"Binance: OBI={signal.obi:+.2f} vol={signal.volume_1s:.2f}BTC"
                    )
                elif status == 'live':
                    logger.info(f"[FRONTRUN] Order LIVE (in orderbook): {side} @ ${aggressive_price:.2f}")
                else:
                    logger.info(
                        f"[FRONTRUN] Order placed: {side} @ ${aggressive_price:.2f} | "
                        f"status={status}"
                    )
            else:
                logger.warning(f"[FRONTRUN] Order failed for {side}")
                
        except Exception as e:
            logger.error(f"[FRONTRUN] Order error: {e}")
        finally:
            self.pending_orders.discard(token_id)
    
    async def _get_best_ask(self, token_id: str) -> Optional[float]:
        """Get best ask price from Polymarket orderbook (via REST)"""
        try:
            if not self.trader.client:
                return None
            
            # get_price with side="BUY" returns the best ask (price to buy at)
            response = self.trader.client.get_price(token_id, side="BUY")
            
            if isinstance(response, dict):
                price = response.get('price')
            else:
                price = response
            
            if price:
                return float(price)
            
        except Exception as e:
            logger.debug(f"Error getting best ask: {e}")
        
        return None
    
    async def on_new_market(self, market_data: Dict) -> None:
        """Called when new market detected (before active)"""
        # We wait for market_active to start trading
        pass
    
    async def on_market_active(self, market_data: Dict) -> None:
        """Called when market becomes active for trading"""
        self.current_up_token = market_data.get('up_token_id')
        self.current_down_token = market_data.get('down_token_id')
        self.current_market = market_data.get('market')
        self.market_active = True
        
        # Reset direction cooldowns for new market
        self.last_up_order_time = 0
        self.last_down_order_time = 0
        
        title = market_data.get('question', 'Unknown')[:50]
        logger.info(f"[{self.name}] Market active: {title}")
        logger.info(f"[{self.name}] UP: {self.current_up_token[:16]}...")
        logger.info(f"[{self.name}] DOWN: {self.current_down_token[:16]}...")
        
        # Log current Binance state
        if self.binance_detector and self.binance_detector.mid_price > 0:
            state = self.binance_detector.get_state()
            logger.info(
                f"[{self.name}] Binance BTC: ${state['mid_price']:,.2f} | "
                f"OBI={state['obi']:+.3f} | "
                f"vol_1s={state['volume_1s']:.2f} BTC"
            )
    
    async def on_price_update(
        self,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market: Dict
    ) -> None:
        """Called on price updates - we don't use this, we react to Binance breakouts"""
        pass
    
    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        """Called when market ends"""
        self.market_active = False
        self.current_up_token = None
        self.current_down_token = None
        self.current_market = None
        
        logger.info(
            f"[{self.name}] Market ended. "
            f"Breakouts: {self.breakouts_received} | "
            f"Orders: {self.orders_placed} | "
            f"Filled: {self.orders_filled}"
        )
    
    async def shutdown(self) -> None:
        """Clean up resources"""
        logger.info(f"[{self.name}] Shutting down...")
        
        # Stop Binance detector
        if self.binance_detector:
            await self.binance_detector.close()
        
        if self._binance_task:
            self._binance_task.cancel()
            try:
                await self._binance_task
            except asyncio.CancelledError:
                pass
        
        logger.info(
            f"[{self.name}] Shutdown complete. "
            f"Total breakouts: {self.breakouts_received} | "
            f"Orders: {self.orders_placed} | "
            f"Filled: {self.orders_filled}"
        )
