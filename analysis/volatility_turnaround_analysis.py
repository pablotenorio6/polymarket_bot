"""
Volatility-Turnaround Correlation Analysis (v2)

Cross-references BTC 15m candles from Binance with Polymarket turnaround events
to determine if BTC volatility predicts turnaround probability.

Turnaround = confirmed market where the winning outcome was priced at ~0.01
at some point and then recovered to win (1.0). These are the early_queue wins.

Data sources:
  - Binance BTCUSDT 15m candles (btcusdt_15m_30d.csv via fetch_btc_candles.py)
  - Polymarket confirmed markets (polymarket.markets WHERE confirmed_winner=true)

Usage:
    python volatility_turnaround_analysis.py
"""

import csv
import math
import statistics
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ─── BTC turnaround events: (market_id, start_time_utc, end_time_utc) ───────
# Sources: polymarket.markets (confirmed_winner=true, question LIKE 'Bitcoin%')
# + 1 manually verified market missing from DB (Feb 7, 5:15AM ET)
# Total: 38 turnarounds (Jan 8 - Feb 8, 2026)
BTC_TURNAROUNDS = [
    (20, "2026-01-08T16:30:02Z", "2026-01-08T16:45:00Z"),
    (55, "2026-01-09T01:15:02Z", "2026-01-09T01:30:00Z"),
    (131, "2026-01-09T13:21:58Z", "2026-01-09T13:30:00Z"),
    (157, "2026-01-09T19:45:07Z", "2026-01-09T20:00:00Z"),
    (349, "2026-01-11T11:45:02Z", "2026-01-11T12:00:00Z"),
    (458, "2026-01-12T15:00:02Z", "2026-01-12T15:15:00Z"),
    (511, "2026-01-13T04:15:02Z", "2026-01-13T04:30:00Z"),
    (518, "2026-01-13T06:00:02Z", "2026-01-13T06:15:00Z"),
    (519, "2026-01-13T06:15:02Z", "2026-01-13T06:30:00Z"),
    (527, "2026-01-13T08:15:02Z", "2026-01-13T08:30:00Z"),
    (707, "2026-01-14T09:15:02Z", "2026-01-14T09:30:00Z"),
    (733, "2026-01-14T11:30:02Z", "2026-01-14T11:45:00Z"),
    (736, "2026-01-14T11:45:02Z", "2026-01-14T12:00:00Z"),
    (853, "2026-01-14T21:45:03Z", "2026-01-14T22:00:00Z"),
    (913, "2026-01-15T02:45:03Z", "2026-01-15T03:00:00Z"),
    (1636, "2026-01-17T12:30:08Z", "2026-01-17T12:45:00Z"),
    (1646, "2026-01-17T13:30:03Z", "2026-01-17T13:45:00Z"),
    (1960, "2026-01-18T15:30:03Z", "2026-01-18T15:45:00Z"),
    (2354, "2026-01-20T01:00:03Z", "2026-01-20T01:15:00Z"),
    (2712, "2026-01-21T08:00:03Z", "2026-01-21T08:15:00Z"),
    (2730, "2026-01-21T09:30:03Z", "2026-01-21T09:45:00Z"),
    (2806, "2026-01-21T14:15:03Z", "2026-01-21T14:30:00Z"),
    (2901, "2026-01-21T22:00:03Z", "2026-01-21T22:15:00Z"),
    (3221, "2026-01-23T00:45:29Z", "2026-01-23T01:00:00Z"),
    (3808, "2026-01-25T02:30:03Z", "2026-01-25T02:45:00Z"),
    (4320, "2026-01-28T10:00:02Z", "2026-01-28T10:15:00Z"),
    (4465, "2026-01-29T22:30:02Z", "2026-01-29T22:45:00Z"),
    (4491, "2026-01-30T05:00:02Z", "2026-01-30T05:15:00Z"),
    (4521, "2026-01-30T12:30:02Z", "2026-01-30T12:45:00Z"),
    (4568, "2026-01-30T16:15:02Z", "2026-01-30T16:30:00Z"),
    (4728, "2026-02-01T08:15:02Z", "2026-02-01T08:30:00Z"),
    (4767, "2026-02-01T14:30:02Z", "2026-02-01T14:45:00Z"),
    (4784, "2026-02-01T18:45:02Z", "2026-02-01T19:00:00Z"),
    (4831, "2026-02-02T06:30:02Z", "2026-02-02T06:45:00Z"),
    (5208, "2026-02-05T20:45:02Z", "2026-02-05T21:00:00Z"),
    (5261, "2026-02-06T10:00:03Z", "2026-02-06T10:15:00Z"),
    # Manually verified - data lost due to ETL issue, not in DB
    (9999, "2026-02-07T10:15:02Z", "2026-02-07T10:30:00Z"),
    (5426, "2026-02-08T12:15:02Z", "2026-02-08T12:30:00Z"),
]


def load_candles(path="btcusdt_15m_30d.csv"):
    candles = []
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append({
                "open_time": datetime.strptime(row["open_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
                "close_time": datetime.strptime(row["close_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "trades": int(row["trades"]),
            })
    return candles


def floor_to_15m(dt):
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def calc_atr(candles, idx, window):
    start = max(1, idx - window + 1)
    if idx < start:
        return None
    trs = []
    for i in range(start, idx + 1):
        h, l = candles[i]["high"], candles[i]["low"]
        prev_c = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr / candles[i]["open"] * 10000)
    return statistics.mean(trs) if trs else None


def calc_rolling_volatility(candles, idx, window):
    start = max(0, idx - window + 1)
    if idx - start < 2:
        return None
    returns = []
    for i in range(start + 1, idx + 1):
        ret = (candles[i]["close"] - candles[i - 1]["close"]) / candles[i - 1]["close"]
        returns.append(ret)
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * 10000


def calc_directional_move(candles, idx, window):
    """Net directional move over window candles (in bps). Positive = up trend."""
    start = max(0, idx - window)
    if start >= idx:
        return None
    return (candles[idx]["close"] - candles[start]["open"]) / candles[start]["open"] * 10000


def calc_volume_avg(candles, idx, window):
    """Average BTC volume over window candles."""
    start = max(0, idx - window + 1)
    vols = [candles[i]["volume"] for i in range(start, idx + 1)]
    return statistics.mean(vols) if vols else None


def build_candle_index(candles):
    index = {}
    for i, c in enumerate(candles):
        index[c["open_time"]] = i
    return index


def map_turnarounds(candles, candle_idx):
    """Map turnaround events to candle indices. Returns (list, set, unmatched_count)."""
    turnaround_candle_indices = []
    unmatched = 0
    for mid, start_str, end_str in BTC_TURNAROUNDS:
        start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        floored = floor_to_15m(start)
        idx = candle_idx.get(floored)
        if idx is not None:
            turnaround_candle_indices.append(idx)
        else:
            floored_prev = floored - timedelta(minutes=15)
            idx = candle_idx.get(floored_prev)
            if idx is not None:
                turnaround_candle_indices.append(idx)
            else:
                unmatched += 1
    return turnaround_candle_indices, set(turnaround_candle_indices), unmatched


def analyze():
    print("=" * 80)
    print("VOLATILITY-TURNAROUND CORRELATION ANALYSIS (v2)")
    print("BTC 15m candles (Binance) vs Polymarket turnaround events")
    print(f"Turnarounds in dataset: {len(BTC_TURNAROUNDS)}")
    print("=" * 80)

    candles = load_candles()
    print(f"\nLoaded {len(candles)} candles: {candles[0]['open_time']} -> {candles[-1]['close_time']}")

    candle_idx = build_candle_index(candles)
    turnaround_list, turnaround_set, unmatched = map_turnarounds(candles, candle_idx)

    print(f"Matched {len(turnaround_set)} / {len(BTC_TURNAROUNDS)} turnarounds to Binance candles"
          f" ({unmatched} unmatched)")

    base_rate = len(turnaround_set) / len(candles) * 100
    print(f"Base turnaround rate: {len(turnaround_set)}/{len(candles)} = {base_rate:.3f}%")

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. CONCURRENT VOLATILITY
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("1. CONCURRENT VOLATILITY (same 15-min candle as market)")
    print("=" * 80)

    t_ranges, n_ranges = [], []
    t_vols, n_vols = [], []
    t_trades, n_trades = [], []
    for i, c in enumerate(candles):
        r = (c["high"] - c["low"]) / c["open"] * 10000
        if i in turnaround_set:
            t_ranges.append(r)
            t_vols.append(c["volume"])
            t_trades.append(c["trades"])
        else:
            n_ranges.append(r)
            n_vols.append(c["volume"])
            n_trades.append(c["trades"])

    if t_ranges and n_ranges:
        print(f"\n  {'Metric':<25} | {'Turnaround':>15} | {'Normal':>15} | {'Ratio':>8}")
        print(f"  {'-'*25}-+-{'-'*15}-+-{'-'*15}-+-{'-'*8}")
        print(f"  {'Range (bps) mean':<25} | {statistics.mean(t_ranges):>15.1f} | {statistics.mean(n_ranges):>15.1f} | {statistics.mean(t_ranges)/statistics.mean(n_ranges):>7.2f}x")
        print(f"  {'Range (bps) median':<25} | {statistics.median(t_ranges):>15.1f} | {statistics.median(n_ranges):>15.1f} | {statistics.median(t_ranges)/statistics.median(n_ranges):>7.2f}x")
        print(f"  {'Volume (BTC) mean':<25} | {statistics.mean(t_vols):>15.1f} | {statistics.mean(n_vols):>15.1f} | {statistics.mean(t_vols)/statistics.mean(n_vols):>7.2f}x")
        print(f"  {'Volume (BTC) median':<25} | {statistics.median(t_vols):>15.1f} | {statistics.median(n_vols):>15.1f} | {statistics.median(t_vols)/statistics.median(n_vols):>7.2f}x")
        print(f"  {'Trades mean':<25} | {statistics.mean(t_trades):>15.0f} | {statistics.mean(n_trades):>15.0f} | {statistics.mean(t_trades)/statistics.mean(n_trades):>7.2f}x")

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. PRECEDING VOLATILITY (ATR)
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("2. PRECEDING VOLATILITY (ATR over N candles BEFORE market)")
    print("   This is what MMs observe to set initial prices")
    print("=" * 80)

    for window_name, window in [("1h (4c)", 4), ("2h (8c)", 8), ("4h (16c)", 16), ("24h (96c)", 96)]:
        print(f"\n  --- Window: {window_name} ---")
        t_atr, n_atr = [], []

        for i in range(window, len(candles)):
            atr = calc_atr(candles, i - 1, window)
            if atr is None:
                continue
            if i in turnaround_set:
                t_atr.append(atr)
            else:
                n_atr.append(atr)

        if t_atr and n_atr:
            print(f"    Turnaround (n={len(t_atr)}):     ATR mean={statistics.mean(t_atr):.2f}, median={statistics.median(t_atr):.2f} bps")
            print(f"    Non-turnaround (n={len(n_atr)}): ATR mean={statistics.mean(n_atr):.2f}, median={statistics.median(n_atr):.2f} bps")
            print(f"    Ratio: {statistics.mean(t_atr)/statistics.mean(n_atr):.2f}x")

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. PRECEDING VOLUME
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("3. PRECEDING VOLUME (avg BTC volume over N candles BEFORE market)")
    print("=" * 80)

    for window_name, window in [("1h (4c)", 4), ("2h (8c)", 8), ("4h (16c)", 16)]:
        print(f"\n  --- Window: {window_name} ---")
        t_vol, n_vol = [], []

        for i in range(window, len(candles)):
            vol = calc_volume_avg(candles, i - 1, window)
            if vol is None:
                continue
            if i in turnaround_set:
                t_vol.append(vol)
            else:
                n_vol.append(vol)

        if t_vol and n_vol:
            print(f"    Turnaround (n={len(t_vol)}):     Vol mean={statistics.mean(t_vol):.1f}, median={statistics.median(t_vol):.1f} BTC")
            print(f"    Non-turnaround (n={len(n_vol)}): Vol mean={statistics.mean(n_vol):.1f}, median={statistics.median(n_vol):.1f} BTC")
            print(f"    Ratio: {statistics.mean(t_vol)/statistics.mean(n_vol):.2f}x")

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. DIRECTIONAL BIAS (trend before turnaround)
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("4. DIRECTIONAL BIAS (net BTC move in bps before market)")
    print("   Positive = BTC was trending UP, Negative = DOWN")
    print("=" * 80)

    for window_name, window in [("1h (4c)", 4), ("2h (8c)", 8), ("4h (16c)", 16)]:
        print(f"\n  --- Window: {window_name} ---")
        t_dir, n_dir = [], []

        for i in range(window, len(candles)):
            d = calc_directional_move(candles, i - 1, window)
            if d is None:
                continue
            if i in turnaround_set:
                t_dir.append(d)
            else:
                n_dir.append(d)

        if t_dir and n_dir:
            print(f"    Turnaround:     mean={statistics.mean(t_dir):+.1f}, median={statistics.median(t_dir):+.1f} bps")
            print(f"    Non-turnaround: mean={statistics.mean(n_dir):+.1f}, median={statistics.median(n_dir):+.1f} bps")
            # Abs direction (magnitude of trend regardless of sign)
            t_abs = [abs(d) for d in t_dir]
            n_abs = [abs(d) for d in n_dir]
            print(f"    |Direction|:    T mean={statistics.mean(t_abs):.1f} vs N mean={statistics.mean(n_abs):.1f} ({statistics.mean(t_abs)/statistics.mean(n_abs):.2f}x)")

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. QUINTILE ANALYSIS (ATR + Volume)
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("5. TURNAROUND RATE BY ATR QUINTILE — preceding 1h (4 candles)")
    print("=" * 80)

    window = 4
    all_data = []
    for i in range(window, len(candles)):
        atr = calc_atr(candles, i - 1, window)
        vol = calc_volume_avg(candles, i - 1, window)
        if atr is None:
            continue
        all_data.append({"idx": i, "atr": atr, "vol": vol, "is_turnaround": i in turnaround_set})

    sorted_by_atr = sorted(all_data, key=lambda x: x["atr"])
    n = len(sorted_by_atr)
    q_size = n // 5

    total_t = sum(1 for d in all_data if d["is_turnaround"])
    print(f"\n  Candles: {n}, Turnarounds: {total_t}\n")

    for q in range(5):
        s = q * q_size
        e = (q + 1) * q_size if q < 4 else n
        seg = sorted_by_atr[s:e]
        t = sum(1 for d in seg if d["is_turnaround"])
        total = len(seg)
        rate = t / total * 100 if total else 0
        ev = rate / 100 * 0.99 - (1 - rate / 100) * 0.01
        bar = "#" * int(rate * 4)
        print(f"  Q{q+1} | ATR {seg[0]['atr']:6.1f}-{seg[-1]['atr']:6.1f} bps | "
              f"{t:2d}/{total} = {rate:5.2f}% | EV={ev:+.4f} | {bar}")

    # Volume quintiles
    print(f"\n  --- By VOLUME quintile (preceding 1h) ---\n")
    sorted_by_vol = sorted(all_data, key=lambda x: x["vol"] or 0)
    q_size_v = n // 5

    for q in range(5):
        s = q * q_size_v
        e = (q + 1) * q_size_v if q < 4 else n
        seg = sorted_by_vol[s:e]
        t = sum(1 for d in seg if d["is_turnaround"])
        total = len(seg)
        rate = t / total * 100 if total else 0
        ev = rate / 100 * 0.99 - (1 - rate / 100) * 0.01
        bar = "#" * int(rate * 4)
        print(f"  Q{q+1} | Vol {seg[0]['vol']:7.1f}-{seg[-1]['vol']:7.1f} BTC | "
              f"{t:2d}/{total} = {rate:5.2f}% | EV={ev:+.4f} | {bar}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 6. DECILE ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("6. TURNAROUND RATE BY ATR DECILE — preceding 1h (4 candles)")
    print("=" * 80)

    d_size = n // 10
    print()
    for d in range(10):
        s = d * d_size
        e = (d + 1) * d_size if d < 9 else n
        seg = sorted_by_atr[s:e]
        t = sum(1 for x in seg if x["is_turnaround"])
        total = len(seg)
        rate = t / total * 100 if total else 0
        ev = rate / 100 * 0.99 - (1 - rate / 100) * 0.01
        bar = "#" * int(rate * 4)
        print(f"  D{d+1:2d} | ATR {seg[0]['atr']:6.1f}-{seg[-1]['atr']:6.1f} bps | "
              f"{t:2d}/{total} = {rate:5.2f}% | EV={ev:+.4f} | {bar}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 7. TIME-OF-DAY ANALYSIS (ET timezone)
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("7. TURNAROUND RATE BY HOUR OF DAY (ET = UTC-5)")
    print("=" * 80)

    hour_total = defaultdict(int)
    hour_turnaround = defaultdict(int)

    for i, c in enumerate(candles):
        et_hour = (c["open_time"].hour - 5) % 24
        hour_total[et_hour] += 1
        if i in turnaround_set:
            hour_turnaround[et_hour] += 1

    print(f"\n  {'Hour ET':>8} | {'Turns':>6} | {'Total':>6} | {'Rate':>7} | {'vs Base':>8} | ")
    print(f"  {'-'*8}-+-{'-'*6}-+-{'-'*6}-+-{'-'*7}-+-{'-'*8}-+")

    for h in range(24):
        total = hour_total[h]
        t = hour_turnaround[h]
        rate = t / total * 100 if total else 0
        lift = rate / base_rate if base_rate else 0
        bar = "#" * int(rate * 3)
        print(f"  {h:5d}:00 | {t:6d} | {total:6d} | {rate:5.2f}% | {lift:7.1f}x | {bar}")

    # Group into sessions
    print(f"\n  --- By trading session ---")
    sessions = [
        ("Asia (20:00-04:00 ET)", list(range(20, 24)) + list(range(0, 4))),
        ("Europe (04:00-09:30 ET)", list(range(4, 10))),
        ("US Open (09:30-16:00 ET)", list(range(10, 16))),
        ("US Evening (16:00-20:00 ET)", list(range(16, 20))),
    ]
    for name, hours in sessions:
        t = sum(hour_turnaround[h] for h in hours)
        total = sum(hour_total[h] for h in hours)
        rate = t / total * 100 if total else 0
        lift = rate / base_rate if base_rate else 0
        print(f"    {name:<30} {t:3d}/{total:4d} = {rate:.2f}% (lift: {lift:.1f}x)")

    # ═══════════════════════════════════════════════════════════════════════════
    # 8. DAY-OF-WEEK ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("8. TURNAROUND RATE BY DAY OF WEEK")
    print("=" * 80)

    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_total = defaultdict(int)
    dow_turnaround = defaultdict(int)

    for i, c in enumerate(candles):
        dow = c["open_time"].weekday()
        dow_total[dow] += 1
        if i in turnaround_set:
            dow_turnaround[dow] += 1

    print()
    for d in range(7):
        total = dow_total[d]
        t = dow_turnaround[d]
        rate = t / total * 100 if total else 0
        lift = rate / base_rate if base_rate else 0
        bar = "#" * int(rate * 4)
        print(f"  {dow_names[d]:>3} | {t:2d}/{total:4d} = {rate:5.2f}% | lift: {lift:.1f}x | {bar}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 9. BTC PRICE LEVEL ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("9. TURNAROUND RATE BY BTC PRICE LEVEL")
    print("=" * 80)

    price_brackets = [
        (60000, 70000), (70000, 80000), (80000, 85000),
        (85000, 90000), (90000, 95000), (95000, 100000), (100000, 110000),
    ]

    print()
    for lo, hi in price_brackets:
        t, total = 0, 0
        for i, c in enumerate(candles):
            if lo <= c["open"] < hi:
                total += 1
                if i in turnaround_set:
                    t += 1
        rate = t / total * 100 if total else 0
        lift = rate / base_rate if base_rate else 0
        bar = "#" * int(rate * 4)
        print(f"  ${lo//1000}k-${hi//1000}k | {t:2d}/{total:4d} = {rate:5.2f}% | lift: {lift:.1f}x | {bar}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 10. CLUSTERING ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("10. TURNAROUND CLUSTERING (do they come in bursts?)")
    print("=" * 80)

    sorted_t = sorted(turnaround_list)
    gaps = []
    for i in range(1, len(sorted_t)):
        gap_h = (sorted_t[i] - sorted_t[i - 1]) * 0.25
        gaps.append(gap_h)
        if gap_h <= 4:
            t1 = candles[sorted_t[i - 1]]["open_time"]
            t2 = candles[sorted_t[i]]["open_time"]
            print(f"  CLUSTER: {t1.strftime('%m-%d %H:%M')} -> {t2.strftime('%m-%d %H:%M')} ({gap_h:.1f}h)")

    if gaps:
        print(f"\n  Gap stats (hours): mean={statistics.mean(gaps):.1f}, median={statistics.median(gaps):.1f}")
        print(f"  Gaps <=1h: {sum(1 for g in gaps if g <= 1)}, "
              f"<=2h: {sum(1 for g in gaps if g <= 2)}, "
              f"<=4h: {sum(1 for g in gaps if g <= 4)}, "
              f"<=8h: {sum(1 for g in gaps if g <= 8)}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 11. CONDITIONAL PROBABILITY
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("11. CONDITIONAL: turnaround rate AFTER a recent turnaround")
    print("=" * 80)

    for h_name, h in [("15min", 1), ("30min", 2), ("1h", 4), ("2h", 8), ("4h", 16), ("8h", 32), ("24h", 96)]:
        subsequent_t, subsequent_total = 0, 0
        for t_idx in sorted_t:
            for offset in range(1, h + 1):
                ci = t_idx + offset
                if ci < len(candles) and ci not in turnaround_set:
                    subsequent_total += 1
                    if ci in turnaround_set:
                        subsequent_t += 1
                elif ci in turnaround_set:
                    subsequent_total += 1
                    subsequent_t += 1

        cond = subsequent_t / subsequent_total * 100 if subsequent_total else 0
        lift = cond / base_rate if base_rate else 0
        print(f"  Within {h_name:>5}: {subsequent_t}/{subsequent_total:5d} = {cond:.2f}% "
              f"(base: {base_rate:.2f}%, lift: {lift:.1f}x)")

    # ═══════════════════════════════════════════════════════════════════════════
    # 12. VOLATILITY REGIME ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("12. VOLATILITY REGIME ANALYSIS")
    print("    Categorize 24h rolling ATR into Low/Medium/High regimes")
    print("=" * 80)

    regime_window = 96  # 24h
    regime_data = []
    for i in range(regime_window, len(candles)):
        atr = calc_atr(candles, i - 1, regime_window)
        if atr is not None:
            regime_data.append({"idx": i, "atr": atr, "is_turnaround": i in turnaround_set})

    if regime_data:
        atrs = [d["atr"] for d in regime_data]
        p33 = sorted(atrs)[len(atrs) // 3]
        p66 = sorted(atrs)[2 * len(atrs) // 3]

        regimes = {"Low": [], "Medium": [], "High": []}
        for d in regime_data:
            if d["atr"] < p33:
                regimes["Low"].append(d)
            elif d["atr"] < p66:
                regimes["Medium"].append(d)
            else:
                regimes["High"].append(d)

        print(f"\n  ATR thresholds: Low < {p33:.1f} < Medium < {p66:.1f} < High\n")
        for name, data in regimes.items():
            t = sum(1 for d in data if d["is_turnaround"])
            total = len(data)
            rate = t / total * 100 if total else 0
            lift = rate / base_rate if base_rate else 0
            atr_range = f"{min(d['atr'] for d in data):.1f}-{max(d['atr'] for d in data):.1f}"
            print(f"  {name:>6} (ATR {atr_range:>12} bps) | {t:2d}/{total:4d} = {rate:.2f}% | lift: {lift:.1f}x")

    # ═══════════════════════════════════════════════════════════════════════════
    # 13. COMBINED SIGNAL: ATR + VOLUME + DIRECTION
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("13. COMBINED SIGNAL ANALYSIS")
    print("    Split by ATR (high/low) x Volume (high/low) x Direction (up/down)")
    print("=" * 80)

    window = 4
    combo_data = []
    for i in range(window, len(candles)):
        atr = calc_atr(candles, i - 1, window)
        vol = calc_volume_avg(candles, i - 1, window)
        direction = calc_directional_move(candles, i - 1, window)
        if atr is None or vol is None or direction is None:
            continue
        combo_data.append({
            "idx": i, "atr": atr, "vol": vol, "dir": direction,
            "is_turnaround": i in turnaround_set
        })

    if combo_data:
        atr_med = statistics.median([d["atr"] for d in combo_data])
        vol_med = statistics.median([d["vol"] for d in combo_data])

        combos = defaultdict(lambda: {"t": 0, "n": 0})
        for d in combo_data:
            atr_label = "HiATR" if d["atr"] >= atr_med else "LoATR"
            vol_label = "HiVol" if d["vol"] >= vol_med else "LoVol"
            dir_label = "Up" if d["dir"] >= 0 else "Down"
            key = f"{atr_label}+{vol_label}+{dir_label}"
            combos[key]["n"] += 1
            if d["is_turnaround"]:
                combos[key]["t"] += 1

        print(f"\n  ATR median: {atr_med:.1f} bps, Volume median: {vol_med:.1f} BTC\n")
        print(f"  {'Combination':<25} | {'Turns':>6} | {'Total':>6} | {'Rate':>7} | {'Lift':>6}")
        print(f"  {'-'*25}-+-{'-'*6}-+-{'-'*6}-+-{'-'*7}-+-{'-'*6}")

        for key in sorted(combos.keys()):
            t = combos[key]["t"]
            total = combos[key]["n"]
            rate = t / total * 100 if total else 0
            lift = rate / base_rate if base_rate else 0
            print(f"  {key:<25} | {t:6d} | {total:6d} | {rate:5.2f}% | {lift:5.1f}x")

    # ═══════════════════════════════════════════════════════════════════════════
    # 14. OPTIMAL ATR FILTER
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("14. OPTIMAL ATR FILTER for early_queue")
    print("=" * 80)

    window = 4
    all_data = []
    for i in range(window, len(candles)):
        atr = calc_atr(candles, i - 1, window)
        if atr is None:
            continue
        all_data.append({"idx": i, "atr": atr, "is_turnaround": i in turnaround_set})

    sorted_data = sorted(all_data, key=lambda x: x["atr"])
    n = len(sorted_data)

    print(f"\n  Filtering by MAX ATR threshold (exclude high-vol):\n")
    print(f"  {'Max ATR':>10} | {'Candles':>8} | {'Turns':>6} | {'Rate':>7} | {'EV/trade':>10} | {'Daily EV':>10}")
    print(f"  {'-'*10}-+-{'-'*8}-+-{'-'*6}-+-{'-'*7}-+-{'-'*10}-+-{'-'*10}")

    for cutoff_pct in range(10, 101, 10):
        cutoff_idx = int(n * cutoff_pct / 100)
        seg = sorted_data[:cutoff_idx]
        if not seg:
            continue
        t = sum(1 for d in seg if d["is_turnaround"])
        total = len(seg)
        rate = t / total * 100
        ev = rate / 100 * 0.99 - (1 - rate / 100) * 0.01
        max_atr = seg[-1]["atr"]
        daily_opps = 96 * cutoff_pct / 100
        daily_ev = ev * daily_opps
        marker = " <-- BEST" if cutoff_pct == 100 else ""
        print(f"  {max_atr:10.1f} | {total:8d} | {t:6d} | {rate:6.2f}% | {ev:+10.4f} | {daily_ev:+10.2f}{marker}")

    print(f"\n  Filtering by MIN ATR threshold (exclude low-vol):\n")
    print(f"  {'Min ATR':>10} | {'Candles':>8} | {'Turns':>6} | {'Rate':>7} | {'EV/trade':>10} | {'Daily EV':>10}")
    print(f"  {'-'*10}-+-{'-'*8}-+-{'-'*6}-+-{'-'*7}-+-{'-'*10}-+-{'-'*10}")

    for cutoff_pct in range(0, 91, 10):
        cutoff_idx = int(n * cutoff_pct / 100)
        seg = sorted_data[cutoff_idx:]
        if not seg:
            continue
        t = sum(1 for d in seg if d["is_turnaround"])
        total = len(seg)
        rate = t / total * 100
        ev = rate / 100 * 0.99 - (1 - rate / 100) * 0.01
        min_atr = seg[0]["atr"]
        daily_opps = 96 * (100 - cutoff_pct) / 100
        daily_ev = ev * daily_opps
        print(f"  {min_atr:10.1f} | {total:8d} | {t:6d} | {rate:6.2f}% | {ev:+10.4f} | {daily_ev:+10.2f}")

    # Best band
    print(f"\n  BAND FILTER — Top 15 by Daily EV:\n")
    print(f"  {'Band':>12} | {'ATR Range':>18} | {'Turns':>8} | {'Rate':>7} | {'EV/trade':>10} | {'Daily EV':>10}")
    print(f"  {'-'*12}-+-{'-'*18}-+-{'-'*8}-+-{'-'*7}-+-{'-'*10}-+-{'-'*10}")

    results = []
    for lo_pct in range(0, 80, 5):
        for hi_pct in range(lo_pct + 20, 101, 5):
            lo_idx = int(n * lo_pct / 100)
            hi_idx = int(n * hi_pct / 100)
            seg = sorted_data[lo_idx:hi_idx]
            if len(seg) < 50:
                continue
            t = sum(1 for d in seg if d["is_turnaround"])
            total = len(seg)
            rate = t / total * 100
            ev = rate / 100 * 0.99 - (1 - rate / 100) * 0.01
            daily_opps = 96 * (hi_pct - lo_pct) / 100
            daily_ev = ev * daily_opps
            if daily_ev > 0:
                lo_atr = seg[0]["atr"]
                hi_atr = seg[-1]["atr"]
                results.append((daily_ev, lo_pct, hi_pct, lo_atr, hi_atr, t, total, rate, ev))

    results.sort(reverse=True)
    for daily_ev, lo_pct, hi_pct, lo_atr, hi_atr, t, total, rate, ev in results[:15]:
        print(f"  P{lo_pct:2d}-P{hi_pct:3d} | {lo_atr:7.1f}-{hi_atr:7.1f} | "
              f"{t:3d}/{total:4d} | {rate:5.2f}% | {ev:+10.4f} | {daily_ev:+10.2f}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 15. TEMPORAL EVOLUTION: turnaround rate over time
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("15. TEMPORAL EVOLUTION — turnaround rate per day")
    print("    Are MMs adapting? Is the edge decaying?")
    print("=" * 80)

    day_total = defaultdict(int)
    day_turnaround = defaultdict(int)

    for i, c in enumerate(candles):
        day_key = c["open_time"].strftime("%m-%d")
        day_total[day_key] += 1
        if i in turnaround_set:
            day_turnaround[day_key] += 1

    print(f"\n  {'Date':>6} | {'Turns':>6} | {'Total':>6} | {'Rate':>7} | ")
    print(f"  {'-'*6}-+-{'-'*6}-+-{'-'*6}-+-{'-'*7}-+")

    for day_key in sorted(day_total.keys()):
        t = day_turnaround[day_key]
        total = day_total[day_key]
        rate = t / total * 100 if total else 0
        bar = "#" * t if t > 0 else ""
        print(f"  {day_key:>6} | {t:6d} | {total:6d} | {rate:5.2f}% | {bar}")

    # Weekly aggregation
    print(f"\n  --- Weekly aggregation ---")
    week_data = defaultdict(lambda: {"t": 0, "n": 0})
    for i, c in enumerate(candles):
        week_key = c["open_time"].strftime("%Y-W%W")
        week_data[week_key]["n"] += 1
        if i in turnaround_set:
            week_data[week_key]["t"] += 1

    for week_key in sorted(week_data.keys()):
        t = week_data[week_key]["t"]
        total = week_data[week_key]["n"]
        rate = t / total * 100 if total else 0
        bar = "#" * t
        print(f"  {week_key} | {t:2d}/{total:4d} = {rate:.2f}% | {bar}")

    # ═══════════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"""
  Dataset: {len(candles)} candles ({candles[0]['open_time'].strftime('%Y-%m-%d')} to {candles[-1]['open_time'].strftime('%Y-%m-%d')})
  Turnarounds: {len(turnaround_set)} matched / {len(BTC_TURNAROUNDS)} total
  Base rate: {base_rate:.3f}% ({len(turnaround_set)}/{len(candles)})
  EV at base rate: {base_rate/100*0.99 - (1-base_rate/100)*0.01:+.4f} per trade
  Daily EV (no filter): {(base_rate/100*0.99 - (1-base_rate/100)*0.01) * 96:+.2f}
""")


if __name__ == "__main__":
    analyze()
