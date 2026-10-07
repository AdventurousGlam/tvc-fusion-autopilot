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

v2.0 (2026-10-06): pandas refactor. Prices → one DataFrame (index=date, columns=assets),
returns via pct_change(), alignment via dropna(), correlation via DataFrame.corr().
Replaces ~60 lines of hand-rolled loops (daily_returns / pearson / nested matrix fill)
with 4 vectorised calls. Output JSON format unchanged; values identical to v1
(sample Pearson — normalisation cancels, so numpy's and the manual formula agree).

Dependencies: pandas, yfinance (pip install pandas yfinance)
"""

import json
import ssl
import time
import sys
import urllib.request as ur
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

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


# --- pandas pipeline (v2.0) ---

def to_price_frame(crypto_prices, trad_prices):
    """{ticker: [(date, close), ...]} → DataFrame(index=date, columns=ALL_ASSETS, values=close).
    Assets with no data become all-NaN columns (kept so the matrix shape stays 14×14)."""
    series = {}
    for ticker in ALL_ASSETS:
        rows = crypto_prices.get(ticker) or trad_prices.get(ticker) or []
        s = pd.Series({d: p for d, p in rows}, dtype="float64", name=ticker)
        series[ticker] = s
    df = pd.DataFrame(series).sort_index()
    df.index.name = "date"
    return df[ALL_ASSETS]


def daily_returns_frame(prices_df):
    """Close prices → simple daily returns, one column per asset.
    Each asset's return is computed against ITS OWN previous observation (so a Monday
    equity close is compared with Friday, not with a NaN weekend) — same as v1, and the
    reason we don't call pct_change() on the whole frame at once."""
    cols = {a: prices_df[a].dropna().pct_change().iloc[1:] for a in prices_df.columns}
    return pd.DataFrame(cols).sort_index()[list(prices_df.columns)]


def build_matrix(returns_df, num_days):
    """Pearson correlation over the last `num_days` dates shared by every asset that has data.
    Mirrors v1 semantics: intersection of dates, trailing window, assets without data get 0.0."""
    live = [a for a in ALL_ASSETS if returns_df[a].notna().any()]
    if not live:
        return None
    aligned = returns_df[live].dropna(how="any")      # intersection of dates across live assets
    if len(aligned) < 5:
        print(f"[matrix] Only {len(aligned)} common dates — need at least 5")
        return None
    window = aligned.tail(num_days)
    corr = window.corr(method="pearson")               # NxN for live assets
    corr = corr.reindex(index=ALL_ASSETS, columns=ALL_ASSETS).fillna(0.0)
    for a in ALL_ASSETS:
        corr.loc[a, a] = 1.0
    matrix = corr.round(3).values.tolist()
    return {"assets": ALL_ASSETS, "matrix": matrix, "data_points": int(len(window))}


# --- Main ---

def main():
    print("=" * 60)
    print("[correlation_matrix] Starting... (pandas v2.0)")
    print("=" * 60)

    # 1. Fetch prices → one DataFrame
    crypto_prices = fetch_crypto_daily(days=TIMEFRAMES[-1] + 5)
    trad_prices = fetch_trad_daily(days=TIMEFRAMES[-1] + 5)
    prices_df = to_price_frame(crypto_prices, trad_prices)

    # 2. Daily returns (vectorised)
    returns_df = daily_returns_frame(prices_df)
    counts = returns_df.notna().sum().to_dict()
    print(f"[returns] Data points per asset: {counts}")

    empty = [t for t, c in counts.items() if c < 5]
    if empty:
        print(f"[WARNING] Insufficient data for: {empty}")

    # 3. Build matrix per timeframe
    output = {"generated_at": datetime.now(timezone.utc).isoformat()}

    for tf in TIMEFRAMES:
        key = f"{tf}d"
        m = build_matrix(returns_df, tf)
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
