#!/usr/bin/env python3
"""
polymarket_predictions.py — Daily BTC/ETH price prediction indicator from Polymarket.

Workflow (runs every cycle in GitHub Actions, but only snapshots once daily at ~18:00 PL):
  1. Search Polymarket gamma API for active BTC/ETH daily price direction markets.
  2. Snapshot current probability (YES/NO) → direction signal when ≥65% on one side.
  3. Next day: check if prediction was correct (compare prices).
  4. Maintain rolling log of predictions + outcomes + accuracy stats.

Output: market_predictions.json
  {
    "generated_at": "...",
    "current": {
      "BTC": {"direction": "UP", "pct": 82, "market_question": "...", "price_at_snapshot": 65400, "snapshot_time": "..."},
      "ETH": {"direction": "DOWN", "pct": 71, ...}
    },
    "stats": {
      "overall_accuracy": 65,
      "confident_accuracy": 82,   // only predictions with ≥65%
      "total_predictions": 45,
      "per_asset": {"BTC": {"accuracy": 60, "total": 25}, "ETH": {"accuracy": 71, "total": 20}}
    },
    "log": [
      {"date": "2026-09-14", "asset": "BTC", "direction": "UP", "pct": 78,
       "price_start": 65400, "price_end": 66100, "outcome": "correct"},
      ...
    ]
  }

auto_fusion.py reads market_predictions.json and embeds it as "market_predictions".

Dependencies: none beyond stdlib (uses urllib only).
"""

import json
import os
import ssl
import sys
import time
import urllib.request as ur
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

UA = "Mozilla/5.0 tvc-predictions/1.0"
FUSION_DIR = Path.home() / "Claude" / "TVCFusion"
OUT_FILE = FUSION_DIR / "market_predictions.json"

# Poland timezone offset (CET=+1, CEST=+2). We approximate: Apr-Oct → +2, else → +1.
def _pl_hour_now():
    """Return current hour in Polish time (approximate DST)."""
    utc = datetime.now(timezone.utc)
    month = utc.month
    offset = 2 if 4 <= month <= 10 else 1
    return (utc + timedelta(hours=offset)).hour

def _pl_date_now():
    """Return current date string in Polish time."""
    utc = datetime.now(timezone.utc)
    month = utc.month
    offset = 2 if 4 <= month <= 10 else 1
    return (utc + timedelta(hours=offset)).strftime("%Y-%m-%d")


SNAPSHOT_HOUR = 18  # snapshot at 18:00 Polish time
SNAPSHOT_WINDOW = 1  # accept snapshot if within ±1 hour

# Confidence threshold — prediction is "active" / "confident" when ≥ this
CONFIDENCE_THRESHOLD = 65

# Assets to track
ASSETS = ["BTC", "ETH"]

# Binance symbols for price reference
BINANCE_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

# Search terms for finding relevant Polymarket markets
SEARCH_PATTERNS = {
    "BTC": ["bitcoin price", "btc price", "bitcoin above", "bitcoin below",
            "bitcoin increase", "bitcoin decrease", "btc above", "btc daily"],
    "ETH": ["ethereum price", "eth price", "ethereum above", "ethereum below",
            "ether price", "ethereum increase", "eth above", "eth daily"],
}


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


# --- Polymarket API ---

def search_markets(query, limit=5):
    """Search Polymarket gamma API for markets matching query."""
    from urllib.parse import quote
    url = f"https://gamma-api.polymarket.com/markets?limit={limit}&active=true&closed=false&_q={quote(query)}"
    try:
        data = _get_json(url, timeout=10)
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"[polymarket] Search failed for '{query}': {e}")
        return []


def find_best_market(asset):
    """Find the most relevant active daily/short-term market for an asset.
    Returns dict with {question, yes_price, no_price, condition_id, end_date} or None.
    """
    patterns = SEARCH_PATTERNS.get(asset, [])
    candidates = []

    for pattern in patterns:
        markets = search_markets(pattern, limit=10)
        for m in markets:
            question = (m.get("question") or "").lower()
            # Filter: must mention the asset and be about price/direction
            asset_lower = asset.lower()
            asset_names = {"BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "eth", "ether"]}
            names = asset_names.get(asset, [asset_lower])

            if not any(n in question for n in names):
                continue

            # Prefer markets about price direction, daily, short-term
            price_related = any(w in question for w in
                                ["price", "above", "below", "increase", "decrease",
                                 "higher", "lower", "rise", "fall", "close", "reach"])
            if not price_related:
                continue

            # Extract prices
            outcomes = m.get("outcomePrices", "")
            try:
                if isinstance(outcomes, str):
                    prices = json.loads(outcomes)
                else:
                    prices = outcomes
                yes_price = float(prices[0]) if prices else None
                no_price = float(prices[1]) if len(prices) > 1 else None
            except (json.JSONDecodeError, IndexError, TypeError, ValueError):
                yes_price = None
                no_price = None

            if yes_price is None:
                continue

            # Score: prefer markets ending sooner (daily > weekly > monthly)
            end_date = m.get("endDate") or m.get("end_date_iso") or ""
            candidates.append({
                "question": m.get("question", ""),
                "condition_id": m.get("conditionId") or m.get("condition_id", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "end_date": end_date,
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("liquidity", 0) or 0),
            })

    if not candidates:
        return None

    # Sort by volume (most liquid = most reliable signal)
    candidates.sort(key=lambda c: c["volume"], reverse=True)
    best = candidates[0]
    print(f"[polymarket] {asset}: Found '{best['question']}' "
          f"(YES={best['yes_price']:.0%}, vol={best['volume']:.0f})")
    return best


def get_binance_price(asset):
    """Fetch current price from Binance."""
    symbol = BINANCE_SYMBOLS.get(asset)
    if not symbol:
        return None
    try:
        url = f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}"
        data = _get_json(url, timeout=10, retries=2)
        return float(data.get("price", 0))
    except Exception as e:
        print(f"[binance] Price fetch failed for {asset}: {e}")
        return None


# --- State management ---

def load_state():
    """Load existing predictions state from file."""
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            pass
    return {"generated_at": None, "current": {}, "stats": {}, "log": []}


def save_state(state):
    """Save predictions state to file."""
    state["generated_at"] = datetime.now(timezone.utc).isoformat()
    OUT_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def interpret_direction(market):
    """Interpret market probability as UP/DOWN/NEUTRAL direction.
    Returns (direction, confidence_pct).
    """
    if not market:
        return "NEUTRAL", 50

    yes_pct = round(market["yes_price"] * 100)
    no_pct = 100 - yes_pct

    # Interpret: if "above" or positive framing → YES=UP
    q = (market.get("question") or "").lower()
    positive_framing = any(w in q for w in ["above", "increase", "higher", "rise", "reach", "hit"])
    negative_framing = any(w in q for w in ["below", "decrease", "lower", "fall", "drop"])

    if positive_framing:
        if yes_pct >= CONFIDENCE_THRESHOLD:
            return "UP", yes_pct
        elif no_pct >= CONFIDENCE_THRESHOLD:
            return "DOWN", no_pct
        else:
            return "NEUTRAL", max(yes_pct, no_pct)
    elif negative_framing:
        if yes_pct >= CONFIDENCE_THRESHOLD:
            return "DOWN", yes_pct
        elif no_pct >= CONFIDENCE_THRESHOLD:
            return "UP", no_pct
        else:
            return "NEUTRAL", max(yes_pct, no_pct)
    else:
        # Ambiguous framing — use YES as bullish signal
        if yes_pct >= CONFIDENCE_THRESHOLD:
            return "UP", yes_pct
        elif no_pct >= CONFIDENCE_THRESHOLD:
            return "DOWN", no_pct
        else:
            return "NEUTRAL", max(yes_pct, no_pct)


# --- Outcome checking ---

def check_yesterday_outcomes(state):
    """Check if yesterday's predictions were correct by comparing prices.
    Updates log entries that have outcome='pending'.
    """
    updated = 0
    for entry in state.get("log", []):
        if entry.get("outcome") != "pending":
            continue

        # Only resolve if we have both prices
        price_start = entry.get("price_start")
        if not price_start:
            continue

        # Check if enough time has passed (at least 20 hours since snapshot)
        snap_time = entry.get("snapshot_time", "")
        if snap_time:
            try:
                snap_dt = datetime.fromisoformat(snap_time.replace("Z", "+00:00"))
                hours_since = (datetime.now(timezone.utc) - snap_dt).total_seconds() / 3600
                if hours_since < 20:
                    continue  # too early to resolve
            except (ValueError, TypeError):
                pass

        # Get current/end price
        asset = entry.get("asset", "BTC")
        price_end = get_binance_price(asset)
        if not price_end:
            continue

        direction = entry.get("direction", "NEUTRAL")
        if direction == "UP":
            actual_correct = price_end > price_start
        elif direction == "DOWN":
            actual_correct = price_end < price_start
        else:
            actual_correct = None  # NEUTRAL — no prediction made

        entry["price_end"] = round(price_end, 2)
        if actual_correct is None:
            entry["outcome"] = "neutral"
        elif actual_correct:
            entry["outcome"] = "correct"
        else:
            entry["outcome"] = "wrong"

        price_change = ((price_end - price_start) / price_start) * 100
        entry["price_change_pct"] = round(price_change, 2)
        updated += 1
        print(f"[outcome] {entry['date']} {asset} {direction} → "
              f"{entry['outcome']} ({price_change:+.2f}%)")

    if updated:
        print(f"[outcome] Resolved {updated} pending predictions")
    return updated


def compute_stats(log):
    """Compute accuracy statistics from the log."""
    resolved = [e for e in log if e.get("outcome") in ("correct", "wrong")]
    confident = [e for e in resolved if e.get("pct", 0) >= CONFIDENCE_THRESHOLD]

    overall_acc = 0
    if resolved:
        correct = sum(1 for e in resolved if e["outcome"] == "correct")
        overall_acc = round(correct / len(resolved) * 100)

    confident_acc = 0
    if confident:
        correct_c = sum(1 for e in confident if e["outcome"] == "correct")
        confident_acc = round(correct_c / len(confident) * 100)

    per_asset = {}
    for asset in ASSETS:
        asset_resolved = [e for e in resolved if e.get("asset") == asset]
        if asset_resolved:
            correct_a = sum(1 for e in asset_resolved if e["outcome"] == "correct")
            per_asset[asset] = {
                "accuracy": round(correct_a / len(asset_resolved) * 100),
                "total": len(asset_resolved),
            }

    return {
        "overall_accuracy": overall_acc,
        "confident_accuracy": confident_acc,
        "total_predictions": len(resolved),
        "confident_predictions": len(confident),
        "per_asset": per_asset,
    }


# --- Main ---

def main():
    print("=" * 60)
    print("[polymarket_predictions] Starting...")
    print("=" * 60)

    state = load_state()

    # 1. Resolve any pending outcomes from previous predictions
    check_yesterday_outcomes(state)

    # 2. Check if we should take a new snapshot (once daily around SNAPSHOT_HOUR PL time)
    pl_hour = _pl_hour_now()
    pl_date = _pl_date_now()
    already_snapped = any(
        e.get("date") == pl_date for e in state.get("log", [])
    )

    should_snapshot = (
        abs(pl_hour - SNAPSHOT_HOUR) <= SNAPSHOT_WINDOW
        and not already_snapped
    )

    if should_snapshot:
        print(f"[snapshot] Taking daily snapshot (PL time ~{pl_hour}:00, date={pl_date})")

        for asset in ASSETS:
            # Find best market
            market = find_best_market(asset)
            direction, pct = interpret_direction(market)
            price = get_binance_price(asset)

            current_entry = {
                "direction": direction,
                "pct": pct,
                "market_question": market["question"] if market else None,
                "price_at_snapshot": round(price, 2) if price else None,
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
                "active": pct >= CONFIDENCE_THRESHOLD,
            }
            state["current"][asset] = current_entry

            # Add to log
            log_entry = {
                "date": pl_date,
                "asset": asset,
                "direction": direction,
                "pct": pct,
                "price_start": round(price, 2) if price else None,
                "price_end": None,
                "price_change_pct": None,
                "outcome": "pending",
                "market_question": market["question"] if market else None,
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
            }
            state["log"].append(log_entry)
            status = "ACTIVE" if pct >= CONFIDENCE_THRESHOLD else "inactive"
            print(f"[snapshot] {asset}: {direction} {pct}% ({status})"
                  f" @ ${price:,.0f}" if price else f"[snapshot] {asset}: {direction} {pct}% ({status})")

    elif already_snapped:
        print(f"[snapshot] Already snapped today ({pl_date}) — updating current prices only")
        # Update current prices without new snapshot
        for asset in ASSETS:
            price = get_binance_price(asset)
            if asset in state.get("current", {}) and price:
                snap_price = state["current"][asset].get("price_at_snapshot")
                if snap_price:
                    change = ((price - snap_price) / snap_price) * 100
                    state["current"][asset]["live_price"] = round(price, 2)
                    state["current"][asset]["live_change_pct"] = round(change, 2)
    else:
        print(f"[snapshot] Not snapshot time (PL hour={pl_hour}, target={SNAPSHOT_HOUR}±{SNAPSHOT_WINDOW})")
        # Still update live prices
        for asset in ASSETS:
            price = get_binance_price(asset)
            if asset in state.get("current", {}) and price:
                snap_price = state["current"][asset].get("price_at_snapshot")
                if snap_price:
                    change = ((price - snap_price) / snap_price) * 100
                    state["current"][asset]["live_price"] = round(price, 2)
                    state["current"][asset]["live_change_pct"] = round(change, 2)

    # 3. Trim log to last 60 entries (30 days × 2 assets)
    state["log"] = state["log"][-60:]

    # 4. Compute stats
    state["stats"] = compute_stats(state["log"])

    # 5. Save
    save_state(state)
    print(f"\n[done] Written to {OUT_FILE}")
    stats = state["stats"]
    print(f"       Accuracy: overall={stats.get('overall_accuracy', 0)}%, "
          f"confident={stats.get('confident_accuracy', 0)}%, "
          f"total={stats.get('total_predictions', 0)}")


if __name__ == "__main__":
    main()
