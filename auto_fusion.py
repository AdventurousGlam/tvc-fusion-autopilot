#!/usr/bin/env python3
"""
auto_fusion.py — Self-running fusion WITHOUT Claude credits.

Generuje fusion decisions automatycznie na podstawie:
- Binance prices (free, no auth)
- Farside.co.uk BTC/ETH ETF flows (public JSON)
- Fear & Greed Index (alternative.me)
- BTC dominance (CoinGecko)
- Simple algorithmic scoring (no LLM)

Uruchomienie:
  python3 auto_fusion.py               # jednorazowo
  python3 auto_fusion.py --loop 4h    # co 4 godziny (podobnie jak scheduled fusion)

Output:
  ~/Claude/TVCFusion/fusion_YYYY-MM-DD.json  (nadpisuje jeśli istnieje)
  Uploads to Gist via paper_bot.py upload
"""

import json
import os
import sys
import time
import ssl
import argparse
import urllib.request as ur
from datetime import datetime, timezone
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

UA = "Mozilla/5.0 tvc-auto-fusion/1.0"
FUSION_DIR = Path.home() / "Claude" / "TVCFusion"

# Ticker → Binance symbol
BINANCE = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "SUI": "SUIUSDT",
}


def _get_json(url, timeout=15, retries=3):
    """Fetch JSON z retry+backoff — GitHub-hosted runnery czasem mają przejściowe
    problemy sieciowe (DNS/timeout na pojedynczym połączeniu), które same znikają
    po kilku sekundach. Bez retry taki jeden przejściowy timeout wywalał CAŁY cykl
    (exit code 1, zero danych na ten przebieg), mimo że kolejna próba zwykle by się
    udała. 3 próby z rosnącym odstępem (3s, 6s, 12s) kosztują góra ~20s dodatkowo,
    ale zamieniają "cykl całkiem padł" na "cykl przeżył krótką awarię sieci"."""
    req = ur.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with ur.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = 3 * (2 ** (attempt - 1))  # 3s, 6s, 12s
                print(f"[http] {url.split('?')[0]} próba {attempt}/{retries} nieudana ({e}) — czekam {wait}s...")
                time.sleep(wait)
    raise last_err


def fetch_prices():
    """Batch fetch 24h ticker data for all tickers."""
    from urllib.parse import quote
    # Compact JSON (no spaces) + URL-encode żeby uniknąć control characters w URL
    symbols_json = json.dumps(list(BINANCE.values()), separators=(",", ":"))
    symbols_encoded = quote(symbols_json)
    url = f"https://data-api.binance.vision/api/v3/ticker/24hr?symbols={symbols_encoded}"
    data = _get_json(url)
    result = {}
    for item in data:
        for ticker, sym in BINANCE.items():
            if item["symbol"] == sym:
                result[ticker] = {
                    "price": float(item["lastPrice"]),
                    "change_24h": float(item["priceChangePercent"]),
                    "volume_24h": float(item["quoteVolume"]),
                    "high_24h": float(item["highPrice"]),
                    "low_24h": float(item["lowPrice"]),
                }
    return result


def fetch_klines(ticker, interval="1d", limit=30):
    """Fetch OHLCV klines dla technical analysis."""
    sym = BINANCE.get(ticker)
    if not sym:
        return []
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}"
    return _get_json(url)


def fetch_fng():
    """Fear & Greed Index."""
    try:
        data = _get_json("https://api.alternative.me/fng/?limit=7")
        items = data.get("data", [])
        if not items:
            return None
        return {
            "current": int(items[0]["value"]),
            "classification": items[0]["value_classification"],
            "week_avg": sum(int(x["value"]) for x in items) // len(items),
        }
    except Exception as e:
        print(f"[fng] fetch failed: {e}")
        return None


def fetch_btc_dominance():
    """BTC dominance from CoinGecko."""
    try:
        data = _get_json("https://api.coingecko.com/api/v3/global")
        return data["data"]["market_cap_percentage"]["btc"]
    except Exception as e:
        print(f"[dom] fetch failed: {e}")
        return None


def fetch_etf_flows():
    """BTC + ETH spot ETF net flows (Farside.co.uk public JSON)."""
    try:
        btc_data = _get_json("https://farside.co.uk/btc/etf-flows.json")
        eth_data = _get_json("https://farside.co.uk/eth/etf-flows.json")
        return {
            "btc_1d": btc_data.get("total", [{}])[-1] if btc_data else None,
            "eth_1d": eth_data.get("total", [{}])[-1] if eth_data else None,
        }
    except Exception:
        return None


def compute_ta_score(klines):
    """
    Prosty technical score 0-100 na podstawie 30 świec dziennych.
    Kombinuje: trend (EMA20 slope), momentum (RSI proxy), volatility.
    """
    if not klines or len(klines) < 20:
        return 50  # neutral

    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    # Trend: EMA20 vs current
    ema20 = closes[-1]  # start
    for c in closes[-20:]:
        ema20 = ema20 * 0.9 + c * 0.1
    trend_score = 50 + ((closes[-1] - ema20) / ema20) * 500  # -50..+50 range
    trend_score = max(0, min(100, trend_score))

    # Momentum: 7-day return
    if len(closes) >= 7:
        pct_7d = (closes[-1] - closes[-7]) / closes[-7] * 100
        momo_score = 50 + pct_7d * 2  # 5% = +10 score
        momo_score = max(0, min(100, momo_score))
    else:
        momo_score = 50

    # Volume trend: recent vs avg
    if len(volumes) >= 20:
        recent_vol = sum(volumes[-3:]) / 3
        avg_vol = sum(volumes[-20:]) / 20
        vol_score = min(100, (recent_vol / max(avg_vol, 1)) * 50)
    else:
        vol_score = 50

    return int(trend_score * 0.5 + momo_score * 0.35 + vol_score * 0.15)


def compute_short_term_momentum(klines_1h):
    """
    Szybki sygnał momentum z godzinowych świec — łapie breakdowns/pumpy ZANIM
    zdąży je zauważyć wolna dzienna EMA/7-day return. Bez tego jeden zły dzień
    prawie nie rusza 20-30-dniowego trendu i TA score zostaje sztucznie wysoki
    mimo trwającego spadku w ostatnich godzinach.
    """
    if not klines_1h or len(klines_1h) < 13:
        return 50  # neutral — brak danych
    closes = [float(k[4]) for k in klines_1h]
    chg_4h = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
    chg_12h = (closes[-1] - closes[-13]) / closes[-13] * 100 if len(closes) >= 13 else 0
    # Recent (4h) move waży mocniej — to jest "wczesne ostrzeżenie" zanim
    # trend dzienny zdąży zareagować
    score = 50 + (chg_4h * 4) + (chg_12h * 1.5)
    return max(0, min(100, int(score)))


def detect_swing_points(klines, lookback=2):
    """
    Proste wykrywanie swing high/low metodą fraktalną: świeca i jest swing high
    jeśli jej high jest wyższe niż `lookback` świec przed i po niej (analogicznie
    dla swing low). Zwraca listę {idx, type: 'H'/'L', price} posortowaną wg idx,
    z deduplikacją kolejnych swingów tego samego typu (zostaje bardziej ekstremalny).
    """
    if not klines or len(klines) < (lookback * 2 + 3):
        return []
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    n = len(klines)
    swings = []
    for i in range(lookback, n - lookback):
        left_h, right_h = highs[i - lookback:i], highs[i + 1:i + 1 + lookback]
        left_l, right_l = lows[i - lookback:i], lows[i + 1:i + 1 + lookback]
        if highs[i] > max(left_h) and highs[i] > max(right_h):
            swings.append({"idx": i, "type": "H", "price": highs[i]})
        if lows[i] < min(left_l) and lows[i] < min(right_l):
            swings.append({"idx": i, "type": "L", "price": lows[i]})
    swings.sort(key=lambda s: s["idx"])

    cleaned = []
    for s in swings:
        if cleaned and cleaned[-1]["type"] == s["type"]:
            if (s["type"] == "H" and s["price"] > cleaned[-1]["price"]) or \
               (s["type"] == "L" and s["price"] < cleaned[-1]["price"]):
                cleaned[-1] = s
        else:
            cleaned.append(s)
    return cleaned


def analyze_market_structure(klines, lookback=2):
    """
    Market structure w stylu smart-money: trend (higher-highs/higher-lows = bullish,
    lower-highs/lower-lows = bearish), BOS (Break of Structure — kontynuacja trendu,
    przełamanie ostatniego swingu W KIERUNKU trendu) i CHoCH (Change of Character —
    pierwsze przełamanie PRZECIWKO trendowi, sygnał potencjalnego odwrócenia).
    Potwierdzenie przez CLOSE świecy (nie knot) — mniej fałszywych sygnałów.
    Zwraca: trend, last_event (BOS_UP/BOS_DOWN/CHOCH_BULL/CHOCH_BEAR/None),
    last_event_price, score (0-100, >50 = bias bullish, <50 = bias bearish).
    """
    default = {"trend": "neutral", "last_event": None, "last_event_price": None, "score": 50}
    if not klines or len(klines) < (lookback * 2 + 5):
        return default

    swings = detect_swing_points(klines, lookback=lookback)
    if len(swings) < 2:
        return default

    closes = [float(k[4]) for k in klines]
    highs_seq = [s for s in swings if s["type"] == "H"]
    lows_seq = [s for s in swings if s["type"] == "L"]

    trend = "neutral"
    if len(highs_seq) >= 2 and len(lows_seq) >= 2:
        if highs_seq[-1]["price"] > highs_seq[-2]["price"] and lows_seq[-1]["price"] > lows_seq[-2]["price"]:
            trend = "bullish"
        elif highs_seq[-1]["price"] < highs_seq[-2]["price"] and lows_seq[-1]["price"] < lows_seq[-2]["price"]:
            trend = "bearish"

    last_high = highs_seq[-1]["price"] if highs_seq else None
    last_low = lows_seq[-1]["price"] if lows_seq else None
    last_swing_idx = swings[-1]["idx"]

    last_event, last_event_price = None, None
    for i in range(last_swing_idx + 1, len(closes)):
        c = closes[i]
        if trend == "bullish":
            if last_low is not None and c < last_low:
                last_event, last_event_price = "CHOCH_BEAR", c
                trend = "bearish"
                last_low = c  # nowy punkt odniesienia dla ew. kontynuacji w dół (BOS_DOWN)
            elif last_high is not None and c > last_high:
                last_event, last_event_price = "BOS_UP", c
                last_high = c
        elif trend == "bearish":
            if last_high is not None and c > last_high:
                last_event, last_event_price = "CHOCH_BULL", c
                trend = "bullish"
                last_high = c
            elif last_low is not None and c < last_low:
                last_event, last_event_price = "BOS_DOWN", c
                last_low = c
        else:
            break

    score_map = {"CHOCH_BEAR": 10, "BOS_DOWN": 25, "BOS_UP": 75, "CHOCH_BULL": 90}
    if last_event:
        score = score_map[last_event]
    elif trend == "bullish":
        score = 60
    elif trend == "bearish":
        score = 40
    else:
        score = 50

    return {
        "trend": trend,
        "last_event": last_event,
        "last_event_price": round(last_event_price, 6) if last_event_price is not None else None,
        "score": score,
    }


def compute_score(ticker, price_data, ta_score, fng, etf_flows):
    """
    Combined fusion score 0-100.
    Weights: OnChain 40% + TA 30% + News 20% + Sentiment 10%.
    """
    # OnChain proxy: ETF flows dla BTC/ETH, sentiment dla altcoinów
    if ticker == "BTC" and etf_flows and etf_flows.get("btc_1d"):
        flow_val = etf_flows["btc_1d"]
        # Positive inflow = bullish
        onchain_score = 65 + min(20, (flow_val / 100_000_000) * 5)  # +$500M = +25
    elif ticker == "ETH" and etf_flows and etf_flows.get("eth_1d"):
        flow_val = etf_flows["eth_1d"]
        onchain_score = 65 + min(20, (flow_val / 100_000_000) * 5)
    else:
        # Altcoins: use price momentum + volume
        onchain_score = 60 + (price_data.get("change_24h", 0) * 2)

    onchain_score = max(0, min(100, onchain_score))

    # News: default 60 (neutral without LLM analysis)
    news_score = 60

    # Sentiment: F&G Index
    if fng:
        # F&G > 65 = greed = bullish for BTC/ETH, potentially topping for alts
        sentiment_score = fng["current"]
    else:
        sentiment_score = 55

    total = (
        onchain_score * 0.40
        + ta_score * 0.30
        + news_score * 0.20
        + sentiment_score * 0.10
    )
    return int(total), {
        "onchain": int(onchain_score),
        "ta": int(ta_score),
        "news": news_score,
        "sentiment": int(sentiment_score),
    }


def score_to_action(score, regime):
    """Score → action mapping (zgodne z Fusion 2.0 spec)."""
    if score >= 75:
        return "STRONG_BUY"
    elif score >= 60:
        return "BUY"
    elif score >= 40:
        return "HOLD"
    elif score >= 25:
        return "SELL" if regime != "TRENDING_UP" else "HOLD"
    else:
        return "STRONG_SELL" if regime != "TRENDING_UP" else "SKIP"


def compute_size(score, regime, ticker):
    """
    Position sizing na bazie score + regime multiplier.
    score >= 60  → LONG sizing (wyższy score = większa pozycja)
    score <= 35  → SHORT sizing (niższy score = większa pozycja, symetrycznie do long)
    36-59        → no man's land, brak pozycji (score za neutralny w obie strony)
    """
    if datetime.now().weekday() in (5, 6):
        weekend_mult = 0.7  # weekend defensive, obie strony
    else:
        weekend_mult = 1.0

    if score >= 60:
        base = max(0.5, (score - 60) * 0.1)  # 60 = 0.5%, 80 = 2.0%
        multiplier = {
            "TRENDING_UP": 1.0,
            "TRENDING_UP_VOLATILE": 0.7,
            "RANGING": 0.6,
            "TRENDING_DOWN": 0.3,
            "TRENDING_DOWN_VOLATILE": 0.2,
            "CRASH": 0.1,
        }.get(regime, 0.7)
    elif score < 40:
        # próg wyrównany z score_to_action() (SELL/STRONG_SELL zaczyna się < 40)
        base = max(0.5, (40 - score) * 0.075)  # 39 = 0.5%, 20 = 1.5%, 0 = 3.0%
        # Regime multiplier dla SHORT: odwrotny do long — trend spadkowy zwiększa
        # przekonanie do shortów, trend wzrostowy je tłumi (nie walcz z trendem)
        multiplier = {
            "TRENDING_DOWN": 1.0,
            "TRENDING_DOWN_VOLATILE": 0.8,
            "CRASH": 0.6,  # crash = duża zmienność, mniejszy size mimo wysokiej konwikcji
            "RANGING": 0.5,
            "TRENDING_UP": 0.15,
            "TRENDING_UP_VOLATILE": 0.1,
        }.get(regime, 0.4)
    else:
        return 0

    multiplier *= weekend_mult
    # SUI/small altcoins → cap
    if ticker in ("SUI", "XRP"):
        return round(min(1.0, base * multiplier), 2)
    return round(min(2.5, base * multiplier), 2)


def generate_catalyst_calendar():
    """
    Generuje real catalyst calendar dla widget'a (parseable dates).
    Kombinuje:
    - Znane fixed events (Sep 15 CLARITY vote)
    - Compute'owane recurring events (PCE = last Tue of month, CPI = 2nd Wed, FOMC dates)
    - Weekly rhythm (Fed speeches, ETF flow updates)
    """
    from datetime import date, timedelta
    now = datetime.now()
    today = date.today()
    events = []

    # ─── Fixed known events ───
    fixed_events = [
        (date(2026, 9, 15), "Pon 15.09: CLARITY Act Senate cloture vote — mid-term regulatory catalyst"),
        (date(2026, 10, 28), "Wt 28.10: Next FOMC decision + press conf 14:30 ET"),
        (date(2026, 12, 16), "Wt 16.12: FOMC year-end decision"),
    ]
    for d, text in fixed_events:
        if d >= today and (d - today).days < 90:
            events.append(text)

    # ─── Compute next PCE (usually last Friday of month, historically ~28th) ───
    # PCE typically released Friday of last full week; approximating as last Friday
    for m_ahead in range(2):
        target_month = now.month + m_ahead
        target_year = now.year
        if target_month > 12:
            target_month -= 12
            target_year += 1
        # Last day of month
        if target_month == 12:
            last_day = date(target_year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(target_year, target_month + 1, 1) - timedelta(days=1)
        # Find last Friday
        while last_day.weekday() != 4:  # 4 = Friday
            last_day -= timedelta(days=1)
        if last_day >= today:
            month_pl = {1:"sty",2:"lut",3:"mar",4:"kwi",5:"maj",6:"cze",7:"lip",8:"sie",9:"wrz",10:"paź",11:"lis",12:"gru"}[target_month]
            events.append(f"Pt {last_day.day:02d}.{target_month:02d}: {month_pl.upper()} PCE Core release 8:30 ET (14:30 CET) — TIER-1 macro event")

    # ─── Compute next CPI (usually 2nd Wed of month) ───
    for m_ahead in range(2):
        target_month = now.month + m_ahead
        target_year = now.year
        if target_month > 12:
            target_month -= 12
            target_year += 1
        first_day = date(target_year, target_month, 1)
        # Find 2nd Wednesday
        days_to_wed = (2 - first_day.weekday()) % 7
        second_wed = first_day + timedelta(days=days_to_wed + 7)
        if second_wed >= today:
            events.append(f"Śr {second_wed.day:02d}.{target_month:02d}: US CPI release 8:30 ET (14:30 CET)")

    # ─── This week: ETF flow watch (daily 4pm ET) ───
    events.append(f"Codziennie: BTC + ETH ETF flow tape post-4pm ET (22:00 CET) — key institutional signal")

    # ─── Weekly rhythm ───
    weekday = now.weekday()  # 0=Mon
    if weekday == 4:  # Friday
        events.append("Dziś (Pt): weekly market close — rebalance przed weekend")
    elif weekday == 0:
        events.append("Dziś (Pon): US market open po weekend — fresh signals")

    return events[:12]  # cap at 12 events


def detect_regime(btc_price_data, fng, btc_dominance):
    """
    Regime detection:
    - BTC 24h < -8% → CRASH (price-led, no F&G gate — crashes don't wait for sentiment to catch up)
    - BTC 24h < -3% → TRENDING_DOWN (price-led — F&G LAGS price, requiring F&G<40 too often
      blocked real downtrends where sentiment hadn't caught up yet, e.g. F&G still "Greed"
      the same day BTC dropped -3%+. Price action is the primary signal for direction.)
    - BTC 24h > +3% + F&G > 60 → TRENDING_UP (kept F&G-gated: chasing pumps without sentiment
      confirmation is a worse failure mode than being slow to catch an uptrend)
    - Wysoki volatility (24h range > 5%) → *_VOLATILE variant
    - Inaczej → RANGING
    """
    if not btc_price_data:
        return "RANGING"
    change = btc_price_data.get("change_24h", 0)
    price = btc_price_data.get("price", 1)
    high = btc_price_data.get("high_24h", 1)
    low = btc_price_data.get("low_24h", 1)
    range_pct = (high - low) / price * 100

    if change < -8:
        return "CRASH"
    if change < -3:
        return "TRENDING_DOWN_VOLATILE" if range_pct > 5 else "TRENDING_DOWN"
    if change > 3 and fng and fng.get("current", 50) > 60:
        return "TRENDING_UP_VOLATILE" if range_pct > 5 else "TRENDING_UP"
    return "RANGING"


def generate_fusion():
    """Main — generate today's fusion JSON."""
    print(f"[auto-fusion] Starting @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    prices = fetch_prices()
    if not prices:
        print("[error] Failed to fetch prices — abort")
        sys.exit(1)
    print(f"[data] Prices: {len(prices)} tickers")

    fng = fetch_fng()
    print(f"[data] F&G: {fng['current'] if fng else 'N/A'}")

    dom = fetch_btc_dominance()
    print(f"[data] BTC dominance: {dom:.2f}%" if dom else "[data] Dominance: N/A")

    etf_flows = fetch_etf_flows()
    print(f"[data] ETF flows: {'✓' if etf_flows else 'N/A'}")

    regime = detect_regime(prices.get("BTC"), fng, dom)
    print(f"[regime] {regime}")

    decisions = []
    for ticker in ["BTC", "ETH", "SOL", "XRP", "SUI"]:
        klines = fetch_klines(ticker)  # daily, 30 candles — trend/momentum baseline
        daily_ta_score = compute_ta_score(klines)
        klines_1h = fetch_klines(ticker, interval="1h", limit=50)  # ~2 dni godzinowych — kontekst dla struktury
        short_term_score = compute_short_term_momentum(klines_1h)
        klines_15m = fetch_klines(ticker, interval="15m", limit=100)  # ~25h, 15-min świece

        # Market structure (BOS/CHoCH) na 1h (mniej szumu) i 15m (czułość na szybkie zmiany)
        ms_1h = analyze_market_structure(klines_1h, lookback=2)
        ms_15m = analyze_market_structure(klines_15m, lookback=2)
        ms_score = int(ms_1h["score"] * 0.6 + ms_15m["score"] * 0.4)

        # Blend: dzienny trend (kontekst) + 1h momentum (świeże ruchy) + market structure
        # (BOS/CHoCH 15m+1h — łapie change of character zanim zrobi to reszta wskaźników)
        ta_score = int(daily_ta_score * 0.40 + short_term_score * 0.25 + ms_score * 0.35)
        score, sources = compute_score(ticker, prices.get(ticker, {}), ta_score, fng, etf_flows)
        action = score_to_action(score, regime)
        size = compute_size(score, regime, ticker)
        current_price = prices.get(ticker, {}).get("price", 0)

        direction = None
        if action in ("STRONG_BUY", "BUY"):
            direction = "long"
        elif action in ("SELL", "STRONG_SELL"):
            direction = "short"

        # CHoCH override — bearish/bullish struktura na 1h (bardziej wiarygodny interwał
        # niż 15m) sama w sobie odblokowuje short/long dla TEGO tokena, niezależnie od
        # globalnego BTC-regime i reszty score. Alt może się osłabiać/wzmacniać niezależnie
        # od tego czy BTC formalnie "crashuje" — regime-gate w paper_bot.py respektuje ten
        # override (patrz choch_override flag w decyzji).
        # Patrzymy na trend (nie tylko last_event) celowo: CHoCH to tylko pojedyncza
        # świeca-moment złamania — jeśli spadek trwa dalej, kolejny odczyt pokazuje już
        # BOS_DOWN (kontynuacja), nie CHOCH_BEAR, i wąskie okno na last_event łatwo
        # przegapić między cyklami co 5 min. trend=="bearish" obejmuje cały czas trwania
        # niedźwiedziej struktury, nie tylko moment jej powstania.
        # v0.3 — override MOCNO ograniczony. Audyt 50 zamkniętych paper trade'ów
        # (2026-09-02): exit=flip_choch to 31/50 trade'ów z win rate 3% (-$9.44),
        # regime RANGING: 39 trade'ów, 5% WR. Override sam z siebie generował
        # ping-pong long↔short co kilka minut na szumie 1h w rynku bocznym.
        # Teraz override wymaga JEDNOCZEŚNIE:
        #   (1) regime != RANGING — w range 1h CHoCH to szum, nie sygnał,
        #   (2) zgodności struktury 1h I 15m (obie bearish / obie bullish),
        #   (3) zgodności z fusion score: short tylko gdy score < 40 (nie shortujemy
        #       tokena, którego własny score mówi 57 = lekko bullish), long tylko
        #       gdy score >= 55.
        choch_override = False
        override_allowed = regime != "RANGING"
        ms_agree_bear = ms_1h.get("trend") == "bearish" and ms_15m.get("trend") == "bearish"
        ms_agree_bull = ms_1h.get("trend") == "bullish" and ms_15m.get("trend") == "bullish"
        if override_allowed and ms_agree_bear and score < 40 and direction != "short":
            direction = "short"
            action = "SELL"
            size = compute_size(30, regime, ticker)  # syntetyczny bearish score do sizing
            choch_override = True
        elif override_allowed and ms_agree_bull and score >= 55 and direction != "long":
            direction = "long"
            action = "BUY"
            size = compute_size(65, regime, ticker)  # syntetyczny bullish score do sizing
            choch_override = True

        # Entry/SL/TP dynamiczne (proste %-based) — mirrored dla short: SL powyżej
        # entry, TP poniżej entry (odwrotnie niż long)
        if size > 0 and direction == "long":
            entry_low = round(current_price * 0.985, 4)
            entry_high = round(current_price * 1.015, 4)
            sl = round(current_price * 0.95, 4)
            tp1 = round(current_price * 1.06, 4)
            tp2 = round(current_price * 1.12, 4)
        elif size > 0 and direction == "short":
            entry_low = round(current_price * 0.985, 4)
            entry_high = round(current_price * 1.015, 4)
            sl = round(current_price * 1.05, 4)
            tp1 = round(current_price * 0.94, 4)
            tp2 = round(current_price * 0.88, 4)
        else:
            entry_low = entry_high = sl = tp1 = tp2 = None

        ms_note = f"MS 1h:{ms_1h['trend']}"
        if ms_1h["last_event"]:
            ms_note += f"/{ms_1h['last_event']}"
        ms_note += f" · 15m:{ms_15m['trend']}"
        if ms_15m["last_event"]:
            ms_note += f"/{ms_15m['last_event']}"

        decisions.append({
            "rank": len(decisions) + 1,
            "ticker": ticker,
            "direction": direction,
            "score": score,
            "action": action,
            "size_pct": size,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "sources": sources,
            "onchain_data_thin": False,
            "market_structure": {"1h": ms_1h, "15m": ms_15m},
            "choch_override": choch_override,
            "risk_flag": f"Auto-generated {datetime.now().strftime('%H:%M')}. TA {ta_score}/100 (daily {daily_ta_score} · 1h momo {short_term_score} · {ms_note}). Current ${current_price:.2f} ({prices[ticker]['change_24h']:+.2f}% 24h)."
                         + (" ⚡ CHoCH OVERRIDE — 1h change of character, wchodzi mimo regime/score." if choch_override else ""),
            "invalidation_note": f"SL @ ${sl}" if sl else "Not entered",
        })

    # Sort by score descending
    decisions.sort(key=lambda x: -x["score"])
    for i, d in enumerate(decisions):
        d["rank"] = i + 1

    aggregate_risk = sum(d["size_pct"] for d in decisions if d["direction"] == "long")
    aggregate_short_risk = sum(d["size_pct"] for d in decisions if d["direction"] == "short")

    dom_str = f"{dom:.2f}%" if dom is not None else "N/A"

    fusion = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "version": f"auto-{datetime.now().strftime('%H%M')}",
        "regime": regime,
        "regime_note": (
            f"Auto-generated {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}. "
            f"BTC ${prices.get('BTC', {}).get('price', 0):.0f} ({prices.get('BTC', {}).get('change_24h', 0):+.2f}% 24h), "
            f"F&G {fng['current'] if fng else 'N/A'} ({fng['classification'] if fng else 'N/A'}), "
            f"BTC.D {dom_str}. "
            f"Weekday sizing ×1.0"
            f"{' (weekend ×0.7 applied)' if datetime.now().weekday() in (5, 6) else ''}."
        ),
        "weights": {"onchain": 0.40, "ta": 0.30, "news": 0.20, "sentiment": 0.10},
        "data_provenance": "Auto-fetched: Binance prices/klines, alternative.me F&G, CoinGecko BTC.D, Farside ETF flows. NO Claude credits used.",
        "decisions": decisions,
        "aggregate_long_risk_pct": round(aggregate_risk, 1),
        "aggregate_short_risk_pct": round(aggregate_short_risk, 1),
        "correlation_warnings": [
            "Auto-fusion nie zawiera qualitative context (news, catalysts). Traktuj jako baseline — Claude fusion daje szerszy context.",
        ],
        "short_blocked_by_regime": [] if regime in ("TRENDING_DOWN", "TRENDING_DOWN_VOLATILE", "CRASH") else [f"Regime {regime} — shorty bez CHoCH override dozwolone tylko w TRENDING_DOWN/CRASH. Token ze świeżym bearish CHoCH na 1h omija ten gate (patrz choch_override w decyzji)."],
        "catalyst_calendar_this_week": generate_catalyst_calendar(),
        "crypto_picks": load_crypto_picks(),
        "conclusion": (
            f"Regime {regime}. Long risk {aggregate_risk:.1f}% · Short risk {aggregate_short_risk:.1f}% capital. "
            f"Top pick: {decisions[0]['ticker']} score {decisions[0]['score']} "
            f"({decisions[0]['action']})."
        ),
    }

    # Write to file
    out_path = FUSION_DIR / f"fusion_{fusion['date']}.json"
    out_path.write_text(json.dumps(fusion, indent=2, ensure_ascii=False))
    print(f"[save] Written to {out_path}")

    # Upload to Gist via paper_bot.py
    print("[upload] Calling paper_bot.py upload...")
    os.system(f"python3 {FUSION_DIR}/paper_bot.py upload")

    print(f"[done] Fusion {fusion['date']} generated + uploaded")


def load_crypto_picks():
    """
    Poranne Crypto Picks (scheduled task morning-crypto-picks na Macu Gosi) zapisuje
    crypto_picks.json do repo i pushuje. Tu tylko przepuszczamy je do fusion_latest.json,
    żeby terminal mógł pokazać osobny panel. Brak pliku / zły JSON → None (panel się nie pokaże).
    """
    path = FUSION_DIR / "crypto_picks.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("picks"), list):
            return None
        # Oznacz wiek — terminal przygasza picks starsze niż 1 dzień
        try:
            age_days = (datetime.now() - datetime.strptime(data.get("date", ""), "%Y-%m-%d")).days
        except ValueError:
            age_days = None
        data["age_days"] = age_days
        return data
    except Exception as e:
        print(f"[picks] crypto_picks.json unreadable: {e}")
        return None


def loop_mode(interval_hours):
    """Run auto-fusion + auto-refresh in loop."""
    interval_sec = interval_hours * 3600
    while True:
        try:
            generate_fusion()
            # Also run paper_bot check + upload for fresh news/whales
            print("[loop] Running paper_bot refresh...")
            os.system(f"python3 {FUSION_DIR}/paper_bot.py refresh")
        except Exception as e:
            print(f"[loop] error: {e}")
        next_run = datetime.now().timestamp() + interval_sec
        print(f"[loop] Next run at {datetime.fromtimestamp(next_run).strftime('%H:%M')}")
        time.sleep(interval_sec)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TVC Auto-Fusion (no Claude credits)")
    parser.add_argument("--loop", type=str, help="Loop mode with interval (e.g. '4h' or '2h')")
    args = parser.parse_args()

    if args.loop:
        hours = float(args.loop.rstrip("h"))
        print(f"[auto-fusion] Loop mode: every {hours}h")
        loop_mode(hours)
    else:
        generate_fusion()
