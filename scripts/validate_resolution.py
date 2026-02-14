"""
Validate Polymarket resolution prices against Chainlink Data Streams.

For each recently resolved BTC 15-min market:
1. Get actual winner from Gamma API (outcomePrices)
2. Query Chainlink DS REST API at start_time and end_time
3. Compare predicted winner (from Chainlink prices) vs actual winner
4. Log both validFromTimestamp and observationsTimestamp to determine which one matters

This tells us:
- Whether our "last report with observationsTs <= boundary" logic is correct
- Whether validFromTimestamp changes the outcome in any case
- Whether price rounding explains discrepancies

Usage:
    python scripts/validate_resolution.py
    python scripts/validate_resolution.py --markets 50
    python scripts/validate_resolution.py --csv data/resolution_validation.csv
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx
from dateutil import parser as dtparser
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from chainlink_ds import FEED_IDS, _OUTER_ABI, _V3_ABI
from eth_abi import decode as abi_decode

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CL_API_BASE = "https://api.dataengine.chain.link"

# --- Chainlink REST API auth (same HMAC scheme as WS) ---

def _cl_auth_headers(user_id: str, secret: str, cf_client_id: str, method: str, path: str) -> dict:
    timestamp = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(b"").hexdigest()
    sign_string = f"{method} {path} {body_hash} {user_id} {timestamp}"
    signature = hmac.new(secret.encode(), sign_string.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": user_id,
        "X-Authorization-Timestamp": timestamp,
        "X-Authorization-Signature-SHA256": signature,
        "CF-Access-Client-Id": cf_client_id,
        "CF-Access-Client-Secret": secret,
    }


def _decode_v3_report(hex_payload: str) -> Optional[Dict]:
    """Decode V3 report returning all fields."""
    try:
        raw = bytes.fromhex(hex_payload.removeprefix("0x"))
        outer = abi_decode(_OUTER_ABI, raw)
        v3 = abi_decode(_V3_ABI, outer[1])
        return {
            "feedId": v3[0].hex(),
            "validFromTimestamp": v3[1],
            "observationsTimestamp": v3[2],
            "benchmarkPrice": v3[6] / 10**18,
            "bid": v3[7] / 10**18,
            "ask": v3[8] / 10**18,
        }
    except Exception as e:
        print(f"  [decode error] {e}")
        return None


# --- Chainlink DS REST API ---

def get_chainlink_report_at(
    client: httpx.Client,
    feed_id: str,
    timestamp_unix: int,
    user_id: str,
    secret: str,
    cf_client_id: str,
) -> Optional[Dict]:
    """
    Query Chainlink Data Streams REST API for a report at a specific timestamp.

    Tries /api/v1/reports?feedID={feedId}&timestamp={ts}
    Returns decoded V3 report dict or None.
    """
    path = f"/api/v1/reports?feedID={feed_id}&timestamp={timestamp_unix}"
    url = f"{CL_API_BASE}{path}"
    headers = _cl_auth_headers(user_id, secret, cf_client_id, "GET", path)

    try:
        resp = client.get(url, headers=headers, timeout=10.0)
        if resp.status_code != 200:
            print(f"  [CL API] {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()

        # Response may be {"report": {"fullReport": "0x..."}} or {"reports": [...]}
        report_data = data.get("report") or (data.get("reports", [None])[0] if data.get("reports") else None)
        if not report_data:
            print(f"  [CL API] Unexpected response structure: {list(data.keys())}")
            return None

        full_hex = report_data.get("fullReport")
        if not full_hex:
            print(f"  [CL API] No fullReport in response")
            return None

        decoded = _decode_v3_report(full_hex)
        return decoded

    except Exception as e:
        print(f"  [CL API] Error: {e}")
        return None


# --- Gamma API ---

def get_resolved_btc_15min_markets(limit: int = 20) -> List[Dict]:
    """Fetch recently resolved BTC 15-min markets from Gamma API."""
    markets = []
    batch_size = 100
    offset = 0

    with httpx.Client(timeout=15.0) as client:
        while len(markets) < limit:
            params = {
                "closed": "true",
                "limit": batch_size,
                "offset": offset,
                "order": "startDate",
                "ascending": "false",
            }
            try:
                resp = client.get(GAMMA_API, params=params, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"[Gamma] Error at offset {offset}: {e}")
                break

            if not data:
                break

            for m in data:
                question = m.get("question", "").lower()
                if not ("bitcoin" in question and "up" in question and "down" in question):
                    continue

                # Check 15-min duration
                time_pat = r'(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)'
                match = re.search(time_pat, m.get("question", ""))
                if not match:
                    continue
                sh, sm, sp, eh, em, ep = match.groups()
                start_m = (int(sh) % 12) * 60 + int(sm) + (720 if sp == "PM" and int(sh) != 12 else 0)
                end_m = (int(eh) % 12) * 60 + int(em) + (720 if ep == "PM" and int(eh) != 12 else 0)
                dur = end_m - start_m
                if dur < 0:
                    dur += 1440
                if dur != 15:
                    continue

                # Check resolved
                outcome_prices = m.get("outcomePrices", [])
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except:
                        continue
                if "1" not in outcome_prices:
                    continue

                # Parse outcomes
                outcomes = m.get("outcomes", [])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                winner_idx = outcome_prices.index("1")
                actual_winner = outcomes[winner_idx] if winner_idx < len(outcomes) else "?"

                # Parse times — IMPORTANT: startDate is market creation date, NOT
                # the 15-min boundary. The actual start boundary = endDate - 15min.
                try:
                    end_dt = dtparser.parse(m["endDate"]) if m.get("endDate") else None
                except:
                    continue

                if not end_dt:
                    continue

                start_dt = end_dt - timedelta(minutes=dur)  # dur=15 for 15-min markets

                markets.append({
                    "question": m.get("question", ""),
                    "condition_id": m.get("conditionId", ""),
                    "actual_winner": actual_winner,
                    "start_dt": start_dt,
                    "end_dt": end_dt,
                    "start_unix": int(start_dt.timestamp()),
                    "end_unix": int(end_dt.timestamp()),
                })

                if len(markets) >= limit:
                    break

            offset += batch_size
            time.sleep(0.2)

    return markets


# --- Validation ---

@dataclass
class ValidationResult:
    question: str
    actual_winner: str
    start_unix: int
    end_unix: int
    # Start boundary
    start_obs_ts: Optional[int] = None
    start_valid_from_ts: Optional[int] = None
    start_price: Optional[float] = None
    # End boundary
    end_obs_ts: Optional[int] = None
    end_valid_from_ts: Optional[int] = None
    end_price: Optional[float] = None
    # Prediction
    predicted_winner_obs: Optional[str] = None    # using observationsTs logic
    match: Optional[bool] = None


def validate_markets(markets: List[Dict], csv_path: Optional[str] = None):
    """Query Chainlink at each boundary and compare with actual resolution."""
    user_id = os.environ.get("CHAINLINK_USERNAME", "")
    cf_client_id = os.environ.get("CHAINLINK_API_KEY", "")
    secret = os.environ.get("CHAINLINK_PASSWORD", "")

    if not user_id or not cf_client_id or not secret:
        print("ERROR: CHAINLINK_USERNAME, CHAINLINK_API_KEY, CHAINLINK_PASSWORD required")
        return

    feed_id = FEED_IDS["BTC"]
    results: List[ValidationResult] = []

    print(f"\nValidating {len(markets)} markets against Chainlink DS REST API...\n")
    print(f"{'#':>3}  {'question':^55}  {'actual':>6}  {'start_price':>14}  {'end_price':>14}  "
          f"{'delta':>10}  {'predicted':>9}  {'ok':>4}  {'obs_ts_s':>9}  {'vf_ts_s':>9}")
    print("-" * 155)

    with httpx.Client(http2=True) as client:
        for i, m in enumerate(markets):
            r = ValidationResult(
                question=m["question"],
                actual_winner=m["actual_winner"],
                start_unix=m["start_unix"],
                end_unix=m["end_unix"],
            )

            # Query Chainlink at start boundary
            start_report = get_chainlink_report_at(
                client, feed_id, m["start_unix"], user_id, secret, cf_client_id
            )
            if start_report:
                r.start_price = start_report["benchmarkPrice"]
                r.start_obs_ts = start_report["observationsTimestamp"]
                r.start_valid_from_ts = start_report["validFromTimestamp"]

            time.sleep(0.3)  # Rate limit

            # Query Chainlink at end boundary
            end_report = get_chainlink_report_at(
                client, feed_id, m["end_unix"], user_id, secret, cf_client_id
            )
            if end_report:
                r.end_price = end_report["benchmarkPrice"]
                r.end_obs_ts = end_report["observationsTimestamp"]
                r.end_valid_from_ts = end_report["validFromTimestamp"]

            time.sleep(0.3)

            # Predict winner
            if r.start_price is not None and r.end_price is not None:
                if r.end_price >= r.start_price:
                    r.predicted_winner_obs = "Up"
                else:
                    r.predicted_winner_obs = "Down"
                r.match = r.predicted_winner_obs == r.actual_winner

            # Print row
            delta = (r.end_price - r.start_price) if r.start_price and r.end_price else 0
            ok_str = "Y" if r.match else ("N" if r.match is False else "?")
            # How far obs_ts and valid_from_ts are from the boundary
            obs_s_diff = f"{r.start_obs_ts - m['start_unix']:+d}" if r.start_obs_ts else "?"
            obs_e_diff = f"{r.end_obs_ts - m['end_unix']:+d}" if r.end_obs_ts else "?"
            vf_s_diff = f"{r.start_valid_from_ts - m['start_unix']:+d}" if r.start_valid_from_ts else "?"
            vf_e_diff = f"{r.end_valid_from_ts - m['end_unix']:+d}" if r.end_valid_from_ts else "?"

            q_short = r.question[:55]
            print(
                f"{i:>3}  {q_short:<55}  {r.actual_winner:>6}  "
                f"${r.start_price or 0:>12,.2f}  ${r.end_price or 0:>12,.2f}  "
                f"${delta:>+9,.2f}  {r.predicted_winner_obs or '?':>9}  {ok_str:>4}  "
                f"{obs_s_diff}/{obs_e_diff}  {vf_s_diff}/{vf_e_diff}"
            )

            results.append(r)

    # --- Summary ---
    total = len(results)
    matched = sum(1 for r in results if r.match is True)
    mismatched = sum(1 for r in results if r.match is False)
    unknown = sum(1 for r in results if r.match is None)

    print(f"\n{'='*80}")
    print(f"  SUMMARY: {matched}/{total} correct, {mismatched} mismatches, {unknown} unknown")
    print(f"{'='*80}")

    if mismatched > 0:
        print(f"\n  MISMATCHES (our prediction != Polymarket resolution):")
        for r in results:
            if r.match is False:
                delta = r.end_price - r.start_price if r.start_price and r.end_price else 0
                print(
                    f"    {r.question[:70]}")
                print(
                    f"      start=${r.start_price:,.8f} (obs_ts={r.start_obs_ts}, vf_ts={r.start_valid_from_ts})")
                print(
                    f"      end  =${r.end_price:,.8f} (obs_ts={r.end_obs_ts}, vf_ts={r.end_valid_from_ts})")
                print(
                    f"      delta=${delta:+.8f}  predicted={r.predicted_winner_obs}  actual={r.actual_winner}")
                print(
                    f"      boundary: start={r.start_unix} end={r.end_unix}")
                print(
                    f"      obs_ts offset: start={r.start_obs_ts - r.start_unix if r.start_obs_ts else '?'}s  "
                    f"end={r.end_obs_ts - r.end_unix if r.end_obs_ts else '?'}s")
                print(
                    f"      vf_ts offset:  start={r.start_valid_from_ts - r.start_unix if r.start_valid_from_ts else '?'}s  "
                    f"end={r.end_valid_from_ts - r.end_unix if r.end_valid_from_ts else '?'}s")
                print()

    # Timestamp offset analysis
    obs_start_offsets = [r.start_obs_ts - r.start_unix for r in results if r.start_obs_ts]
    obs_end_offsets = [r.end_obs_ts - r.end_unix for r in results if r.end_obs_ts]
    vf_start_offsets = [r.start_valid_from_ts - r.start_unix for r in results if r.start_valid_from_ts]
    vf_end_offsets = [r.end_valid_from_ts - r.end_unix for r in results if r.end_valid_from_ts]

    if obs_start_offsets:
        print(f"\n  observationsTimestamp offsets from boundary:")
        print(f"    start: min={min(obs_start_offsets)}s  max={max(obs_start_offsets)}s  "
              f"avg={sum(obs_start_offsets)/len(obs_start_offsets):.1f}s")
        print(f"    end:   min={min(obs_end_offsets)}s  max={max(obs_end_offsets)}s  "
              f"avg={sum(obs_end_offsets)/len(obs_end_offsets):.1f}s")

    if vf_start_offsets:
        print(f"\n  validFromTimestamp offsets from boundary:")
        print(f"    start: min={min(vf_start_offsets)}s  max={max(vf_start_offsets)}s  "
              f"avg={sum(vf_start_offsets)/len(vf_start_offsets):.1f}s")
        print(f"    end:   min={min(vf_end_offsets)}s  max={max(vf_end_offsets)}s  "
              f"avg={sum(vf_end_offsets)/len(vf_end_offsets):.1f}s")

    # CSV export
    if csv_path:
        import csv
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "question", "actual_winner", "start_unix", "end_unix",
                "start_price", "end_price", "delta",
                "predicted_winner", "match",
                "start_obs_ts", "start_vf_ts", "end_obs_ts", "end_vf_ts",
                "start_obs_offset_s", "end_obs_offset_s",
                "start_vf_offset_s", "end_vf_offset_s",
            ])
            for r in results:
                delta = (r.end_price - r.start_price) if r.start_price and r.end_price else None
                w.writerow([
                    r.question, r.actual_winner, r.start_unix, r.end_unix,
                    f"{r.start_price:.8f}" if r.start_price else "",
                    f"{r.end_price:.8f}" if r.end_price else "",
                    f"{delta:.8f}" if delta is not None else "",
                    r.predicted_winner_obs or "", r.match if r.match is not None else "",
                    r.start_obs_ts or "", r.start_valid_from_ts or "",
                    r.end_obs_ts or "", r.end_valid_from_ts or "",
                    r.start_obs_ts - r.start_unix if r.start_obs_ts else "",
                    r.end_obs_ts - r.end_unix if r.end_obs_ts else "",
                    r.start_valid_from_ts - r.start_unix if r.start_valid_from_ts else "",
                    r.end_valid_from_ts - r.end_unix if r.end_valid_from_ts else "",
                ])
        print(f"\n  CSV saved: {csv_path}")


def main():
    parser = argparse.ArgumentParser(description="Validate Polymarket resolution vs Chainlink")
    parser.add_argument("--markets", type=int, default=20, help="Number of markets to validate")
    parser.add_argument("--csv", default=None, help="Export to CSV")
    args = parser.parse_args()

    csv_path = args.csv or f"data/resolution_validation_{int(time.time())}.csv"

    print("Fetching resolved BTC 15-min markets from Gamma API...")
    markets = get_resolved_btc_15min_markets(limit=args.markets)
    print(f"Found {len(markets)} resolved markets\n")

    if not markets:
        print("No resolved markets found")
        return

    validate_markets(markets, csv_path)


if __name__ == "__main__":
    main()
