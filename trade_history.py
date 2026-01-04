"""
Script to get trade history from Polymarket wallet

Shows all BTC buy and sell orders from your wallet.
"""

import os
import sys
import logging
from datetime import datetime
from typing import List, Dict

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Reduce noise
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('py_clob_client').setLevel(logging.WARNING)

from auth import get_auth


def get_all_trades() -> List[Dict]:
    """Get all trades from Polymarket wallet"""
    auth = get_auth()
    client = auth.get_client()
    
    if not client:
        logger.error("Could not initialize client")
        return []
    
    try:
        trades = client.get_trades()
        return trades if trades else []
    except Exception as e:
        logger.error(f"Error getting trades: {e}")
        return []


def filter_btc_trades(trades: List[Dict]) -> List[Dict]:
    """
    Filter to only BTC Up/Down trades
    
    BTC trades have outcome 'Up' or 'Down'
    Other markets have 'Yes' or 'No'
    """
    return [t for t in trades if t.get('outcome') in ('Up', 'Down')]


def format_timestamp(ts) -> str:
    """Format Unix timestamp to readable date"""
    try:
        if isinstance(ts, str):
            ts = int(ts)
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except:
        return str(ts)


def calculate_totals(trades: List[Dict]) -> Dict:
    """Calculate summary statistics"""
    total_buys = 0.0
    total_sells = 0.0
    total_buy_volume = 0.0
    total_sell_volume = 0.0
    buy_count = 0
    sell_count = 0
    
    for trade in trades:
        side = trade.get('side', '')
        price = float(trade.get('price', 0))
        size = float(trade.get('size', 0))
        total = price * size
        
        if side == 'BUY':
            total_buys += total
            total_buy_volume += size
            buy_count += 1
        elif side == 'SELL':
            total_sells += total
            total_sell_volume += size
            sell_count += 1
    
    return {
        'buy_count': buy_count,
        'sell_count': sell_count,
        'total_buys_usd': total_buys,
        'total_sells_usd': total_sells,
        'total_buy_volume': total_buy_volume,
        'total_sell_volume': total_sell_volume,
        'net_pnl': total_sells - total_buys
    }


def main():
    """Main function to display trade history"""
    print("=" * 100)
    print("POLYMARKET BTC TRADE HISTORY")
    print("=" * 100)
    
    # Get wallet address
    auth = get_auth()
    client = auth.get_client()
    
    if not client:
        print("ERROR: Could not connect to Polymarket")
        return
    
    wallet = client.get_address()
    funder = auth.funder_address
    
    print(f"\nWallet (Signer): {wallet}")
    if funder:
        print(f"Wallet (Funder): {funder}")
    
    # Get all trades and filter to BTC only
    print("\nFetching trades...")
    all_trades = get_all_trades()
    
    if not all_trades:
        print("\nNo trades found")
        return
    
    # Filter to BTC trades only (Up/Down outcomes)
    trades = filter_btc_trades(all_trades)
    print(f"Found {len(trades)} BTC trades (filtered from {len(all_trades)} total)")
    
    # Separate BUY and SELL
    buys = [t for t in trades if t.get('side') == 'BUY']
    sells = [t for t in trades if t.get('side') == 'SELL']
    
    # ==================== BUY ORDERS ====================
    print("\n" + "=" * 100)
    print(f"BUY ORDERS ({len(buys)} trades)")
    print("=" * 100)
    print(f"\n{'DATE & TIME':<22} {'OUTCOME':<8} {'SIZE':>10} {'PRICE':>10} {'TOTAL USD':>12} {'STATUS':<12}")
    print("-" * 100)
    
    for trade in buys:
        timestamp = format_timestamp(trade.get('match_time', 0))
        outcome = trade.get('outcome', 'Unknown')
        size = float(trade.get('size', 0))
        price = float(trade.get('price', 0))
        total = size * price
        status = trade.get('status', 'UNKNOWN')
        
        print(f"{timestamp:<22} {outcome:<8} {size:>10.4f} ${price:>9.4f} ${total:>11.2f} {status:<12}")
    
    # ==================== SELL ORDERS ====================
    print("\n" + "=" * 100)
    print(f"SELL ORDERS ({len(sells)} trades)")
    print("=" * 100)
    
    if sells:
        print(f"\n{'DATE & TIME':<22} {'OUTCOME':<8} {'SIZE':>10} {'PRICE':>10} {'TOTAL USD':>12} {'STATUS':<12}")
        print("-" * 100)
        
        for trade in sells:
            timestamp = format_timestamp(trade.get('match_time', 0))
            outcome = trade.get('outcome', 'Unknown')
            size = float(trade.get('size', 0))
            price = float(trade.get('price', 0))
            total = size * price
            status = trade.get('status', 'UNKNOWN')
            
            print(f"{timestamp:<22} {outcome:<8} {size:>10.4f} ${price:>9.4f} ${total:>11.2f} {status:<12}")
    else:
        print("\n  (No sell orders found)")
    
    # ==================== SUMMARY ====================
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    
    stats = calculate_totals(trades)
    
    print(f"\n  Total BUY orders:    {stats['buy_count']:>6}  |  Volume: {stats['total_buy_volume']:>10.2f} shares  |  ${stats['total_buys_usd']:>10.2f} USD")
    print(f"  Total SELL orders:   {stats['sell_count']:>6}  |  Volume: {stats['total_sell_volume']:>10.2f} shares  |  ${stats['total_sells_usd']:>10.2f} USD")
    print("-" * 100)
    
    # Group by outcome (Up/Down)
    up_trades = [t for t in trades if t.get('outcome') == 'Up']
    down_trades = [t for t in trades if t.get('outcome') == 'Down']
    
    up_stats = calculate_totals(up_trades)
    down_stats = calculate_totals(down_trades)
    
    print(f"\n  UP positions:   {len(up_trades):>4} trades  |  Bought: ${up_stats['total_buys_usd']:>8.2f}  |  Sold: ${up_stats['total_sells_usd']:>8.2f}")
    print(f"  DOWN positions: {len(down_trades):>4} trades  |  Bought: ${down_stats['total_buys_usd']:>8.2f}  |  Sold: ${down_stats['total_sells_usd']:>8.2f}")
    
    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
