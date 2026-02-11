"""
Whale Frontrun Strategy (v3) - BaseStrategy port of frontrun_strategy.py

Monitors Binance aggTrade and Polymarket orderbook via WebSocket.
Uses dynamic threshold (percentile of last N minutes of gross volume) to detect
significant directional volume imbalances. Logs signal and price evolution to CSV.

Core logic:
- Direct Binance aggTrade WebSocket for trade flow
- Direct Polymarket orderbook WebSocket for bid/ask prices
- Dynamic percentile-based thresholds (robust to changing volatility regimes)
- Black-Scholes theoretical pricing for UP/DOWN tokens
- EWMA volatility from Binance 1-min candles
- Timed position exits (buy -> hold EXIT_DELAY_MS -> sell)
- WebSocket fills tracking for position management
- CSV signal logging with price evolution at 250ms intervals

Configuration via environment variables:
- MIN_THRESHOLD_BTC: Floor minimum for threshold (default: 2)
- PERCENTILE_THRESHOLD: Percentile for dynamic threshold (default: 99.9)
- LOOKBACK_MS: Lookback window for threshold calc (default: 600000)
- MIN_VOLUME_HISTORY: Min samples before trading (default: 1000)
- TRADING_ENABLED: Enable live trading (default: false)
- POSITION_SIZE_USD: USD per trade (default: 10)
- EXIT_DELAY_MS: Hold time before selling (default: 4000)
- TRADE_COOLDOWN_MS: Cooldown between trades (default: 5000)
- ALLOWED_SLIPPAGE: Max slippage on buy (default: 0.015)
- OUTPUT_DIR: Directory for CSV output (default: .)

Use with: python main.py --market btc-1h --strategy whale_frontrun
"""

import asyncio
import csv
import json
import time
import os
import logging
import math
from datetime import datetime, timezone
from typing import Optional, Dict, List
from dataclasses import dataclass, asdict
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import websockets

from strategies.base import BaseStrategy
from ws_monitor import WebSocketUserFillsTracker

logger = logging.getLogger(__name__)

# Pricing constants
VOLATILITY_UPDATE_INTERVAL = 60
VOLATILITY_LOOKBACK_HOURS = 8
MINUTES_PER_YEAR = 365 * 24 * 60
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
EWMA_LAMBDA = 0.97
VOLATILITY_MULTIPLIER = 3
MIN_VOLATILITY = 0.25

# Trade aggregation
AGG_WINDOW_MS = 100
TRACK_DURATION_MS = 5000
TRACK_INTERVAL_MS = 500

# Dynamic threshold configuration
MIN_THRESHOLD_BTC = float(os.environ.get('MIN_THRESHOLD_BTC', '2'))
PERCENTILE_THRESHOLD = float(os.environ.get('PERCENTILE_THRESHOLD', '99.9'))
LOOKBACK_MS = int(os.environ.get('LOOKBACK_MS', '600000'))
MIN_VOLUME_HISTORY = int(os.environ.get('MIN_VOLUME_HISTORY', '1000'))

# Trading configuration
TRADING_ENABLED = os.environ.get('TRADING_ENABLED', 'false').lower() == 'true'
POSITION_SIZE_USD = float(os.environ.get('POSITION_SIZE_USD', '10'))
EXIT_DELAY_MS = int(os.environ.get('EXIT_DELAY_MS', '4000'))
TRADE_COOLDOWN_MS = int(os.environ.get('TRADE_COOLDOWN_MS', '5000'))
ALLOWED_SLIPPAGE = float(os.environ.get('ALLOWED_SLIPPAGE', '0.015'))

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

OUTPUT_DIR = os.environ.get('OUTPUT_DIR', '.')


@dataclass
class Signal:
    """Trade signal with price evolution tracking"""
    signal_id: int
    timestamp_ms: int
    timestamp_iso: str
    direction: str  # 'BUY' or 'SELL'
    total_qty_btc: float
    trade_count: int
    binance_price: float
    threshold_used: float
    dynamic_threshold_calc: float

    # Theoretical pricing (Black-Scholes)
    strike_price: Optional[float] = None
    volatility: Optional[float] = None
    minutes_remaining: Optional[float] = None
    theo_up: Optional[float] = None
    theo_down: Optional[float] = None

    # Initial prices at T=0
    up_ask_0: Optional[float] = None
    up_bid_0: Optional[float] = None
    down_ask_0: Optional[float] = None
    down_bid_0: Optional[float] = None

    # Prices at each interval
    up_ask_250: Optional[float] = None
    up_ask_500: Optional[float] = None
    up_ask_750: Optional[float] = None
    up_ask_1000: Optional[float] = None
    up_ask_1500: Optional[float] = None
    up_ask_2000: Optional[float] = None
    up_ask_2500: Optional[float] = None
    up_ask_3000: Optional[float] = None
    up_ask_3500: Optional[float] = None
    up_ask_4000: Optional[float] = None
    up_ask_4500: Optional[float] = None
    up_ask_5000: Optional[float] = None

    down_ask_250: Optional[float] = None
    down_ask_500: Optional[float] = None
    down_ask_750: Optional[float] = None
    down_ask_1000: Optional[float] = None
    down_ask_1500: Optional[float] = None
    down_ask_2000: Optional[float] = None
    down_ask_2500: Optional[float] = None
    down_ask_3000: Optional[float] = None
    down_ask_3500: Optional[float] = None
    down_ask_4000: Optional[float] = None
    down_ask_4500: Optional[float] = None
    down_ask_5000: Optional[float] = None


class WhaleFrontrunStrategy(BaseStrategy):
    """
    Low-latency strategy that monitors Binance aggTrade volume and
    Polymarket orderbook to frontrun directional moves.

    Manages its own WebSocket connections (Binance + Polymarket orderbook)
    and background async loops internally.
    """

    name = "whale_frontrun"
    description = "Frontrun Binance volume signals on Polymarket"

    # Resource requirements - all handled internally
    requires_price_websocket = False
    requires_data_collector = False
    requires_rtds = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Token IDs for current market (set via on_market_active)
        self.up_token_id: str = ''
        self.down_token_id: str = ''
        self.condition_id: str = ''
        self.running = False

        # Current Polymarket orderbook state (from own WS)
        self.up_bid: Optional[float] = None
        self.up_ask: Optional[float] = None
        self.down_bid: Optional[float] = None
        self.down_ask: Optional[float] = None

        # Binance trades buffer with incremental sums
        self.trade_buffer: deque = deque()
        self.buy_qty_sum: float = 0.0
        self.sell_qty_sum: float = 0.0
        self.last_signal_ms: int = 0
        self.last_price: float = 0.0

        # Dynamic threshold tracking
        self.volume_history: deque = deque()
        self.current_window_ms: int = 0
        self.current_window_total_vol: float = 0.0
        self.current_threshold: float = MIN_THRESHOLD_BTC
        self.dynamic_threshold_calc: float = MIN_THRESHOLD_BTC

        # Active signals being tracked
        self.active_signals: Dict[int, Signal] = {}
        self.signal_counter: int = 0

        # CSV output
        self.csv_file = None
        self.csv_writer = None
        self.signals_written = 0

        # Polymarket WS reconnect flag
        self.needs_reconnect: bool = False

        # Pricing cache
        self.strike_price: Optional[float] = None
        self.cached_volatility: float = 0.30

        # Trading execution
        self.fills_tracker: Optional[WebSocketUserFillsTracker] = None
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="frontrun_exec")
        self.active_positions: Dict[int, Dict] = {}
        self.pending_buy_fills: Dict[str, List] = {}
        self.pending_sell_fills: Dict[str, List] = {}
        self.last_trade_ms: int = 0

        # Background tasks
        self._tasks: List[asyncio.Task] = []

    # ==================== BaseStrategy lifecycle ====================

    async def initialize(self) -> None:
        """Start all background async loops"""
        logger.info(
            f"[{self.name}] Initializing | threshold_min={MIN_THRESHOLD_BTC} "
            f"percentile={PERCENTILE_THRESHOLD} lookback={LOOKBACK_MS/1000:.0f}s"
        )

        if TRADING_ENABLED:
            logger.info(f"[{self.name}] TRADING ENABLED: ${POSITION_SIZE_USD}/trade, exit after {EXIT_DELAY_MS}ms")
        else:
            logger.info(f"[{self.name}] TRADING DISABLED (simulation mode)")

        self._open_csv()
        self.running = True

        # Start background loops
        self._tasks = [
            asyncio.create_task(self._binance_ws_loop()),
            asyncio.create_task(self._tracker_loop()),
            asyncio.create_task(self._threshold_update_loop()),
            asyncio.create_task(self._volatility_update_loop()),
        ]

        if TRADING_ENABLED:
            self._tasks.append(asyncio.create_task(self._position_manager_loop()))

        logger.info(f"[{self.name}] Strategy initialized ({len(self._tasks)} background tasks)")

    async def on_new_market(self, market_data: Dict) -> None:
        """Called when new market detected - store tokens and start Polymarket WS"""
        up_token = market_data.get('up_token_id', '')
        down_token = market_data.get('down_token_id', '')
        condition_id = market_data.get('condition_id', '')

        if not up_token or not down_token:
            return

        old_up = self.up_token_id
        self.up_token_id = up_token
        self.down_token_id = down_token
        self.condition_id = condition_id

        # Clear stale price data
        self.up_bid = None
        self.up_ask = None
        self.down_bid = None
        self.down_ask = None

        # Discard signals from previous market
        if self.active_signals:
            logger.info(f"[{self.name}] Discarding {len(self.active_signals)} signals from previous market")
            self.active_signals.clear()

        # Update strike price for new market
        self._update_strike_price()

        # Start or reconnect Polymarket WS
        if old_up:
            # Had a previous market - signal reconnect
            self.needs_reconnect = True
        else:
            # First market - start the Polymarket WS loop
            self._tasks.append(asyncio.create_task(self._polymarket_ws_loop()))

        # Initialize fills tracker for trading
        if TRADING_ENABLED and self.trader.client and condition_id:
            if self.fills_tracker:
                await self.fills_tracker.add_condition_id(condition_id)
            else:
                self.fills_tracker = WebSocketUserFillsTracker(self.trader.client)
                self.fills_tracker.on_fill = self._on_fill_received
                if await self.fills_tracker.start([condition_id]):
                    logger.info(f"[{self.name}] Fills tracker started for {condition_id[:16]}...")
                else:
                    logger.warning(f"[{self.name}] Fills tracker failed to start")
                    self.fills_tracker = None

        title = market_data.get('question', 'Unknown')[:50]
        logger.info(f"[{self.name}] New market: {title}")
        logger.info(f"[{self.name}] UP={up_token[:16]}... DOWN={down_token[:16]}...")

    async def on_market_active(self, market_data: Dict) -> None:
        """Called when market becomes active - tokens already set in on_new_market"""
        up_token = market_data.get('up_token_id', '')
        down_token = market_data.get('down_token_id', '')

        # Update tokens if different (e.g. on_new_market was skipped)
        if up_token and down_token:
            if up_token != self.up_token_id or down_token != self.down_token_id:
                await self.on_new_market(market_data)

        title = market_data.get('question', 'Unknown')[:50]
        logger.info(f"[{self.name}] Market active: {title}")

    async def on_price_update(
        self,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market: Dict,
    ) -> None:
        """No-op - strategy uses its own Polymarket WS for bid/ask prices"""
        pass

    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        """Clear state on market end"""
        # Discard active signals
        if self.active_signals:
            logger.info(f"[{self.name}] Market end - discarding {len(self.active_signals)} active signals")
            self.active_signals.clear()

        # Clear positions (will be resolved by bot's redemption loop)
        if self.active_positions:
            logger.info(f"[{self.name}] Market end - clearing {len(self.active_positions)} active positions")
            self.active_positions.clear()

        self.up_bid = None
        self.up_ask = None
        self.down_bid = None
        self.down_ask = None

        logger.info(f"[{self.name}] Market ended | Signals written: {self.signals_written}")

    async def shutdown(self) -> None:
        """Cancel all background tasks and clean up"""
        logger.info(f"[{self.name}] Shutting down...")
        self.running = False

        # Cancel background tasks
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        # Close fills tracker
        if self.fills_tracker:
            await self.fills_tracker.close()
            self.fills_tracker = None

        # Close CSV
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None
            self.csv_writer = None

        self.executor.shutdown(wait=False)

        logger.info(f"[{self.name}] Shutdown complete | Signals written: {self.signals_written}")

    # ==================== CSV Output ====================

    def _open_csv(self):
        """Open CSV file for writing signals (line-buffered for Docker)"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(OUTPUT_DIR, f"signals_{timestamp}.csv")
        self.csv_file = open(filename, 'w', newline='', buffering=1)
        fieldnames = list(Signal.__dataclass_fields__.keys())
        self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=fieldnames)
        self.csv_writer.writeheader()
        self.csv_file.flush()
        logger.info(f"[{self.name}] Writing signals to: {filename}")

    def _write_signal(self, signal: Signal):
        """Write completed signal to CSV"""
        if self.csv_writer:
            self.csv_writer.writerow(asdict(signal))
            self.csv_file.flush()
            self.signals_written += 1

    # ==================== Binance Trade Processing ====================

    def _process_binance_trade(self, data: dict):
        """Process Binance aggTrade - O(1) optimized with incremental sums"""
        now_ms = int(time.time() * 1000)
        qty = float(data.get('q', 0))
        is_buyer_maker = data.get('m', False)

        trade = {
            'qty': qty,
            'is_buyer_maker': is_buyer_maker,
            'time_ms': now_ms,
        }

        self.trade_buffer.append(trade)
        if is_buyer_maker:
            self.sell_qty_sum += qty
        else:
            self.buy_qty_sum += qty
        self.last_price = float(data.get('p', 0))

        # Remove expired trades
        cutoff = now_ms - AGG_WINDOW_MS
        while self.trade_buffer and self.trade_buffer[0]['time_ms'] < cutoff:
            old = self.trade_buffer.popleft()
            if old['is_buyer_maker']:
                self.sell_qty_sum -= old['qty']
            else:
                self.buy_qty_sum -= old['qty']

        self._check_signal(now_ms)

    def _check_signal(self, now_ms: int):
        """Check if NET directional volume exceeds threshold"""
        net_volume = abs(self.buy_qty_sum - self.sell_qty_sum)

        gross_volume = self.buy_qty_sum + self.sell_qty_sum
        window_ms = (now_ms // AGG_WINDOW_MS) * AGG_WINDOW_MS
        if window_ms != self.current_window_ms:
            if self.current_window_ms > 0 and self.current_window_total_vol > 0:
                self.volume_history.append((self.current_window_ms, self.current_window_total_vol))
            self.current_window_ms = window_ms
            self.current_window_total_vol = gross_volume
        else:
            self.current_window_total_vol = gross_volume

        if now_ms - self.last_signal_ms < AGG_WINDOW_MS:
            return

        if net_volume >= self.current_threshold:
            direction = 'SELL' if self.sell_qty_sum > self.buy_qty_sum else 'BUY'
            self.last_signal_ms = now_ms
            self._create_signal(now_ms, direction, net_volume, len(self.trade_buffer), self.last_price)

    # ==================== Threshold Calculation ====================

    async def _threshold_update_loop(self):
        """Recalculate dynamic threshold every second using percentile"""
        while self.running:
            try:
                now_ms = int(time.time() * 1000)

                cutoff = now_ms - LOOKBACK_MS
                while self.volume_history and self.volume_history[0][0] < cutoff:
                    self.volume_history.popleft()

                if len(self.volume_history) >= 10:
                    volumes = sorted([v for _, v in self.volume_history])
                    n = len(volumes)

                    p_idx = (PERCENTILE_THRESHOLD / 100) * (n - 1)
                    lower_idx = int(p_idx)
                    upper_idx = min(lower_idx + 1, n - 1)
                    fraction = p_idx - lower_idx

                    dynamic_threshold = volumes[lower_idx] + fraction * (volumes[upper_idx] - volumes[lower_idx])
                    self.dynamic_threshold_calc = dynamic_threshold
                    self.current_threshold = max(MIN_THRESHOLD_BTC, dynamic_threshold)

                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[{self.name}] Threshold update error: {e}")
                await asyncio.sleep(1)

    # ==================== Pricing (Black-Scholes) ====================

    def _update_strike_price(self):
        """Fetch strike price (BTC open at hour start) from Binance"""
        try:
            import requests
            now = datetime.now(timezone.utc)
            hour_start = now.replace(minute=0, second=0, microsecond=0)
            timestamp_ms = int(hour_start.timestamp() * 1000)

            params = {
                "symbol": "BTCUSDT",
                "interval": "1h",
                "startTime": timestamp_ms,
                "limit": 1,
            }

            response = requests.get(BINANCE_KLINES_URL, params=params, timeout=5)
            candles = response.json()

            if candles and len(candles) > 0:
                self.strike_price = float(candles[0][1])
                logger.info(f"[{self.name}] Strike price: ${self.strike_price:,.2f}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to update strike price: {e}")

    def _update_volatility(self):
        """Calculate EWMA volatility from 1-min candles"""
        try:
            import requests
            lookback_minutes = int(VOLATILITY_LOOKBACK_HOURS * 60)

            params = {
                "symbol": "BTCUSDT",
                "interval": "1m",
                "limit": lookback_minutes,
            }

            response = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
            candles = response.json()

            if len(candles) < 10:
                return

            closes = [float(c[4]) for c in candles]
            returns = []
            for i in range(1, len(closes)):
                if closes[i - 1] > 0:
                    returns.append(math.log(closes[i] / closes[i - 1]))

            if len(returns) < 10:
                return

            ewma_variance = returns[0] ** 2
            for r in returns[1:]:
                ewma_variance = EWMA_LAMBDA * ewma_variance + (1 - EWMA_LAMBDA) * (r ** 2)

            std_1min = math.sqrt(ewma_variance)
            vol_raw = std_1min * math.sqrt(MINUTES_PER_YEAR)
            self.cached_volatility = max(vol_raw * VOLATILITY_MULTIPLIER, MIN_VOLATILITY)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to update volatility: {e}")

    @staticmethod
    def _norm_cdf(x: float) -> float:
        """Standard normal CDF (Abramowitz-Stegun approximation)"""
        a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
        p = 0.3275911
        sign = 1 if x >= 0 else -1
        x = abs(x)
        t = 1.0 / (1.0 + p * x)
        y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2)
        return 0.5 * (1.0 + sign * y)

    def _calculate_theo_prices(self, spot_price: float) -> tuple:
        """Calculate theoretical UP/DOWN prices using cached strike and volatility"""
        if self.strike_price is None or self.cached_volatility <= 0:
            return (None, None)

        now = datetime.now(timezone.utc)
        minutes_remaining = 60 - now.minute - now.second / 60

        if minutes_remaining <= 0:
            return (1.0, 0.0) if spot_price > self.strike_price else (0.0, 1.0)

        T = minutes_remaining / MINUTES_PER_YEAR
        sqrt_T = math.sqrt(T)

        d2 = (math.log(spot_price / self.strike_price) - (self.cached_volatility ** 2) * T / 2) / (
            self.cached_volatility * sqrt_T
        )

        price_up = self._norm_cdf(d2)
        price_down = 1.0 - price_up

        return (round(price_up, 4), round(price_down, 4), round(minutes_remaining, 2))

    async def _volatility_update_loop(self):
        """Update volatility periodically in background"""
        self._update_strike_price()
        self._update_volatility()

        while self.running:
            try:
                await asyncio.sleep(VOLATILITY_UPDATE_INTERVAL)
                self._update_volatility()
            except Exception as e:
                logger.error(f"[{self.name}] Volatility update error: {e}")
                await asyncio.sleep(VOLATILITY_UPDATE_INTERVAL)

    # ==================== Signal Creation & Tracking ====================

    def _create_signal(self, timestamp_ms: int, direction: str, qty: float, count: int, price: float):
        """Create new signal and start tracking"""
        self.signal_counter += 1

        theo_result = self._calculate_theo_prices(price)
        theo_up, theo_down, mins_remaining = (None, None, None)
        if theo_result and len(theo_result) == 3:
            theo_up, theo_down, mins_remaining = theo_result

        signal = Signal(
            signal_id=self.signal_counter,
            timestamp_ms=timestamp_ms,
            timestamp_iso=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            direction=direction,
            total_qty_btc=qty,
            trade_count=count,
            binance_price=price,
            threshold_used=self.current_threshold,
            dynamic_threshold_calc=self.dynamic_threshold_calc,
            strike_price=self.strike_price,
            volatility=self.cached_volatility,
            minutes_remaining=mins_remaining,
            theo_up=theo_up,
            theo_down=theo_down,
            up_ask_0=self.up_ask,
            up_bid_0=self.up_bid,
            down_ask_0=self.down_ask,
            down_bid_0=self.down_bid,
        )

        self.active_signals[self.signal_counter] = signal

        # Execute trade if enabled and tokens are set
        if TRADING_ENABLED and self.up_token_id and self.down_token_id:
            if mins_remaining is None or not (3 <= mins_remaining <= 50):
                return

            if len(self.volume_history) < MIN_VOLUME_HISTORY:
                return

            now_ms = int(time.time() * 1000)
            if now_ms - self.last_trade_ms < TRADE_COOLDOWN_MS:
                return

            if direction == 'BUY' and self.up_bid:
                if not (0.14 <= self.up_bid <= 0.86):
                    return
                self.last_trade_ms = now_ms
                self.executor.submit(
                    self._execute_buy_order, self.signal_counter, self.up_token_id, 'up', self.up_bid, timestamp_ms
                )
                logger.info(f"[{self.name}] SIGNAL #{self.signal_counter} | BUY {qty:.2f} BTC | UP @{self.up_bid}")
            elif direction == 'SELL' and self.down_bid:
                if not (0.14 <= self.down_bid <= 0.86):
                    return
                self.last_trade_ms = now_ms
                self.executor.submit(
                    self._execute_buy_order, self.signal_counter, self.down_token_id, 'down', self.down_bid, timestamp_ms
                )
                logger.info(f"[{self.name}] SIGNAL #{self.signal_counter} | SELL {qty:.2f} BTC | DOWN @{self.down_bid}")

    def _update_signal_prices(self):
        """Update price evolution for active signals"""
        now_ms = int(time.time() * 1000)
        completed = []

        for sig_id, signal in self.active_signals.items():
            elapsed = now_ms - signal.timestamp_ms

            if elapsed >= 250 and signal.up_ask_250 is None:
                signal.up_ask_250 = self.up_ask
                signal.down_ask_250 = self.down_ask
            if elapsed >= 500 and signal.up_ask_500 is None:
                signal.up_ask_500 = self.up_ask
                signal.down_ask_500 = self.down_ask
            if elapsed >= 750 and signal.up_ask_750 is None:
                signal.up_ask_750 = self.up_ask
                signal.down_ask_750 = self.down_ask
            if elapsed >= 1000 and signal.up_ask_1000 is None:
                signal.up_ask_1000 = self.up_ask
                signal.down_ask_1000 = self.down_ask
            if elapsed >= 1500 and signal.up_ask_1500 is None:
                signal.up_ask_1500 = self.up_ask
                signal.down_ask_1500 = self.down_ask
            if elapsed >= 2000 and signal.up_ask_2000 is None:
                signal.up_ask_2000 = self.up_ask
                signal.down_ask_2000 = self.down_ask
            if elapsed >= 2500 and signal.up_ask_2500 is None:
                signal.up_ask_2500 = self.up_ask
                signal.down_ask_2500 = self.down_ask
            if elapsed >= 3000 and signal.up_ask_3000 is None:
                signal.up_ask_3000 = self.up_ask
                signal.down_ask_3000 = self.down_ask
            if elapsed >= 3500 and signal.up_ask_3500 is None:
                signal.up_ask_3500 = self.up_ask
                signal.down_ask_3500 = self.down_ask
            if elapsed >= 4000 and signal.up_ask_4000 is None:
                signal.up_ask_4000 = self.up_ask
                signal.down_ask_4000 = self.down_ask
            if elapsed >= 4500 and signal.up_ask_4500 is None:
                signal.up_ask_4500 = self.up_ask
                signal.down_ask_4500 = self.down_ask
            if elapsed >= 5000 and signal.up_ask_5000 is None:
                signal.up_ask_5000 = self.up_ask
                signal.down_ask_5000 = self.down_ask
                completed.append(sig_id)

        for sig_id in completed:
            signal = self.active_signals.pop(sig_id)
            self._write_signal(signal)

    # ==================== Trading Execution ====================

    def _on_fill_received(self, token_id: str, side: str, size: float):
        """Callback when WS receives a fill - creates position from WS (source of truth)"""
        if side == 'BUY':
            pending_list = self.pending_buy_fills.get(token_id, [])
            if pending_list:
                pending = pending_list.pop(0)
                signal_id = pending['signal_id']
                now_ms = time.time() * 1000
                latency_ms = now_ms - pending['signal_time_ms']

                self.active_positions[signal_id] = {
                    'token_id': token_id,
                    'side': pending['side'],
                    'shares': size,
                    'shares_source': 'ws_fill',
                    'entry_price': pending['entry_price'],
                    'fill_time_ms': now_ms,
                    'exit_time_ms': pending['signal_time_ms'] + EXIT_DELAY_MS,
                }
                logger.info(
                    f"[{self.name}] MATCHED #{signal_id} | {pending['side'].upper()} "
                    f"{size:.2f}sh @{pending['entry_price']} | {latency_ms:.0f}ms from signal"
                )
            else:
                logger.info(f"[{self.name}] WS FILL BUY {size:.4f} of {token_id[:10]}... no pending signal")

        elif side == 'SELL':
            pending_list = self.pending_sell_fills.get(token_id, [])
            if pending_list:
                pending = pending_list.pop(0)
                signal_id = pending['signal_id']
                shares_requested = pending['shares_requested']
                exit_price = self.up_bid if pending['side'] == 'up' else self.down_bid

                if exit_price:
                    entry_price = pending['entry_price']
                    pnl = (exit_price - entry_price) * size
                    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                    partial_note = f" (partial: {size:.2f}/{shares_requested:.2f})" if abs(size - shares_requested) > 0.01 else ""
                    logger.info(
                        f"[{self.name}] SOLD #{signal_id} | {pending['side'].upper()} "
                        f"{size:.2f}sh{partial_note} | {entry_price}->{exit_price} | {pnl_str}"
                    )
                else:
                    logger.info(f"[{self.name}] SOLD #{signal_id} | {pending['side'].upper()} {size:.2f}sh")
            else:
                logger.info(f"[{self.name}] WS FILL SELL {size:.4f} of {token_id[:10]}... no pending signal")

    def _execute_buy_order(self, signal_id: int, token_id: str, side: str, bid_price: float, signal_time_ms: int):
        """Execute buy order in background thread"""
        try:
            limit_price = bid_price + ALLOWED_SLIPPAGE
            usd_amount = round(POSITION_SIZE_USD, 2)

            if token_id not in self.pending_buy_fills:
                self.pending_buy_fills[token_id] = []
            self.pending_buy_fills[token_id].append({
                'signal_id': signal_id,
                'side': side,
                'entry_price': limit_price,
                'signal_time_ms': signal_time_ms,
                'usd_amount': usd_amount,
            })

            result = self.trader.place_buy_order_fast(token_id, limit_price, usd_amount)

            if not result:
                self._remove_pending_buy(token_id, signal_id)
                logger.error(f"[{self.name}] BUY #{signal_id} FAILED | NO_RESULT")
            else:
                status = result.get('status', '').upper()
                if status not in ['MATCHED', 'FILLED', 'LIVE']:
                    self._remove_pending_buy(token_id, signal_id)
                    logger.error(f"[{self.name}] BUY #{signal_id} FAILED | {status}")
        except Exception as e:
            self._remove_pending_buy(token_id, signal_id)
            logger.error(f"[{self.name}] BUY #{signal_id} ERROR: {e}")

    def _execute_sell_order(self, signal_id: int, position: Dict):
        """Execute sell order in background thread with retry logic"""
        try:
            token_id = position['token_id']
            side = position['side']
            entry_price = position['entry_price']
            shares = math.floor(position['shares'] * 100) / 100

            if shares <= 0:
                return

            max_retries = 3
            for attempt in range(max_retries):
                if shares <= 0:
                    return

                if token_id not in self.pending_sell_fills:
                    self.pending_sell_fills[token_id] = []
                pending_entry = {
                    'signal_id': signal_id,
                    'side': side,
                    'entry_price': entry_price,
                    'shares_requested': shares,
                    'attempt': attempt,
                }
                self.pending_sell_fills[token_id].append(pending_entry)

                result = self.trader.place_sell_order_fast(token_id, shares)
                status = result.get('status', '').upper() if result else ''

                if status in ['MATCHED', 'FILLED', 'LIVE']:
                    return
                else:
                    self._remove_pending_sell(token_id, signal_id)
                    logger.warning(
                        f"[{self.name}] SELL #{signal_id} RETRY {attempt+1}/{max_retries} | "
                        f"{status or 'NO_RESULT'} | shares={shares:.2f}"
                    )
                    shares = round(shares - 0.01, 2)

            logger.error(f"[{self.name}] SELL #{signal_id} FAILED | All {max_retries} retries exhausted")
        except Exception as e:
            self._remove_pending_sell(position.get('token_id', ''), signal_id)
            logger.error(f"[{self.name}] SELL #{signal_id} ERROR: {e}")

    def _remove_pending_buy(self, token_id: str, signal_id: int):
        """Remove a signal_id from pending_buy_fills"""
        pending_list = self.pending_buy_fills.get(token_id, [])
        self.pending_buy_fills[token_id] = [p for p in pending_list if p.get('signal_id') != signal_id]

    def _remove_pending_sell(self, token_id: str, signal_id: int):
        """Remove a signal_id from pending_sell_fills"""
        pending_list = self.pending_sell_fills.get(token_id, [])
        self.pending_sell_fills[token_id] = [p for p in pending_list if p.get('signal_id') != signal_id]

    # ==================== WebSocket Loops ====================

    async def _binance_ws_loop(self):
        """Binance aggTrade WebSocket loop with auto-reconnect"""
        while self.running:
            try:
                async with websockets.connect(BINANCE_WS_URL) as ws:
                    logger.info(f"[{self.name}] Binance WS connected")
                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            self._process_binance_trade(json.loads(msg))
                        except asyncio.TimeoutError:
                            continue
                        except Exception:
                            break
            except Exception:
                if self.running:
                    await asyncio.sleep(1)

    async def _polymarket_ws_loop(self):
        """Polymarket orderbook WebSocket loop - reconnects on token refresh"""
        while self.running:
            # Wait until we have tokens
            if not self.up_token_id or not self.down_token_id:
                await asyncio.sleep(0.5)
                continue

            try:
                logger.info(
                    f"[{self.name}] Polymarket WS connecting: "
                    f"UP={self.up_token_id[:16]}... DOWN={self.down_token_id[:16]}..."
                )
                async with websockets.connect(POLYMARKET_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    sub = {
                        "auth": {},
                        "type": "market",
                        "assets_ids": [self.up_token_id, self.down_token_id],
                    }
                    await ws.send(json.dumps(sub))

                    while self.running and not self.needs_reconnect:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            self._process_polymarket_message(msg)
                        except asyncio.TimeoutError:
                            try:
                                await ws.ping()
                            except Exception:
                                break
                        except Exception:
                            break

                    if self.needs_reconnect:
                        self.needs_reconnect = False
                        logger.info(f"[{self.name}] Polymarket WS reconnecting with new tokens...")
            except Exception:
                if self.running:
                    await asyncio.sleep(1)

    def _process_polymarket_message(self, raw: str):
        """Process Polymarket WebSocket message"""
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for item in data:
                    self._handle_poly_event(item)
            elif isinstance(data, dict):
                self._handle_poly_event(data)
        except Exception:
            pass

    def _handle_poly_event(self, event: dict):
        """Handle Polymarket orderbook event"""
        event_type = event.get('event_type', '')
        asset_id = event.get('asset_id', '')

        if event_type == 'book':
            bids = event.get('bids', [])
            asks = event.get('asks', [])
            best_bid = float(bids[0]['price']) if bids else None
            best_ask = float(asks[0]['price']) if asks else None

            if best_bid and best_ask and (best_ask - best_bid) > 0.5:
                return

            if asset_id == self.up_token_id:
                self.up_bid = best_bid
                self.up_ask = best_ask
            elif asset_id == self.down_token_id:
                self.down_bid = best_bid
                self.down_ask = best_ask

        elif event_type == 'price_change':
            for pc in event.get('price_changes', []):
                pc_id = pc.get('asset_id', '')
                best_bid = pc.get('best_bid')
                best_ask = pc.get('best_ask')

                if best_bid:
                    best_bid = float(best_bid) if best_bid != '0' else None
                if best_ask:
                    best_ask = float(best_ask) if best_ask != '0' else None

                if best_bid and best_ask and (best_ask - best_bid) > 0.5:
                    continue

                if pc_id == self.up_token_id:
                    if best_bid:
                        self.up_bid = best_bid
                    if best_ask:
                        self.up_ask = best_ask
                elif pc_id == self.down_token_id:
                    if best_bid:
                        self.down_bid = best_bid
                    if best_ask:
                        self.down_ask = best_ask

    # ==================== Background Loops ====================

    async def _tracker_loop(self):
        """Update signal price tracking every 100ms"""
        while self.running:
            self._update_signal_prices()
            await asyncio.sleep(0.1)

    async def _position_manager_loop(self):
        """Check positions and exit after EXIT_DELAY_MS"""
        while self.running:
            try:
                now_ms = time.time() * 1000
                to_close = []

                for signal_id, pos in self.active_positions.items():
                    if now_ms >= pos['exit_time_ms']:
                        to_close.append(signal_id)

                for signal_id in to_close:
                    pos = self.active_positions.pop(signal_id)
                    self.executor.submit(self._execute_sell_order, signal_id, pos)

                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"[{self.name}] Position manager error: {e}")
                await asyncio.sleep(0.1)
