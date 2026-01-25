"""
Strategy system for Polymarket trading bot

Strategies define HOW to trade, while the bot provides
the infrastructure (trader, monitors, data collection).

Available strategies:
- early_queue: Place GTC orders on market creation for FIFO priority
- expected_value: Trade based on historical probability edge with Kelly sizing
- whale_copy: Copy trades from suspected market manipulators
- whale_frontrun: Frontrun large Binance orders on Polymarket
"""

from strategies.base import BaseStrategy
from strategies.early_queue import EarlyQueueStrategy
from strategies.expected_value import ExpectedValueStrategy
from strategies.whale_copy import WhaleCopyStrategy
from strategies.whale_frontrun import WhaleFrontrunStrategy

# Registry of available strategies
STRATEGIES = {
    "early_queue": EarlyQueueStrategy,
    "expected_value": ExpectedValueStrategy,
    "whale_copy": WhaleCopyStrategy,
    "whale_frontrun": WhaleFrontrunStrategy,
}

def get_strategy(name: str) -> type:
    """Get strategy class by name"""
    if name not in STRATEGIES:
        available = ", ".join(STRATEGIES.keys())
        raise ValueError(f"Unknown strategy '{name}'. Available: {available}")
    return STRATEGIES[name]

__all__ = ["BaseStrategy", "EarlyQueueStrategy", "ExpectedValueStrategy", "WhaleCopyStrategy", "WhaleFrontrunStrategy", "STRATEGIES", "get_strategy"]
