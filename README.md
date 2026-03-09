# Polymarket Trading Bot

An async, high-performance trading bot for Polymarket crypto prediction markets (BTC, ETH, SOL). Trades 15-minute and hourly up/down markets on the Polygon network via the official `py-clob-client` SDK.

---

## Features

- **Sub-100ms order execution** via pre-signed orders (sign at market detection, POST at trade time)
- **Dual-loop architecture**: ~10ms fast path for price updates, ~15min slow path for market management
- **Pluggable strategy system**: swap strategies at launch with a single CLI flag
- **Three independent WebSocket feeds**: Polymarket CLOB prices, Chainlink oracle prices, Binance order flow
- **Auto-redemption**: converts resolved winning positions back to USDC automatically
- **Docker-native**: multi-service compose setup for running multiple bots simultaneously

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   FastTradingBot                    │
│  ┌──────────────────┐   ┌───────────────────────┐  │
│  │   Fast Loop      │   │     Slow Loop          │  │
│  │   (~10ms)        │   │     (~15 min)          │  │
│  │  Price updates   │   │  Market discovery      │  │
│  │  Strategy calls  │   │  Order recovery        │  │
│  │  Order execution │   │  Position redemption   │  │
│  └──────────────────┘   └───────────────────────┘  │
└─────────────────────────────────────────────────────┘
         │               │               │
  ┌──────┴──────┐  ┌─────┴──────┐  ┌────┴──────────┐
  │  CLOB WS    │  │ Chainlink  │  │    Binance    │
  │ (token      │  │ DS / RTDS  │  │  aggTrade +   │
  │  prices)    │  │ (BTC/ETH/  │  │  OBI signals  │
  └─────────────┘  │  SOL spot) │  └───────────────┘
                   └────────────┘
```

### Core Components

| Module | Class | Responsibility |
|---|---|---|
| `main.py` | `FastTradingBot` | Event loop orchestrator, strategy lifecycle, CLI |
| `trader.py` | `FastTrader` | Pre-signed order execution, position tracking |
| `monitor.py` | `FastMarketMonitor` | Market discovery via Gamma API, batch price fetching |
| `risk_manager.py` | `FastRiskManager` | Stop loss enforcement, position limits |
| `auth.py` | `PolymarketAuth` | Wallet authentication (EOA / Magic / Browser proxy) |
| `redeem.py` | `RedeemManager` | Auto-redeem resolved positions to USDC |
| `data_collector.py` | `DataCollector` | Per-second price snapshots, API submission on market end |
| `ws_monitor.py` | `HybridPriceMonitor` | Real-time CLOB token prices with HTTP fallback |
| `rtds_crypto_prices.py` | `RTDSCryptoPrices` | Chainlink prices via Polymarket RTDS relay (~1s latency) |
| `chainlink_ds.py` | `ChainlinkDataStreams` | Direct Chainlink Data Streams (lower latency + REST API) |
| `binance_ws.py` | `BinanceBreakoutDetector` | Binance order book imbalance + trade flow signals |

---

## Strategies

### `early_queue` (default)
Places GTC limit orders on both UP and DOWN outcomes immediately when a market is detected (~24h before start). Exploits Polymarket's FIFO price-tie resolution — earlier orders get queue priority at the same price level.

### `expected_value`
Trades mispriced tokens using historical probability lookup tables. Calculates expected value against current odds and sizes positions with 1/4 Kelly Criterion. Targets a delta-neutral book by accumulating both sides roughly balanced.

### `whale_copy`
Monitors a configurable list of target wallets for large directional buys in 15-minute crypto markets. Copies their position when accumulated buy flow exceeds a detection threshold.

### `whale_frontrun`
Detects unusually large Binance directional volume (99.9th percentile of trailing 10-minute flow) and anticipates the resulting price move on Polymarket. Uses Black-Scholes pricing and EWMA volatility for entry sizing. Exits after a fixed hold period (~4s).

### `postclose_sniper`
Places a directional bet based on the Chainlink oracle price delta between market open and close. Queries the Chainlink REST API at exact boundary timestamps for definitive prices, then POSTs the winning-side order during the post-close CLOB settlement lag window.

---

## Requirements

- Python 3.12+ (required for `polymarket_apis` / auto-redemption)
- Polygon wallet funded with USDC
- Polymarket account with approved USDC/CTF allowances (run `scripts/set_allowances.py` once)

---

## Installation

```bash
git clone <repo-url>
cd polymarket-tests
pip install -r requirements.txt
```

Copy and populate the environment file:

```bash
cp .env.example .env
```

---

## Configuration

### `.env` — Secrets

| Variable | Required | Description |
|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | Yes | Wallet private key (hex, no `0x` prefix) |
| `POLYMARKET_FUNDER_ADDRESS` | No | Separate funding wallet address |
| `SIGNATURE_TYPE` | No | `0` = EOA (default), `1` = Magic, `2` = Browser proxy |
| `CHAINLINK_API_KEY` | No | Enables direct Chainlink Data Streams (lower latency) |
| `CHAINLINK_USERNAME` | No | Chainlink HMAC username |
| `CHAINLINK_PASSWORD` | No | Chainlink HMAC secret |
| `DATA_COLLECTOR_API_URL` | No | Local API endpoint for price snapshot submission |
| `WHALE_TARGET_WALLETS` | No | Comma-separated whale wallet addresses (`whale_copy`) |
| `BREAKOUT_OBI_THRESHOLD` | No | OBI threshold for `whale_frontrun` |
| `SNIPE_SIZE_USD` | No | Order size for `postclose_sniper` (default: 10) |

### `config.py` — Trading Parameters

Key defaults (override in `config.py`):

```python
ENTRY_PRICE = 0.01              # GTC limit order price
MAX_POSITION_SIZE = 200         # USD per market
MAX_CONCURRENT_POSITIONS = 2
POLL_INTERVAL = 0.01            # 10ms fast loop
ENABLE_STOP_LOSS = False
```

---

## Usage

### One-time setup

```bash
python scripts/set_allowances.py   # Approve USDC + CTF contracts (once per wallet)
python scripts/cash_balance.py     # Check available USDC balance
```

### Running the bot

```bash
# BTC 15-min, early_queue strategy, trade mode (defaults)
python main.py

# Specify market and strategy
python main.py --market eth --strategy early_queue
python main.py --market btc --strategy expected_value
python main.py --market btc --strategy whale_frontrun
python main.py --market btc --strategy postclose_sniper

# Monitor only (no orders placed)
python main.py --market eth --mode monitor
```

**Available markets:** `btc`, `eth`, `sol` (15-minute) | `btc-1h`, `eth-1h` (hourly, no fees)

**Available strategies:** `early_queue`, `expected_value`, `whale_copy`, `whale_frontrun`, `postclose_sniper`

---

## Docker Deployment

```bash
# BTC trading (default)
docker compose up bot-btc-trade

# ETH or SOL trading
docker compose --profile eth up
docker compose --profile sol up

# Multiple bots simultaneously
docker compose --profile eth --profile sol up

# Specialized strategies
docker compose --profile whale up      # Whale copy
docker compose --profile ev up         # Expected value
docker compose --profile sniper up     # Post-close sniper
docker compose --profile frontrun up   # Binance frontrun signal logger
```

---

## Scripts

Standalone utilities in `scripts/` — run directly with `python scripts/<name>.py`:

| Script | Purpose |
|---|---|
| `cash_balance.py` | Fetch available USDC balance from CLOB |
| `set_allowances.py` | Approve USDC/CTF for Polymarket (one-time setup) |
| `test_order.py` | Test order placement end-to-end |
| `trade_history.py` | Fetch and display full trade history for wallet |
| `frontrun_strategy.py` | Standalone Binance aggTrade signal detector (logs to CSV) |
| `chainlink_latency.py` | Measure and compare RTDS vs direct Chainlink latency |
| `compare_chainlink_sources.py` | Log BTC price delta between RTDS relay and direct feed |
| `validate_resolution.py` | Verify Chainlink boundary prices match actual market outcomes |

---

## WebSocket Feeds

### Polymarket CLOB (`ws_monitor.py`)
Real-time UP/DOWN token prices. Used by all strategies that need live market odds.
Endpoint: `wss://ws-subscriptions-clob.polymarket.com/ws/market`

### Chainlink Data Streams (`chainlink_ds.py`) — recommended
Direct connection to Chainlink oracle. Provides lower latency than RTDS and exposes a REST API for querying prices at exact timestamps (used by `postclose_sniper`).
Requires: `CHAINLINK_API_KEY`, `CHAINLINK_USERNAME`, `CHAINLINK_PASSWORD`

### RTDS (`rtds_crypto_prices.py`) — fallback
Chainlink prices relayed through Polymarket's RTDS service. No credentials required but adds ~1–3s latency. Used automatically when `CHAINLINK_API_KEY` is not set.

### Binance (`binance_ws.py`)
`@bookTicker` + `@aggTrade` streams for BTCUSDT. Used exclusively by `whale_frontrun`.

---

## Adding a Strategy

1. Create `strategies/my_strategy.py` — see `strategies/template.py` for the full interface
2. Subclass `BaseStrategy` and implement `on_new_market()` and `on_price_update()`
3. Declare resource requirements as class attributes:

   ```python
   requires_price_websocket = True   # Polymarket CLOB prices
   requires_data_collector = True    # Price snapshot recording
   requires_rtds = True              # Chainlink crypto prices
   post_close_grace_seconds = 0      # Extended monitoring after market end
   ```

4. Register in `strategies/__init__.py`:

   ```python
   from .my_strategy import MyStrategy
   STRATEGIES["my_strategy"] = MyStrategy
   ```

5. Run with `python main.py --strategy my_strategy`

---

## API Rate Limits

| Endpoint | Limit |
|---|---|
| Gamma API `/events` | 50 req/s |
| CLOB API (general) | 900 req/s |
| CLOB POST `/order` | 350 req/s burst |

Batch `/midprices` is preferred over individual `/midpoint` calls wherever possible.

---

## Risk Disclaimer

This software is provided for educational and research purposes. Prediction market trading involves significant financial risk. Past performance is not indicative of future results. Use at your own risk.
