#!/usr/bin/env python3
"""
polymarket_predictions.py v2.0 — BTC/ETH price prediction indicator from Polymarket.

v2.0 rewrite: Uses slug-based UP/DOWN market fetching instead of keyword search.
Polymarket UP/DOWN markets follow a predictable slug pattern:
  {asset}-updown-{interval}-{aligned_unix_ts}
  e.g. btc-updown-5m-1726600800

Workflow (runs every cycle in GitHub Actions):
  1. Build slug for current 5m/15m/1h/4h/daily UP/DOWN market for BTC/ETH.
  2. Fetch event by slug → extract UP vs DOWN probabilities.
  3. Maintain rolling log of predictions + outcomes + accuracy stats.
  4. Next cycle: check if resolved predictions were correct.

Output: market_predictions.json (read by tvc-terminal.html Predictions panel).

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
from urllib.parse import quote

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

UA = "Mozilla/5.0 tvc-predictions/2.0"
FUSION_DIR = Path.home() / "Claude" / "TVCFusion"
OUT_FILE = FUSION_DIR / "market_predictions.json"

# Confidence threshold
CONFIDENCE_THRESHOLD = 55  # lowered from 65 — UP/DOWN markets are often close to 50/50

# Assets to track
ASSETS = ["BTC", "ETH"]
ASSET_SLUGS = {"BTC": "btc", "ETH": "eth"}

# Binance symbols for price reference
BINANCE_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

# Intervals to try (in order of preference: shorter = more frequent, more data)
INTERVALS = [
    ("5m",  300),
    ("15m", 900),
    ("1h",  3600),
    ("4h",  14400),
    ("1d",  86400),
]


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
                wait = 2 * attempt
                print(f"[http] retry {attempt}/{retries} ({e}) — waiting {wait}s...")
                time.sleep(wait)
    raise last_err


def _pl_tz_offset():
    """Return Poland UTC offset (approximate DST: Apr-Oct = +2, else = +1)."""
    month = datetime.now(timezone.utc).month
    return 2 if 4 <= month <= 10 else 1


def _pl_date_now():
    offset = _pl_tz_offset()
    return (datetime.now(timezone.utc) + timedelta(hours=offset)).strftime("%Y-%m-%d")


def _pl_hour_now():
    offset = _pl_tz_offset()
    return (datetime.now(timezone.utc) + timedelta(hours=offset)).hour


# --- Polymarket slug-based API ---

def _aligned_ts(now_ts, interval_secs):
    """Align timestamp to interval boundary (floor)."""
    return int(now_ts) - (int(now_ts) % interval_secs)


def _build_slugs(asset, now_ts):
    """Build candidate slugs for an asset at current time.
    Returns list of (interval_label, slug) tuples.
    """
    slug_base = ASSET_SLUGS.get(asset, asset.lower())
    results = []
    for label, secs in INTERVALS:
        aligned = _aligned_ts(now_ts, secs)
        slug = f"{slug_base}-updown-{label}-{aligned}"
        results.append((label, slug))
        # Also try previous interval (market may have just expired)
        prev = aligned - secs
        results.append((label, f"{slug_base}-updown-{label}-{prev}"))
    return results


def fetch_updown_market(asset):
    """Fetch UP/DOWN probabilities for an asset from Polymarket.
    Tries multiple intervals, returns first successful match.
    Returns dict with {interval, up_pct, down_pct, slug, question, end_date} or None.
    """
    now_ts = time.time()
    slugs = _build_slugs(asset, now_ts)

    for interval_label, slug in slugs:
        # Try events endpoint first (events contain nested markets)
        try:
            url = f"https://gamma-api.polymarket.com/events?slug={quote(slug)}"
            data = _get_json(url, timeout=10, retries=1)
            if isinstance(data, list) and data:
                event = data[0]
                markets = event.get("markets", [])
                result = _parse_markets(markets, asset, interval_label, slug)
                if result:
                    return result
        except Exception:
            pass

        # Fallback: markets endpoint directly
        try:
            url = f"https://gamma-api.polymarket.com/markets?slug={quote(slug)}"
            data = _get_json(url, timeout=10, retries=1)
            if isinstance(data, list) and data:
                result = _parse_markets(data, asset, interval_label, slug)
                if result:
                    return result
            elif isinstance(data, dict) and data.get("question"):
                # Single market response
                result = _parse_single_market(data, asset, interval_label, slug)
                if result:
                    return result
        except Exception:
            pass

    # Fallback: keyword search (legacy, less reliable)
    return _keyword_search_fallback(asset)


def _parse_markets(markets, asset, interval_label, slug):
    """Parse a list of markets from an event to extract UP/DOWN probabilities."""
    up_pct = None
    down_pct = None
    question = None
    end_date = None

    for m in markets:
        q = (m.get("question") or m.get("groupItemTitle") or "").lower()
        outcomes = m.get("outcomePrices") or m.get("outcome_prices", "")
        try:
            if isinstance(outcomes, str):
                prices = json.loads(outcomes) if outcomes else []
            else:
                prices = outcomes
            yes_price = float(prices[0]) if prices else None
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            yes_price = None

        if yes_price is None:
            continue

        pct = round(yes_price * 100)
        end_date = end_date or m.get("endDate") or m.get("end_date_iso")

        if "up" in q or "higher" in q or "increase" in q or "above" in q:
            up_pct = pct
            question = m.get("question") or question
        elif "down" in q or "lower" in q or "decrease" in q or "below" in q:
            down_pct = pct
            question = question or m.get("question")

    # If we found at least one direction
    if up_pct is not None or down_pct is not None:
        # Infer missing direction
        if up_pct is not None and down_pct is None:
            down_pct = 100 - up_pct
        elif down_pct is not None and up_pct is None:
            up_pct = 100 - down_pct

        return {
            "interval": interval_label,
            "up_pct": up_pct,
            "down_pct": down_pct,
            "slug": slug,
            "question": question,
            "end_date": end_date,
            "source": "slug",
        }

    # Also check if it's a binary YES/NO about price going up
    if len(markets) == 1:
        return _parse_single_market(markets[0], asset, interval_label, slug)

    return None


def _parse_single_market(m, asset, interval_label, slug):
    """Parse a single binary market (YES/NO) as UP/DOWN signal."""
    q = (m.get("question") or "").lower()
    outcomes = m.get("outcomePrices") or m.get("outcome_prices", "")
    try:
        if isinstance(outcomes, str):
            prices = json.loads(outcomes) if outcomes else []
        else:
            prices = outcomes
        yes_price = float(prices[0]) if prices else None
    except (json.JSONDecodeError, IndexError, TypeError, ValueError):
        yes_price = None

    if yes_price is None:
        return None

    yes_pct = round(yes_price * 100)
    no_pct = 100 - yes_pct

    # Determine framing
    positive = any(w in q for w in ["up", "above", "higher", "increase", "rise"])
    negative = any(w in q for w in ["down", "below", "lower", "decrease", "fall"])

    if positive:
        up_pct, down_pct = yes_pct, no_pct
    elif negative:
        up_pct, down_pct = no_pct, yes_pct
    else:
        up_pct, down_pct = yes_pct, no_pct  # assume YES = UP

    return {
        "interval": interval_label,
        "up_pct": up_pct,
        "down_pct": down_pct,
        "slug": slug,
        "question": m.get("question"),
        "end_date": m.get("endDate") or m.get("end_date_iso"),
        "source": "slug-single",
    }


def _keyword_search_fallback(asset):
    """Legacy keyword search as a last resort."""
    queries = {
        "BTC": ["bitcoin price up down", "btc updown", "bitcoin daily"],
        "ETH": ["ethereum price up down", "eth updown", "ethereum daily"],
    }
    for q in queries.get(asset, []):
        try:
            url = f"https://gamma-api.polymarket.com/markets?limit=5&active=true&closed=false&_q={quote(q)}"
            data = _get_json(url, timeout=10, retries=1)
            if isinstance(data, list):
                for m in data:
                    question = (m.get("question") or "").lower()
                    asset_lower = asset.lower()
                    if asset_lower not in question and {"BTC": "bitcoin", "ETH": "ethereum"}.get(asset, "") not in question:
                        continue
                    result = _parse_single_market(m, asset, "search", f"search-{asset_lower}")
                    if result:
                        result["source"] = "keyword-search"
                        return result
        except Exception:
            pass
    return None


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
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            pass
    return {"generated_at": None, "current": {}, "stats": {}, "log": []}


def save_state(state):
    state["generated_at"] = datetime.now(timezone.utc).isoformat()
    state["version"] = "2.0"
    OUT_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# --- Outcome checking ---

def check_outcomes(state):
    """Resolve pending predictions by checking if price moved in predicted direction."""
    updated = 0
    for entry in state.get("log", []):
        if entry.get("outcome") != "pending":
            continue

        price_start = entry.get("price_start")
        if not price_start:
            continue

        # Check if enough time has passed
        snap_time = entry.get("snapshot_time", "")
        if snap_time:
            try:
                snap_dt = datetime.fromisoformat(snap_time.replace("Z", "+00:00"))
                hours_since = (datetime.now(timezone.utc) - snap_dt).total_seconds() / 3600
                # For short intervals, resolve faster
                interval = entry.get("interval", "1d")
                min_hours = {"5m": 0.2, "15m": 0.5, "1h": 1.5, "4h": 5, "1d": 20}.get(interval, 20)
                if hours_since < min_hours:
                    continue
            except (ValueError, TypeError):
                pass

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
            actual_correct = None

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
        print(f"[outcome] {entry.get('date','')} {asset} {direction} → "
              f"{entry['outcome']} ({price_change:+.2f}%)")

    if updated:
        print(f"[outcome] Resolved {updated} pending predictions")
    return updated


def compute_stats(log):
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
    print("[polymarket_predictions v2.0] Starting...")
    print("=" * 60)

    state = load_state()

    # 1. Resolve pending outcomes
    check_outcomes(state)

    # 2. Fetch current UP/DOWN markets for each asset
    for asset in ASSETS:
        market = fetch_updown_market(asset)
        price = get_binance_price(asset)

        if market:
            up_pct = market["up_pct"]
            down_pct = market["down_pct"]

            if up_pct > down_pct:
                direction = "UP"
                pct = up_pct
            elif down_pct > up_pct:
                direction = "DOWN"
                pct = down_pct
            else:
                direction = "NEUTRAL"
                pct = 50

            current_entry = {
                "direction": direction,
                "pct": pct,
                "up_pct": up_pct,
                "down_pct": down_pct,
                "interval": market.get("interval", "?"),
                "market_question": market.get("question"),
                "slug": market.get("slug"),
                "source": market.get("source", "slug"),
                "price_at_snapshot": round(price, 2) if price else None,
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
                "active": pct >= CONFIDENCE_THRESHOLD,
            }
            state["current"][asset] = current_entry

            print(f"[market] {asset}: {direction} {pct}% "
                  f"(UP={up_pct}% DOWN={down_pct}% "
                  f"interval={market['interval']} source={market['source']})")
        else:
            # No market found — keep stale current if exists, mark inactive
            if asset in state.get("current", {}):
                state["current"][asset]["active"] = False
                state["current"][asset]["stale"] = True
            else:
                state["current"][asset] = {
                    "direction": "NEUTRAL",
                    "pct": 50,
                    "up_pct": 50,
                    "down_pct": 50,
                    "active": False,
                    "stale": True,
                    "price_at_snapshot": round(price, 2) if price else None,
                    "snapshot_time": datetime.now(timezone.utc).isoformat(),
                }
            print(f"[market] {asset}: No UP/DOWN market found")

        # Update live price
        if price and asset in state.get("current", {}):
            snap_price = state["current"][asset].get("price_at_snapshot")
            if snap_price:
                change = ((price - snap_price) / snap_price) * 100
                state["current"][asset]["live_price"] = round(price, 2)
                state["current"][asset]["live_change_pct"] = round(change, 2)

    # 3. Log snapshot (once per 15 min max to avoid spam)
    now_iso = datetime.now(timezone.utc).isoformat()
    last_log_time = None
    if state.get("log"):
        last_log_time = state["log"][-1].get("snapshot_time")

    should_log = True
    if last_log_time:
        try:
            last_dt = datetime.fromisoformat(last_log_time.replace("Z", "+00:00"))
            minutes_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            should_log = minutes_since >= 14
        except (ValueError, TypeError):
            pass

    if should_log:
        for asset in ASSETS:
            cur = state.get("current", {}).get(asset, {})
            if cur.get("active") and not cur.get("stale"):
                log_entry = {
                    "date": _pl_date_now(),
                    "asset": asset,
                    "direction": cur.get("direction", "NEUTRAL"),
                    "pct": cur.get("pct", 50),
                    "up_pct": cur.get("up_pct", 50),
                    "down_pct": cur.get("down_pct", 50),
                    "interval": cur.get("interval", "?"),
                    "price_start": cur.get("price_at_snapshot"),
                    "price_end": None,
                    "price_change_pct": None,
                    "outcome": "pending",
                    "snapshot_time": now_iso,
                }
                state["log"].append(log_entry)

    # 4. Trim log (keep last 200 entries)
    state["log"] = state["log"][-200:]

    # 5. Compute stats
    state["stats"] = compute_stats(state["log"])

    # 6. Save
    save_state(state)
    print(f"\n[done] Written to {OUT_FILE}")
    stats = state["stats"]
    print(f"       Accuracy: overall={stats.get('overall_accuracy', 0)}%, "
          f"confident={stats.get('confident_accuracy', 0)}%, "
          f"total={stats.get('total_predictions', 0)}")


if __name__ == "__main__":
    main()
