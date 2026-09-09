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
FETCH_ERRORS = []   # diagnostyka: trafia do fusion json (logi Actions wymagają admina)
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


def _get_text(url, timeout=20):
    req = ur.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                                   "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"})
    with ur.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read().decode("utf-8", errors="replace")


def _parse_farside_table(text, days=60):
    """
    Farside publikuje TYLKO HTML (endpoint .json nigdy nie istniał — stara funkcja zwracała None
    od pierwszego dnia, więc On-Chain sub-score BTC/ETH liczył się z formuły zapasowej).
    Cloudflare Farside odrzuca IP runnerów GitHub (403), więc pobieramy przez r.jina.ai (reader
    proxy → markdown). Parser obsługuje OBA formaty:
      HTML:     <td>04 Sep 2026</td> ... <td>174.6</td>
      markdown: | 04 Sep 2026 | 117.4 | ... | 174.6 |
    Liczby: "(19.6)" = ujemna, "-" = brak, "1,119.9". Zwraca [{date, total}] w mln USD, najstarsze pierwsze.
    """
    import re as _re
    from datetime import datetime as _dt
    rows = []
    if "<tr" in text:
        lines = []
        for tr in _re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=_re.S):
            lines.append([_re.sub(r"<[^>]+>", "", c).strip() for c in _re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=_re.S)])
    else:
        lines = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in text.splitlines() if ln.strip().startswith("|")]
    for cells in lines:
        if len(cells) < 3:
            continue
        if not _re.match(r"^\d{2} \w{3} \d{4}$", cells[0]):
            continue
        raw = cells[-1].replace(",", "").strip()
        if raw in ("-", ""):
            continue
        neg = raw.startswith("(")
        try:
            val = float(raw.strip("()"))
            d = _dt.strptime(cells[0], "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        rows.append({"date": d, "total": -val if neg else val})
    return rows[-days:]


def _etf_from_series(series, source_tag):
    """Helper: przelicza [{date,total}] na dict z d1/d7/d30/streak."""
    vals = [r["total"] for r in series]
    streak, sign = 0, (1 if vals[-1] > 0 else -1 if vals[-1] < 0 else 0)
    for v in reversed(vals):
        if sign and (v > 0) == (sign > 0) and v != 0:
            streak += 1
        else:
            break
    return {"series": series, "d1": vals[-1], "d7": sum(vals[-5:]), "d30": sum(vals[-21:]),
            "streak": streak * sign, "last_date": series[-1]["date"], "unit": "USD mln",
            "cum_60d": sum(vals), "source": source_tag}


def fetch_etf_flows():
    """
    BTC + ETH spot ETF net flows (Farside, mln USD).
    Łańcuch prób: (1) Farside bezpośrednio, (2) własny PHP proxy na Hostinger,
    (3) Gist (zapisywany przez terminal w przeglądarce Gosi).
    Zwraca None gdy wszystko puste; d1/d7/d30/streak liczone jak w przeglądarce.
    """
    HOSTINGER_PROXY = "https://tradingventureclub.com/terminal/etf-proxy.php?asset={asset}"
    out = {"btc_1d": None, "eth_1d": None, "btc": None, "eth": None}
    for key, url in (("btc", "https://farside.co.uk/bitcoin-etf-flow-all-data/"),
                     ("eth", "https://farside.co.uk/ethereum-etf-flow-all-data/")):
        # 1) Direct Farside
        try:
            series = _parse_farside_table(_get_text(url))
            if not series:
                raise ValueError("pusta tabela")
            out[key] = _etf_from_series(series, "farside-direct")
            out[f"{key}_1d"] = series[-1]["total"] * 1_000_000
            print(f"[etf] {key.upper()} direct {series[-1]['date']}: {series[-1]['total']:+.1f}M")
            continue
        except Exception as e:
            FETCH_ERRORS.append(f"etf.{key}.direct: {type(e).__name__}: {str(e)[:60]}")
        # 2) Hostinger PHP proxy
        try:
            series = _parse_farside_table(_get_text(HOSTINGER_PROXY.format(asset=key)))
            if not series:
                raise ValueError("pusta tabela")
            out[key] = _etf_from_series(series, "hostinger-proxy")
            out[f"{key}_1d"] = series[-1]["total"] * 1_000_000
            print(f"[etf] {key.upper()} hostinger-proxy {series[-1]['date']}: {series[-1]['total']:+.1f}M")
        except Exception as e:
            FETCH_ERRORS.append(f"etf.{key}.hostinger: {type(e).__name__}: {str(e)[:60]}")
    if not out["btc"] and not out["eth"]:
        # 3) Gist fallback
        gist_id = os.environ.get("TVC_GIST_ID", "e88c461a964ed22d2cf14326c65b4438")
        try:
            g = _get_json(f"https://gist.githubusercontent.com/AdventurousGlam/{gist_id}/raw/etf_flows.json?t={int(time.time())}", retries=1)
            age_h = (time.time() * 1000 - g.get("ts", 0)) / 3600_000
            if age_h > 96:
                raise ValueError(f"etf_flows.json ma {age_h:.0f}h — za stare")
            for key in ("btc", "eth"):
                if g.get(key):
                    out[key] = {**g[key], "source": f"gist/browser ({age_h:.0f}h)"}
                    out[f"{key}_1d"] = float(g[key]["d1"]) * 1_000_000
                    print(f"[etf] {key.upper()} z Gist ({age_h:.0f}h): {g[key]['d1']:+.1f}M · streak {g[key].get('streak')}")
        except Exception as e:
            FETCH_ERRORS.append(f"etf.gist: {type(e).__name__}: {str(e)[:80]}")
    return out if (out["btc"] or out["eth"]) else None


# ─── MAKRO: korelacje BTC z rynkami tradycyjnymi (Yahoo chart API, bez klucza) ───
MACRO_SYMBOLS = {
    "NQ":   ("^IXIC",    "Nasdaq Composite"),
    "SPX":  ("^GSPC",    "S&P 500"),
    "DXY":  ("DX-Y.NYB", "Dollar Index"),
    "US10Y": ("^TNX",    "US 10Y yield"),
    "GOLD": ("GC=F",     "Gold futures"),
}


STOOQ_SYMBOLS = {"BTC-USD": "btcusd", "^IXIC": "^ndq", "^GSPC": "^spx", "DX-Y.NYB": "dx.f", "^TNX": "10yusy.b", "GC=F": "xauusd"}


def _stooq_daily(symbol):
    """stooq.com CSV (Date,Open,High,Low,Close,Volume) — bez klucza, bez rate limitu dla kilku wywołań."""
    s = STOOQ_SYMBOLS.get(symbol, symbol)
    txt = _get_text(f"https://stooq.com/q/d/l/?s={ur.quote(s)}&i=d")
    out = []
    for ln in txt.splitlines()[1:]:
        p = ln.split(",")
        if len(p) >= 5 and p[0][:4].isdigit():
            try:
                out.append((p[0], float(p[4])))
            except ValueError:
                pass
    if len(out) < 20:
        raise ValueError(f"stooq {s}: {len(out)} wierszy")
    return out[-120:]


def _yahoo_daily(symbol, rng="3mo"):
    """Yahoo chart API — fallback (z współdzielonych IP GitHub łapie 429)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ur.quote(symbol)}?range={rng}&interval=1d"
    req = ur.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36", "Accept": "application/json"})
    with ur.urlopen(req, timeout=20, context=SSL_CTX) as r:
        j = json.loads(r.read())
    res = j["chart"]["result"][0]
    ts = res["timestamp"]; closes = res["indicators"]["quote"][0]["close"]
    return [(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), c) for t, c in zip(ts, closes) if c is not None]


def _daily_series(symbol):
    """stooq → Yahoo. Rzuca ostatni błąd gdy oba padną."""
    try:
        return _stooq_daily(symbol)
    except Exception as e1:
        print(f"[macro] stooq {symbol} failed ({e1}) — Yahoo fallback")
        FETCH_ERRORS.append(f"macro.stooq.{symbol}: {type(e1).__name__}: {str(e1)[:80]}")
        time.sleep(1.5)
        return _yahoo_daily(symbol)


def _pearson(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy) ** 0.5


def fetch_macro_context():
    """
    Zmiana 1d/5d + korelacja 30-sesyjna dziennych zwrotów BTC z NQ/SPX/DXY/US10Y/GOLD.
    Interpretacja: corr(BTC,NQ) > 0.5 = crypto handluje jak beta na ryzyko (stopy rządzą);
    corr(BTC,DXY) < -0.4 = dolar rządzi. Zwraca None gdy Yahoo padnie — cykl idzie dalej.
    """
    try:
        btc = dict(_daily_series("BTC-USD"))
    except Exception as e:
        print(f"[macro] BTC-USD failed: {e}")
        FETCH_ERRORS.append(f"macro.BTC-USD: {type(e).__name__}: {str(e)[:160]}")
        return None
    out = {"assets": {}, "as_of": max(btc.keys()) if btc else None}
    for key, (sym, name) in MACRO_SYMBOLS.items():
        try:
            series = _daily_series(sym)
            if len(series) < 6:
                raise ValueError("za krótka seria")
            closes = [c for _, c in series]
            last, prev, prev5 = closes[-1], closes[-2], closes[-6]
            # wspólne daty → dzienne zwroty → korelacja z ostatnich 30 wspólnych sesji
            common = [d for d, _ in series if d in btc]
            pairs = []
            sd = dict(series)
            for i in range(1, len(common)):
                d0, d1 = common[i - 1], common[i]
                pairs.append(((sd[d1] / sd[d0] - 1), (btc[d1] / btc[d0] - 1)))
            pairs = pairs[-30:]
            corr = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
            if key == "US10Y" and last > 20:   # Yahoo ^TNX podaje yield×10; stooq podaje %
                last, prev, prev5 = last / 10, prev / 10, prev5 / 10
            out["assets"][key] = {"name": name, "symbol": sym, "last": round(last, 3), "chg_1d": round((last / prev - 1) * 100, 2),
                                  "chg_5d": round((last / prev5 - 1) * 100, 2), "corr_30d": round(corr, 2) if corr is not None else None,
                                  "date": series[-1][0]}
        except Exception as e:
            print(f"[macro] {key} failed: {e}")
            FETCH_ERRORS.append(f"macro.{key}: {type(e).__name__}: {str(e)[:160]}")
    a = out["assets"]
    nq, dxy, y10 = a.get("NQ", {}).get("corr_30d"), a.get("DXY", {}).get("corr_30d"), a.get("US10Y", {}).get("corr_30d")
    if nq is not None and nq > 0.5:
        out["regime"] = "risk_beta"; out["regime_note"] = f"BTC handluje jak beta na ryzyko (corr NQ {nq:+.2f}) — makro i stopy rządzą kierunkiem"
    elif dxy is not None and dxy < -0.4:
        out["regime"] = "dollar_driven"; out["regime_note"] = f"Dolar rządzi (corr DXY {dxy:+.2f}) — patrz na DXY przed wejściem"
    elif nq is not None and abs(nq) < 0.25 and (dxy is None or abs(dxy) < 0.25):
        out["regime"] = "decoupled"; out["regime_note"] = f"BTC odklejony od makro (corr NQ {nq:+.2f}) — czynniki własne rynku crypto dominują"
    else:
        out["regime"] = "mixed"; out["regime_note"] = "Korelacje umiarkowane — makro to tło, nie sterownik"
    print(f"[macro] regime {out.get('regime')} · " + " · ".join(f"{k} {v['chg_1d']:+.1f}% corr {v['corr_30d']}" for k, v in a.items()))
    return out


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


# ─── MAKRO: prawdziwy harmonogram 2026 (UTC) ───
# Powód: 4.09.2026 flush BTC −2.5% w świecy 12:00 UTC = NFP o 12:30 UTC. Kalendarz nie miał NFP,
# ani FOMC 15–16.09. Bez tego ani terminal (Flush Risk), ani paper_bot (blackout) nie wiedzą,
# że za 2h jest event. Godziny: NFP/CPI/PCE 8:30 ET = 12:30 UTC (13:30 UTC gdy DST kończy się
# w listopadzie — pomijamy tę subtelność, blackout ma 3h zapasu). FOMC decyzja 14:00 ET = 18:00 UTC.
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
             "2026-09-16", "2026-10-28", "2026-12-09"]   # dzień decyzji (2. dzień posiedzenia)
# CPI — BLS publikuje zwykle między 10. a 15.; daty 2026 zweryfikuj na bls.gov/schedule — oznaczone approx
CPI_2026_APPROX = {9: 11, 10: 14, 11: 12, 12: 10}


def generate_macro_events(days_ahead=45):
    """Zwraca listę structured eventów: {date, time_utc, ts_utc(ISO), name, tier, approx}."""
    from datetime import date, timedelta
    today = date.today()
    out = []

    def add(d, hh, mm, name, tier, approx=False):
        if d < today - timedelta(days=1) or (d - today).days > days_ahead:
            return
        ts = datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)
        out.append({"date": d.isoformat(), "time_utc": f"{hh:02d}:{mm:02d}", "ts_utc": ts.isoformat(),
                    "name": name, "tier": tier, "approx": approx})

    for m_ahead in range(3):
        y, m = today.year, today.month + m_ahead
        if m > 12:
            m -= 12; y += 1
        first = date(y, m, 1)
        # NFP — pierwszy piątek miesiąca (wyjątek: święto → BLS przesuwa; approx)
        nfp = first + timedelta(days=(4 - first.weekday()) % 7)
        add(nfp, 12, 30, "US NFP (Non-Farm Payrolls) — jobs report", 1, approx=True)
        # CPI
        cpi_day = CPI_2026_APPROX.get(m) if y == 2026 else None
        if cpi_day:
            add(date(y, m, cpi_day), 12, 30, "US CPI (inflacja)", 1, approx=True)
        # PCE — ostatni piątek miesiąca (approx)
        last = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1))
        while last.weekday() != 4:
            last -= timedelta(days=1)
        add(last, 12, 30, "US PCE Core (ulubiona miara Fed)", 1, approx=True)
    for s in FOMC_2026:
        d = date.fromisoformat(s)
        add(d, 18, 0, "FOMC decyzja o stopach + konferencja 14:30 ET", 1)
    # Cotygodniowe jobless claims (czwartek) — tier 2, ruszają rynkiem gdy Fed patrzy na pracę
    for w in range(7):
        d = today + timedelta(days=(3 - today.weekday()) % 7 + 7 * w)
        add(d, 12, 30, "US Initial Jobless Claims", 2)
    out.sort(key=lambda e: e["ts_utc"])
    return out


def generate_catalyst_calendar():
    """
    Catalyst calendar dla terminala (parseable "Dzień DD.MM: ..." — terminal parsuje datę).
    Makro (NFP/CPI/PCE/FOMC/claims) pochodzi z generate_macro_events() — jedno źródło prawdy
    dla kalendarza, Flush Risk w terminalu i blackoutu w paper_bot. Plus znane eventy krypto.
    """
    from datetime import date, timedelta
    now = datetime.now()
    today = date.today()
    events = []
    dow_pl = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Nd"]

    # ─── Znane eventy krypto / regulacyjne (ręcznie, weryfikuj co miesiąc) ───
    fixed_events = [
        (date(2026, 9, 9),  "Solana Transaction V1 mainnet (większy limit tx — ZK/cross-chain)"),
        (date(2026, 9, 15), "CLARITY Act — Senat, cloture vote (regulacyjny katalizator)"),
        (date(2026, 9, 24), "Phantom kończy wsparcie SUI — migracja walletów (presja on-chain SUI)"),
    ]
    for d, text in fixed_events:
        if d >= today and (d - today).days < 90:
            events.append(f"{dow_pl[d.weekday()]} {d.day:02d}.{d.month:02d}: {text}")

    # ─── Makro z jednego źródła ───
    for ev in generate_macro_events(days_ahead=45):
        d = date.fromisoformat(ev["date"])
        if d < today:
            continue
        # 12:30 UTC = 14:30 CEST; 18:00 UTC = 20:00 CEST (do końca października)
        hh, mm = ev["time_utc"].split(":")
        local = f"{(int(hh) + 2) % 24:02d}:{mm}"
        tier = "TIER-1 ⚠" if ev["tier"] == 1 else "tier-2"
        events.append(f"{dow_pl[d.weekday()]} {d.day:02d}.{d.month:02d}: {ev['name']} {local} PL — {tier}{' (data approx)' if ev.get('approx') else ''}")

    events.append("Codziennie: BTC + ETH ETF flow tape po 16:00 ET (22:00 PL) — sygnał instytucjonalny")
    weekday = now.weekday()
    if weekday == 4:
        events.append("Dziś (Pt): weekly close — rebalans przed weekendem, płytsza płynność")
    elif weekday == 0:
        events.append("Dziś (Pon): US open po weekendzie — świeże przepływy")

    # sort: eventy z datą chronologicznie, reszta na końcu
    import re as _re
    def _key(s):
        m = _re.match(r"^\w+ (\d{2})\.(\d{2}):", s)
        return (0, int(m.group(2)), int(m.group(1))) if m else (1, 0, 0)
    events.sort(key=_key)
    return events[:20]



# ═══════════════════════════════════════════════════════════════════════════
# v0.4 — POZIOMY ZE STRUKTURY + JAKOŚĆ WEJŚCIA
# Do 07.09.2026 entry/SL/TP były czystym % od bieżącej ceny (entry = cena ±1.5%,
# SL −5%, TP1 +6%), przeliczanym co 5 min — "wejście" jechało razem z ceną, nie
# wiedziało, że 6% wyżej stoi MA200D, ani że token jest +4% 24h (pogoń).
# Przykład: SUI 05–07.09 — entry 0.8224 tuż pod MA200D 0.85, R:R do oporu ≈0.5.
# Teraz: entry = strefa przy najbliższym wsparciu, SL POD strukturą, TP = najbliższe
# opory, a entry_quality mówi botowi: ok (wejdź teraz) / wait (złóż wejście oczekujące
# w strefie) / skip (nie ma sensu).
# ═══════════════════════════════════════════════════════════════════════════
EXTENDED_ATR_MULT = 1.0     # cena > EMA21(1h) + 1.0×ATR(1h) → rozciągnięta, czekaj na pullback
EXTENDED_CHG24_PCT = 4.0    # |24h| > 4% → pogoń
MIN_RR = 1.5                # R:R do najbliższego oporu poniżej tego = skip
MAX_SL_PCT = 6.0            # SL pod strukturą dalej niż 6% = struktura za daleko, skip
MIN_SL_PCT = 1.2            # SL nie bliżej niż 1.2% (szum 1h)
WEEKEND_SKIP_TICKERS = ("SOL", "XRP", "SUI")   # alty: brak nowych wejść sob/niedz (płynność)
LEVEL_CLUSTER_PCT = 0.5     # poziomy bliżej niż 0.5% sklejamy w jeden (touches++)


def _atr(klines, n=14):
    """ATR (średni prawdziwy zakres) w jednostkach ceny; klines Binance [ts,o,h,l,c,...]."""
    if not klines or len(klines) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = float(klines[i][2]), float(klines[i][3]), float(klines[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


def _ema(values, n):
    if not values:
        return 0.0
    k = 2 / (n + 1)
    e = values[0]
    for v in values:
        e = v * k + e * (1 - k)
    return e


def _cluster_levels(levels, price):
    """levels: [(price, weight, label)] → sklejone {price, touches, labels} posortowane po cenie."""
    levels = sorted(levels, key=lambda x: x[0])
    out = []
    for lp, w, lab in levels:
        if out and abs(lp - out[-1]["price"]) / price * 100 < LEVEL_CLUSTER_PCT:
            c = out[-1]
            tot = c["touches"] + w
            c["price"] = (c["price"] * c["touches"] + lp * w) / tot
            c["touches"] = tot
            if lab not in c["labels"]:
                c["labels"].append(lab)
        else:
            out.append({"price": lp, "touches": w, "labels": [lab]})
    return out


def compute_levels(ticker, direction, price, change_24h, klines_d, klines_1h):
    """
    Zwraca dict: entry_low/high, sl, tp1, tp2, rr, atr_pct, supports, resistances,
    entry_quality {verdict: ok|wait|skip, flags: [...], note}.
    Poziomy: swing high/low 1h (ostatnie ~200h) i dzienne (60 dni), MA50D/MA200D,
    EMA21 1h, high/low 7d. Wszystko sklejone w klastry ±0.5%.
    """
    if not price or price <= 0 or direction not in ("long", "short"):
        return None
    closes_1h = [float(k[4]) for k in klines_1h] if klines_1h else []
    closes_d = [float(k[4]) for k in klines_d] if klines_d else []
    atr = _atr(klines_1h) or price * 0.01
    atr_pct = atr / price * 100
    ema21 = _ema(closes_1h[-60:], 21) if len(closes_1h) >= 21 else price

    raw = []
    for s in detect_swing_points(klines_1h, lookback=3) if klines_1h else []:
        raw.append((s["price"], 1.0, f"swing1h_{s['type']}"))
    for s in detect_swing_points(klines_d[-60:], lookback=2) if klines_d else []:
        raw.append((s["price"], 2.0, f"swingD_{s['type']}"))
    if len(closes_d) >= 200:
        raw.append((sum(closes_d[-200:]) / 200, 2.5, "MA200D"))
    if len(closes_d) >= 50:
        raw.append((sum(closes_d[-50:]) / 50, 1.5, "MA50D"))
    if klines_d and len(klines_d) >= 7:
        raw.append((max(float(k[2]) for k in klines_d[-7:]), 1.5, "high7d"))
        raw.append((min(float(k[3]) for k in klines_d[-7:]), 1.5, "low7d"))
    raw.append((ema21, 1.0, "EMA21_1h"))
    levels = _cluster_levels(raw, price)
    gap = max(atr * 0.25, price * 0.0015)   # poziom "przy cenie" nie liczy się jako wsparcie/opór
    supports = [l for l in levels if l["price"] < price - gap]
    resistances = [l for l in levels if l["price"] > price + gap]
    supports.sort(key=lambda l: -l["price"])       # najbliższe najpierw
    resistances.sort(key=lambda l: l["price"])

    flags = []
    extended_up = price > ema21 + EXTENDED_ATR_MULT * atr or (change_24h or 0) > EXTENDED_CHG24_PCT
    extended_down = price < ema21 - EXTENDED_ATR_MULT * atr or (change_24h or 0) < -EXTENDED_CHG24_PCT
    wd = datetime.now(timezone.utc).weekday()
    if wd in (5, 6):
        flags.append("weekend")

    def _pick(levels_list, entry, min_dist):
        # TP: pojedynczy swing 1h (touches 1.0) to szum — liczy się tylko, gdy jest ≥2 ATR
        # od wejścia; poziomy z wagą ≥1.5 (dzienne swingi, MA, high/low 7d, klastry) od min_dist.
        for l in levels_list:
            d = abs(l["price"] - entry)
            if d >= min_dist and (l["touches"] >= 1.5 or d >= 2.0 * atr):
                return l
        return None

    if direction == "long":
        s1 = supports[0] if supports else None
        if extended_up:
            flags.append("extended")
            # Strefa pullbacku: między wsparciem (lub EMA21) a EMA21 + 0.3 ATR — nie gonimy
            zone_high = min(price - 0.2 * atr, ema21 + 0.3 * atr)
            zone_low = max(s1["price"], price * 0.94) if s1 else ema21 - 0.5 * atr
            if zone_low > zone_high:
                zone_low = zone_high - 0.6 * atr
        else:
            zone_high = price * 1.003
            zone_low = max(s1["price"], price * 0.975) if s1 else price * 0.985
        entry_mid = (zone_low + zone_high) / 2
        sl_struct = (s1["price"] - max(0.5 * atr, price * 0.003)) if s1 else zone_low - 1.0 * atr
        sl = min(sl_struct, entry_mid * (1 - MIN_SL_PCT / 100))
        sl_pct = (entry_mid - sl) / entry_mid * 100
        risk = entry_mid - sl
        r1 = _pick(resistances, entry_mid, max(1.0 * atr, risk * 0.8))
        r2 = _pick([r for r in resistances if not r1 or r["price"] > r1["price"]], entry_mid, 0) if r1 else None
        tp1 = r1["price"] * 0.998 if r1 else entry_mid + 2.0 * risk
        tp2 = r2["price"] * 0.998 if r2 else max(tp1 + 1.0 * risk, entry_mid + 3.0 * risk)
        near_res = resistances[0] if resistances else None
        if near_res and (near_res["price"] - price) / price * 100 < 1.0 and near_res["touches"] >= 1.5:
            flags.append("under_resistance")
            if "extended" not in flags:
                # Tuż pod oporem: nie kupuj w opór — czekaj na zejście w stronę wsparcia
                zone_high = min(price - 0.3 * atr, (s1["price"] + 0.5 * atr) if s1 else price - 0.3 * atr)
                zone_low = min(zone_low, zone_high - 0.4 * atr)
                entry_mid = (zone_low + zone_high) / 2
                risk = entry_mid - sl
    else:
        r1 = resistances[0] if resistances else None
        if extended_down:
            flags.append("extended")
            zone_low = max(price + 0.2 * atr, ema21 - 0.3 * atr)
            zone_high = min(r1["price"], price * 1.06) if r1 else ema21 + 0.5 * atr
            if zone_high < zone_low:
                zone_high = zone_low + 0.6 * atr
        else:
            zone_low = price * 0.997
            zone_high = min(r1["price"], price * 1.025) if r1 else price * 1.015
        entry_mid = (zone_low + zone_high) / 2
        sl_struct = (r1["price"] + max(0.5 * atr, price * 0.003)) if r1 else zone_high + 1.0 * atr
        sl = max(sl_struct, entry_mid * (1 + MIN_SL_PCT / 100))
        sl_pct = (sl - entry_mid) / entry_mid * 100
        risk = sl - entry_mid
        s1 = _pick(supports, entry_mid, max(1.0 * atr, risk * 0.8))
        s2 = _pick([x for x in supports if not s1 or x["price"] < s1["price"]], entry_mid, 0) if s1 else None
        tp1 = s1["price"] * 1.002 if s1 else entry_mid - 2.0 * risk
        tp2 = s2["price"] * 1.002 if s2 else min(tp1 - 1.0 * risk, entry_mid - 3.0 * risk)
        near_sup = supports[0] if supports else None
        if near_sup and (price - near_sup["price"]) / price * 100 < 1.0 and near_sup["touches"] >= 1.5:
            flags.append("under_resistance")   # dla shorta: "nad wsparciem" — ta sama flaga, ten sam sens
            if "extended" not in flags:
                zone_low = max(price + 0.3 * atr, (r1["price"] - 0.5 * atr) if r1 else price + 0.3 * atr)
                zone_high = max(zone_high, zone_low + 0.4 * atr)
                entry_mid = (zone_low + zone_high) / 2
                risk = sl - entry_mid

    rr = abs(tp1 - entry_mid) / max(1e-12, abs(entry_mid - sl))
    if rr < MIN_RR:
        flags.append("low_rr")
    if sl_pct > MAX_SL_PCT:
        flags.append("wide_sl")

    if "low_rr" in flags or "wide_sl" in flags or ("weekend" in flags and ticker in WEEKEND_SKIP_TICKERS):
        verdict = "skip"
    elif "extended" in flags or "under_resistance" in flags:
        verdict = "wait"
    else:
        verdict = "ok"

    fmtp = lambda v: round(v, 6 if price < 1 else 4 if price < 100 else 2)
    nm = lambda l: (l["labels"][0] + ("+" if len(l["labels"]) > 1 else "")) if l else "—"
    note = (f"R:R {rr:.1f} · SL {sl_pct:.1f}% pod {nm(s1) if direction == 'long' else nm(r1)} · "
            f"TP1 {nm(r1) if direction == 'long' else nm(s1)} · ATR1h {atr_pct:.2f}%"
            + (f" · {', '.join(flags)}" if flags else ""))
    return {
        "entry_low": fmtp(zone_low), "entry_high": fmtp(zone_high), "sl": fmtp(sl),
        "tp1": fmtp(tp1), "tp2": fmtp(tp2), "rr": round(rr, 2), "atr_pct": round(atr_pct, 3),
        "ema21_1h": fmtp(ema21),
        "supports": [{"price": fmtp(l["price"]), "touches": round(l["touches"], 1), "labels": l["labels"]} for l in supports[:4]],
        "resistances": [{"price": fmtp(l["price"]), "touches": round(l["touches"], 1), "labels": l["labels"]} for l in resistances[:4]],
        "entry_quality": {"verdict": verdict, "flags": flags, "note": note},
    }



# ═══════════════════════════════════════════════════════════════════════════
# v0.4 krok 3 — WARSTWY W DECYZJI BOTA
# Smart Money (wieloryby Hyperliquid), CVD spot (Binance taker buy vs sell),
# Flush/Pump Risk lite (OI/funding HL + kompresja/SFP z Binance 1h) — do teraz
# liczone tylko w przeglądarce (werdykt na ekranie), bot ich nie widział.
# Tu: liczone na runnerze co cykl, zapisywane w decyzji jako `layers`, paper_bot
# wymaga zgody ≥2/3 warstw i respektuje veto (Flush high / wieloryby mocno przeciwnie).
# Cache (layers_cache.json — celowo NIE fusion_*.json, żeby find_fusion_input go nie brał):
#   - wieloryby odświeżane co 30 min (leaderboard + 30× clearinghouseState),
#   - seria OI z HL zapisywana co cykl (HL nie daje historii OI) → Δ4h/Δ24h.
# ═══════════════════════════════════════════════════════════════════════════
HL_INFO = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
LAYERS_CACHE = FUSION_DIR / "layers_cache.json"
SM_REFRESH_MIN = 30
SM_TICKERS = ["BTC", "ETH", "SOL", "XRP", "SUI"]


def _post_json(url, payload, timeout=20):
    req = ur.Request(url, data=json.dumps(payload).encode(), headers={"User-Agent": UA, "Content-Type": "application/json"})
    with ur.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read())


def _load_layers_cache():
    try:
        return json.loads(LAYERS_CACHE.read_text())
    except Exception:
        return {}


def _save_layers_cache(c):
    try:
        LAYERS_CACHE.write_text(json.dumps(c, separators=(",", ":")))
    except Exception as e:
        FETCH_ERRORS.append(f"layers.cache.save: {e}")


def fetch_hl_ctx():
    """Funding (godzinowy) + OI (USD) per coin z Hyperliquid metaAndAssetCtxs."""
    meta, ctxs = _post_json(HL_INFO, {"type": "metaAndAssetCtxs"})
    out = {}
    for u, c in zip(meta.get("universe", []), ctxs):
        name = u.get("name")
        if name in SM_TICKERS:
            px = float(c.get("markPx") or 0)
            out[name] = {"funding_h": float(c.get("funding") or 0) * 100,   # % na godzinę
                         "oi_usd": float(c.get("openInterest") or 0) * px, "mark": px}
    return out


def fetch_hl_whales():
    """Top-30 portfeli wg tygodniowego PnL (equity $5M–$400M, vlm tyg. >$10M), bez
    market-makerów (>12 pozycji) i hedgerów (≥3 pozycje jedna strona, PnL < −4% księgi).
    Ta sama logika co panel Smart Money w terminalu."""
    lb = _get_json(HL_LEADERBOARD, timeout=40)
    rows = []
    for r in lb.get("leaderboardRows", []):
        w = next((x[1] for x in r.get("windowPerformances", []) if x[0] == "week"), {}) or {}
        v = float(r.get("accountValue") or 0)
        if 5e6 <= v <= 4e8 and float(w.get("vlm") or 0) > 1e7:
            rows.append((float(w.get("pnl") or 0), r.get("ethAddress"), v))
    rows.sort(key=lambda x: -x[0])
    agg = {t: {"long_usd": 0.0, "short_usd": 0.0, "n_long": 0, "n_short": 0} for t in SM_TICKERS}
    wallets = 0
    for _pnl, addr, _v in rows[:30]:
        try:
            st = _post_json(HL_INFO, {"type": "clearinghouseState", "user": addr}, timeout=15)
        except Exception:
            continue
        ps = [p.get("position", {}) for p in st.get("assetPositions", [])]
        ps = [p for p in ps if float(p.get("szi") or 0) != 0]
        if len(ps) > 12:
            continue
        book = sum(abs(float(p.get("positionValue") or 0)) for p in ps)
        bpnl = sum(float(p.get("unrealizedPnl") or 0) for p in ps)
        sides = {("L" if float(p.get("szi")) > 0 else "S") for p in ps}
        if len(ps) >= 3 and len(sides) == 1 and book and bpnl / book * 100 < -4:
            continue
        wallets += 1
        for p in ps:
            coin = p.get("coin")
            if coin not in agg:
                continue
            val = abs(float(p.get("positionValue") or 0))
            if float(p.get("szi")) > 0:
                agg[coin]["long_usd"] += val; agg[coin]["n_long"] += 1
            else:
                agg[coin]["short_usd"] += val; agg[coin]["n_short"] += 1
    for t, a in agg.items():
        tot = a["long_usd"] + a["short_usd"]
        a["net"] = round((a["long_usd"] - a["short_usd"]) / tot, 3) if tot else 0.0
        a["total_usd"] = round(tot)
        a["verdict"] = ("long" if a["net"] >= 0.3 and tot >= 5e6 else
                        "short" if a["net"] <= -0.3 and tot >= 5e6 else "neutral")
    return {"ts": int(time.time()), "wallets": wallets, "agg": agg, "addrs": [a for _p, a, _v in rows[:30]]}


def fetch_layers_inputs():
    """Zbiera wejścia dla warstw: HL ctx (funding/OI) + wieloryby (cache 30 min) + seria OI."""
    cache = _load_layers_cache()
    now = int(time.time())
    try:
        ctx = fetch_hl_ctx()
    except Exception as e:
        FETCH_ERRORS.append(f"layers.hl_ctx: {type(e).__name__}: {e}")
        ctx = {}
    oi = cache.get("oi", {})
    for t, c in ctx.items():
        ser = [x for x in oi.get(t, []) if now - x[0] < 26 * 3600]
        ser.append([now, round(c["oi_usd"])])
        oi[t] = ser
    cache["oi"] = oi
    sm = cache.get("sm")
    if not sm or now - int(sm.get("ts", 0)) > SM_REFRESH_MIN * 60:
        try:
            sm = fetch_hl_whales()
            cache["sm"] = sm
        except Exception as e:
            FETCH_ERRORS.append(f"layers.hl_whales: {type(e).__name__}: {e}")
    _save_layers_cache(cache)
    return {"ctx": ctx, "oi": oi, "sm": sm or {}}


def _oi_change_pct(series, hours, now=None):
    now = now or int(time.time())
    if not series or len(series) < 2:
        return None
    cur = series[-1][1]
    target = now - hours * 3600
    past = min(series, key=lambda x: abs(x[0] - target))
    if abs(past[0] - target) > hours * 3600 * 0.5 or not past[1]:
        return None
    return round((cur - past[1]) / past[1] * 100, 2)


def compute_layers(ticker, direction, price, change_24h, klines_1h, inputs):
    """Zwraca dict layers: smart_money, cvd, flush, pump, agree, veto, note.
    klines_1h: Binance spot 1h ([ts,o,h,l,c,v,closeTs,quoteVol,trades,takerBuyBase,takerBuyQuote])."""
    if not klines_1h:
        return None
    ctx = (inputs.get("ctx") or {}).get(ticker, {})
    sm_all = (inputs.get("sm") or {}).get("agg", {})
    sm = sm_all.get(ticker)
    oi_ser = (inputs.get("oi") or {}).get(ticker, [])
    oi4 = _oi_change_pct(oi_ser, 4)
    oi24 = _oi_change_pct(oi_ser, 24)
    funding = ctx.get("funding_h")   # % / h

    # ── CVD spot 4h (Binance taker): delta = 2·takerBuyQuote − quoteVol ──
    last4 = klines_1h[-4:]
    try:
        qv = sum(float(k[7]) for k in last4)
        delta = sum(2 * float(k[10]) - float(k[7]) for k in last4)
        ratio = delta / qv if qv else 0.0
    except (IndexError, ValueError, TypeError):
        ratio = 0.0
    px4 = (float(klines_1h[-1][4]) / float(klines_1h[-5][4]) - 1) * 100 if len(klines_1h) >= 5 else 0.0
    oi_up = oi4 is not None and oi4 > 2.0
    oi_flat = oi4 is None or abs(oi4) <= 1.0
    if ratio > 0.08 and px4 > 0.5 and oi_up:
        cvd = "confirmed_up"
    elif ratio > 0.08 and px4 > 0.5:
        cvd = "spot_led_up"
    elif ratio > 0.08 and abs(px4) <= 0.5:
        cvd = "flat_spot_accum"
    elif px4 > 1.0 and ratio < 0.03 and oi_up:
        cvd = "lev_pump"
    elif ratio < -0.08 and px4 < -0.5 and oi_up:
        cvd = "confirmed_down"
    elif ratio < -0.08 and px4 < -0.5:
        cvd = "spot_led_down"
    elif ratio < -0.08 and px4 >= -0.5:
        cvd = "spot_distrib"
    elif abs(px4) > 1.0 and oi_up and abs(ratio) < 0.03:
        cvd = "flat_lev_only"
    else:
        cvd = "neutral"

    # ── Flush / Pump lite ──
    highs = [float(k[2]) for k in klines_1h]; lows = [float(k[3]) for k in klines_1h]; closes = [float(k[4]) for k in klines_1h]
    atr = _atr(klines_1h) or price * 0.01
    rng8 = (max(highs[-8:]) - min(lows[-8:])) if len(highs) >= 8 else atr * 8
    compressed = rng8 < 3.0 * atr
    sfp_bear = sfp_bull = False
    n = len(klines_1h)
    if n >= 40:
        for j in range(n - 12, n):
            prior_hi = max(highs[j - 20:j]); prior_lo = min(lows[j - 20:j])
            later = closes[j + 1:] or [closes[j]]
            if highs[j] > prior_hi and closes[j] < prior_hi and max(later) <= highs[j]:
                sfp_bear = True
            if lows[j] < prior_lo and closes[j] > prior_lo and min(later) >= lows[j]:
                sfp_bull = True
    fl = {}; pu = {}
    if oi24 is not None and oi24 >= 3 and (change_24h or 0) <= 0.5:
        fl["oi_divergence"] = 20 if (change_24h or 0) < 0 else 12
    if oi24 is not None and oi24 >= 3:
        pu["oi_build"] = 15
    if compressed:
        fl["compression"] = 15; pu["compression"] = 10
    if sfp_bear: fl["sfp_bear"] = 15
    if sfp_bull: pu["sfp_bull"] = 15
    # funding HL jest GODZINOWY w %: neutral ≈ 0.00125%/h (= 0.01%/8h)
    if funding is not None:
        if funding >= 0.006: fl["funding"] = 15       # ≈ 0.05%/8h — tłok w longach
        elif funding >= 0.0025: fl["funding"] = 10    # ≈ 0.02%/8h
        if funding <= -0.004: pu["funding"] = 20      # ≈ −0.03%/8h — shorty płacą, paliwo na squeeze
        elif funding <= -0.00125: pu["funding"] = 10
    if sm and sm.get("total_usd", 0) >= 5e6:
        if sm["net"] <= -0.3: fl["whales_short"] = 10
        if sm["net"] >= 0.3: pu["whales_long"] = 10
    if cvd == "lev_pump": fl["cvd_lev_pump"] = 10
    if cvd in ("flat_spot_accum", "spot_led_up"): pu["cvd_accum"] = 10
    if cvd == "flat_lev_only": fl["cvd_flat_lev"] = 5
    fs = min(100, sum(fl.values())); ps = min(100, sum(pu.values()))
    lvl = lambda v: "high" if v >= 55 else "elevated" if v >= 30 else "low"
    flush = {"score": fs, "level": lvl(fs), "parts": fl}
    pump = {"score": ps, "level": lvl(ps), "parts": pu}

    # ── Zgoda warstw z kierunkiem ──
    agree = 0; conf = []; veto = None
    if direction == "long":
        if sm and sm["verdict"] == "long": agree += 1; conf.append("sm")
        if cvd in ("confirmed_up", "spot_led_up", "flat_spot_accum"): agree += 1; conf.append("cvd")
        if flush["level"] == "low": agree += 1; conf.append("flush")
        if flush["level"] == "high": veto = "flush_high"
        elif sm and sm["verdict"] == "short" and sm["net"] <= -0.5 and sm["total_usd"] >= 1e7: veto = "whales_short"
    elif direction == "short":
        if sm and sm["verdict"] == "short": agree += 1; conf.append("sm")
        if cvd in ("confirmed_down", "spot_led_down", "spot_distrib", "lev_pump"): agree += 1; conf.append("cvd")
        if pump["level"] == "low": agree += 1; conf.append("pump")
        if pump["level"] == "high": veto = "pump_high"
        elif sm and sm["verdict"] == "long" and sm["net"] >= 0.5 and sm["total_usd"] >= 1e7: veto = "whales_long"
    note = (f"SM {sm['verdict']} ({sm['net']:+.2f}, ${sm['total_usd']/1e6:.0f}M)" if sm else "SM n/a") + \
           f" · CVD {cvd} ({ratio:+.2f}, OI4h {oi4 if oi4 is not None else '?'}%)" + \
           f" · Flush {fs} {flush['level']} · Pump {ps} {pump['level']}" + \
           (f" · fund {funding:+.4f}%/h" if funding is not None else "") + \
           (f" · VETO {veto}" if veto else "") + (f" · zgoda {agree}/3 [{','.join(conf)}]" if direction else "")
    return {"smart_money": sm, "cvd": {"read": cvd, "ratio_4h": round(ratio, 3), "px_4h": round(px4, 2), "oi_4h": oi4, "oi_24h": oi24},
            "funding_h": funding, "flush": flush, "pump": pump, "agree": agree, "confirms": conf, "veto": veto, "note": note}


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
    try:
        macro_ctx = fetch_macro_context()
    except Exception as e:
        print(f"[macro] failed: {e}"); macro_ctx = None

    regime = detect_regime(prices.get("BTC"), fng, dom)
    print(f"[regime] {regime}")

    try:
        layer_inputs = fetch_layers_inputs()
        print(f"[layers] HL ctx {len(layer_inputs.get('ctx') or {})} coins · whales {(layer_inputs.get('sm') or {}).get('wallets', 0)} portfeli")
    except Exception as e:
        FETCH_ERRORS.append(f"layers.inputs: {type(e).__name__}: {e}"); layer_inputs = {}

    decisions = []
    for ticker in ["BTC", "ETH", "SOL", "XRP", "SUI"]:
        klines_d220 = fetch_klines(ticker, limit=220) or []   # v0.4: 220 dni → MA200D/MA50D + swingi dzienne
        klines = klines_d220[-30:] if klines_d220 else fetch_klines(ticker)  # daily, 30 candles — trend/momentum baseline
        daily_ta_score = compute_ta_score(klines)
        klines_1h_200 = fetch_klines(ticker, interval="1h", limit=200) or []  # v0.4: ~8 dni → swingi 1h, ATR, EMA21
        klines_1h = klines_1h_200[-50:] if klines_1h_200 else fetch_klines(ticker, interval="1h", limit=50)
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

        # v0.4 — Entry/SL/TP ze STRUKTURY (wsparcia/opory 1h+D, MA200D, EMA21, ATR),
        # nie z % od ceny. Patrz compute_levels(). Fallback na stare % tylko gdy brak danych.
        levels = None
        if size > 0 and direction in ("long", "short"):
            try:
                levels = compute_levels(ticker, direction, current_price,
                                        prices.get(ticker, {}).get("change_24h", 0), klines_d220, klines_1h_200)
            except Exception as e:
                FETCH_ERRORS.append(f"levels.{ticker}: {type(e).__name__}: {e}")
                levels = None
        try:
            layers = compute_layers(ticker, direction, current_price, prices.get(ticker, {}).get("change_24h", 0), klines_1h_200, layer_inputs)
        except Exception as e:
            FETCH_ERRORS.append(f"layers.{ticker}: {type(e).__name__}: {e}"); layers = None
        if levels:
            entry_low, entry_high, sl, tp1, tp2 = levels["entry_low"], levels["entry_high"], levels["sl"], levels["tp1"], levels["tp2"]
        elif size > 0 and direction == "long":
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
            "levels": ({k: levels[k] for k in ("rr", "atr_pct", "ema21_1h", "supports", "resistances")} if levels else None),
            "entry_quality": (levels["entry_quality"] if levels else None),
            "layers": layers,
            "choch_override": choch_override,
            "risk_flag": f"Auto-generated {datetime.now().strftime('%H:%M')}. TA {ta_score}/100 (daily {daily_ta_score} · 1h momo {short_term_score} · {ms_note}). Current ${current_price:.2f} ({prices[ticker]['change_24h']:+.2f}% 24h)."
                         + (" ⚡ CHoCH OVERRIDE — 1h change of character, wchodzi mimo regime/score." if choch_override else ""),
            "invalidation_note": (f"SL @ ${sl}" + (f" · {levels['entry_quality']['note']}" if levels else "")) if sl else "Not entered",
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
        "pump_radar": load_pump_radar(),
        "macro_events": generate_macro_events(days_ahead=45),
        "etf": {k: etf_flows.get(k) for k in ("btc", "eth")} if etf_flows else None,
        "macro_context": macro_ctx,
        "fetch_errors": FETCH_ERRORS,
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


def load_pump_radar():
    """pump_radar.json z pump_radar.py (krok w workflow przed auto_fusion). Zwraca None gdy brak/stary (>30 min)."""
    try:
        p = FUSION_DIR / "pump_radar.json"
        d = json.loads(p.read_text())
        ts = datetime.fromisoformat(d["generated_at"].replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - ts).total_seconds() > 30 * 60:
            return None
        return d
    except Exception:
        return None


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
