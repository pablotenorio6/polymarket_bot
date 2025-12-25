"""
API functions for interacting with Polymarket
"""

import requests
import json
import re
import time
from config import GAMMA_API, CLOB_API, REQUESTS_TIMEOUT, RATE_LIMIT_DELAY, BATCH_SIZE, MAX_BATCHES


def get_btc_15min_markets(max_markets=2000, verbose=True):
    """
    Fetch closed Bitcoin Up or Down 15-minute markets.
    Only returns markets with enableOrderBook=True (which have price history).
    
    Args:
        max_markets: Maximum number of markets to fetch
        verbose: Print progress updates
    
    Returns:
        List of market dictionaries
    """
    all_markets = []
    
    if verbose:
        print(f"\nFetching up to {max_markets} Bitcoin 15-minute markets...")
        print("=" * 80)
    
    for batch_num in range(MAX_BATCHES):
        if len(all_markets) >= max_markets:
            break
            
        offset = batch_num * BATCH_SIZE
        params = {
            "closed": "true",
            "limit": BATCH_SIZE,
            "offset": offset,
            "order": "startDate",
            "ascending": "false"
        }
        
        try:
            response = requests.get(GAMMA_API, params=params, timeout=REQUESTS_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            
            if not data:
                if verbose:
                    print(f"  No more markets available at offset {offset}")
                break
            
            # Filter for Bitcoin Up or Down 15-minute markets with order book
            batch_found = 0
            for market in data:
                if len(all_markets) >= max_markets:
                    break
                    
                if is_valid_btc_15min_market(market):
                    all_markets.append(market)
                    batch_found += 1
            
            if verbose:
                print(f"  Batch {batch_num + 1}: Scanned {len(data)} markets, found {batch_found} valid (Total: {len(all_markets)})")
            
            time.sleep(RATE_LIMIT_DELAY)
            
        except Exception as e:
            if verbose:
                print(f"  Error at batch {batch_num + 1}: {e}")
            break
    
    if verbose:
        print(f"\nTotal markets fetched: {len(all_markets)}")
        print("=" * 80)
    
    return all_markets


def is_valid_btc_15min_market(market):
    """
    Check if a market is a valid Bitcoin Up or Down 15-minute market.
    
    Requirements:
    - Question contains "bitcoin", "up", and "down"
    - Has enableOrderBook=True (needed for price history)
    - Is resolved (has "1" in outcomePrices)
    - Duration is 15 minutes
    """
    question = market.get('question', '').lower()
    
    # Check if it's a Bitcoin Up or Down market
    if not ('bitcoin' in question and 'up' in question and 'down' in question):
        return False
    
    # Must have order book enabled (otherwise no price history)
    if not market.get('enableOrderBook'):
        return False
    
    # Check if resolved
    outcome_prices = market.get('outcomePrices', [])
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except:
            return False
    
    if '1' not in outcome_prices:
        return False
    
    # Check duration is 15 minutes
    if not is_15_minute_duration(market.get('question', '')):
        return False
    
    return True


def is_15_minute_duration(question):
    """
    Parse question to check if market duration is 15 minutes.
    E.g., "7:00AM-7:15AM" or "7:15PM-7:30PM"
    """
    time_pattern = r'(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)'
    match = re.search(time_pattern, question)
    
    if not match:
        return False
    
    start_hour, start_min, start_period, end_hour, end_min, end_period = match.groups()
    
    # Convert to minutes for comparison
    start_total = (int(start_hour) % 12) * 60 + int(start_min)
    if start_period == 'PM' and int(start_hour) != 12:
        start_total += 12 * 60
    
    end_total = (int(end_hour) % 12) * 60 + int(end_min)
    if end_period == 'PM' and int(end_hour) != 12:
        end_total += 12 * 60
    
    duration = end_total - start_total
    if duration < 0:
        duration += 24 * 60  # Handle midnight wrap
    
    return duration == 15


def get_price_history(token_id):
    """
    Get price history for a token.
    
    Returns list of price points: [{"t": timestamp, "p": price}, ...]
    """
    url = f"{CLOB_API}/prices-history"
    params = {"market": token_id, "interval": "max", "fidelity": 10}
    
    try:
        resp = requests.get(url, params=params, timeout=REQUESTS_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get('history', []) if isinstance(data, dict) else []
    except:
        return []


def parse_market_data(market):
    """
    Parse a market's JSON string fields and extract key data.
    
    Returns dict with:
    - question: str
    - winner: "Up" or "Down"
    - winner_idx: 0 or 1
    - loser: "Up" or "Down"
    - loser_idx: 0 or 1
    - token_ids: [up_token_id, down_token_id]
    - volume: float
    """
    # Parse outcomes
    outcomes = market.get('outcomes', [])
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    
    # Parse outcome prices
    outcome_prices = market.get('outcomePrices', [])
    if isinstance(outcome_prices, str):
        outcome_prices = json.loads(outcome_prices)
    
    # Parse token IDs
    token_ids = market.get('clobTokenIds', [])
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    
    # Determine winner
    winner_idx = outcome_prices.index('1') if '1' in outcome_prices else -1
    loser_idx = 1 - winner_idx if winner_idx >= 0 else -1
    
    return {
        'question': market.get('question', ''),
        'winner': outcomes[winner_idx] if winner_idx >= 0 and winner_idx < len(outcomes) else 'Unknown',
        'winner_idx': winner_idx,
        'loser': outcomes[loser_idx] if loser_idx >= 0 and loser_idx < len(outcomes) else 'Unknown',
        'loser_idx': loser_idx,
        'token_ids': token_ids,
        'volume': float(market.get('volumeClob', 0) or 0)
    }

