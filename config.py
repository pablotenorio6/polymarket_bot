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

# Logging
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_FILE = "logs/trading_bot.log"
