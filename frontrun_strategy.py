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
from datetime import datetime, timezone
from typing import Optional, Dict, List
from dataclasses import dataclass, field, asdict
from collections import deque

import websockets

# Minimal logging - only errors
logging.basicConfig(level=logging.WARNING, format='%(asctime)s|%(levelname)s|%(message)s')
logger = logging.getLogger(__name__)

# ============== CONFIGURATION ==============
AGG_WINDOW_MS = 100  # Rolling window for trade aggregation
TRACK_DURATION_MS = 5000  # Track prices for 5 seconds after signal
TRACK_INTERVAL_MS = 500  # Record every 500ms

# Dynamic threshold configuration
MIN_THRESHOLD_BTC = float(os.environ.get('MIN_THRESHOLD_BTC', '5.0'))  # Floor minimum
STD_MULTIPLIER = float(os.environ.get('STD_MULTIPLIER', '3.0'))  # Multiplier for std dev
LOOKBACK_MS = int(os.environ.get('LOOKBACK_MS', '600000'))  # 5 minutes in ms

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
    threshold_used: float  # Dynamic threshold at signal time
    
    # Initial prices at T=0
    up_ask_0: Optional[float] = None
    up_bid_0: Optional[float] = None
    down_ask_0: Optional[float] = None
    down_bid_0: Optional[float] = None
    
    # Prices at each interval (500ms, 1000ms, etc.)
    up_ask_500: Optional[float] = None
    up_ask_1000: Optional[float] = None
    up_ask_1500: Optional[float] = None
    up_ask_2000: Optional[float] = None
    up_ask_2500: Optional[float] = None
    up_ask_3000: Optional[float] = None
    up_ask_3500: Optional[float] = None
    up_ask_4000: Optional[float] = None
    up_ask_4500: Optional[float] = None
    up_ask_5000: Optional[float] = None
    
    down_ask_500: Optional[float] = None
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
    
    def __init__(self, up_token_id: str, down_token_id: str):
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
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
        self.volume_history: deque = deque()  # (window_ms, max_net_volume) per 100ms window
        self.current_window_ms: int = 0
        self.current_window_max_vol: float = 0.0
        self.current_threshold: float = MIN_THRESHOLD_BTC
        
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
        
        # Track max volume per 100ms window for threshold calculation
        window_ms = (now_ms // AGG_WINDOW_MS) * AGG_WINDOW_MS
        if window_ms != self.current_window_ms:
            # New window - save previous window's max if significant
            if self.current_window_ms > 0 and self.current_window_max_vol > 0:
                self.volume_history.append((self.current_window_ms, self.current_window_max_vol))
            self.current_window_ms = window_ms
            self.current_window_max_vol = net_volume
        else:
            # Same window - track max
            self.current_window_max_vol = max(self.current_window_max_vol, net_volume)
        
        # Cooldown: don't fire multiple signals within the same window
        if now_ms - self.last_signal_ms < AGG_WINDOW_MS:
            return
        
        # Check threshold on NET volume
        if net_volume >= self.current_threshold:
            direction = 'SELL' if self.sell_qty_sum > self.buy_qty_sum else 'BUY'
            self.last_signal_ms = now_ms
            self._create_signal(now_ms, direction, net_volume, len(self.trade_buffer), self.last_price)
    
    async def _threshold_update_loop(self):
        """Async loop to recalculate dynamic threshold every second"""
        while self.running:
            try:
                now_ms = int(time.time() * 1000)
                
                # Clean old entries from volume history
                cutoff = now_ms - LOOKBACK_MS
                while self.volume_history and self.volume_history[0][0] < cutoff:
                    self.volume_history.popleft()
                
                # Recalculate threshold if we have enough data
                if len(self.volume_history) >= 10:
                    volumes = [v for _, v in self.volume_history]
                    mean_vol = sum(volumes) / len(volumes)
                    
                    if len(volumes) > 1:
                        variance = sum((v - mean_vol) ** 2 for v in volumes) / len(volumes)
                        std_vol = variance ** 0.5
                    else:
                        std_vol = 0
                    
                    dynamic_threshold = mean_vol + STD_MULTIPLIER * std_vol
                    self.current_threshold = max(MIN_THRESHOLD_BTC, dynamic_threshold)
                    
                    # logger.warning(f"Threshold: {self.current_threshold:.2f} BTC (calc: {dynamic_threshold:.2f}, mean: {mean_vol:.2f}, std: {std_vol:.2f}, samples: {len(volumes)})")
                
                await asyncio.sleep(1)  # Update every 1 second
            except Exception as e:
                logger.error(f"Threshold update error: {e}")
                await asyncio.sleep(1)
    
    def _create_signal(self, timestamp_ms: int, direction: str, qty: float, count: int, price: float):
        """Create new signal and start tracking"""
        self.signal_counter += 1
        
        signal = Signal(
            signal_id=self.signal_counter,
            timestamp_ms=timestamp_ms,
            timestamp_iso=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            direction=direction,
            total_qty_btc=qty,
            trade_count=count,
            binance_price=price,
            threshold_used=self.current_threshold,
            up_ask_0=self.up_ask,
            up_bid_0=self.up_bid,
            down_ask_0=self.down_ask,
            down_bid_0=self.down_bid
        )
        
        self.active_signals[self.signal_counter] = signal
        logger.warning(f"SIGNAL #{self.signal_counter}: {direction} {qty:.4f} BTC @ ${price:.2f}")
    
    def _update_signal_prices(self):
        """Update price evolution for active signals"""
        now_ms = int(time.time() * 1000)
        completed = []
        
        for sig_id, signal in self.active_signals.items():
            elapsed = now_ms - signal.timestamp_ms
            
            # Update prices at each interval
            if elapsed >= 500 and signal.up_ask_500 is None:
                signal.up_ask_500 = self.up_ask
                signal.down_ask_500 = self.down_ask
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
                        new_up, new_down = await get_market_tokens()
                        self.up_token_id = new_up
                        self.down_token_id = new_down
                        logger.warning(f"New tokens: UP={new_up[:16]}... DOWN={new_down[:16]}...")
                        
                        # Signal WebSocket to reconnect
                        self.needs_reconnect = True
                    except Exception as e:
                        logger.error(f"Failed to refresh tokens: {e}")
                
                # Check every 10 seconds
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"Market refresh error: {e}")
                await asyncio.sleep(10)
    
    async def run(self):
        """Run the strategy"""
        logger.warning(f"Starting Frontrun Strategy | Dynamic Threshold: min={MIN_THRESHOLD_BTC} BTC, multiplier={STD_MULTIPLIER}x std, lookback={LOOKBACK_MS/1000:.0f}s")
        logger.warning(f"UP Token: {self.up_token_id[:20]}...")
        logger.warning(f"DOWN Token: {self.down_token_id[:20]}...")
        
        self._open_csv()
        self.running = True
        
        tasks = [
            asyncio.create_task(self._binance_ws_loop()),
            asyncio.create_task(self._polymarket_ws_loop()),
            asyncio.create_task(self._tracker_loop()),
            asyncio.create_task(self._market_refresh_loop()),
            asyncio.create_task(self._threshold_update_loop())
        ]
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            for t in tasks:
                t.cancel()
            if self.csv_file:
                self.csv_file.close()
            logger.warning(f"Stopped. Signals written: {self.signals_written}")


async def get_market_tokens():
    """Fetch current hourly BTC market token IDs"""
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
        
        up_idx = 0 if outcomes[0].lower() == 'up' else 1
        down_idx = 1 - up_idx
        
        return clob_tokens[up_idx], clob_tokens[down_idx]
    finally:
        await monitor.close()


async def main():
    # Get token IDs
    up_token = UP_TOKEN_ID
    down_token = DOWN_TOKEN_ID
    
    if not up_token or not down_token:
        logger.warning("Fetching market token IDs...")
        up_token, down_token = await get_market_tokens()
    
    strategy = FrontrunStrategy(up_token, down_token)
    
    try:
        await strategy.run()
    except KeyboardInterrupt:
        logger.warning("Interrupted")


if __name__ == "__main__":
    asyncio.run(main())
