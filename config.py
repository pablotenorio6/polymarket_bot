"""
Configuration for Polymarket Trading Bot
"""

# API Endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Trading Parameters
TRIGGER_PRICE = 0.96  # When a side reaches this price, we buy that side
ENTRY_PRICE = 0.97    # Price we're willing to pay (fill or kill)
ORDER_PRICE = 0.97    # Alias for ENTRY_PRICE (backwards compatibility)
STOP_LOSS_PRICE = 0.8  # Emergency stop loss if price collapses
MAX_ATTEMPTS_PER_MARKET = 3

# Position sizing
MAX_POSITION_SIZE = 5  # Maximum USD to risk per trade

# ============================================
# MONITORING & PERFORMANCE
# ============================================
POLL_INTERVAL = 0.05  # Seconds between price checks (100ms)
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
# MULTI-MARKET SUPPORT (Future)
# ============================================
# Crypto markets to monitor (slug prefixes)
# Each market adds ~4 requests per loop cycle
ENABLED_MARKETS = [
    "btc-updown-15m-",  # Bitcoin
    # "eth-updown-15m-",  # Ethereum (uncomment when ready)
    # "sol-updown-15m-",  # Solana (uncomment when ready)
]

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
ENABLE_STOP_LOSS = True  # Enable automatic stop loss
ENABLE_TAKE_PROFIT = False  # Take profit at 0.99 (False = hold until market resolution)


# ============================================
# LOGGING
# ============================================
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_FILE = "logs/trading_bot.log"
