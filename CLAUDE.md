# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Async Python trading bot for Polymarket crypto prediction markets (BTC/ETH/SOL 15-minute and hourly up/down markets). Trades on Polygon chain via the `py-clob-client` SDK. Written in Spanish-influenced style (README, some comments).

## Commands

```bash
# Install dependencies (requires Python 3.12+ for polymarket_apis)
pip install -r requirements.txt

# Run bot (default: BTC 15-min, early_queue strategy, trade mode)
python main.py
python main.py --market eth --mode monitor
python main.py --market btc --strategy expected_value
python main.py --market btc-1h --strategy whale_frontrun

# Docker deployment
docker compose up bot-btc-trade                    # BTC trading
docker compose --profile eth up                    # ETH trading
docker compose --profile whale up                  # Whale copy strategy
docker compose --profile frontrun up               # Binance frontrun
docker compose --profile ev up                     # Expected value strategy
```

Available markets: `btc`, `eth`, `sol` (15-min), `btc-1h`, `eth-1h` (hourly, no fees).
Available strategies: `early_queue`, `expected_value`, `whale_copy`, `whale_frontrun`.
Modes: `trade` (place orders), `monitor` (price data only).

## Architecture

### Dual-Loop Design

The bot runs two concurrent loops with different frequencies:

- **Fast path (~10ms)**: Price checks via WebSocket, strategy `on_price_update()` calls, order execution. Only touches 2 token IDs per cycle.
- **Slow path (~15 min)**: Market discovery, order recovery, redemption of resolved positions, data collection.

### Strategy System

Strategies are pluggable via `strategies/__init__.py` registry (`STRATEGIES` dict). All inherit from `BaseStrategy` (ABC) and implement:

```
initialize() -> on_new_market() -> on_market_active() -> on_price_update() [loop] -> on_market_end() -> shutdown()
```

Each strategy declares resource requirements via class attributes: `requires_price_websocket`, `requires_data_collector`, `requires_rtds`. The bot skips initializing unused components.

To add a new strategy: subclass `BaseStrategy`, implement `on_new_market()` and `on_price_update()` (abstract), register in `strategies/__init__.py` STRATEGIES dict.

### Pre-Signed Orders

`FastTrader` pre-signs buy/sell orders when a market is detected, so execution at trade time is just an HTTP POST (no signing delay). This achieves <100ms execution latency.

### WebSocket Architecture

Three independent WebSocket connections serve different purposes:
- **Polymarket CLOB WS** (`ws_monitor.py`): Real-time token prices for UP/DOWN outcomes
- **RTDS** (`rtds_crypto_prices.py`): Real-time BTC/ETH/SOL spot prices (~1s updates, faster than Chainlink)
- **Binance WS** (`binance_ws.py`): `@bookTicker` + `@aggTrade` streams for the whale_frontrun strategy (OBI/volume signals)

All have HTTP polling fallbacks on disconnect.

### Core Components (root directory)

- `main.py` — `FastTradingBot`: Event loop orchestrator, CLI arg parsing, strategy lifecycle
- `trader.py` — `FastTrader`: Pre-signed order execution, position tracking, thread-safe with locks
- `monitor.py` — `FastMarketMonitor`: Market discovery via Gamma API, batch price fetching, future market scanning
- `risk_manager.py` — `FastRiskManager`: Stop loss tracking, position limits
- `auth.py` — `PolymarketAuth`: Wallet auth via py-clob-client, supports EOA/Magic/Browser proxy (SIGNATURE_TYPE 0/1/2)
- `redeem.py` — Auto-redemption of resolved positions back to USDC
- `data_collector.py` — Records per-second price snapshots, submits to local API on market end

### Directory Layout

- `strategies/` — Pluggable trading strategies (all inherit `BaseStrategy`)
- `scripts/` — Standalone utility scripts (cash_balance, trade_history, set_allowances, test_order, frontrun_strategy). Run directly with `python scripts/<name>.py`.
- `analysis/` — Research and backtesting tools (not used by the bot at runtime)
- `data/` — Data files (CSVs, etc. — gitignored)

### Key Patterns

- `TYPE_CHECKING` imports used to avoid circular dependencies between core modules
- `orjson` used instead of `json` for 5-10x faster parsing in hot paths
- `httpx` with HTTP/2 and connection pooling for all HTTP calls
- `ThreadPoolExecutor` for non-blocking order submission from the async loop
- Thread locks on shared state (positions, prices) in `FastTrader`

## Configuration

All trading parameters in `config.py`. Secrets via `.env` file:
- `POLYMARKET_PRIVATE_KEY` (required for trading)
- `POLYMARKET_FUNDER_ADDRESS` (optional, separate funding wallet)
- `SIGNATURE_TYPE` (0=EOA, 1=Magic, 2=Browser proxy)
- `DATA_COLLECTOR_API_URL` (for price history submission)
- Strategy-specific env vars: `WHALE_TARGET_WALLETS`, `BREAKOUT_OBI_THRESHOLD`, `BREAKOUT_DEVIATION_PCT`, `BREAKOUT_MIN_VOLUME`, `BREAKOUT_COPY_SIZE`, `BREAKOUT_PRICE_SLIPPAGE`

## API Rate Limits

- Gamma API (`/events`): 50 req/s
- CLOB API (general): 900 req/s
- CLOB POST `/order`: 350 req/s burst
- Batch `/midprices` preferred over individual `/midpoint` calls
