"""
Black-Scholes Pricing for Polymarket Binary Options (UP/DOWN tokens)

Calculates theoretical prices based on:
- Strike: BTC open price at start of the hour
- Volatility: Realized volatility from last 2 hours (1-min candles)
- Time: Minutes remaining until market close
"""

import requests
import math
from datetime import datetime, timezone
from typing import Tuple, Optional

# Constants
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
VOLATILITY_LOOKBACK_HOURS = 8  # Lookback for EWMA calculation
MINUTES_PER_YEAR = 365 * 24 * 60  # For annualization
EWMA_LAMBDA = 0.99  # EWMA decay factor
MIN_VOLATILITY = 0.25  # 25% floor to avoid extreme predictions

# Dynamic volatility multiplier (VIX-style): increases as T approaches 0
VOL_MULT_MIN = 0.7  # Multiplier at T=60 min
VOL_MULT_MAX = 1 # Multiplier at T=0 min

# Fat tails: T-Student degrees of freedom (lower = fatter tails, higher = closer to normal)
T_STUDENT_DF = 15  # Degrees of freedom (higher = closer to normal, less conservative)


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function (approximation)"""
    # Abramowitz and Stegun approximation
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911
    
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2)
    
    return 0.5 * (1.0 + sign * y)


def t_student_cdf(x: float, df: float = T_STUDENT_DF) -> float:
    """
    T-Student CDF approximation for fat tails.
    Uses the relationship with the incomplete beta function.
    """
    if df <= 0:
        return norm_cdf(x)  # Fallback to normal
    
    # For large df, approaches normal
    if df > 100:
        return norm_cdf(x)
    
    # Incomplete beta function approximation for T-Student
    t2 = x * x
    y = t2 / df
    
    # Use continued fraction approximation
    if abs(x) < 1e-10:
        return 0.5
    
    # Approximation using the regularized incomplete beta function
    # I_x(a,b) where x = df/(df+t^2), a = df/2, b = 1/2
    z = df / (df + t2)
    a = df / 2.0
    b = 0.5
    
    # Beta function approximation using Stirling
    def log_beta(a, b):
        return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    
    # Regularized incomplete beta using continued fraction (simplified)
    # For our use case, a simpler approximation works well
    beta_val = math.exp(log_beta(a, b))
    
    # Simple series expansion for small z
    if z > 0.5:
        # Use the reflection formula
        result = 1.0 - t_student_cdf(-x, df)
        return result
    
    # Direct approximation using power series
    term = (z ** a) * ((1 - z) ** b) / (a * beta_val)
    sum_val = term
    
    for n in range(1, 50):
        term *= (a + n - 1) * z / n
        if n > 1:
            term *= (1 - b) / (a + n - 1)
        sum_val += term
        if abs(term) < 1e-10:
            break
    
    # Convert to T-Student CDF
    if x > 0:
        return 1.0 - 0.5 * sum_val
    else:
        return 0.5 * sum_val


def t_student_cdf_simple(x: float, df: float = T_STUDENT_DF) -> float:
    """
    Simplified T-Student CDF using normal CDF with adjusted variance.
    This is a practical approximation that captures fat tail behavior.
    """
    if df <= 2:
        df = 2.1  # Avoid undefined variance
    
    # T-Student has heavier tails than normal
    # Approximate by scaling x to compress probabilities toward 0.5
    # The variance of T-Student is df/(df-2) for df > 2
    variance_factor = math.sqrt(df / (df - 2)) if df > 2 else 2.0
    
    # Compress the z-score to simulate fatter tails
    # This keeps probabilities closer to 0.5
    x_adjusted = x / variance_factor
    
    # Additional compression for extreme values (fat tails)
    if abs(x) > 2:
        compression = 1.0 + 0.1 * (abs(x) - 2)
        x_adjusted = x_adjusted / compression
    
    return norm_cdf(x_adjusted)


def get_dynamic_vol_multiplier(minutes_remaining: float) -> float:
    """
    VIX-style dynamic multiplier: increases as T approaches 0.
    At T=60 min: returns VOL_MULT_MIN
    At T=0 min: returns VOL_MULT_MAX
    """
    # Linear interpolation
    t_fraction = max(0, min(60, minutes_remaining)) / 60.0  # 1 at start, 0 at end
    multiplier = VOL_MULT_MAX - t_fraction * (VOL_MULT_MAX - VOL_MULT_MIN)
    return multiplier


def get_strike_price(hour_start_utc: Optional[datetime] = None) -> float:
    """
    Get the BTC open price at the start of the hour (strike price).
    If hour_start_utc is None, uses the current hour.
    """
    if hour_start_utc is None:
        now = datetime.now(timezone.utc)
        hour_start_utc = now.replace(minute=0, second=0, microsecond=0)
    
    timestamp_ms = int(hour_start_utc.timestamp() * 1000)
    
    params = {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "startTime": timestamp_ms,
        "limit": 1
    }
    
    response = requests.get(BINANCE_KLINES_URL, params=params, timeout=5)
    candles = response.json()
    
    if candles and len(candles) > 0:
        # Binance format: [Time, Open, High, Low, Close, Volume, ...]
        return float(candles[0][1])
    
    raise ValueError(f"Could not fetch strike price for {hour_start_utc}")


def get_realized_volatility(lookback_hours: float = VOLATILITY_LOOKBACK_HOURS) -> float:
    """
    Calculate EWMA volatility from 1-minute candles (λ=0.97).
    Returns annualized volatility.
    """
    lookback_minutes = int(lookback_hours * 60)
    
    # Get 1-minute candles
    params = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "limit": lookback_minutes
    }
    
    response = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
    candles = response.json()
    
    if len(candles) < 10:
        raise ValueError("Not enough candles for volatility calculation")
    
    # Calculate log returns from close prices
    closes = [float(c[4]) for c in candles]  # Index 4 is close price
    returns = []
    
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            log_return = math.log(closes[i] / closes[i-1])
            returns.append(log_return)
    
    if len(returns) < 10:
        raise ValueError("Not enough returns for volatility calculation")
    
    # EWMA variance: σ²_t = λ * σ²_{t-1} + (1-λ) * r²_t
    ewma_variance = returns[0] ** 2
    
    for r in returns[1:]:
        ewma_variance = EWMA_LAMBDA * ewma_variance + (1 - EWMA_LAMBDA) * (r ** 2)
    
    std_1min = math.sqrt(ewma_variance)
    
    # Annualize: σ_annual = σ_1min * √(minutes_per_year)
    volatility_annual = std_1min * math.sqrt(MINUTES_PER_YEAR)
    
    # Apply floor only (multiplier is now dynamic in calculate_binary_prices)
    return max(volatility_annual, MIN_VOLATILITY)


def calculate_binary_prices(
    spot_price: float,
    strike_price: float,
    minutes_remaining: float,
    volatility: float,
    use_fat_tails: bool = True
) -> Tuple[float, float]:
    """
    Calculate theoretical prices for UP and DOWN binary tokens.
    
    Uses:
    - Dynamic volatility multiplier (VIX-style): increases as T->0
    - T-Student distribution for fat tails (crypto-appropriate)
    
    For digital/binary options:
    P_up = CDF(d2)    where d2 = (ln(S/K) - σ²T/2) / (σ√T)
    P_down = 1 - P_up
    
    Args:
        spot_price: Current BTC price
        strike_price: Strike price (open at hour start)
        minutes_remaining: Minutes until market closes
        volatility: Base annualized volatility (will be scaled dynamically)
        use_fat_tails: If True, use T-Student CDF instead of Normal
    
    Returns:
        (price_up, price_down) as decimals between 0 and 1
    """
    if minutes_remaining <= 0:
        # Market closed - binary outcome
        return (1.0, 0.0) if spot_price > strike_price else (0.0, 1.0)
    
    if volatility <= 0:
        raise ValueError("Volatility must be positive")
    
    # Dynamic volatility multiplier (VIX-style): higher as T approaches 0
    vol_multiplier = get_dynamic_vol_multiplier(minutes_remaining)
    adjusted_volatility = volatility * vol_multiplier
    
    # Time to expiry in years
    T = minutes_remaining / MINUTES_PER_YEAR
    
    # Calculate d2 with adjusted volatility
    sqrt_T = math.sqrt(T)
    d2 = (math.log(spot_price / strike_price) - (adjusted_volatility ** 2) * T / 2) / (adjusted_volatility * sqrt_T)
    
    # Use T-Student CDF for fat tails (keeps predictions closer to 0.5)
    if use_fat_tails:
        price_up = t_student_cdf_simple(d2, T_STUDENT_DF)
    else:
        price_up = norm_cdf(d2)
    
    price_down = 1.0 - price_up  # Binary tokens sum to 1
    
    return (price_up, price_down)


def get_theoretical_prices(current_btc_price: Optional[float] = None) -> dict:
    """
    Get complete theoretical pricing for current market.
    
    Returns dict with all pricing components:
    - strike: Strike price (open at hour start)
    - spot: Current BTC price
    - volatility: Annualized realized volatility
    - minutes_remaining: Time to expiry
    - price_up: Theoretical UP token price
    - price_down: Theoretical DOWN token price
    """
    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    
    # Calculate minutes remaining (market closes at end of hour)
    minutes_remaining = 60 - now.minute - now.second / 60
    
    # Get strike price
    strike = get_strike_price(hour_start)
    
    # Get current spot price if not provided
    if current_btc_price is None:
        # Get latest 1m candle close
        params = {"symbol": "BTCUSDT", "interval": "1m", "limit": 1}
        response = requests.get(BINANCE_KLINES_URL, params=params, timeout=5)
        candles = response.json()
        current_btc_price = float(candles[0][4])  # Close price
    
    # Get realized volatility (base, before dynamic multiplier)
    volatility_base = get_realized_volatility()
    
    # Get dynamic multiplier for this time
    vol_multiplier = get_dynamic_vol_multiplier(minutes_remaining)
    volatility_adjusted = volatility_base * vol_multiplier
    
    # Calculate theoretical prices (with fat tails)
    price_up, price_down = calculate_binary_prices(
        spot_price=current_btc_price,
        strike_price=strike,
        minutes_remaining=minutes_remaining,
        volatility=volatility_base,  # Base vol, multiplier applied inside
        use_fat_tails=True
    )
    
    return {
        "timestamp": now.isoformat(),
        "hour_start": hour_start.isoformat(),
        "strike": strike,
        "spot": current_btc_price,
        "volatility_base": volatility_base,
        "volatility_base_pct": volatility_base * 100,
        "vol_multiplier": vol_multiplier,
        "volatility_adjusted": volatility_adjusted,
        "volatility_pct": volatility_adjusted * 100,
        "minutes_remaining": minutes_remaining,
        "t_student_df": T_STUDENT_DF,
        "price_up": round(price_up, 4),
        "price_down": round(price_down, 4),
    }


def print_pricing_report():
    """Print a formatted pricing report"""
    try:
        p = get_theoretical_prices()
        
        print("=" * 60)
        print("BLACK-SCHOLES PRICING - Polymarket BTC Hourly")
        print("=" * 60)
        print(f"Timestamp:         {p['timestamp'][:19]} UTC")
        print(f"Market hour:       {p['hour_start'][:13]}:00 UTC")
        print(f"Minutes remaining: {p['minutes_remaining']:.1f}")
        print()
        print(f"Strike (Open):     ${p['strike']:,.2f}")
        print(f"Spot (Current):    ${p['spot']:,.2f}")
        print(f"Difference:        ${p['spot'] - p['strike']:+,.2f} ({(p['spot']/p['strike']-1)*100:+.3f}%)")
        print()
        print(f"Vol base (EWMA):   {p['volatility_base_pct']:.1f}%")
        print(f"Vol multiplier:    x{p['vol_multiplier']:.2f} (dynamic: {VOL_MULT_MIN}-{VOL_MULT_MAX})")
        print(f"Vol adjusted:      {p['volatility_pct']:.1f}%")
        print(f"Fat tails:         T-Student df={p['t_student_df']}")
        print()
        print(f"Theoretical UP:    {p['price_up']:.4f}")
        print(f"Theoretical DOWN:  {p['price_down']:.4f}")
        print("=" * 60)
        
        return p
        
    except Exception as e:
        print(f"Error: {e}")
        return None


if __name__ == "__main__":
    print_pricing_report()
