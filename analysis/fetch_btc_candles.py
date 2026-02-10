"""
Fetch BTCUSDT 15m candles for the last 30 days from Binance API.
Saves to CSV.

Note: 30 days * 96 candles/day = 2880 candles.
Binance limit is 1000 per request, so we paginate.

Usage:
    python fetch_btc_candles.py
"""

import requests
import csv
from datetime import datetime, timedelta, timezone

SYMBOL = "BTCUSDT"
INTERVAL = "15m"
DAYS = 33
OUTPUT_FILE = "btcusdt_15m_30d.csv"

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
LIMIT_PER_REQUEST = 1000


def fetch_candles():
    """Fetch 15m candles from Binance public API (no auth required).

    Paginates automatically since 30 days of 15m candles (~2880) exceeds
    the 1000-per-request limit.
    """
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=DAYS)).timestamp() * 1000)

    all_raw = []
    current_start = start_ms

    print(f"Fetching {SYMBOL} {INTERVAL} candles for last {DAYS} days (paginating)...")

    while current_start < end_ms:
        params = {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": LIMIT_PER_REQUEST,
        }

        resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        all_raw.extend(batch)
        print(f"  Fetched {len(batch)} candles (total so far: {len(all_raw)})")

        # Move start to after the last candle's close time
        last_close_ms = batch[-1][6]  # close_time field
        current_start = last_close_ms + 1

        if len(batch) < LIMIT_PER_REQUEST:
            break  # No more data

    print(f"Total: {len(all_raw)} candles received")

    # Binance kline format: [open_time, open, high, low, close, volume, close_time, ...]
    rows = []
    for k in all_raw:
        rows.append({
            "open_time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "quote_volume": float(k[7]),
            "trades": int(k[8]),
        })

    return rows


def save_csv(rows):
    """Write rows to CSV."""
    if not rows:
        print("No data to save.")
        return

    fieldnames = list(rows[0].keys())
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} candles to {OUTPUT_FILE}")
    print(f"  Period: {rows[0]['open_time']} -> {rows[-1]['close_time']}")


if __name__ == "__main__":
    candles = fetch_candles()
    save_csv(candles)
