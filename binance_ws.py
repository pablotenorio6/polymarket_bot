"""
Binance WebSocket client for real-time BTCUSDT monitoring.

Architecture:
1. @bookTicker (Primary): Best bid/ask updates - shows "intention" before execution
2. @aggTrade (Confirmation): Trade flow to filter spoofs and confirm momentum

Implements Order Book Imbalance (OBI) detection for anticipating price moves.

Binance WebSocket docs:
https://binance-docs.github.io/apidocs/spot/en/#individual-symbol-book-ticker-streams
https://binance-docs.github.io/apidocs/spot/en/#aggregate-trade-streams
"""

import asyncio
import logging
from typing import Dict, Optional, Callable, List
from dataclasses import dataclass
from collections import deque
import time

import websockets
from websockets.exceptions import ConnectionClosed

try:
    import orjson
    def json_loads(s): return orjson.loads(s)
    def json_dumps(d): return orjson.dumps(d).decode('utf-8')
except ImportError:
    import json
    json_loads = json.loads
    json_dumps = json.dumps

logger = logging.getLogger(__name__)

# Binance WebSocket endpoints
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"
# Combined streams: bookTicker (primary) + aggTrade (confirmation)
BINANCE_STREAMS = "btcusdt@bookTicker/btcusdt@aggTrade"


@dataclass
class BreakoutSignal:
    """Represents a detected breakout signal"""
    direction: str       # 'UP' or 'DOWN'
    mid_price: float     # Current mid price
    ema_price: float     # EMA reference price
    deviation_pct: float # How far price deviated from EMA
    obi: float           # Order Book Imbalance (-1 to 1)
    volume_1s: float     # Trade volume in last second (BTC)
    timestamp: float     # Detection time


class BinanceBreakoutDetector:
    """
    Real-time Binance BTCUSDT breakout detector.
    
    Uses:
    - @bookTicker for best bid/ask (primary signal)
    - @aggTrade for volume confirmation (anti-spoof filter)
    - OBI (Order Book Imbalance) for pressure detection
    - EMA for micro-breakout detection
    """
    
    def __init__(
        self,
        # Trigger thresholds
        obi_threshold: float = 0.6,           # OBI > 0.6 for UP, < -0.6 for DOWN
        deviation_threshold_pct: float = 0.02, # Price deviation from EMA to trigger (0.02%)
        min_volume_1s: float = 1.0,           # Minimum BTC volume in last second
        
        # EMA parameters
        ema_periods: int = 100,               # EMA lookback (in ticks, ~10s at 100ms/tick)
        
        # Callback
        on_breakout_detected: Optional[Callable[[BreakoutSignal], None]] = None
    ):
        self.obi_threshold = obi_threshold
        self.deviation_threshold_pct = deviation_threshold_pct
        self.min_volume_1s = min_volume_1s
        self.ema_periods = ema_periods
        self.on_breakout_detected = on_breakout_detected
        
        # Current best bid/ask from @bookTicker
        self.best_bid: float = 0.0
        self.best_bid_qty: float = 0.0
        self.best_ask: float = 0.0
        self.best_ask_qty: float = 0.0
        self.mid_price: float = 0.0
        
        # OBI (Order Book Imbalance)
        self.obi: float = 0.0
        
        # EMA tracking
        self.ema_price: float = 0.0
        self.ema_alpha: float = 2.0 / (ema_periods + 1)  # EMA smoothing factor
        self.tick_count: int = 0
        
        # Trade volume tracking (from @aggTrade)
        # Store recent trades: (timestamp, quantity, is_buyer_maker)
        self.recent_trades: deque = deque(maxlen=1000)
        self.volume_window_seconds: float = 1.0  # Look at last 1 second
        
        # Connection state
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self.running = False
        
        # Stats
        self.bookticker_count = 0
        self.aggtrade_count = 0
        self.breakouts_detected = 0
        
        # Cooldown to avoid rapid-fire signals
        self.last_signal_time: float = 0
        self.signal_cooldown_seconds: float = 0.5  # Min time between signals
        
        # Last signal direction (avoid duplicate signals in same direction)
        self.last_signal_direction: Optional[str] = None
    
    async def connect(self) -> bool:
        """Connect to Binance combined WebSocket stream"""
        url = f"{BINANCE_WS_URL}?streams={BINANCE_STREAMS}"
        try:
            self.ws = await websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            )
            self.connected = True
            logger.info(f"[BINANCE] Connected to {BINANCE_STREAMS}")
            return True
        except Exception as e:
            logger.error(f"[BINANCE] Connection failed: {e}")
            self.connected = False
            return False
    
    async def start(self):
        """Start listening for market data"""
        self.running = True
        
        while self.running:
            if not self.connected:
                if not await self.connect():
                    await asyncio.sleep(5)
                    continue
            
            try:
                async for message in self.ws:
                    if not self.running:
                        break
                    self._process_message(message)
                    
            except ConnectionClosed:
                logger.warning("[BINANCE] Connection closed, reconnecting...")
                self.connected = False
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[BINANCE] Error: {e}")
                self.connected = False
                await asyncio.sleep(1)
    
    def _process_message(self, raw_message: str):
        """Process incoming WebSocket message"""
        try:
            data = json_loads(raw_message)
            
            # Combined stream format: {"stream": "btcusdt@bookTicker", "data": {...}}
            stream = data.get('stream', '')
            payload = data.get('data', {})
            
            if 'bookTicker' in stream:
                self._process_bookticker(payload)
            elif 'aggTrade' in stream:
                self._process_aggtrade(payload)
                
        except Exception as e:
            logger.debug(f"[BINANCE] Message parse error: {e}")
    
    def _process_bookticker(self, data: Dict):
        """
        Process @bookTicker message (PRIMARY SIGNAL)
        
        Format:
        {
            "u": 400900217,     // order book updateId
            "s": "BTCUSDT",     // symbol
            "b": "25.35190000", // best bid price
            "B": "31.21000000", // best bid qty
            "a": "25.36520000", // best ask price
            "A": "40.66000000"  // best ask qty
        }
        """
        self.bookticker_count += 1
        
        self.best_bid = float(data.get('b', 0))
        self.best_bid_qty = float(data.get('B', 0))
        self.best_ask = float(data.get('a', 0))
        self.best_ask_qty = float(data.get('A', 0))
        
        if self.best_bid > 0 and self.best_ask > 0:
            self.mid_price = (self.best_bid + self.best_ask) / 2
            
            # Calculate OBI (Order Book Imbalance)
            total_qty = self.best_bid_qty + self.best_ask_qty
            if total_qty > 0:
                self.obi = (self.best_bid_qty - self.best_ask_qty) / total_qty
            
            # Update EMA
            self.tick_count += 1
            if self.ema_price == 0:
                self.ema_price = self.mid_price  # Initialize
            else:
                self.ema_price = self.ema_alpha * self.mid_price + (1 - self.ema_alpha) * self.ema_price
            
            # Log periodically
            if self.bookticker_count <= 3 or self.bookticker_count % 1000 == 0:
                spread = self.best_ask - self.best_bid
                logger.info(
                    f"[BOOKTICKER] #{self.bookticker_count} | "
                    f"mid=${self.mid_price:,.2f} ema=${self.ema_price:,.2f} | "
                    f"bid=${self.best_bid:,.2f}x{self.best_bid_qty:.2f} "
                    f"ask=${self.best_ask:,.2f}x{self.best_ask_qty:.2f} | "
                    f"spread=${spread:.2f} | OBI={self.obi:+.3f}"
                )
            
            # Check for breakout signal
            self._check_breakout()
    
    def _process_aggtrade(self, data: Dict):
        """
        Process @aggTrade message (CONFIRMATION)
        
        Format:
        {
            "e": "aggTrade",
            "E": 123456789,     // Event time
            "s": "BTCUSDT",
            "a": 12345,         // Aggregate trade ID
            "p": "0.001",       // Price
            "q": "100",         // Quantity
            "f": 100,           // First trade ID
            "l": 105,           // Last trade ID
            "T": 123456785,     // Trade time
            "m": true,          // Is buyer maker? (true = SELL, false = BUY)
            "M": true           // Ignore
        }
        """
        self.aggtrade_count += 1
        
        trade_time = data.get('T', time.time() * 1000) / 1000  # Convert to seconds
        qty = float(data.get('q', 0))
        is_buyer_maker = data.get('m', False)  # True = seller aggressor (SELL), False = buyer aggressor (BUY)
        
        self.recent_trades.append((trade_time, qty, is_buyer_maker))
        
        # Log large trades
        price = float(data.get('p', 0))
        usd_value = qty * price
        if usd_value >= 100_000:  # Log trades >= $100k
            side = "SELL" if is_buyer_maker else "BUY"
            logger.info(f"[AGGTRADE] {side} ${usd_value:,.0f} ({qty:.4f} BTC) @ ${price:,.2f}")
    
    def _get_recent_volume(self, side: Optional[str] = None) -> float:
        """
        Get trade volume in last N seconds.
        
        Args:
            side: 'BUY', 'SELL', or None for total
        """
        now = time.time()
        cutoff = now - self.volume_window_seconds
        
        total_volume = 0.0
        for trade_time, qty, is_buyer_maker in self.recent_trades:
            if trade_time < cutoff:
                continue
            
            if side is None:
                total_volume += qty
            elif side == 'BUY' and not is_buyer_maker:
                total_volume += qty
            elif side == 'SELL' and is_buyer_maker:
                total_volume += qty
        
        return total_volume
    
    def _check_breakout(self):
        """Check if conditions are met for a breakout signal"""
        # Need enough data to establish EMA
        if self.tick_count < 10:
            return
        
        # Calculate deviation from EMA
        if self.ema_price <= 0:
            return
        
        deviation_pct = ((self.mid_price - self.ema_price) / self.ema_price) * 100
        
        # Get recent volume
        volume_1s = self._get_recent_volume()
        
        # Determine direction and check conditions
        signal_direction = None
        
        # UP breakout: price above EMA + OBI positive + volume confirmation
        if (deviation_pct >= self.deviation_threshold_pct and 
            self.obi >= self.obi_threshold and 
            volume_1s >= self.min_volume_1s):
            signal_direction = 'UP'
        
        # DOWN breakout: price below EMA + OBI negative + volume confirmation
        elif (deviation_pct <= -self.deviation_threshold_pct and 
              self.obi <= -self.obi_threshold and 
              volume_1s >= self.min_volume_1s):
            signal_direction = 'DOWN'
        
        if signal_direction is None:
            return
        
        # Cooldown check
        now = time.time()
        if now - self.last_signal_time < self.signal_cooldown_seconds:
            return
        
        # Avoid duplicate signals in same direction
        if signal_direction == self.last_signal_direction:
            return
        
        # Fire signal!
        self.last_signal_time = now
        self.last_signal_direction = signal_direction
        self.breakouts_detected += 1
        
        signal = BreakoutSignal(
            direction=signal_direction,
            mid_price=self.mid_price,
            ema_price=self.ema_price,
            deviation_pct=deviation_pct,
            obi=self.obi,
            volume_1s=volume_1s,
            timestamp=now
        )
        
        self._on_breakout(signal)
    
    def _on_breakout(self, signal: BreakoutSignal):
        """Handle detected breakout signal"""
        logger.info(
            f"[BREAKOUT] {signal.direction} | "
            f"mid=${signal.mid_price:,.2f} (ema=${signal.ema_price:,.2f}, dev={signal.deviation_pct:+.3f}%) | "
            f"OBI={signal.obi:+.3f} | vol_1s={signal.volume_1s:.2f} BTC"
        )
        
        if self.on_breakout_detected:
            try:
                self.on_breakout_detected(signal)
            except Exception as e:
                logger.error(f"[BREAKOUT] Callback error: {e}")
    
    def get_state(self) -> Dict:
        """Get current detector state"""
        return {
            'mid_price': self.mid_price,
            'ema_price': self.ema_price,
            'best_bid': self.best_bid,
            'best_bid_qty': self.best_bid_qty,
            'best_ask': self.best_ask,
            'best_ask_qty': self.best_ask_qty,
            'spread': self.best_ask - self.best_bid if self.best_ask > 0 else 0,
            'obi': self.obi,
            'volume_1s': self._get_recent_volume(),
            'volume_1s_buy': self._get_recent_volume('BUY'),
            'volume_1s_sell': self._get_recent_volume('SELL'),
            'bookticker_count': self.bookticker_count,
            'aggtrade_count': self.aggtrade_count,
            'breakouts_detected': self.breakouts_detected
        }
    
    async def close(self):
        """Close connection"""
        self.running = False
        if self.ws:
            await self.ws.close()
            self.connected = False
        logger.info(
            f"[BINANCE] Closed. "
            f"BookTicker: {self.bookticker_count}, AggTrade: {self.aggtrade_count}, "
            f"Breakouts: {self.breakouts_detected}"
        )


# Simple test
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
    
    def on_breakout(signal: BreakoutSignal):
        print(f"\n{'='*60}")
        print(f"BREAKOUT DETECTED: {signal}")
        print(f"{'='*60}\n")
    
    async def main():
        detector = BinanceBreakoutDetector(
            obi_threshold=0.3,           # Lower for testing
            deviation_threshold_pct=0.01, # 0.01% for testing
            min_volume_1s=0.1,           # Lower for testing
            on_breakout_detected=on_breakout
        )
        
        try:
            await detector.start()
        except KeyboardInterrupt:
            await detector.close()
    
    asyncio.run(main())
