#!/usr/bin/env python3
"""
Polymarket BTC 15-Minute Market Collapse Analyzer

Analyzes historical Bitcoin Up or Down markets to find cases where
a side reached a high probability (95%+) but then collapsed and lost.

Usage:
    python main.py                    # Run full analysis (2000+ markets)
    python main.py --quick            # Quick test (100 markets)
    python main.py --markets 500      # Custom market count
    python main.py --export           # Export results to CSV
"""

import argparse
import sys
from api import get_btc_15min_markets
from analysis import analyze_collapses, print_analysis_results, export_results_csv
from config import COLLAPSE_THRESHOLDS


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description='Analyze Polymarket BTC 15-minute markets for threshold collapses'
    )
    parser.add_argument(
        '--markets', '-m',
        type=int,
        default=2000,
        help='Maximum number of markets to analyze (default: 2000)'
    )
    parser.add_argument(
        '--quick', '-q',
        action='store_true',
        help='Quick test mode (100 markets)'
    )
    parser.add_argument(
        '--thresholds', '-t',
        type=str,
        default=None,
        help='Comma-separated thresholds (e.g., "0.95,0.97,0.99")'
    )
    parser.add_argument(
        '--export', '-e',
        action='store_true',
        help='Export results to CSV file'
    )
    parser.add_argument(
        '--no-details',
        action='store_true',
        help='Skip detailed collapse examples in output'
    )
    
    args = parser.parse_args()
    
    # Set market count
    max_markets = 100 if args.quick else args.markets
    
    # Parse thresholds
    if args.thresholds:
        thresholds = [float(t) for t in args.thresholds.split(',')]
    else:
        thresholds = COLLAPSE_THRESHOLDS
    
    # Print header
    print("=" * 80)
    print("POLYMARKET BTC 15-MINUTE COLLAPSE ANALYZER")
    print("=" * 80)
    print(f"\nConfiguration:")
    print(f"  Target markets: {max_markets}")
    print(f"  Thresholds: {[f'{t:.0%}' for t in thresholds]}")
    
    # Fetch markets
    markets = get_btc_15min_markets(max_markets=max_markets, verbose=True)
    
    if not markets:
        print("\nNo markets found. Exiting.")
        return 1
    
    # Run analysis
    results = analyze_collapses(markets, thresholds=thresholds, verbose=True)
    
    # Print results
    print_analysis_results(results, detailed=not args.no_details)
    
    # Export if requested
    if args.export:
        export_results_csv(results)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
