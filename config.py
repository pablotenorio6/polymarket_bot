"""
Configuration for Polymarket Trading Bot
"""

import os

# API Endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Trading Parameters - Early Queue Strategy
# Place both UP and DOWN limit orders at market start for FIFO priority
ENTRY_PRICE = 0.01    # Limit order price (GTC - waits in orderbook)
ORDER_PRICE = 0.01    # Alias for ENTRY_PRICE (backwards compatibility)
STOP_LOSS_PRICE = 0.01  # Not used in current strategy
MAX_ATTEMPTS_PER_MARKET = 3

# Position sizing
MAX_POSITION_SIZE = 200  # Maximum USD to risk per trade

# ============================================
# MONITORING & PERFORMANCE
# ============================================
POLL_INTERVAL = 0.01  # Seconds between price checks (10ms)
REQUEST_TIMEOUT = 5  # HTTP request timeout

# Rate limit safety (based on Polymarket API limits)
# See: https://docs.polymarket.com/quickstart/introduction/rate-limits
# 
# Key limits:
#   - /events: 500 req/10s (50/s)
#   - /midprices: 500 req/10s (50/s)
#   - CLOB general: 9000 req/10s (900/s)
#   - POST /order: 3500 req/10s burst (350/s)
#
# With POLL_INTERVAL=0.1s (10 loops/s), we use:
#   - ~4 /events requests per loop (slug lookups)
#   - ~1 /midprices request per loop (batch)
#   - Total: ~50 req/s (well within limits)

# ============================================
# MULTI-MARKET SUPPORT
# ============================================
# Available crypto markets (slug prefixes)
# 15-minute markets (have orderbook fees)
AVAILABLE_MARKETS = {
    "btc": {
        "prefix": "btc-updown-15m-",
        "name": "Bitcoin",
        "symbol": "BTC"
    },
    "eth": {
        "prefix": "eth-updown-15m-",
        "name": "Ethereum", 
        "symbol": "ETH"
    },
    "sol": {
        "prefix": "sol-updown-15m-",
        "name": "Solana",
        "symbol": "SOL"
    },
    # Hourly markets (NO FEES - market orders are free)
    # Slug format: bitcoin-up-or-down-{month}-{day}-{hour}{am/pm}-et
    "btc-1h": {
        "prefix": "bitcoin-up-or-down-",
        "name": "Bitcoin Hourly",
        "symbol": "BTC",
        "type": "hourly"  # Flag to use different slug generation
    },
    "eth-1h": {
        "prefix": "ethereum-up-or-down-",
        "name": "Ethereum Hourly",
        "symbol": "ETH",
        "type": "hourly"
    }
}

# Bot operation modes
BOT_MODES = {
    "monitor": "Only collect price data, no trading",
    "trade": "Collect data AND place orders on new markets"
}

# Default market (can be overridden via CLI or env var)
DEFAULT_MARKET = os.getenv("BOT_MARKET", "btc")
DEFAULT_MODE = os.getenv("BOT_MODE", "trade")

# Authentication (REQUIRED for trading)
# Set these environment variables in your .env file:
#
# POLYMARKET_PRIVATE_KEY: Your MetaMask wallet private key (signs transactions)
# POLYMARKET_FUNDER_ADDRESS: (Optional) Polymarket browser wallet address with funds
#                            If set, uses this wallet's funds instead of signer's
# SIGNATURE_TYPE: Signature type for authentication
#                 0 = EOA/MetaMask (default)
#                 1 = Email/Magic wallet
#                 2 = Browser wallet proxy (use with funder)

# Polygon Chain ID
CHAIN_ID = 137

# Risk Management
MAX_CONCURRENT_POSITIONS = 2  # Max number of simultaneous positions
ENABLE_STOP_LOSS = False  # Disabled - hold position until market resolution
ENABLE_TAKE_PROFIT = False  # Take profit at 0.99 (False = hold until market resolution)


# ============================================
# DATA COLLECTION
# ============================================
DATA_COLLECTOR_API_URL = os.getenv("DATA_COLLECTOR_API_URL", "http://localhost:8000")

# ============================================
# LOGGING
# ============================================
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_FILE = "logs/trading_bot.log"
