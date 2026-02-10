"""
Backtest Analysis

Analyzes collected data to find:
1. Correlation between Binance trades and Polymarket price movements
2. Latency of Polymarket reaction to large trades
3. Optimal trade size thresholds for signals

Latency compensation:
    - Binance data arrives ~110ms later than Polymarket (default)
    - We adjust by looking at Polymarket state BEFORE the trade timestamp
    - Use --binance-latency to customize

Usage:
    python backtest_analysis.py backtest_20260124_123456.csv
    python backtest_analysis.py backtest_*.csv --threshold 0.5  # 0.5 BTC min trade
    python backtest_analysis.py backtest_*.csv --binance-latency 110  # adjust latency
"""

# Latency offset: Binance arrives this many ms LATER than Polymarket
DEFAULT_BINANCE_LATENCY_MS = 110

import argparse
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Tuple, Optional
import glob


def load_data(file_pattern: str) -> pd.DataFrame:
    """Load and concatenate CSV files matching pattern"""
    files = glob.glob(file_pattern)
    if not files:
        raise FileNotFoundError(f"No files matching: {file_pattern}")
    
    dfs = []
    for f in files:
        df = pd.read_csv(f)
        df['source_file'] = f
        dfs.append(df)
        print(f"Loaded {f}: {len(df)} rows")
    
    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.sort_values('timestamp_ms').reset_index(drop=True)
    
    print(f"\nTotal rows: {len(combined)}")
    return combined


def analyze_trades(df: pd.DataFrame, min_qty_btc: float = 0.1) -> pd.DataFrame:
    """
    Filter and analyze significant Binance trades.
    
    Args:
        df: Raw data
        min_qty_btc: Minimum trade size in BTC to consider "significant"
    
    Returns:
        DataFrame with significant trades and their impact
    """
    # Filter rows with Binance trades
    trades = df[df['binance_trade_id'].notna()].copy()
    
    # Filter by size
    trades = trades[trades['binance_qty'] >= min_qty_btc].copy()
    
    # Add direction (buy = taker bought, sell = taker sold)
    # is_buyer_maker = True means seller was taker (market sell)
    # is_buyer_maker = False means buyer was taker (market buy)
    trades['direction'] = trades['binance_is_buyer_maker'].apply(
        lambda x: 'SELL' if x else 'BUY'
    )
    
    print(f"\nSignificant trades (>= {min_qty_btc} BTC): {len(trades)}")
    print(f"  BUY trades: {len(trades[trades['direction'] == 'BUY'])}")
    print(f"  SELL trades: {len(trades[trades['direction'] == 'SELL'])}")
    
    return trades


def find_price_reaction(
    df: pd.DataFrame,
    trade_timestamp_ms: int,
    direction: str,
    window_ms: int = 5000,
    binance_latency_ms: int = DEFAULT_BINANCE_LATENCY_MS
) -> dict:
    """
    Find Polymarket price reaction after a Binance trade.
    
    Latency compensation:
        - Binance data arrives ~binance_latency_ms LATER than Polymarket
        - So when we record a Binance trade at timestamp T, Polymarket already had
          binance_latency_ms to react
        - We look at Polymarket state at T - binance_latency_ms as the "initial" state
        - This lets us measure true reaction time from the real Binance event
    
    Args:
        df: Full dataset
        trade_timestamp_ms: Timestamp when we recorded the Binance trade
        direction: 'BUY' or 'SELL'
        window_ms: How long to look for reaction (ms)
        binance_latency_ms: How much later Binance data arrives vs Polymarket
    
    Returns:
        Dict with reaction metrics
    """
    # Adjust for latency: look at Polymarket state BEFORE trade arrived to us
    # This is approximately when the trade actually happened on Binance
    real_trade_time_ms = trade_timestamp_ms - binance_latency_ms
    
    # Get initial state (at the time trade actually happened)
    initial_mask = (
        (df['timestamp_ms'] >= real_trade_time_ms - 50) &
        (df['timestamp_ms'] <= real_trade_time_ms + 50)
    )
    initial_data = df[initial_mask]
    
    if len(initial_data) == 0:
        # Fallback: use trade timestamp if no earlier data
        initial_mask = df['timestamp_ms'] == trade_timestamp_ms
        initial_data = df[initial_mask]
        if len(initial_data) == 0:
            return None
    
    initial = initial_data.iloc[0]
    
    result = {
        'trade_time_ms': trade_timestamp_ms,
        'real_trade_time_ms': real_trade_time_ms,
        'direction': direction,
        'latency_offset_ms': binance_latency_ms,
        'initial_up_mid': initial['poly_mid_up'],
        'initial_down_mid': initial['poly_mid_down'],
        'initial_up_ask': initial['poly_up_ask'],
        'initial_down_ask': initial['poly_down_ask'],
    }
    
    # Track changes over time windows (measured from REAL trade time)
    for delay_ms in [100, 200, 500, 1000, 2000, 5000]:
        target_time = real_trade_time_ms + delay_ms
        delay_mask = (
            (df['timestamp_ms'] >= target_time - 50) &
            (df['timestamp_ms'] <= target_time + 50)
        )
        delay_data = df[delay_mask]
        
        if len(delay_data) > 0:
            point = delay_data.iloc[0]
            
            # Calculate changes from initial state
            if initial['poly_mid_up'] and point['poly_mid_up']:
                result[f'up_change_{delay_ms}ms'] = round(
                    point['poly_mid_up'] - initial['poly_mid_up'], 4
                )
            else:
                result[f'up_change_{delay_ms}ms'] = None
            
            if initial['poly_mid_down'] and point['poly_mid_down']:
                result[f'down_change_{delay_ms}ms'] = round(
                    point['poly_mid_down'] - initial['poly_mid_down'], 4
                )
            else:
                result[f'down_change_{delay_ms}ms'] = None
    
    return result


def analyze_reactions(
    df: pd.DataFrame, 
    trades: pd.DataFrame,
    binance_latency_ms: int = DEFAULT_BINANCE_LATENCY_MS
) -> pd.DataFrame:
    """Analyze price reactions for all significant trades"""
    reactions = []
    
    for _, trade in trades.iterrows():
        reaction = find_price_reaction(
            df,
            trade['timestamp_ms'],
            trade['direction'],
            binance_latency_ms=binance_latency_ms
        )
        if reaction:
            reaction['binance_qty'] = trade['binance_qty']
            reaction['binance_price'] = trade['binance_price']
            reactions.append(reaction)
    
    if not reactions:
        print("No reactions found!")
        return pd.DataFrame()
    
    return pd.DataFrame(reactions)


def print_summary(reactions: pd.DataFrame):
    """Print analysis summary"""
    if reactions.empty:
        print("No data to analyze")
        return
    
    latency = reactions['latency_offset_ms'].iloc[0] if 'latency_offset_ms' in reactions.columns else 0
    
    print("\n" + "=" * 70)
    print("REACTION ANALYSIS SUMMARY")
    print(f"(Binance latency offset: {latency}ms - delays are measured from REAL trade time)")
    print("=" * 70)
    
    # Separate by direction
    buys = reactions[reactions['direction'] == 'BUY']
    sells = reactions[reactions['direction'] == 'SELL']
    
    print(f"\n{'Metric':<30} {'BUY trades':<20} {'SELL trades':<20}")
    print("-" * 70)
    print(f"{'Count':<30} {len(buys):<20} {len(sells):<20}")
    
    # Average reactions at different delays
    for delay in [100, 200, 500, 1000, 2000]:
        up_col = f'up_change_{delay}ms'
        down_col = f'down_change_{delay}ms'
        
        if up_col in reactions.columns:
            buy_up = buys[up_col].mean() if len(buys) > 0 else 0
            sell_up = sells[up_col].mean() if len(sells) > 0 else 0
            
            buy_down = buys[down_col].mean() if len(buys) > 0 else 0
            sell_down = sells[down_col].mean() if len(sells) > 0 else 0
            
            print(f"\n{f'UP token @ {delay}ms':<30} {buy_up:+.4f}             {sell_up:+.4f}")
            print(f"{f'DOWN token @ {delay}ms':<30} {buy_down:+.4f}             {sell_down:+.4f}")
    
    print("\n" + "=" * 70)
    print("INTERPRETATION:")
    print("=" * 70)
    print("""
    If Binance BUY -> UP price increases & DOWN price decreases = CORRECT
    If Binance SELL -> UP price decreases & DOWN price increases = CORRECT
    
    Positive correlation means Polymarket reacts correctly to Binance.
    The delay where we see the reaction tells us our opportunity window.
    """)
    
    # Calculate correlation score
    print("\nCORRELATION SCORES (higher = better prediction):")
    for delay in [100, 200, 500, 1000]:
        up_col = f'up_change_{delay}ms'
        
        if up_col in reactions.columns and reactions[up_col].notna().sum() > 0:
            # For BUY: UP should increase (positive correlation)
            # For SELL: UP should decrease (negative change, but sell is negative direction)
            correct_buy = (buys[up_col] > 0).sum() if len(buys) > 0 else 0
            correct_sell = (sells[up_col] < 0).sum() if len(sells) > 0 else 0
            
            total = len(buys) + len(sells)
            if total > 0:
                accuracy = (correct_buy + correct_sell) / total * 100
                print(f"  @ {delay}ms: {accuracy:.1f}% directional accuracy")


def analyze_by_size(
    df: pd.DataFrame, 
    thresholds: List[float] = [0.1, 0.5, 1.0, 2.0, 5.0],
    binance_latency_ms: int = DEFAULT_BINANCE_LATENCY_MS
):
    """Analyze reaction strength by trade size"""
    print("\n" + "=" * 70)
    print("ANALYSIS BY TRADE SIZE")
    print(f"(Binance latency offset: {binance_latency_ms}ms)")
    print("=" * 70)
    
    for threshold in thresholds:
        trades = analyze_trades(df, min_qty_btc=threshold)
        if len(trades) < 5:
            print(f"\n{threshold} BTC: Insufficient data ({len(trades)} trades)")
            continue
        
        reactions = analyze_reactions(df, trades, binance_latency_ms=binance_latency_ms)
        if reactions.empty:
            continue
        
        # Quick summary
        buys = reactions[reactions['direction'] == 'BUY']
        sells = reactions[reactions['direction'] == 'SELL']
        
        up_500 = 'up_change_500ms'
        if up_500 in reactions.columns:
            avg_buy_up = buys[up_500].mean() if len(buys) > 0 else 0
            avg_sell_up = sells[up_500].mean() if len(sells) > 0 else 0
            
            print(f"\n{threshold} BTC threshold ({len(trades)} trades):")
            print(f"  BUY  -> UP change @ 500ms: {avg_buy_up:+.4f}")
            print(f"  SELL -> UP change @ 500ms: {avg_sell_up:+.4f}")
            
            # Directional accuracy
            correct = ((buys[up_500] > 0).sum() + (sells[up_500] < 0).sum())
            total = len(buys) + len(sells)
            if total > 0:
                print(f"  Directional accuracy: {correct/total*100:.1f}%")


def main():
    parser = argparse.ArgumentParser(description="Backtest Analysis")
    parser.add_argument(
        "file",
        type=str,
        help="CSV file or glob pattern (e.g., backtest_*.csv)"
    )
    parser.add_argument(
        "--threshold", "-t",
        type=float,
        default=0.1,
        help="Minimum trade size in BTC (default: 0.1)"
    )
    parser.add_argument(
        "--by-size",
        action="store_true",
        help="Analyze by different trade size thresholds"
    )
    parser.add_argument(
        "--binance-latency", "-l",
        type=int,
        default=DEFAULT_BINANCE_LATENCY_MS,
        help=f"Binance latency offset in ms (default: {DEFAULT_BINANCE_LATENCY_MS}ms). "
             "This is how much LATER Binance data arrives vs Polymarket."
    )
    
    args = parser.parse_args()
    
    # Load data
    print("Loading data...")
    df = load_data(args.file)
    
    # Basic stats
    print("\nData summary:")
    print(f"  Time range: {df['timestamp_iso'].iloc[0]} to {df['timestamp_iso'].iloc[-1]}")
    print(f"  Total Binance trades: {df['binance_trade_id'].notna().sum()}")
    print(f"  Total orderbook polls: {df['poly_up_bid'].notna().sum()}")
    print(f"  Binance latency offset: {args.binance_latency}ms")
    
    if df['binance_qty'].notna().sum() > 0:
        print(f"  Trade size range: {df['binance_qty'].min():.4f} - {df['binance_qty'].max():.4f} BTC")
    
    # Analyze
    if args.by_size:
        analyze_by_size(df, binance_latency_ms=args.binance_latency)
    else:
        trades = analyze_trades(df, min_qty_btc=args.threshold)
        
        if len(trades) == 0:
            print("No trades above threshold. Try lowering --threshold")
            return
        
        print("\nAnalyzing price reactions...")
        print(f"(Using Binance latency offset: {args.binance_latency}ms)")
        reactions = analyze_reactions(df, trades, binance_latency_ms=args.binance_latency)
        
        print_summary(reactions)
        
        # Save reactions to CSV
        output_file = args.file.replace('.csv', '_reactions.csv')
        if '*' in output_file:
            output_file = 'reactions_analysis.csv'
        reactions.to_csv(output_file, index=False)
        print(f"\nReactions saved to: {output_file}")


if __name__ == "__main__":
    main()
