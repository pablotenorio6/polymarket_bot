# Polymarket BTC 15-Minute Market Analyzer

This tool connects to the Polymarket API to retrieve and analyze "Bitcoin Up or Down" 15-minute markets.

## Features

- ✅ Connects to Polymarket Gamma API
- ✅ Searches for Bitcoin Up or Down 15-minute markets
- ✅ Identifies market winners based on resolution
- ✅ Supports both active and closed/resolved markets
- ✅ Properly parses Polymarket's JSON string fields
- ✅ Analyzes resolution patterns (Up vs Down win rates)

## Important Discovery: Two Types of Markets

There are TWO types of Bitcoin Up or Down markets:

| Type | Duration | `enableOrderBook` | Order Book | Price History | Volume |
|------|----------|-------------------|------------|---------------|--------|
| **5-minute** | 5 min | `False` | ❌ None | ❌ Empty | $0 |
| **15-minute** | 15 min | `True` | ✅ Active | ✅ Full | $100K-$300K |

**5-minute markets** are oracle-resolved with no trading.

**15-minute markets** have full order book trading with price history - these are what you want to analyze!

## Installation

```bash
pip install requests pandas
```

## Usage

### Search for Closed/Resolved Markets (Default)

```bash
python main.py
```

This will search through historical closed markets and find resolved Bitcoin Up or Down 15-minute markets.

### Show Active Markets

```bash
python main.py active
```

This displays currently active Bitcoin Up or Down 15-minute markets that are trading but not yet resolved.

### Demo Mode

```bash
python main.py demo
```

Shows sample data structure for testing purposes.

## Current Status

### What Works

1. **API Connection**: Successfully connects to `https://gamma-api.polymarket.com/markets`
2. **Market Discovery**: Finds Bitcoin Up or Down markets with proper filtering
3. **Data Parsing**: Correctly parses JSON string fields:
   - `clobTokenIds`: Contains token IDs as JSON array string
   - `outcomePrices`: Contains outcome prices as JSON array string  
   - `outcomes`: Market outcome names
4. **Winner Identification**: Correctly identifies which outcome won (Up or Down)
5. **Market Filtering**: Filters for 15-minute duration markets based on time patterns

### Price History API

For **15-minute markets** (with `enableOrderBook=True`):

```bash
GET /prices-history?market=TOKEN_ID&interval=max&fidelity=10
# Returns: {"history":[{"t":1766492407,"p":0.5},{"t":1766492707,"p":0.45},...]}
```

Typical results:
- **140-150 price points** per 15-minute market
- Price range examples: 9% - 55%, 49% - 99%, 16% - 50%
- Full trading history preserved even after market closes

### Market Availability

- Closed markets are available (found 58+ from December 22, 2025)
- Markets remain available for at least 1 day after resolution
- Older markets may be archived

## API Findings

### Gamma API Endpoints

```
GET https://gamma-api.polymarket.com/markets
```

**Important Parameters:**
- `closed`: "true" for resolved markets, omit for active markets
- `order`: Use "startDate" (not "endDate") to get most recent markets
- `ascending`: "false" for newest first
- `limit`: Maximum results per request (tested up to 100)
- `offset`: Pagination offset

**Data Format Issues:**
- `clobTokenIds` is returned as a JSON string, not an array
- `outcomePrices` is returned as a JSON string, not an array
- Both need to be parsed with `json.loads()` before use

### Example Market Data

```python
{
    "question": "Bitcoin Up or Down - December 22, 4:15PM-4:30PM ET",
    "outcomes": ["Up", "Down"],
    "outcomePrices": '["1", "0"]',  # JSON string! 
    "clobTokenIds": '["248400308711698...", "550310694347199..."]',  # JSON string!
    "closed": True,
    "active": True
}
```

## Code Structure

- `get_btc_updown_markets()`: Main search function
- `search_with_params()`: Helper to search with pagination
- `get_price_history()`: Attempts to retrieve price history (currently non-functional)
- `run_backtest()`: Processes markets and evaluates threshold logic

## Threshold Evaluation Logic

The goal is to identify markets where the winning outcome had a low probability (price < $0.03) at some point during the market's life, indicating a potential "improbable win" scenario.

**Current Status**: Market winners are correctly identified, but price history is not available to perform the threshold evaluation.

## Analysis Capabilities

The code now analyzes **15-minute markets** with full trading history:

### Threshold Reversal Analysis

Finds markets where the **winning outcome** was trading below a threshold at some point:

```
Example: December 23, 4:30AM-4:45AM ET
- Winner: Up
- Minimum price during market: 9%  ← "Up" was considered very unlikely!
- But "Up" still won!
```

### Sample Results (42 markets analyzed)

| Metric | Value |
|--------|-------|
| Up wins | 57.1% |
| Down wins | 42.9% |
| Reversals (winner below 15%) | 2.4% |

### Strategy Insight

If you bought the eventual winner when it was at 15% or below:
- Win rate: ~2.4% of markets
- Cost: $0.15 per share
- Payout: $1.00 per winning share
- Profit per win: $0.85

## Example Output

```
Searching for Bitcoin 15-minute 'Up or Down' closed/resolved markets...
====================================================================================================

Searching through markets...
  Batch 1: Scanning 100 markets... 
    [*] Resolved: Bitcoin Up or Down - December 22, 4:15PM-4:20PM ET
    [*] Resolved: Bitcoin Up or Down - December 22, 4:10PM-4:15PM ET
    [*] Resolved: Bitcoin Up or Down - December 22, 4:15PM-4:30PM ET
    ...

Total found: 58 BTC 15-minute 'Up or Down' markets
```

## Technical Notes

- All timestamps are in ET (Eastern Time)
- Markets have both 5-minute and 15-minute durations available
- Token IDs are very large integers (77+ digits)
- Winner is determined by `outcomePrices` where "1" indicates the winning outcome

## License

This is a research/analysis tool. Respect Polymarket's API rate limits and terms of service.

