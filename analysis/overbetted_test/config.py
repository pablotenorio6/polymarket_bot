"""
Configuration constants for Polymarket BTC analysis
"""

# API Endpoints
GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"

# Analysis thresholds (price levels to check for collapses)
COLLAPSE_THRESHOLDS = [0.95, 0.96, 0.97, 0.98, 0.99]

# API settings
REQUESTS_TIMEOUT = 15
RATE_LIMIT_DELAY = 0.1  # seconds between API calls
BATCH_SIZE = 100
MAX_BATCHES = 10  # 200 batches * 100 = 20000 markets max

# Market filters
MIN_MARKET_DURATION = 15  # minutes (only 15-min markets have order books)

