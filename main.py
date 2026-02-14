"""
Optimized main trading bot with async operations and pluggable strategies

Performance optimizations:
1. Async market monitoring with batch price fetching
2. Non-blocking order execution
3. Minimal processing between price update and trade decision
4. Efficient position management

Strategy system:
- Strategies define HOW to trade (when to place orders, at what price)
- Bot provides infrastructure (monitors, trader, data collection)
- Switch strategies via --strategy parameter

Usage:
    # Bitcoin trading mode with early_queue strategy (default)
    python main.py
    
    # Ethereum monitor-only mode
    python main.py --market eth --mode monitor
    
    # Different strategy
    python main.py --market btc --mode trade --strategy early_queue
"""

import asyncio
import argparse
import os
import signal
import sys
import logging
import time
from typing import Dict, Optional, Set
from datetime import datetime, timedelta
import pytz

from config import (
    ENTRY_PRICE, MAX_POSITION_SIZE, POLL_INTERVAL,
    AVAILABLE_MARKETS, BOT_MODES, DEFAULT_MARKET, DEFAULT_MODE
)
from strategies import get_strategy, STRATEGIES


def setup_logging(market: str, mode: str):
    """Configure logging (console only, no file)"""
    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s | {market.upper()} | %(levelname)s | %(message)s',
        datefmt='%H:%M:%S',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Reduce noise from other libraries
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('py_clob_client').setLevel(logging.WARNING)
    
    return logging.getLogger(__name__)


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='Polymarket Crypto Trading Bot',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                          # BTC trading mode (default)
  python main.py --market eth             # ETH trading mode  
  python main.py --market sol --mode monitor  # SOL monitor-only
  python main.py -m btc -M trade          # Short form
        """
    )
    
    parser.add_argument(
        '-m', '--market',
        type=str,
        choices=list(AVAILABLE_MARKETS.keys()),
        default=DEFAULT_MARKET,
        help=f'Crypto market to monitor/trade (default: {DEFAULT_MARKET})'
    )
    
    parser.add_argument(
        '-M', '--mode',
        type=str,
        choices=list(BOT_MODES.keys()),
        default=DEFAULT_MODE,
        help=f'Bot operation mode (default: {DEFAULT_MODE})'
    )
    
    parser.add_argument(
        '--size',
        type=float,
        default=MAX_POSITION_SIZE,
        help=f'Position size in USD (default: {MAX_POSITION_SIZE})'
    )
    
    parser.add_argument(
        '--price',
        type=float,
        default=ENTRY_PRICE,
        help=f'Entry price for limit orders (default: {ENTRY_PRICE})'
    )
    
    parser.add_argument(
        '-s', '--strategy',
        type=str,
        choices=list(STRATEGIES.keys()),
        default='early_queue',
        help='Trading strategy to use (default: early_queue)'
    )
    
    return parser.parse_args()


# Parse args before importing other modules that might log
args = parse_args()
logger = setup_logging(args.market, args.mode)


from monitor import FastMarketMonitor
from trader import FastTrader
from risk_manager import FastRiskManager
from redeem import RedeemManager
from ws_monitor import HybridPriceMonitor
from data_collector import DataCollector
from rtds_crypto_prices import RTDSCryptoPrices
from chainlink_ds import ChainlinkDataStreams
from scripts.cash_balance import get_available_cash_usdc_from_clob_client


class FastTradingBot:
    """
    High-performance async trading bot with pluggable strategies
    
    Architecture:
    - FAST LOOP: When market locked, only fetch 2 token prices
    - SLOW LOOP: Market discovery and redeem (every 15 min)
    - Strategy system: Strategies define trading logic
    
    Args:
        market: Crypto market to trade ('btc', 'eth', 'sol')
        mode: Operation mode ('monitor' or 'trade')
        position_size: Size of orders in USD
        entry_price: Price for limit orders
        strategy_name: Name of strategy to use
    """
    
    def __init__(
        self,
        market: str = DEFAULT_MARKET,
        mode: str = DEFAULT_MODE,
        position_size: float = MAX_POSITION_SIZE,
        entry_price: float = ENTRY_PRICE,
        strategy_name: str = "early_queue"
    ):
        # Store configuration
        self.market = market
        self.mode = mode
        self.position_size = position_size
        self.entry_price = entry_price
        self.strategy_name = strategy_name
        self.trading_enabled = (mode == "trade")
        
        # Get market info
        market_info = AVAILABLE_MARKETS.get(market, AVAILABLE_MARKETS["btc"])
        self.market_prefix = market_info["prefix"]
        self.market_name = market_info["name"]
        self.market_symbol = market_info["symbol"]
        self.market_type = market_info.get("type", "15min")  # "15min" or "hourly"
        
        # Display configuration
        logger.info("=" * 60)
        logger.info("POLYMARKET CRYPTO BOT")
        logger.info("=" * 60)
        market_duration = "Hourly" if self.market_type == "hourly" else "15-min"
        logger.info(f"Market: {self.market_name} ({self.market_symbol}) {market_duration} Up/Down")
        logger.info(f"Mode: {mode.upper()} - {BOT_MODES[mode]}")
        logger.info(f"Strategy: {strategy_name}")
        if self.trading_enabled:
            logger.info(f"Entry Price: ${entry_price:.3f} | Size: ${position_size}")
        else:
            logger.info("Trading DISABLED - Monitor only")
        logger.info("=" * 60)
        
        # Future market scanning config
        self.future_scan_hours = 48  # Scan 48 hours ahead
        self.future_scan_interval = 5  # Scan every 5 seconds
        
        # Core components (use persistent client for best performance)
        # Pass market prefix and type to monitor
        self.monitor = FastMarketMonitor(
            use_persistent_client=True,
            market_prefix=self.market_prefix,
            market_type=self.market_type
        )
        self.trader = FastTrader()
        self.risk_manager = FastRiskManager(self.trader)  # Inject trader
        self.redeem_manager = RedeemManager()
        
        # WebSocket price monitor (real-time, low latency)
        self.ws_monitor = HybridPriceMonitor(self.monitor)
        self.use_websocket = True  # Can disable for fallback to HTTP polling
        
        # Data collector for price history
        self.data_collector = DataCollector()
        
        # RTDS client for real-time crypto price from Polymarket
        self.rtds_client: Optional[RTDSCryptoPrices] = None
        
        # Strategy instance (created after RTDS is connected)
        self.strategy = None
        
        # State tracking
        self.running = False
        self.last_market_id: Optional[str] = None
        self.last_redeem_check = 0
        self.redeem_interval = 3600  # 1 hour
        
        # Track attempts per market (avoid infinite retry loops)
        self.market_attempts: Dict[str, int] = {}
        
        # === LOCKED MARKET STATE (for fast loop) ===
        self.locked_market: Optional[Dict] = None  # Current market data
        self.locked_up_token: Optional[str] = None
        self.locked_down_token: Optional[str] = None
        self.market_end_time: Optional[datetime] = None
        
        # Performance metrics
        self.loop_count = 0
        self.total_latency = 0.0
        
    async def run(self):
        """Main async trading loop with FAST PATH optimization"""
        self.running = True
        
        # Get strategy class to check its requirements
        StrategyClass = get_strategy(self.strategy_name)
        
        # Start RTDS client for real-time crypto price (if strategy needs it)
        if StrategyClass.requires_rtds:
            if os.environ.get("CHAINLINK_USERNAME"):
                self.rtds_client = ChainlinkDataStreams()
                source_label = "Chainlink DS"
            else:
                self.rtds_client = RTDSCryptoPrices()
                source_label = "RTDS"

            if await self.rtds_client.start():
                crypto_symbol = self.market.upper()  # btc -> BTC, eth -> ETH, sol -> SOL
                logger.info(f"{source_label} connected - real-time {crypto_symbol} price enabled")
                if StrategyClass.requires_data_collector:
                    self.data_collector.set_rtds_client(self.rtds_client, crypto_symbol)
            else:
                logger.warning(f"{source_label} connection failed - crypto price will not be recorded")
        else:
            logger.info("Strategy does not require RTDS - skipping")
        
        # Start WebSocket connection for real-time prices (if strategy needs it)
        if StrategyClass.requires_price_websocket:
            if self.use_websocket:
                if await self.ws_monitor.start():
                    logger.info("WebSocket connected - real-time prices enabled")
                else:
                    logger.warning("WebSocket failed, falling back to HTTP polling")
                    self.use_websocket = False
        else:
            logger.info("Strategy does not require price WebSocket - skipping")
            self.use_websocket = False
        
        # Set data collector flag (used throughout the bot)
        self._data_collector_enabled = StrategyClass.requires_data_collector
        if not self._data_collector_enabled:
            logger.info("Strategy does not require data collector - disabled")
        
        # Create strategy instance
        self.strategy = StrategyClass(
            trader=self.trader,
            monitor=self.monitor,
            ws_monitor=self.ws_monitor,
            data_collector=self.data_collector,
            rtds_client=self.rtds_client,
            market_symbol=self.market_symbol,
            entry_price=self.entry_price,
            position_size=self.position_size,
        )
        
        # Initialize strategy (recover state, etc.) - only if trading enabled
        if self.trading_enabled:
            await self.strategy.initialize()
        
        logger.info(f"Starting {'trading' if self.trading_enabled else 'monitoring'} loop...")

        # Launch future market scanner as independent background task
        # so it doesn't block the fast price-recording loop
        if self.trading_enabled:
            asyncio.create_task(self._future_scan_loop())

        while self.running:
            loop_start = time.perf_counter()

            try:
                # Check if we need to find/refresh current market
                if self._needs_market_refresh():
                    await asyncio.sleep(POLL_INTERVAL)
                    await self._refresh_market()

                # FAST PATH: Only fetch prices for locked tokens
                if self.locked_market:
                    await self._fast_iteration()

            except Exception as e:
                logger.error(f"Error in trading loop: {e}", exc_info=True)

            # Track performance
            latency = time.perf_counter() - loop_start
            self.loop_count += 1
            self.total_latency += latency

            await asyncio.sleep(0.0001)  # 0.1ms sleep

        await self.shutdown()
    
    async def _future_scan_loop(self):
        """Background task: scan for future markets every N seconds without blocking the fast path."""
        while self.running:
            try:
                await self._scan_and_place_future_orders()
            except Exception as e:
                logger.error(f"Error in future scan loop: {e}", exc_info=True)
            await asyncio.sleep(self.future_scan_interval)

    def _needs_market_refresh(self) -> bool:
        """Check if we need to find a new market"""
        # No market locked yet
        if not self.locked_market:
            return True
        
        # Market expired - compare in ET timezone
        if self.market_end_time:
            et_tz = pytz.timezone('America/New_York')
            now_et = datetime.now(et_tz)
            
            # Ensure market_end_time is timezone-aware
            if self.market_end_time.tzinfo is None:
                market_end_et = et_tz.localize(self.market_end_time)
            else:
                market_end_et = self.market_end_time.astimezone(et_tz)
            
            # Allow strategies to extend monitoring past end_time
            grace = getattr(self.strategy, 'post_close_grace_seconds', 0) if self.strategy else 0
            effective_end_et = market_end_et + timedelta(seconds=grace)

            if now_et >= effective_end_et:
                if grace > 0:
                    logger.info(f"Market expired after {grace}s grace (now: {now_et.strftime('%H:%M:%S')} ET >= end+grace: {effective_end_et.strftime('%H:%M:%S')} ET)")
                else:
                    logger.info(f"Market expired (now: {now_et.strftime('%H:%M:%S')} ET >= end: {market_end_et.strftime('%H:%M:%S')} ET)")
                # Mark for saving - will be saved in _refresh_market
                self._market_expired = True
                return True
        
        return False

    def _get_available_cash_usdc(self) -> Optional[float]:
        """
        Return *available* collateral (USDC cash) for the trading account.

        Important: This is NOT portfolio value. It comes from the CLOB L2 balance
        endpoint (COLLATERAL balance).
        """
        client = getattr(self.trader, "client", None) if self.trader else None
        return get_available_cash_usdc_from_clob_client(client)

    async def _redeem_if_low_cash_after_market_end(self, threshold_usd: float = 100.0) -> None:
        """
        At market end: if available cash is below threshold, run `redeem.py`.
        Runs redeem in a background thread to avoid blocking the asyncio loop.
        """
        cash = self._get_available_cash_usdc()
        if cash is None:
            return

        if cash >= threshold_usd:
            return

        logger.info(f"Low cash balance: ${cash:.2f} < ${threshold_usd:.2f} -> running redeem")
        try:
            results = await asyncio.to_thread(self.redeem_manager.run_redeem)
            # Keep this log compact; redeem.py already logs details.
            if isinstance(results, dict):
                logger.info(
                    f"Redeem finished: success={results.get('success')} "
                    f"redeemed={results.get('positions_redeemed')} "
                    f"found={results.get('positions_found')}"
                )
        except Exception as e:
            logger.warning(f"Redeem failed: {e}")
    
    async def _refresh_market(self):
        """SLOW PATH: Find new market and set up (runs every ~15 min)"""
        # Save previous market data if it expired
        if getattr(self, '_market_expired', False):
            # Determine winner based on last known prices
            winner = None
            if self.locked_up_token and self.locked_down_token:
                prices = self.ws_monitor.get_prices() if self.use_websocket else {}
                if prices:
                    up_price = prices.get(self.locked_up_token, 0)
                    down_price = prices.get(self.locked_down_token, 0)
                    if up_price > 0.5:
                        winner = 'UP'
                    elif down_price > 0.5:
                        winner = 'DOWN'
            
            # Notify strategy of market end
            if self.strategy and self.locked_market:
                await self.strategy.on_market_end(
                    market_data={'condition_id': self.locked_market.get('conditionId'), 
                                 'question': self.locked_market.get('question', ''),
                                 'slug': self.locked_market.get('slug', '')},
                    winner=winner
                )
            
            # Save data if collector is enabled
            if self._data_collector_enabled and self.data_collector.has_active_market():
                await self.data_collector.save_market(winner=winner)

            # If market ended, check cash and redeem if needed
            await self._redeem_if_low_cash_after_market_end(threshold_usd=100.0)
            self._market_expired = False
        
        # Clear locked state
        self.locked_market = None
        self.locked_up_token = None
        self.locked_down_token = None
        
        # Find active markets
        prices = await self.monitor.get_all_market_prices()
        
        if not prices:
            return
        
        # Lock onto first active market
        price_data = prices[0]
        market = price_data['market']
        market_id = market.get('conditionId', '')[:10]
        
        self.locked_market = market
        self.locked_up_token = price_data['up_token_id']
        self.locked_down_token = price_data['down_token_id']
        
        # Calculate market end time (keep timezone info!)
        et_tz = pytz.timezone('America/New_York')
        if self.monitor.current_market_end_time:
            # Keep the timezone-aware datetime from monitor
            self.market_end_time = self.monitor.current_market_end_time
        else:
            # Fallback: 15 min from now in ET
            self.market_end_time = datetime.now(et_tz) + timedelta(minutes=15)
        
        # Log new market
        if market_id != self.last_market_id:
            question = market.get('question', 'Unknown')[:50]
            logger.info(f"NEW MARKET: {question}...")
            end_time_et = self.market_end_time.astimezone(et_tz) if self.market_end_time.tzinfo else et_tz.localize(self.market_end_time)
            logger.info(f"  Ends: {end_time_et.strftime('%H:%M:%S')} ET")
            self.last_market_id = market_id
            self.market_attempts.clear()
            
            # Subscribe to WebSocket for real-time price updates
            if self.use_websocket:
                await self.ws_monitor.subscribe_to_market(
                    self.locked_up_token,
                    self.locked_down_token
                )
            
            # Start collecting price data for this market (if enabled)
            if self._data_collector_enabled:
                self.data_collector.start_market(
                    condition_id=market.get('conditionId', ''),
                    question=market.get('question', 'Unknown'),
                    up_token_id=self.locked_up_token,
                    down_token_id=self.locked_down_token,
                    start_time=datetime.now(et_tz),
                    end_time=self.market_end_time
                )
            
            # Notify strategy that market is now active
            if self.strategy and self.trading_enabled:
                await self.strategy.on_market_active({
                    'condition_id': market.get('conditionId', ''),
                    'question': market.get('question', 'Unknown'),
                    'up_token_id': self.locked_up_token,
                    'down_token_id': self.locked_down_token,
                    'start_time': datetime.now(et_tz),
                    'end_time': self.market_end_time,
                    'market': market
                })
        
        # Periodic redeem (only on slow path)
        # await self._periodic_redeem()
    
    async def _fast_iteration(self):
        """
        FAST PATH: Minimal latency price check and execution.
        Uses WebSocket for instant prices (no HTTP call).
        """
        # Check for stale WebSocket data and force reconnect if needed
        if self.use_websocket:
            await self.ws_monitor.check_staleness()

        # Get prices - WebSocket (instant) or HTTP (fallback)
        if self.use_websocket:
            # INSTANT: Read from memory (no network call!)
            prices = self.ws_monitor.get_prices()
            if not prices:
                prices = await self.ws_monitor.get_prices_with_fallback()
        else:
            # HTTP polling fallback
            prices = await self.monitor.get_prices_batch([
                self.locked_up_token,
                self.locked_down_token
            ])

        if not prices:
            return

        up_price = prices.get(self.locked_up_token)
        down_price = prices.get(self.locked_down_token)

        # Skip if no valid prices
        if up_price is None or down_price is None:
            return

        # Record price snapshot (if data collector enabled)
        if self._data_collector_enabled:
            self.data_collector.record_price(up_price, down_price)

        # Notify strategy of price update (if trading enabled)
        if self.trading_enabled and self.strategy:
            await self.strategy.on_price_update(
                up_price=up_price,
                down_price=down_price,
                up_token_id=self.locked_up_token,
                down_token_id=self.locked_down_token,
                market=self.locked_market
            )
    
    async def _scan_and_place_future_orders(self):
        """
        Scan for new future markets and notify strategy.
        Strategy decides whether/how to place orders.
        """
        try:
            # Get all future markets (up to 24h ahead)
            future_markets = await self.monitor.get_future_markets(self.future_scan_hours)
            
            if not future_markets:
                return
            
            # Notify strategy about each new market
            for market_data in future_markets:
                condition_id = market_data['condition_id']
                if condition_id and not self.strategy.is_market_processed(condition_id):
                    await self.strategy.on_new_market(market_data)
                    # Small delay between markets to avoid rate limits
                    await asyncio.sleep(0.5)
                    
        except Exception as e:
            logger.error(f"Error scanning future markets: {e}")
    
    async def _periodic_redeem(self):
        """Periodically check for redeemable positions"""
        now = time.time()
        
        if now - self.last_redeem_check > self.redeem_interval:
            self.last_redeem_check = now
            
            # Run redeem in background to not block trading
            try:
                self.redeem_manager.check_and_redeem()
            except Exception as e:
                logger.debug(f"Redeem check failed: {e}")
    
    async def shutdown(self):
        """Clean up resources"""
        logger.info("Shutting down...")
        
        # Shutdown strategy
        if self.strategy:
            await self.strategy.shutdown()
        
        # Save any pending market data
        if self.data_collector.has_active_market():
            logger.info("Saving pending market data...")
            await self.data_collector.save_market()
        
        await self.data_collector.close()
        
        # Close RTDS connection
        if self.rtds_client:
            await self.rtds_client.close()
        
        # Close WebSocket connection
        if self.use_websocket:
            await self.ws_monitor.close()
        
        await self.monitor.close()
        
        positions = self.trader.get_all_positions()
        if positions:
            logger.warning(f"{len(positions)} open positions - close manually on Polymarket")
        
        logger.info("Bot stopped")


async def main():
    """Entry point with signal handling"""
    # Create bot with CLI arguments
    bot = FastTradingBot(
        market=args.market,
        mode=args.mode,
        position_size=args.size,
        entry_price=args.price,
        strategy_name=args.strategy
    )
    
    # Handle shutdown signals
    loop = asyncio.get_running_loop()
    
    def signal_handler():
        logger.info("Shutdown signal received")
        bot.running = False
    
    if sys.platform != 'win32':
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
    
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")

