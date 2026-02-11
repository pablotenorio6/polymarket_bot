"""
Measure latency between Chainlink oracle timestamp and local receive time.

Connects to RTDS WebSocket, prints delta for each BTC price update.
Usage: python scripts/chainlink_latency.py
"""

import asyncio
import json
import time

import websockets

RTDS_URL = "wss://ws-live-data.polymarket.com"
SYMBOL = "BTC"


async def main():
    print(f"Connecting to {RTDS_URL}...")
    async with websockets.connect(RTDS_URL) as ws:
        # Subscribe using RTDS format
        sub = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "*"}
            ]
        }
        await ws.send(json.dumps(sub))
        print(f"Subscribed. Waiting for {SYMBOL} updates...\n")
        print(f"{'chainlink_ts':>16}  {'receive_ts':>16}  {'delta_ms':>10}  {'price':>14}")
        print("-" * 65)

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
                symbol = (payload.get("symbol") or "").split("/")[0].upper()
                if symbol != SYMBOL:
                    continue

                chainlink_ms = payload.get("timestamp")
                price = payload.get("value")
                if chainlink_ms is None or price is None:
                    continue

                delta = receive_ms - chainlink_ms
                deltas.append(delta)

                print(
                    f"{chainlink_ms:>16}  {receive_ms:>16}  {delta:>8} ms  ${price:>12,.2f}"
                )

                if len(deltas) % 20 == 0:
                    avg = sum(deltas) / len(deltas)
                    mn = min(deltas)
                    mx = max(deltas)
                    print(f"\n  [{len(deltas)} samples] avg={avg:.0f}ms  min={mn}ms  max={mx}ms\n")

            except websockets.ConnectionClosed:
                print("Connection closed")
                break
            except KeyboardInterrupt:
                break

    if deltas:
        avg = sum(deltas) / len(deltas)
        print(f"\nFinal: {len(deltas)} samples, avg={avg:.0f}ms, min={min(deltas)}ms, max={max(deltas)}ms")


if __name__ == "__main__":
    asyncio.run(main())
