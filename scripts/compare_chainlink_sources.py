"""
Compare BTC price data between RTDS (Polymarket relay) and direct Chainlink Data Streams.

Connects to both WebSockets simultaneously and logs side-by-side:
  - Price from each source
  - Price delta (should be 0 if same Chainlink report)
  - RTDS timestamp vs Chainlink validFromTimestamp vs observationsTimestamp
  - Latency of each source (oracle ts → local receive)

Goal: determine which Chainlink timestamp RTDS relays, and whether prices match.

Usage:
    python scripts/compare_chainlink_sources.py
    python scripts/compare_chainlink_sources.py --symbol ETH
    python scripts/compare_chainlink_sources.py --duration 300  # 5 minutes
"""

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import websockets
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from chainlink_ds import (
    FEED_IDS,
    WS_BASE,
    WS_PATH,
    _OUTER_ABI,
    _V3_ABI,
    _FEED_TO_SYMBOL,
    _build_auth_headers,
)
from eth_abi import decode as abi_decode

RTDS_URL = "wss://ws-live-data.polymarket.com"


@dataclass
class ChainlinkReport:
    """Full decoded Chainlink V3 report with both timestamps."""
    symbol: str
    price: float
    valid_from_ts: int       # uint32 seconds
    observations_ts: int     # uint32 seconds
    bid: float
    ask: float
    receive_ms: int          # local wall clock ms


@dataclass
class RTDSUpdate:
    """RTDS Chainlink price update."""
    symbol: str
    price: float
    payload_ts: int          # ms from payload.timestamp
    envelope_ts: int         # ms from top-level message.timestamp
    receive_ms: int          # local wall clock ms


@dataclass
class MatchedPair:
    """A matched pair of RTDS + Chainlink updates for the same observations_ts."""
    observations_ts: int
    valid_from_ts: int
    chainlink_price: float
    rtds_price: float
    price_delta: float
    chainlink_receive_ms: int
    rtds_receive_ms: int
    rtds_payload_ts: int
    rtds_envelope_ts: int


def decode_full_report(hex_payload: str) -> Optional[ChainlinkReport]:
    """Decode V3 report extracting BOTH timestamps + bid/ask."""
    try:
        raw = bytes.fromhex(hex_payload.removeprefix("0x"))
        outer = abi_decode(_OUTER_ABI, raw)
        report_blob = outer[1]
        v3 = abi_decode(_V3_ABI, report_blob)

        feed_id_hex = v3[0].hex()
        valid_from_ts = v3[1]       # uint32 seconds
        observations_ts = v3[2]     # uint32 seconds
        benchmark_price = v3[6] / 10**18
        bid = v3[7] / 10**18
        ask = v3[8] / 10**18

        symbol = _FEED_TO_SYMBOL.get(feed_id_hex)
        if not symbol:
            return None

        return ChainlinkReport(
            symbol=symbol,
            price=benchmark_price,
            valid_from_ts=valid_from_ts,
            observations_ts=observations_ts,
            bid=bid,
            ask=ask,
            receive_ms=0,  # set by caller
        )
    except Exception as e:
        print(f"  [decode error] {e}")
        return None


def fmt_ts(epoch_s: int) -> str:
    """Format epoch seconds as HH:MM:SS."""
    dt = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
    return dt.strftime("%H:%M:%S")


def fmt_ms(epoch_ms: int) -> str:
    """Format epoch ms as HH:MM:SS.mmm."""
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return dt.strftime("%H:%M:%S") + f".{epoch_ms % 1000:03d}"


async def listen_chainlink(symbol: str, reports: List[ChainlinkReport], stop: asyncio.Event):
    """Connect to direct Chainlink DS and collect reports."""
    user_id = os.environ.get("CHAINLINK_USERNAME", "")
    cf_client_id = os.environ.get("CHAINLINK_API_KEY", "")
    secret = os.environ.get("CHAINLINK_PASSWORD", "")

    if not user_id or not cf_client_id or not secret:
        print("ERROR: CHAINLINK_USERNAME, CHAINLINK_API_KEY, CHAINLINK_PASSWORD required")
        stop.set()
        return

    feed_csv = ",".join(FEED_IDS.values())
    path = f"{WS_PATH}?feedIDs={feed_csv}"
    url = f"{WS_BASE}{path}"
    headers = _build_auth_headers(user_id, secret, cf_client_id, "GET", path)

    print(f"[ChainlinkDS] Connecting to {WS_BASE}...")
    try:
        async with websockets.connect(url, additional_headers=headers, ping_interval=20) as ws:
            print(f"[ChainlinkDS] Connected")
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    receive_ms = int(time.time() * 1000)

                    data = json.loads(raw)
                    report_data = data.get("report")
                    if not report_data:
                        continue
                    full_hex = report_data.get("fullReport")
                    if not full_hex:
                        continue

                    report = decode_full_report(full_hex)
                    if not report or report.symbol != symbol:
                        continue

                    report.receive_ms = receive_ms
                    reports.append(report)

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    print("[ChainlinkDS] Connection closed")
                    break
    except Exception as e:
        print(f"[ChainlinkDS] Error: {e}")


async def listen_rtds(symbol: str, updates: List[RTDSUpdate], stop: asyncio.Event):
    """Connect to RTDS and collect price updates."""
    print(f"[RTDS] Connecting to {RTDS_URL}...")
    try:
        async with websockets.connect(RTDS_URL, ping_interval=20) as ws:
            sub = {
                "action": "subscribe",
                "subscriptions": [
                    {"topic": "crypto_prices_chainlink", "type": "*"}
                ],
            }
            await ws.send(json.dumps(sub))
            print(f"[RTDS] Connected & subscribed")

            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    receive_ms = int(time.time() * 1000)

                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    msg = json.loads(raw)
                    if msg.get("topic") != "crypto_prices_chainlink":
                        continue

                    payload = msg.get("payload", {})
                    sym = (payload.get("symbol") or "").split("/")[0].upper()
                    if sym != symbol:
                        continue

                    price = payload.get("value")
                    payload_ts = payload.get("timestamp")
                    envelope_ts = msg.get("timestamp", 0)
                    if price is None or payload_ts is None:
                        continue

                    updates.append(RTDSUpdate(
                        symbol=sym,
                        price=float(price),
                        payload_ts=int(payload_ts),
                        envelope_ts=int(envelope_ts),
                        receive_ms=receive_ms,
                    ))

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    print("[RTDS] Connection closed")
                    break
    except Exception as e:
        print(f"[RTDS] Error: {e}")


def match_and_analyze(
    reports: List[ChainlinkReport],
    updates: List[RTDSUpdate],
    symbol: str,
    csv_path: Optional[str],
):
    """Match RTDS updates to Chainlink reports and print comparison."""
    print(f"\n{'='*100}")
    print(f"  COMPARISON RESULTS — {symbol}  ({len(reports)} Chainlink reports, {len(updates)} RTDS updates)")
    print(f"{'='*100}\n")

    if not reports or not updates:
        print("Not enough data to compare.")
        return

    # --- 1. Raw data summary ---
    print("--- Chainlink Direct: validFromTimestamp vs observationsTimestamp ---")
    print(f"{'#':>4}  {'validFrom':>12}  {'observations':>12}  {'diff_s':>7}  {'price':>14}  {'bid':>14}  {'ask':>14}  {'latency_ms':>10}")
    print("-" * 100)
    for i, r in enumerate(reports[:50]):
        diff = r.observations_ts - r.valid_from_ts
        lat = r.receive_ms - r.observations_ts * 1000
        print(
            f"{i:>4}  {fmt_ts(r.valid_from_ts):>12}  {fmt_ts(r.observations_ts):>12}  "
            f"{diff:>5}s  ${r.price:>12,.2f}  ${r.bid:>12,.2f}  ${r.ask:>12,.2f}  {lat:>8}ms"
        )
    if len(reports) > 50:
        print(f"  ... ({len(reports) - 50} more)")

    # --- 2. RTDS timestamp analysis ---
    print(f"\n--- RTDS: payload.timestamp analysis ---")
    print(f"{'#':>4}  {'payload_ts':>16}  {'envelope_ts':>16}  {'price':>14}  {'latency_ms':>10}")
    print("-" * 80)
    for i, u in enumerate(updates[:30]):
        lat = u.receive_ms - u.payload_ts
        print(
            f"{i:>4}  {fmt_ms(u.payload_ts):>16}  {fmt_ms(u.envelope_ts):>16}  "
            f"${u.price:>12,.2f}  {lat:>8}ms"
        )
    if len(updates) > 30:
        print(f"  ... ({len(updates) - 30} more)")

    # --- 3. Match by timestamp ---
    # Build index: observations_ts (seconds) -> ChainlinkReport
    cl_by_obs = {}
    for r in reports:
        cl_by_obs[r.observations_ts] = r

    # Also build index by valid_from_ts
    cl_by_valid = {}
    for r in reports:
        cl_by_valid[r.valid_from_ts] = r

    # Try matching RTDS payload_ts to observations_ts*1000 or valid_from_ts*1000
    matches_obs = []
    matches_valid = []
    matches_obs_floor = []   # RTDS ts // 1000 == observations_ts

    for u in updates:
        ts_s = u.payload_ts // 1000  # Convert ms to seconds (floor)

        if ts_s in cl_by_obs:
            matches_obs_floor.append((u, cl_by_obs[ts_s]))

        # Exact ms match against observations_ts * 1000
        if u.payload_ts in {r.observations_ts * 1000 for r in reports}:
            r = cl_by_obs[u.payload_ts // 1000]
            matches_obs.append((u, r))

        # Exact ms match against valid_from_ts * 1000
        if u.payload_ts in {r.valid_from_ts * 1000 for r in reports}:
            r = cl_by_valid[u.payload_ts // 1000]
            matches_valid.append((u, r))

    print(f"\n--- Timestamp Matching ---")
    print(f"  RTDS payload_ts//1000 == observationsTimestamp:  {len(matches_obs_floor)} matches")
    print(f"  RTDS payload_ts == observationsTimestamp*1000:   {len(matches_obs)} matches (exact)")
    print(f"  RTDS payload_ts == validFromTimestamp*1000:      {len(matches_valid)} matches (exact)")

    # Check if RTDS ts has sub-second precision
    rtds_subsecond = sum(1 for u in updates if u.payload_ts % 1000 != 0)
    print(f"  RTDS timestamps with sub-second precision:       {rtds_subsecond}/{len(updates)}")

    # --- 4. Closest-match pairing ---
    print(f"\n--- Price Comparison (closest-timestamp pairs) ---")
    pairs: List[MatchedPair] = []

    for u in updates:
        # Find closest Chainlink report by observations_ts
        best_r = None
        best_diff = float('inf')
        for r in reports:
            diff = abs(u.payload_ts - r.observations_ts * 1000)
            if diff < best_diff:
                best_diff = diff
                best_r = r

        if best_r and best_diff <= 5000:  # Within 5s
            pairs.append(MatchedPair(
                observations_ts=best_r.observations_ts,
                valid_from_ts=best_r.valid_from_ts,
                chainlink_price=best_r.price,
                rtds_price=u.price,
                price_delta=u.price - best_r.price,
                chainlink_receive_ms=best_r.receive_ms,
                rtds_receive_ms=u.receive_ms,
                rtds_payload_ts=u.payload_ts,
                rtds_envelope_ts=u.envelope_ts,
            ))

    if pairs:
        print(f"{'#':>4}  {'obs_ts':>10}  {'valid_ts':>10}  {'CL price':>14}  {'RTDS price':>14}  "
              f"{'price_d':>10}  {'RTDS_ts':>16}  {'ts_match':>12}  {'CL_lat':>8}  {'RTDS_lat':>8}")
        print("-" * 130)

        price_deltas = []
        ts_match_obs = 0
        ts_match_valid = 0
        ts_match_neither = 0

        for i, p in enumerate(pairs[:80]):
            cl_lat = p.chainlink_receive_ms - p.observations_ts * 1000
            rtds_lat = p.rtds_receive_ms - p.observations_ts * 1000

            # Determine which timestamp RTDS is using
            rtds_ts_s = p.rtds_payload_ts // 1000
            if rtds_ts_s == p.observations_ts:
                ts_match = "obs_ts"
                ts_match_obs += 1
            elif rtds_ts_s == p.valid_from_ts:
                ts_match = "valid_ts"
                ts_match_valid += 1
            else:
                ts_match = f"?({p.rtds_payload_ts - p.observations_ts * 1000:+d}ms)"
                ts_match_neither += 1

            price_deltas.append(p.price_delta)

            print(
                f"{i:>4}  {fmt_ts(p.observations_ts):>10}  {fmt_ts(p.valid_from_ts):>10}  "
                f"${p.chainlink_price:>12,.2f}  ${p.rtds_price:>12,.2f}  "
                f"{p.price_delta:>+10.2f}  {fmt_ms(p.rtds_payload_ts):>16}  "
                f"{ts_match:>12}  {cl_lat:>6}ms  {rtds_lat:>6}ms"
            )

        if len(pairs) > 80:
            # Count rest
            for p in pairs[80:]:
                price_deltas.append(p.price_delta)
                rtds_ts_s = p.rtds_payload_ts // 1000
                if rtds_ts_s == p.observations_ts:
                    ts_match_obs += 1
                elif rtds_ts_s == p.valid_from_ts:
                    ts_match_valid += 1
                else:
                    ts_match_neither += 1
            print(f"  ... ({len(pairs) - 80} more)")

        print(f"\n--- Summary ({len(pairs)} matched pairs) ---")
        nonzero_deltas = [d for d in price_deltas if d != 0]
        print(f"  Price deltas == 0:          {len(price_deltas) - len(nonzero_deltas)}/{len(price_deltas)}")
        if nonzero_deltas:
            print(f"  Price deltas != 0:          {len(nonzero_deltas)}")
            print(f"    min={min(nonzero_deltas):+.2f}  max={max(nonzero_deltas):+.2f}  "
                  f"avg={sum(abs(d) for d in nonzero_deltas)/len(nonzero_deltas):.2f}")
        print(f"  RTDS ts matches obs_ts:     {ts_match_obs}")
        print(f"  RTDS ts matches valid_ts:   {ts_match_valid}")
        print(f"  RTDS ts matches neither:    {ts_match_neither}")

        # Latency comparison
        cl_lats = [p.chainlink_receive_ms - p.observations_ts * 1000 for p in pairs]
        rtds_lats = [p.rtds_receive_ms - p.observations_ts * 1000 for p in pairs]
        print(f"\n  Chainlink DS latency:  avg={sum(cl_lats)/len(cl_lats):.0f}ms  "
              f"min={min(cl_lats)}ms  max={max(cl_lats)}ms")
        print(f"  RTDS relay latency:    avg={sum(rtds_lats)/len(rtds_lats):.0f}ms  "
              f"min={min(rtds_lats)}ms  max={max(rtds_lats)}ms")

    # --- 5. Export CSV ---
    if csv_path and pairs:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "observations_ts", "valid_from_ts", "chainlink_price", "rtds_price",
                "price_delta", "rtds_payload_ts_ms", "rtds_envelope_ts_ms",
                "chainlink_receive_ms", "rtds_receive_ms",
                "chainlink_latency_ms", "rtds_latency_ms",
            ])
            for p in pairs:
                w.writerow([
                    p.observations_ts, p.valid_from_ts, f"{p.chainlink_price:.8f}",
                    f"{p.rtds_price:.2f}", f"{p.price_delta:.8f}",
                    p.rtds_payload_ts, p.rtds_envelope_ts,
                    p.chainlink_receive_ms, p.rtds_receive_ms,
                    p.chainlink_receive_ms - p.observations_ts * 1000,
                    p.rtds_receive_ms - p.observations_ts * 1000,
                ])
        print(f"\n  CSV saved: {csv_path}")


async def run(symbol: str, duration: int, csv_path: Optional[str]):
    reports: List[ChainlinkReport] = []
    updates: List[RTDSUpdate] = []
    stop = asyncio.Event()

    print(f"Collecting {symbol} data for {duration}s from both sources...\n")

    # Live counter task
    async def status_printer():
        t0 = time.time()
        while not stop.is_set():
            elapsed = int(time.time() - t0)
            remaining = duration - elapsed
            print(
                f"\r  [{elapsed}s / {duration}s] "
                f"ChainlinkDS: {len(reports)} reports | RTDS: {len(updates)} updates"
                f"  (Ctrl+C to stop early)   ",
                end="", flush=True,
            )
            if remaining <= 0:
                stop.set()
                break
            await asyncio.sleep(1)

    tasks = [
        asyncio.create_task(listen_chainlink(symbol, reports, stop)),
        asyncio.create_task(listen_rtds(symbol, updates, stop)),
        asyncio.create_task(status_printer()),
    ]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        # Give tasks a moment to clean up
        await asyncio.sleep(0.5)

    match_and_analyze(reports, updates, symbol, csv_path)


def main():
    parser = argparse.ArgumentParser(description="Compare RTDS vs Chainlink DS prices")
    parser.add_argument("--symbol", default="BTC", help="Symbol (BTC, ETH, SOL)")
    parser.add_argument("--duration", type=int, default=120, help="Collection duration in seconds")
    parser.add_argument("--csv", default=None, help="Export matched pairs to CSV")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    csv_path = args.csv or f"data/chainlink_compare_{symbol}_{int(time.time())}.csv"

    print(f"Source comparison: RTDS vs Chainlink Direct")
    print(f"Symbol: {symbol} | Duration: {args.duration}s | CSV: {csv_path}\n")

    asyncio.run(run(symbol, args.duration, csv_path))


if __name__ == "__main__":
    main()
