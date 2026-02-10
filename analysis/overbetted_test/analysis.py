"""
Analysis functions for evaluating market collapses
"""

import time
from collections import defaultdict
from api import get_price_history, parse_market_data
from config import COLLAPSE_THRESHOLDS, RATE_LIMIT_DELAY


def analyze_collapses(markets, thresholds=None, verbose=True):
    """
    Analyze markets for threshold collapses.
    
    A "collapse" occurs when:
    - One side (Up or Down) reaches a high threshold (e.g., 95%)
    - But the OTHER side ends up winning
    
    Args:
        markets: List of market dictionaries
        thresholds: List of thresholds to check (default: [0.95, 0.96, 0.97, 0.98, 0.99])
        verbose: Print progress
    
    Returns:
        dict with analysis results
    """
    if thresholds is None:
        thresholds = COLLAPSE_THRESHOLDS
    
    # Initialize results
    results = {
        'total_markets': len(markets),
        'markets_with_history': 0,
        'up_wins': 0,
        'down_wins': 0,
        'thresholds': {},
        'collapse_examples': []
    }
    
    # Initialize threshold tracking
    for t in thresholds:
        results['thresholds'][t] = {
            'reached': 0,        # Markets where ANY side reached this threshold
            'collapsed': 0,      # Markets where the side that reached threshold LOST
            'collapse_rate': 0.0
        }
    
    if verbose:
        print(f"\nAnalyzing {len(markets)} markets for collapses...")
        print(f"Thresholds: {[f'{t:.0%}' for t in thresholds]}")
        print("=" * 80)
    
    # Process each market
    for i, market in enumerate(markets):
        try:
            # Parse market data
            data = parse_market_data(market)
            
            if data['winner_idx'] < 0 or len(data['token_ids']) < 2:
                continue
            
            # Count wins
            if data['winner'] == 'Up':
                results['up_wins'] += 1
            elif data['winner'] == 'Down':
                results['down_wins'] += 1
            
            # Get price history for BOTH outcomes (Up and Down)
            up_history = get_price_history(data['token_ids'][0])
            down_history = get_price_history(data['token_ids'][1])
            
            if not up_history and not down_history:
                continue
            
            results['markets_with_history'] += 1
            
            # Extract prices
            up_prices = [float(h.get('p', 0)) for h in up_history if 'p' in h]
            down_prices = [float(h.get('p', 0)) for h in down_history if 'p' in h]
            
            if not up_prices:
                up_prices = [0.5]  # Default if no history
            if not down_prices:
                down_prices = [0.5]
            
            up_max = max(up_prices)
            down_max = max(down_prices)
            
            # Check each threshold
            for threshold in thresholds:
                up_reached = up_max >= threshold
                down_reached = down_max >= threshold
                
                if up_reached or down_reached:
                    results['thresholds'][threshold]['reached'] += 1
                    
                    # Check for collapse
                    # Collapse = the side that reached threshold LOST
                    collapse = False
                    collapse_side = None
                    collapse_max = 0
                    
                    if up_reached and data['winner'] == 'Down':
                        collapse = True
                        collapse_side = 'Up'
                        collapse_max = up_max
                    elif down_reached and data['winner'] == 'Up':
                        collapse = True
                        collapse_side = 'Down'
                        collapse_max = down_max
                    
                    if collapse:
                        results['thresholds'][threshold]['collapsed'] += 1
                        
                        # Store example (limit to first 10 per threshold)
                        if len([e for e in results['collapse_examples'] if e['threshold'] == threshold]) < 10:
                            results['collapse_examples'].append({
                                'question': data['question'][:60],
                                'threshold': threshold,
                                'collapse_side': collapse_side,
                                'max_price': collapse_max,
                                'winner': data['winner'],
                                'volume': data['volume']
                            })
            
            # Progress update
            if verbose and (i + 1) % 100 == 0:
                print(f"  Processed {i + 1}/{len(markets)} markets...")
            
            time.sleep(RATE_LIMIT_DELAY)
            
        except Exception as e:
            if verbose:
                print(f"  Error processing market {i}: {e}")
            continue
    
    # Calculate collapse rates
    for threshold in thresholds:
        reached = results['thresholds'][threshold]['reached']
        collapsed = results['thresholds'][threshold]['collapsed']
        if reached > 0:
            results['thresholds'][threshold]['collapse_rate'] = collapsed / reached
    
    return results


def print_analysis_results(results, detailed=True):
    """
    Print analysis results in a formatted way.
    """
    print("\n" + "=" * 80)
    print("COLLAPSE ANALYSIS RESULTS")
    print("=" * 80)
    
    print(f"\nMARKET STATISTICS:")
    print(f"  Total markets: {results['total_markets']}")
    print(f"  Markets with price history: {results['markets_with_history']}")
    
    total_wins = results['up_wins'] + results['down_wins']
    if total_wins > 0:
        print(f"  Up wins: {results['up_wins']} ({results['up_wins']/total_wins*100:.1f}%)")
        print(f"  Down wins: {results['down_wins']} ({results['down_wins']/total_wins*100:.1f}%)")
    
    print(f"\nCOLLAPSE ANALYSIS:")
    print(f"  A 'collapse' = one side reached threshold but LOST")
    print()
    print(f"  {'Threshold':<12} | {'Reached':<10} | {'Collapsed':<10} | {'Collapse Rate':<15} | {'Strategy Edge'}")
    print(f"  {'-'*12}-+-{'-'*10}-+-{'-'*10}-+-{'-'*15}-+-{'-'*20}")
    
    for threshold in sorted(results['thresholds'].keys()):
        t_data = results['thresholds'][threshold]
        reached = t_data['reached']
        collapsed = t_data['collapsed']
        rate = t_data['collapse_rate']
        
        # Calculate strategy edge
        # If you bet against when price reaches threshold:
        # - Cost: (1 - threshold) to buy opposite side
        # - Win rate: collapse_rate
        # - Expected value: win_rate * 1 - (1 - win_rate) * cost
        cost = 1 - threshold
        if rate > 0:
            ev = rate * 1 - (1 - rate) * cost
            edge_str = f"EV: ${ev:.3f}" if ev > 0 else f"EV: -${abs(ev):.3f}"
        else:
            edge_str = "N/A"
        
        print(f"  {threshold:>10.0%}   | {reached:<10} | {collapsed:<10} | {rate:>13.2%}   | {edge_str}")
    
    if detailed and results['collapse_examples']:
        print(f"\nCOLLAPSE EXAMPLES:")
        print(f"  (Markets where one side hit threshold but lost)")
        print()
        
        # Group by threshold
        by_threshold = defaultdict(list)
        for ex in results['collapse_examples']:
            by_threshold[ex['threshold']].append(ex)
        
        for threshold in sorted(by_threshold.keys(), reverse=True):
            examples = by_threshold[threshold]
            print(f"  --- {threshold:.0%} Threshold Collapses ---")
            for ex in examples[:5]:
                print(f"    {ex['question']}")
                print(f"      {ex['collapse_side']} reached {ex['max_price']:.1%} but {ex['winner']} won (Vol: ${ex['volume']:,.0f})")
            print()
    
    # Strategy summary
    print("=" * 80)
    print("STRATEGY INSIGHT")
    print("=" * 80)
    print("""
If a side reaches 95%+, there's a chance it still loses (collapses).

Strategy: When you see a price reach 95%+, consider buying the opposite side cheap.
- At 95%: opposite side costs ~$0.05
- At 99%: opposite side costs ~$0.01

The collapse rate tells you how often this "improbable" reversal happens.
A positive EV means the strategy is profitable in expectation.
""")


def export_results_csv(results, filename='collapse_analysis.csv'):
    """
    Export results to CSV file.
    """
    import csv
    
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        
        # Header
        writer.writerow(['Threshold', 'Reached', 'Collapsed', 'Collapse Rate', 'Expected Value'])
        
        # Data
        for threshold in sorted(results['thresholds'].keys()):
            t_data = results['thresholds'][threshold]
            cost = 1 - threshold
            rate = t_data['collapse_rate']
            ev = rate * 1 - (1 - rate) * cost if rate > 0 else 0
            
            writer.writerow([
                f"{threshold:.0%}",
                t_data['reached'],
                t_data['collapsed'],
                f"{rate:.2%}",
                f"${ev:.4f}"
            ])
    
    print(f"\nResults exported to {filename}")

