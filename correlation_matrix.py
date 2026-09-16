#!/usr/bin/env python3
"""
correlation_matrix.py — Pearson correlation matrix for 14 assets across 4 timeframes.

Assets:
  10 crypto (Binance daily klines): BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, DOT
  4 traditional (Yahoo Finance):    GOLD, SILVER, OIL, SP500

Timeframes: 7d, 14d, 30d, 90d

Output: correlation_data.json
  {
    "generated_at": "...",
    "7d":  { "assets": [...], "matrix": [[...]] },
    "14d": { ... },
    "30d": { ... },
    "90d": { ... }
  }

Runs once daily in GitHub Actions (before auto_fusion.py).
auto_fusion.py reads correlation_data.json and embeds it as "correlation_matrix".

Dependencies: yfinance (pip install yfinance)
"""

import json
import math
import ssl
import time
import sys
import urllib.request as ur
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

UA = "Mozilla/5.0 tvc-correlation/1.0"
FUSION_DIR = Path.home() / "Claude" / "TVCFusion"
OUT_FILE = FUSION_DIR / "correlation_data.json"

# --- Assets ---
CRYPTO_ASSETS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT"]
CRYPTO_SYMBOLS = {t: f"{t}USDT" for t in CRYPTO_ASSETS}

TRAD_ASSETS = ["GOLD", "SILVER", "OIL", "SP500"]
TRAD_YAHOO = {
    "GOLD":   "GC=F",
    "SILVER": "SI=F",
    "OIL":    "CL=F",
    "SP500":  "^GSPC",
}

ALL_ASSETS = CRYPTO_ASSETS + TRAD_ASSETS
TIMEFRAMES = [7, 14, 30, 90]


def _get_json(url, timeout=15, retries=3):
    req = ur.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with ur.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = 3 * (2 ** (attempt - 1))
                print(f"[http] retry {attempt}/{retries} ({e}) — waiting {wait}s...")
                time.sleep(wait)
    raise last_err


# --- Fetch daily close prices ---

def fetch_crypto_daily(days=92):
    """Fetch daily close prices from Binance klines for all crypto assets.
    Returns {ticker: [(date_str, close_price), ...]} sorted oldest→newest.
    We fetch `days+2` to have margin for timezone edge cases.
    """
    result = {}
    # Binance klines: interval=1d, limit=days
    for ticker, symbol in CRYPTO_SYMBOLS.items():
        try:
            url = (
                f"https://data-api.binance.vision/api/v3/klines"
                f"?symbol={symbol}&interval=1d&limit={days + 2}"
            )
            data = _get_json(url)
            prices = []
            for candle in data:
                # candle: [openTime, open, high, low, close, volume, closeTime, ...]
                ts = candle[0] / 1000
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                close = float(candle[4])
                prices.append((dt, close))
            result[ticker] = prices
            print(f"[crypto] {ticker}: {len(prices)} daily candles")
        except Exception as e:
            print(f"[crypto] {ticker} FAILED: {e}")
            result[ticker] = []
    return result


def fetch_trad_daily(days=92):
    """Fetch daily close prices from Yahoo Finance for traditional assets.
    Returns {ticker: [(date_str, close_price), ...]} sorted oldest→newest.
    """
    try:
        import yfinance as yf
    except ImportError:
        print("[trad] yfinance not installed — pip install yfinance")
        return {t: [] for t in TRAD_ASSETS}

    result = {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 5)

    for label, yahoo_sym in TRAD_YAHOO.items():
        try:
            ticker = yf.Ticker(yahoo_sym)
            df = ticker.history(start=start.strftime("%Y-%m-%d"),
                                end=end.strftime("%Y-%m-%d"),
                                interval="1d")
            prices = []
            for idx, row in df.iterrows():
                dt = idx.strftime("%Y-%m-%d")
                prices.append((dt, float(row["Close"])))
            result[label] = prices
            print(f"[trad] {label} ({yahoo_sym}): {len(prices)} daily points")
        except Exception as e:
            print(f"[trad] {label} FAILED: {e}")
            result[label] = []
    return result


# --- Compute daily returns ---

def daily_returns(prices):
    """Convert [(date, close), ...] → {date: pct_return}.
    Return = (close_today - close_yesterday) / close_yesterday
    """
    ret = {}
    for i in range(1, len(prices)):
        d_prev, p_prev = prices[i - 1]
        d_curr, p_curr = prices[i]
        if p_prev > 0:
            ret[d_curr] = (p_curr - p_prev) / p_prev
    return ret


# --- Pearson correlation ---

def pearson(xs, ys):
    """Compute Pearson correlation between two lists of equal length.
    Returns float in [-1, 1] or 0.0 if insufficient data.
    """
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


# --- Build matrix for a given timeframe ---

def build_matrix(all_returns, num_days):
    """Given {asset: {date: return}} and a timeframe (num_days),
    compute the NxN Pearson correlation matrix using the most recent num_days of shared dates.
    """
    # Find dates shared by ALL assets (intersection)
    date_sets = [set(r.keys()) for r in all_returns.values() if r]
    if not date_sets:
        return None
    common_dates = sorted(set.intersection(*date_sets))

    # Take the last `num_days` common dates
    if len(common_dates) < 5:
        print(f"[matrix] Only {len(common_dates)} common dates — need at least 5")
        return None
    dates = common_dates[-num_days:] if len(common_dates) >= num_days else common_dates

    assets = ALL_ASSETS
    n = len(assets)
    matrix = [[0.0] * n for _ in range(n)]

    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[i][j] = 1.0
            elif j > i:
                ri = all_returns.get(assets[i], {})
                rj = all_returns.get(assets[j], {})
                xs = [ri.get(d, 0.0) for d in dates]
                ys = [rj.get(d, 0.0) for d in dates]
                corr = pearson(xs, ys)
                matrix[i][j] = round(corr, 3)
                matrix[j][i] = round(corr, 3)

    return {"assets": assets, "matrix": matrix, "data_points": len(dates)}


# --- Main ---

def main():
    print("=" * 60)
    print("[correlation_matrix] Starting...")
    print("=" * 60)

    # 1. Fetch prices
    crypto_prices = fetch_crypto_daily(days=TIMEFRAMES[-1] + 5)
    trad_prices = fetch_trad_daily(days=TIMEFRAMES[-1] + 5)

    # 2. Compute daily returns for each asset
    all_returns = {}
    for ticker in CRYPTO_ASSETS:
        all_returns[ticker] = daily_returns(crypto_prices.get(ticker, []))
    for ticker in TRAD_ASSETS:
        all_returns[ticker] = daily_returns(trad_prices.get(ticker, []))

    # Check we have enough data
    counts = {t: len(r) for t, r in all_returns.items()}
    print(f"[returns] Data points per asset: {counts}")

    empty = [t for t, c in counts.items() if c < 5]
    if empty:
        print(f"[WARNING] Insufficient data for: {empty}")

    # 3. Build matrix per timeframe
    output = {"generated_at": datetime.now(timezone.utc).isoformat()}

    for tf in TIMEFRAMES:
        key = f"{tf}d"
        m = build_matrix(all_returns, tf)
        if m:
            output[key] = m
            print(f"[matrix] {key}: {m['data_points']} data points ✓")
        else:
            print(f"[matrix] {key}: SKIPPED (insufficient data)")

    # 4. Write output
    OUT_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"\n[done] Written to {OUT_FILE}")
    print(f"       Timeframes: {[k for k in output if k != 'generated_at']}")


if __name__ == "__main__":
    main()
