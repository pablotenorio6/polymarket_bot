"""
Configuration for Polymarket Trading Bot
"""

# API Endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Trading Parameters
TRIGGER_PRICE = 0.96  # When a side reaches this price, we buy that side
ORDER_PRICE = 0.97    # Price we're willing to pay (fill or kill)
STOP_LOSS_PRICE = 0.8  # Emergency stop loss if price collapses (optional - disable if holding to resolution)

# Position sizing
MAX_POSITION_SIZE = 5  # Maximum USD to risk per trade

# Monitoring
POLL_INTERVAL = 0.1  # Seconds between price checks
REQUEST_TIMEOUT = 5  # HTTP request timeout

# Authentication (REQUIRED for trading)
# You need to set these environment variables:
# - POLYMARKET_PRIVATE_KEY: Your wallet private key
# - POLYMARKET_API_KEY: Your Polymarket API key (if required)
# - POLYMARKET_API_SECRET: Your Polymarket API secret (if required)

# Risk Management
MAX_CONCURRENT_POSITIONS = 2  # Max number of simultaneous positions
ENABLE_STOP_LOSS = True  # Enable automatic stop loss
ENABLE_TAKE_PROFIT = False  # Take profit at 0.99 (False = hold until market resolution)

# Logging
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_FILE = "trading_bot.log"
