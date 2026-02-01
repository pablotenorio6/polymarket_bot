"""
Binance-Polymarket Frontrun Strategy - Production Version

Monitors Binance aggTrade and Polymarket orderbook via WebSocket.
Uses dynamic threshold (mean + 3*std of last 5 min) to detect significant
directional volume imbalances. Logs signal and price evolution to CSV.

Optimized for minimum latency.
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
from dataclasses import dataclass, field, asdict
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import websockets
import requests

from trader import FastTrader
from ws_monitor import WebSocketUserFillsTracker

# Pricing constants
VOLATILITY_UPDATE_INTERVAL = 60  # Update volatility every 30 seconds
VOLATILITY_LOOKBACK_HOURS = 8  # Lookback for EWMA calculation
MINUTES_PER_YEAR = 365 * 24 * 60
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
EWMA_LAMBDA = 0.97  # EWMA decay factor (higher = more smoothing, lower = more reactive)
VOLATILITY_MULTIPLIER = 3  # Scale realized vol to approximate implied vol
MIN_VOLATILITY = 0.25  # 25% floor

# Minimal logging - only errors
logging.basicConfig(level=logging.WARNING, format='%(asctime)s|%(levelname)s|%(message)s')
logger = logging.getLogger(__name__)

# ============== CONFIGURATION ==============
AGG_WINDOW_MS = 100  # Rolling window for trade aggregation
TRACK_DURATION_MS = 5000  # Track prices for 5 seconds after signal
TRACK_INTERVAL_MS = 500  # Record every 500ms

# Dynamic threshold configuration
MIN_THRESHOLD_BTC = float(os.environ.get('MIN_THRESHOLD_BTC', '2'))  # Floor minimum
PERCENTILE_THRESHOLD = float(os.environ.get('PERCENTILE_THRESHOLD', '99.9'))  # Percentile (~50 signals/hour from 36k buckets)
LOOKBACK_MS = int(os.environ.get('LOOKBACK_MS', '600000'))  # 10 minutes in ms
MIN_VOLUME_HISTORY = int(os.environ.get('MIN_VOLUME_HISTORY', '1000'))  # Min samples before trading

# Trading configuration
TRADING_ENABLED = os.environ.get('TRADING_ENABLED', 'false').lower() == 'true'
POSITION_SIZE_USD = float(os.environ.get('POSITION_SIZE_USD', '10'))  # USD per trade
EXIT_DELAY_MS = int(os.environ.get('EXIT_DELAY_MS', '4000'))  # 4 seconds exit delay
TRADE_COOLDOWN_MS = int(os.environ.get('TRADE_COOLDOWN_MS', '5000'))  # 5 seconds between trades
ALLOWED_SLIPPAGE = float(os.environ.get('ALLOWED_SLIPPAGE', '0.015'))  # 1,5% slippage

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Token IDs - set via environment or will be fetched
UP_TOKEN_ID = os.environ.get('UP_TOKEN_ID', '')
DOWN_TOKEN_ID = os.environ.get('DOWN_TOKEN_ID', '')

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
    threshold_used: float  # Dynamic threshold at signal time (with MIN floor)
    dynamic_threshold_calc: float  # Raw calculated threshold (before MIN floor)
    
    # Theoretical pricing (Black-Scholes)
    strike_price: Optional[float] = None  # BTC open at hour start
    volatility: Optional[float] = None  # Annualized volatility (cached)
    minutes_remaining: Optional[float] = None  # Time to expiry
    theo_up: Optional[float] = None  # Theoretical UP price
    theo_down: Optional[float] = None  # Theoretical DOWN price
    
    # Initial prices at T=0
    up_ask_0: Optional[float] = None
    up_bid_0: Optional[float] = None
    down_ask_0: Optional[float] = None
    down_bid_0: Optional[float] = None
    
    # Prices at each interval (250ms, 500ms, 750ms, 1000ms, etc.)
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


class FrontrunStrategy:
    """
    Low-latency strategy that monitors Binance trades and Polymarket orderbook.
    Auto-refreshes tokens when hourly market changes.
    """
    
    def __init__(self, up_token_id: str, down_token_id: str, condition_id: str = ''):
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self.condition_id = condition_id
        self.running = False
        
        # Current Polymarket state
        self.up_bid: Optional[float] = None
        self.up_ask: Optional[float] = None
        self.down_bid: Optional[float] = None
        self.down_ask: Optional[float] = None
        
        # Binance trades buffer (rolling window) with incremental sums
        self.trade_buffer: deque = deque()
        self.buy_qty_sum: float = 0.0  # Incremental sum of buy volume
        self.sell_qty_sum: float = 0.0  # Incremental sum of sell volume
        self.last_signal_ms: int = 0  # Cooldown to avoid duplicate signals
        self.last_price: float = 0.0  # Last trade price
        
        # Dynamic threshold tracking (updated async every 1s)
        self.volume_history: deque = deque()  # (window_ms, gross_volume) per 100ms window
        self.current_window_ms: int = 0
        self.current_window_total_vol: float = 0.0
        self.current_threshold: float = MIN_THRESHOLD_BTC
        self.dynamic_threshold_calc: float = MIN_THRESHOLD_BTC  # Raw calculated value
        
        # Active signals being tracked
        self.active_signals: Dict[int, Signal] = {}
        self.signal_counter: int = 0
        
        # CSV output
        self.csv_file = None
        self.csv_writer = None
        self.signals_written = 0
        
        # Market refresh tracking
        self.current_hour: int = datetime.now(timezone.utc).hour
        self.needs_reconnect: bool = False
        
        # Pricing cache (updated async to avoid latency)
        self.strike_price: Optional[float] = None  # Updated on hour change
        self.cached_volatility: float = 0.30  # Default 30% until first calculation
        
        # Trading execution
        self.trader: Optional[FastTrader] = None
        self.fills_tracker: Optional[WebSocketUserFillsTracker] = None
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trade_exec")
        self.active_positions: Dict[int, Dict] = {}  # signal_id -> position info
        self.pending_buy_fills: Dict[str, List] = {}  # token_id -> [pending buy info]
        self.pending_sell_fills: Dict[str, List] = {}  # token_id -> [pending sell info]
        self.last_trade_ms: int = 0  # Cooldown tracking
    
    def _open_csv(self):
        """Open CSV file for writing signals (line-buffered for Docker)"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(OUTPUT_DIR, f"signals_{timestamp}.csv")
        # buffering=1 = line buffering, writes to disk after each line
        self.csv_file = open(filename, 'w', newline='', buffering=1)
        fieldnames = list(Signal.__dataclass_fields__.keys())
        self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=fieldnames)
        self.csv_writer.writeheader()
        self.csv_file.flush()  # Ensure header is written
        logger.warning(f"Writing signals to: {filename}")
    
    def _write_signal(self, signal: Signal):
        """Write completed signal to CSV"""
        if self.csv_writer:
            self.csv_writer.writerow(asdict(signal))
            self.csv_file.flush()  # Flush every signal for Docker
            self.signals_written += 1
    
    def _process_binance_trade(self, data: dict):
        """Process Binance aggTrade - O(1) optimized with incremental sums"""
        now_ms = int(time.time() * 1000)
        qty = float(data.get('q', 0))
        is_buyer_maker = data.get('m', False)
        
        trade = {
            'qty': qty,
            'is_buyer_maker': is_buyer_maker,
            'time_ms': now_ms
        }
        
        # Add to buffer and update incremental sums
        self.trade_buffer.append(trade)
        if is_buyer_maker:
            self.sell_qty_sum += qty
        else:
            self.buy_qty_sum += qty
        self.last_price = float(data.get('p', 0))
        
        # Remove old trades and subtract from sums (O(k) where k = expired trades, usually 0-1)
        cutoff = now_ms - AGG_WINDOW_MS
        while self.trade_buffer and self.trade_buffer[0]['time_ms'] < cutoff:
            old = self.trade_buffer.popleft()
            if old['is_buyer_maker']:
                self.sell_qty_sum -= old['qty']
            else:
                self.buy_qty_sum -= old['qty']
        
        # Check for signal - O(1) operation now
        self._check_signal(now_ms)
    
    def _check_signal(self, now_ms: int):
        """Check if NET directional volume exceeds threshold - O(1) optimized"""
        # Calculate net volume from incremental sums (O(1))
        net_volume = abs(self.buy_qty_sum - self.sell_qty_sum)
        
        # Track GROSS volume per 100ms window for threshold calculation
        # (using net volume gives near-zero median since buys/sells cancel out)
        gross_volume = self.buy_qty_sum + self.sell_qty_sum
        window_ms = (now_ms // AGG_WINDOW_MS) * AGG_WINDOW_MS
        if window_ms != self.current_window_ms:
            # New window - save previous window's gross volume if significant
            if self.current_window_ms > 0 and self.current_window_total_vol > 0:
                self.volume_history.append((self.current_window_ms, self.current_window_total_vol))
            self.current_window_ms = window_ms
            self.current_window_total_vol = gross_volume
        else:
            # Same window - update with current gross volume
            self.current_window_total_vol = gross_volume
        
        # Cooldown: don't fire multiple signals within the same window
        if now_ms - self.last_signal_ms < AGG_WINDOW_MS:
            return
        
        # Check threshold on NET volume
        if net_volume >= self.current_threshold:
            direction = 'SELL' if self.sell_qty_sum > self.buy_qty_sum else 'BUY'
            self.last_signal_ms = now_ms
            self._create_signal(now_ms, direction, net_volume, len(self.trade_buffer), self.last_price)
    
    async def _threshold_update_loop(self):
        """Async loop to recalculate dynamic threshold every second using MAD (robust to outliers)"""
        while self.running:
            try:
                now_ms = int(time.time() * 1000)
                
                # Clean old entries from volume history
                cutoff = now_ms - LOOKBACK_MS
                while self.volume_history and self.volume_history[0][0] < cutoff:
                    self.volume_history.popleft()
                
                # Recalculate threshold if we have enough data
                if len(self.volume_history) >= 10:
                    volumes = sorted([v for _, v in self.volume_history])
                    n = len(volumes)
                    
                    # Calculate percentile threshold
                    # percentile_index = (P/100) * (n-1), then interpolate
                    p_idx = (PERCENTILE_THRESHOLD / 100) * (n - 1)
                    lower_idx = int(p_idx)
                    upper_idx = min(lower_idx + 1, n - 1)
                    fraction = p_idx - lower_idx
                    
                    dynamic_threshold = volumes[lower_idx] + fraction * (volumes[upper_idx] - volumes[lower_idx])
                    self.dynamic_threshold_calc = dynamic_threshold  # Store raw calculated value
                    self.current_threshold = max(MIN_THRESHOLD_BTC, dynamic_threshold)
                    
                    # Also log median and max for reference
                    median_vol = volumes[n // 2]
                    max_vol = volumes[-1]
                    # logger.warning(f"Threshold: {self.current_threshold:.2f} BTC (p{PERCENTILE_THRESHOLD:.0f}: {dynamic_threshold:.2f}, median: {median_vol:.2f}, max: {max_vol:.2f}, samples: {n})")
                
                await asyncio.sleep(1)  # Update every 1 second
            except Exception as e:
                logger.error(f"Threshold update error: {e}")
                await asyncio.sleep(1)
    
    # ============== PRICING METHODS ==============
    
    def _update_strike_price(self):
        """Fetch strike price (BTC open at hour start) - called on hour change"""
        try:
            now = datetime.now(timezone.utc)
            hour_start = now.replace(minute=0, second=0, microsecond=0)
            timestamp_ms = int(hour_start.timestamp() * 1000)
            
            params = {
                "symbol": "BTCUSDT",
                "interval": "1h",
                "startTime": timestamp_ms,
                "limit": 1
            }
            
            response = requests.get(BINANCE_KLINES_URL, params=params, timeout=5)
            candles = response.json()
            
            if candles and len(candles) > 0:
                self.strike_price = float(candles[0][1])  # Open price
                logger.warning(f"Strike price updated: ${self.strike_price:,.2f}")
        except Exception as e:
            logger.error(f"Failed to update strike price: {e}")
    
    def _update_volatility(self):
        """Calculate EWMA volatility from 1-min candles (λ=0.97)"""
        try:
            lookback_minutes = int(VOLATILITY_LOOKBACK_HOURS * 60)
            
            params = {
                "symbol": "BTCUSDT",
                "interval": "1m",
                "limit": lookback_minutes
            }
            
            response = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
            candles = response.json()
            
            if len(candles) < 10:
                return
            
            # Calculate log returns from close prices
            closes = [float(c[4]) for c in candles]
            returns = []
            
            for i in range(1, len(closes)):
                if closes[i-1] > 0:
                    returns.append(math.log(closes[i] / closes[i-1]))
            
            if len(returns) < 10:
                return
            
            # EWMA variance: σ²_t = λ * σ²_{t-1} + (1-λ) * r²_t
            # Initialize with first return squared
            ewma_variance = returns[0] ** 2
            
            for r in returns[1:]:
                ewma_variance = EWMA_LAMBDA * ewma_variance + (1 - EWMA_LAMBDA) * (r ** 2)
            
            std_1min = math.sqrt(ewma_variance)
            
            # Annualize and apply multiplier + floor
            vol_raw = std_1min * math.sqrt(MINUTES_PER_YEAR)
            self.cached_volatility = max(vol_raw * VOLATILITY_MULTIPLIER, MIN_VOLATILITY)
            # logger.warning(f"Volatility: {self.cached_volatility * 100:.1f}% (raw={vol_raw*100:.1f}%, x{VOLATILITY_MULTIPLIER})")
            
        except Exception as e:
            logger.error(f"Failed to update volatility: {e}")
    
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
        
        d2 = (math.log(spot_price / self.strike_price) - (self.cached_volatility ** 2) * T / 2) / (self.cached_volatility * sqrt_T)
        
        price_up = self._norm_cdf(d2)
        price_down = 1.0 - price_up
        
        return (round(price_up, 4), round(price_down, 4), round(minutes_remaining, 2))
    
    async def _volatility_update_loop(self):
        """Update volatility every 60 seconds in background"""
        # Initial update
        self._update_strike_price()
        self._update_volatility()
        
        while self.running:
            try:
                await asyncio.sleep(VOLATILITY_UPDATE_INTERVAL)
                self._update_volatility()
            except Exception as e:
                logger.error(f"Volatility update error: {e}")
                await asyncio.sleep(VOLATILITY_UPDATE_INTERVAL)
    
    def _on_fill_received(self, token_id: str, side: str, size: float):
        """Callback when WS receives a fill - CREATE position from WS (source of truth)"""
        if side == 'BUY':
            # Find the oldest pending signal for this token
            pending_list = self.pending_buy_fills.get(token_id, [])
            
            if pending_list:
                pending = pending_list.pop(0)  # FIFO - oldest first
                signal_id = pending['signal_id']
                
                # Calculate latency: signal detection -> WS MATCHED
                now_ms = time.time() * 1000
                latency_ms = now_ms - pending['signal_time_ms']
                
                # CREATE position from WS fill (this is the source of truth)
                self.active_positions[signal_id] = {
                    'token_id': token_id,
                    'side': pending['side'],
                    'shares': size,  # Real shares from WS
                    'shares_source': 'ws_fill',
                    'entry_price': pending['entry_price'],
                    'fill_time_ms': now_ms,
                    'exit_time_ms': pending['signal_time_ms'] + EXIT_DELAY_MS
                }
                logger.warning(f"MATCHED #{signal_id} | {pending['side'].upper()} {size:.2f}sh @{pending['entry_price']} | {latency_ms:.0f}ms from signal")
            else:
                logger.warning(f"[WS FILL] BUY {size:.4f} of {token_id[:10]}... but no pending signal")
        
        elif side == 'SELL':
            # Find the oldest pending sell for this token
            pending_list = self.pending_sell_fills.get(token_id, [])
            
            if pending_list:
                pending = pending_list.pop(0)  # FIFO - oldest first
                signal_id = pending['signal_id']
                entry_price = pending['entry_price']
                
                # Get actual sell price from WS (we need to calculate from the trade)
                # For now, use the shares_requested vs actual to detect partial fills
                shares_requested = pending['shares_requested']
                
                # Calculate P&L using entry price and current bid (best estimate)
                # Note: actual sell price may differ slightly
                exit_price = self.up_bid if pending['side'] == 'up' else self.down_bid
                
                if exit_price:
                    pnl = (exit_price - entry_price) * size
                    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                    partial_note = f" (partial: {size:.2f}/{shares_requested:.2f})" if abs(size - shares_requested) > 0.01 else ""
                    logger.warning(f"SOLD #{signal_id} | {pending['side'].upper()} {size:.2f}sh{partial_note} | {entry_price}->{exit_price} | {pnl_str} | via WS")
                else:
                    logger.warning(f"SOLD #{signal_id} | {pending['side'].upper()} {size:.2f}sh | via WS")
            else:
                logger.warning(f"[WS FILL] SELL {size:.4f} of {token_id[:10]}... but no pending signal")
    
    def _execute_buy_order(self, signal_id: int, token_id: str, side: str, bid_price: float, signal_time_ms: int):
        """Execute buy order in background thread - LATENCY CRITICAL
        
        Position is NOT created here - it's created when WS fill arrives (source of truth).
        We only register pending metadata and send the order.
        """
        try:
            # Price = bid + ALLOWED_SLIPPAGE (aggressive to get filled)
            limit_price = bid_price + ALLOWED_SLIPPAGE
            
            # Use USD amount directly (avoids decimal precision issues)
            usd_amount = round(POSITION_SIZE_USD, 2)
            
            # Register as pending fill BEFORE sending order (with all metadata)
            if token_id not in self.pending_buy_fills:
                self.pending_buy_fills[token_id] = []
            self.pending_buy_fills[token_id].append({
                'signal_id': signal_id,
                'side': side,
                'entry_price': limit_price,
                'signal_time_ms': signal_time_ms,
                'usd_amount': usd_amount
            })
            
            # Send order - position will be created when WS fill arrives
            result = self.trader.place_buy_order_fast(token_id, limit_price, usd_amount)
            
            # Only log errors - success is logged when WS fill arrives
            if not result:
                self._remove_pending(token_id, signal_id)
                logger.error(f"BUY #{signal_id} FAILED | NO_RESULT")
            else:
                status = result.get('status', '').upper()
                if status not in ['MATCHED', 'FILLED', 'LIVE']:
                    self._remove_pending(token_id, signal_id)
                    logger.error(f"BUY #{signal_id} FAILED | {status}")
                # Success case: wait for WS fill to create position and log
                
        except Exception as e:
            self._remove_pending(token_id, signal_id)
            logger.error(f"BUY #{signal_id} ERROR: {e}")
    
    def _remove_pending(self, token_id: str, signal_id: int):
        """Remove a signal_id from pending_buy_fills"""
        pending_list = self.pending_buy_fills.get(token_id, [])
        self.pending_buy_fills[token_id] = [p for p in pending_list if p.get('signal_id') != signal_id]
    
    def _create_signal(self, timestamp_ms: int, direction: str, qty: float, count: int, price: float):
        """Create new signal and start tracking"""
        self.signal_counter += 1
        
        # Calculate theoretical prices (uses cached strike/volatility - no latency)
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
            down_bid_0=self.down_bid
        )
        
        self.active_signals[self.signal_counter] = signal
        # logger.warning(f"SIGNAL #{self.signal_counter}: {direction} {qty:.4f} BTC @ ${price:.2f} | Theo: UP={theo_up} DOWN={theo_down}")
        
        # === EXECUTE TRADE (non-blocking) ===
        if TRADING_ENABLED and self.trader:
            # Check time window: only trade between 50-3 minutes remaining
            if mins_remaining is None or not (3 <= mins_remaining <= 50):
                logger.warning(f"SKIP #{self.signal_counter}: Outside time window (mins={mins_remaining})")
                return
            
            # Check minimum volume history for reliable threshold
            if len(self.volume_history) < MIN_VOLUME_HISTORY:
                logger.warning(f"SKIP #{self.signal_counter}: Insufficient volume history ({len(self.volume_history)}/{MIN_VOLUME_HISTORY})")
                return
            
            # Check cooldown between trades
            now_ms = int(time.time() * 1000)
            if now_ms - self.last_trade_ms < TRADE_COOLDOWN_MS:
                cooldown_remaining = (TRADE_COOLDOWN_MS - (now_ms - self.last_trade_ms)) / 1000
                logger.warning(f"SKIP #{self.signal_counter}: Cooldown ({cooldown_remaining:.1f}s remaining)")
                return
            
            # BUY signal on Binance -> price going UP -> buy UP token
            # SELL signal on Binance -> price going DOWN -> buy DOWN token
            # Pass signal timestamp for exit_time calculation
            if direction == 'BUY' and self.up_bid:
                # Check price range (0.14-0.86)
                if not (0.14 <= self.up_bid <= 0.86):
                    logger.warning(f"SKIP #{self.signal_counter}: UP price {self.up_bid} outside range")
                    return
                self.last_trade_ms = now_ms  # Update cooldown
                self.executor.submit(self._execute_buy_order, self.signal_counter, self.up_token_id, 'up', self.up_bid, timestamp_ms)
                logger.warning(f"SIGNAL #{self.signal_counter} | BUY {qty:.2f} BTC | UP @{self.up_bid}")
            elif direction == 'SELL' and self.down_bid:
                # Check price range (0.14-0.86)
                if not (0.14 <= self.down_bid <= 0.86):
                    logger.warning(f"SKIP #{self.signal_counter}: DOWN price {self.down_bid} outside range")
                    return
                self.last_trade_ms = now_ms  # Update cooldown
                self.executor.submit(self._execute_buy_order, self.signal_counter, self.down_token_id, 'down', self.down_bid, timestamp_ms)
                logger.warning(f"SIGNAL #{self.signal_counter} | SELL {qty:.2f} BTC | DOWN @{self.down_bid}")
    
    def _update_signal_prices(self):
        """Update price evolution for active signals"""
        now_ms = int(time.time() * 1000)
        completed = []
        
        for sig_id, signal in self.active_signals.items():
            elapsed = now_ms - signal.timestamp_ms
            
            # Update prices at each interval
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
        
        # Write and remove completed signals
        for sig_id in completed:
            signal = self.active_signals.pop(sig_id)
            self._write_signal(signal)
    
    def _process_polymarket_message(self, raw: str):
        """Process Polymarket WebSocket message - optimized"""
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for item in data:
                    self._handle_poly_event(item)
            elif isinstance(data, dict):
                self._handle_poly_event(data)
        except:
            pass
    
    def _handle_poly_event(self, event: dict):
        """Handle Polymarket event"""
        event_type = event.get('event_type', '')
        asset_id = event.get('asset_id', '')
        
        if event_type == 'book':
            bids = event.get('bids', [])
            asks = event.get('asks', [])
            best_bid = float(bids[0]['price']) if bids else None
            best_ask = float(asks[0]['price']) if asks else None
            
            # Filter extreme spreads
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
                    if best_bid: self.up_bid = best_bid
                    if best_ask: self.up_ask = best_ask
                elif pc_id == self.down_token_id:
                    if best_bid: self.down_bid = best_bid
                    if best_ask: self.down_ask = best_ask
    
    async def _binance_ws_loop(self):
        """Binance WebSocket loop"""
        while self.running:
            try:
                async with websockets.connect(BINANCE_WS_URL) as ws:
                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            self._process_binance_trade(json.loads(msg))
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            break
            except Exception as e:
                if self.running:
                    await asyncio.sleep(1)
    
    async def _polymarket_ws_loop(self):
        """Polymarket WebSocket loop - reconnects on token refresh"""
        while self.running:
            try:
                logger.warning(f"Connecting to Polymarket WS with tokens: UP={self.up_token_id[:16]}... DOWN={self.down_token_id[:16]}...")
                async with websockets.connect(POLYMARKET_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    sub = {"auth": {}, "type": "market", "assets_ids": [self.up_token_id, self.down_token_id]}
                    await ws.send(json.dumps(sub))
                    
                    while self.running and not self.needs_reconnect:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            self._process_polymarket_message(msg)
                        except asyncio.TimeoutError:
                            try:
                                await ws.ping()
                            except:
                                break
                        except:
                            break
                    
                    # If reconnect needed, clear the flag
                    if self.needs_reconnect:
                        self.needs_reconnect = False
                        logger.warning("Reconnecting with new tokens...")
            except Exception as e:
                if self.running:
                    await asyncio.sleep(1)
    
    async def _tracker_loop(self):
        """Update signal price tracking every 100ms"""
        while self.running:
            self._update_signal_prices()
            await asyncio.sleep(0.1)
    
    async def _market_refresh_loop(self):
        """Check for hourly market change and refresh tokens"""
        while self.running:
            try:
                now = datetime.now(timezone.utc)
                current_hour = now.hour
                
                # Check if hour changed
                if current_hour != self.current_hour:
                    logger.warning(f"Hour changed: {self.current_hour} -> {current_hour}. Refreshing market tokens...")
                    self.current_hour = current_hour
                    
                    # Clear stale price data
                    self.up_bid = None
                    self.up_ask = None
                    self.down_bid = None
                    self.down_ask = None
                    
                    # Cancel any active signals (they belong to old market)
                    if self.active_signals:
                        logger.warning(f"Discarding {len(self.active_signals)} active signals from previous market")
                        self.active_signals.clear()
                    
                    # Fetch new market tokens
                    try:
                        new_up, new_down, new_condition = await get_market_tokens()
                        self.up_token_id = new_up
                        self.down_token_id = new_down
                        self.condition_id = new_condition
                        logger.warning(f"New tokens: UP={new_up[:16]}... DOWN={new_down[:16]}...")
                        
                        # Update strike price for new hour
                        self._update_strike_price()
                        
                        # Update fills tracker subscription for new market
                        if self.fills_tracker and new_condition:
                            await self.fills_tracker.add_condition_id(new_condition)
                        
                        # Signal WebSocket to reconnect
                        self.needs_reconnect = True
                    except Exception as e:
                        logger.error(f"Failed to refresh tokens: {e}")
                
                # Check every 10 seconds
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"Market refresh error: {e}")
                await asyncio.sleep(10)
    
    def _execute_sell_order(self, signal_id: int, position: Dict):
        """Execute sell order in background thread with retry logic.
        
        Success is NOT logged here - it's logged when WS fill arrives (source of truth).
        """
        try:
            token_id = position['token_id']
            side = position['side']
            entry_price = position['entry_price']
            
            # Use shares from this specific position (updated by WS callback if available)
            # Round DOWN to 2 decimals to avoid "insufficient shares" errors
            shares = math.floor(position['shares'] * 100) / 100
            
            if shares <= 0:
                logger.warning(f"SELL #{signal_id} SKIPPED | No shares to sell")
                return
            
            # Retry logic: up to 3 attempts, reducing shares by 0.01 each time
            max_retries = 3
            for attempt in range(max_retries):
                if shares <= 0:
                    logger.warning(f"SELL #{signal_id} SKIPPED | No shares left after retries")
                    return
                
                # Register as pending sell BEFORE sending order
                if token_id not in self.pending_sell_fills:
                    self.pending_sell_fills[token_id] = []
                pending_entry = {
                    'signal_id': signal_id,
                    'side': side,
                    'entry_price': entry_price,
                    'shares_requested': shares,
                    'attempt': attempt
                }
                self.pending_sell_fills[token_id].append(pending_entry)
                
                # Send order
                result = self.trader.place_sell_order_fast(token_id, shares)
                
                # Check status
                status = result.get('status', '').upper() if result else ''
                
                if status in ['MATCHED', 'FILLED', 'LIVE']:
                    # Success - WS fill will log the confirmation
                    return
                else:
                    # Failed - remove from pending and retry with fewer shares
                    self._remove_pending_sell(token_id, signal_id)
                    logger.warning(f"SELL #{signal_id} RETRY {attempt+1}/{max_retries} | {status or 'NO_RESULT'} | shares={shares:.2f}")
                    shares = round(shares - 0.01, 2)  # Reduce shares for next attempt
            
            # All retries exhausted
            logger.error(f"SELL #{signal_id} FAILED | All {max_retries} retries exhausted")
                
        except Exception as e:
            self._remove_pending_sell(token_id, signal_id)
            logger.error(f"SELL #{signal_id} ERROR: {e}")
    
    def _remove_pending_sell(self, token_id: str, signal_id: int):
        """Remove a signal_id from pending_sell_fills"""
        pending_list = self.pending_sell_fills.get(token_id, [])
        self.pending_sell_fills[token_id] = [p for p in pending_list if p.get('signal_id') != signal_id]
    
    async def _position_manager_loop(self):
        """Check positions and exit after EXIT_DELAY_MS"""
        while self.running:
            try:
                now_ms = time.time() * 1000
                to_close = []
                
                for signal_id, pos in self.active_positions.items():
                    if now_ms >= pos['exit_time_ms']:
                        to_close.append(signal_id)
                
                # Execute sells in background
                for signal_id in to_close:
                    pos = self.active_positions.pop(signal_id)
                    self.executor.submit(self._execute_sell_order, signal_id, pos)
                
                await asyncio.sleep(0.1)  # Check every 100ms
            except Exception as e:
                logger.error(f"Position manager error: {e}")
                await asyncio.sleep(0.1)
    
    async def run(self):
        """Run the strategy"""
        logger.warning(f"Starting Frontrun Strategy | Dynamic Threshold: min={MIN_THRESHOLD_BTC} BTC, percentile={PERCENTILE_THRESHOLD}, lookback={LOOKBACK_MS/1000:.0f}s")
        logger.warning(f"UP Token: {self.up_token_id[:20]}...")
        logger.warning(f"DOWN Token: {self.down_token_id[:20]}...")
        
        # Initialize trader if enabled
        if TRADING_ENABLED:
            logger.warning(f"TRADING ENABLED: ${POSITION_SIZE_USD}/trade, exit after {EXIT_DELAY_MS}ms")
            self.trader = FastTrader()
            
            # Initialize fills tracker for real-time position updates
            logger.warning(f"Fills tracker init: client={bool(self.trader.client)} condition_id={self.condition_id[:16] if self.condition_id else 'NONE'}...")
            if self.trader.client and self.condition_id:
                self.fills_tracker = WebSocketUserFillsTracker(self.trader.client)
                self.fills_tracker.on_fill = self._on_fill_received  # Register callback
                if await self.fills_tracker.start([self.condition_id]):
                    logger.warning(f"Fills tracker started for condition: {self.condition_id[:16]}...")
                else:
                    logger.warning("Fills tracker failed to start - using estimated shares")
                    self.fills_tracker = None
            else:
                logger.warning("Fills tracker NOT initialized - missing client or condition_id")
        else:
            logger.warning("TRADING DISABLED (simulation mode)")
        
        self._open_csv()
        self.running = True
        
        tasks = [
            asyncio.create_task(self._binance_ws_loop()),
            asyncio.create_task(self._polymarket_ws_loop()),
            asyncio.create_task(self._tracker_loop()),
            asyncio.create_task(self._market_refresh_loop()),
            asyncio.create_task(self._threshold_update_loop()),
            asyncio.create_task(self._volatility_update_loop())
        ]
        
        # Add position manager if trading enabled
        if TRADING_ENABLED:
            tasks.append(asyncio.create_task(self._position_manager_loop()))
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            for t in tasks:
                t.cancel()
            if self.fills_tracker:
                await self.fills_tracker.close()
            if self.csv_file:
                self.csv_file.close()
            self.executor.shutdown(wait=False)
            logger.warning(f"Stopped. Signals written: {self.signals_written}")


async def get_market_tokens():
    """Fetch current hourly BTC market token IDs and condition ID"""
    from monitor import FastMarketMonitor
    
    monitor = FastMarketMonitor(
        use_persistent_client=True,
        market_prefix="bitcoin-up-or-down-",
        market_type="hourly"
    )
    
    try:
        markets = await monitor.get_active_markets()
        if not markets:
            raise Exception("No active market found")
        
        market = markets[0]
        clob_tokens = market.get('clobTokenIds', [])
        if isinstance(clob_tokens, str):
            clob_tokens = json.loads(clob_tokens)
        
        outcomes = market.get('outcomes', [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        
        condition_id = market.get('conditionId', '')
        
        up_idx = 0 if outcomes[0].lower() == 'up' else 1
        down_idx = 1 - up_idx
        
        return clob_tokens[up_idx], clob_tokens[down_idx], condition_id
    finally:
        await monitor.close()


async def main():
    # Get token IDs and condition ID
    up_token = UP_TOKEN_ID
    down_token = DOWN_TOKEN_ID
    condition_id = os.environ.get('CONDITION_ID', '')
    
    if not up_token or not down_token:
        logger.warning("Fetching market token IDs...")
        up_token, down_token, condition_id = await get_market_tokens()
    
    strategy = FrontrunStrategy(up_token, down_token, condition_id)
    
    try:
        await strategy.run()
    except KeyboardInterrupt:
        logger.warning("Interrupted")


if __name__ == "__main__":
    asyncio.run(main())
