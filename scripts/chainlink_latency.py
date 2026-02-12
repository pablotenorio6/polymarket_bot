"""
Measure latency between Chainlink oracle timestamp and local receive time.

Supports two sources:
  - chainlink: Direct Chainlink Data Streams WebSocket (requires CHAINLINK_API_KEY/PASSWORD)
  - rtds:      Polymarket RTDS relay (no credentials needed)

Auto-detects source based on env vars. Override with --source.

Usage:
    python scripts/chainlink_latency.py
    python scripts/chainlink_latency.py --source rtds
    python scripts/chainlink_latency.py --source chainlink
    python scripts/chainlink_latency.py --symbol ETH
"""

import argparse
import asyncio
import json
import os
import sys
import time

import websockets
from dotenv import load_dotenv

# Add project root to path so we can import chainlink_ds
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from chainlink_ds import (
    FEED_IDS,
    WS_BASE,
    WS_PATH,
    _build_auth_headers,
    _decode_report,
)

RTDS_URL = "wss://ws-live-data.polymarket.com"


async def measure_rtds(symbol: str):
    """Measure latency via Polymarket RTDS relay."""
    print(f"[RTDS] Connecting to {RTDS_URL}...")
    async with websockets.connect(RTDS_URL) as ws:
        sub = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "*"}
            ],
        }
        await ws.send(json.dumps(sub))
        print(f"[RTDS] Subscribed. Waiting for {symbol} updates...\n")
        _print_header()

        deltas = []
        while True:
            try:
                raw = await ws.recv()
                receive_ms = int(time.time() * 1000)

                if not isinstance(raw, str) or not raw.strip():
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("topic") != "crypto_prices_chainlink":
                    continue

                payload = msg.get("payload", {})
                sym = (payload.get("symbol") or "").split("/")[0].upper()
                if sym != symbol:
                    continue

                chainlink_ms = payload.get("timestamp")
                price = payload.get("value")
                if chainlink_ms is None or price is None:
                    continue

                delta = receive_ms - chainlink_ms
                deltas.append(delta)
                _print_row(chainlink_ms, receive_ms, delta, price, deltas)

            except websockets.ConnectionClosed:
                print("Connection closed")
                break
            except KeyboardInterrupt:
                break

    _print_final(deltas)


async def measure_chainlink_ds(symbol: str):
    """Measure latency via direct Chainlink Data Streams WebSocket."""
    user_id = os.environ.get("CHAINLINK_USERNAME", "")
    cf_client_id = os.environ.get("CHAINLINK_API_KEY", "")
    secret = os.environ.get("CHAINLINK_PASSWORD", "")
    if not user_id or not cf_client_id or not secret:
        print("ERROR: CHAINLINK_USERNAME, CHAINLINK_API_KEY, and CHAINLINK_PASSWORD required")
        return

    feed_csv = ",".join(FEED_IDS.values())
    path = f"{WS_PATH}?feedIDs={feed_csv}"
    url = f"{WS_BASE}{path}"
    headers = _build_auth_headers(user_id, secret, cf_client_id, "GET", path)

    print(f"[ChainlinkDS] Connecting to {WS_BASE}...")
    async with websockets.connect(url, additional_headers=headers) as ws:
        print(f"[ChainlinkDS] Connected. Waiting for {symbol} updates...\n")
        _print_header()

        deltas = []
        while True:
            try:
                raw = await ws.recv()
                receive_ms = int(time.time() * 1000)

                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue

                report = data.get("report")
                if not report:
                    continue
                full_report_hex = report.get("fullReport")
                if not full_report_hex:
                    continue

                result = _decode_report(full_report_hex)
                if result is None:
                    continue

                sym, price, chainlink_ms = result
                if sym != symbol:
                    continue

                delta = receive_ms - chainlink_ms
                deltas.append(delta)
                _print_row(chainlink_ms, receive_ms, delta, price, deltas)

            except websockets.ConnectionClosed:
                print("Connection closed")
                break
            except KeyboardInterrupt:
                break

    _print_final(deltas)


def _fmt_ts(ms: int) -> str:
    """Format epoch ms as HH:MM:SS.mmm"""
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%H:%M:%S") + f".{ms % 1000:03d}"


def _print_header():
    print(f"{'chainlink_ts':>16}  {'receive_ts':>16}  {'delta_ms':>10}  {'price':>14}")
    print("-" * 65)


def _print_row(chainlink_ms, receive_ms, delta, price, deltas):
    print(f"{_fmt_ts(chainlink_ms):>16}  {_fmt_ts(receive_ms):>16}  {delta:>8} ms  ${price:>12,.2f}")
    if len(deltas) % 20 == 0:
        avg = sum(deltas) / len(deltas)
        mn, mx = min(deltas), max(deltas)
        print(f"\n  [{len(deltas)} samples] avg={avg:.0f}ms  min={mn}ms  max={mx}ms\n")


def _print_final(deltas):
    if deltas:
        avg = sum(deltas) / len(deltas)
        print(f"\nFinal: {len(deltas)} samples, avg={avg:.0f}ms, min={min(deltas)}ms, max={max(deltas)}ms")


def main():
    parser = argparse.ArgumentParser(description="Chainlink latency measurement")
    parser.add_argument(
        "--source",
        choices=["chainlink", "rtds"],
        default=None,
        help="Price source (auto-detects from env if omitted)",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="BTC",
        help="Symbol to track (BTC, ETH, SOL)",
    )
    args = parser.parse_args()

    symbol = args.symbol.upper()
    source = args.source
    if source is None:
        source = "chainlink" if os.environ.get("CHAINLINK_USERNAME") else "rtds"

    print(f"Source: {source} | Symbol: {symbol}\n")

    if source == "chainlink":
        asyncio.run(measure_chainlink_ds(symbol))
    else:
        asyncio.run(measure_rtds(symbol))


if __name__ == "__main__":
    main()
