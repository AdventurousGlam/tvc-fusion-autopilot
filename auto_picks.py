#!/usr/bin/env python3
"""
TVC Auto-Picks — algorytmiczny poranny shortlist (zero kredytów Claude, działa w GitHub Actions).

Zastępuje scheduled task Cowork "morning-crypto-picks", który wymagał obudzonego Maca.
Ten sam framework 4 kategorii, ale policzony z liczb zamiast z researchu:

  1. Momentum / wybicia  — 24h & 7d zmiana, close > EMA20 > EMA50, wolumen 24h vs śr. 30d, higher-highs
  2. Derywaty            — funding ekstremalny (ujemny = paliwo na short squeeze, dodatni = crowded long),
                           OI (holdVol) względem obrotu
  3. Nowe listingi       — kontrakt utworzony ≤ 30 dni temu z realnym obrotem
  4. Mean reversion      — RSI14 < 32, 7d ≤ −10%, blisko 20-dniowego dołka, tylko quality (mcap ≥ $500M)

Uniwersum: perpy USDT na MEXC (contract.mexc.com, publiczne API, działa z runnerów GitHub).
Filtr płynności: obrót 24h ≥ $5M. Filtr mcap (CoinGecko): ≥ $20M, wyjątek dla nowych listingów.
Wyjście: crypto_picks.json (schemat czytany przez terminal) + opcjonalnie Telegram.
Raz dziennie po PICKS_HOUR_UTC — kolejne cykle autopilota są no-op, dopóki data się nie zmieni.
"""

import json
import os
import ssl
import sys
import time
import argparse
import urllib.request as ur
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

UA = "Mozilla/5.0 tvc-auto-picks/1.0"
FUSION_DIR = Path.home() / "Claude" / "TVCFusion"
OUT_PATH = FUSION_DIR / "crypto_picks.json"
PICKS_HOUR_UTC = 5              # generuj raz dziennie po tej godzinie (07:00 PL)

MIN_TURNOVER_USD = 5_000_000    # płynność — poniżej złe fille
MIN_MCAP_USD = 20_000_000
NEW_LISTING_DAYS = 30
NEW_LISTING_MIN_TURNOVER = 20_000_000
QUALITY_MCAP = 500_000_000      # mean reversion tylko na dużych/średnich
MAX_PER_CATEGORY = 2
TOP_N = 5
RADAR_N = 3
EXCLUDE = {"USDT", "USDC", "USDE", "DAI", "FDUSD", "TUSD", "USD1", "PYUSD", "BUSD", "EUR", "GOLD", "XAUT", "PAXG",
           # MEXC listuje też tokenizowane akcje, indeksy i surowce jako perpy — to nie crypto picks
           "XAU", "SILVER", "USOIL", "UKOIL", "NAS100", "SPX500", "SPY", "SOXL", "SOXS", "DRAM", "TESLA", "NVIDIA",
           "COIN", "MSTR", "HOOD", "CRCL", "GME", "AMZN", "AAPL", "MSFT", "GOOGL", "META", "NFLX", "PLTR", "AMD", "INTC"}
EXCLUDE_SUBSTR = ("STOCK", "3L", "3S", "5L", "5S", "2L", "2S")


def _is_crypto(base):
    if base in EXCLUDE:
        return False
    return not any(s in base for s in EXCLUDE_SUBSTR)


def _get_json(url, timeout=20, retries=3):
    req = ur.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last = None
    for attempt in range(1, retries + 1):
        try:
            with ur.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(3 * attempt)
    raise last


# ─── Dane ────────────────────────────────────────────────────────────────────

def fetch_universe():
    """Wszystkie perpy MEXC + detale (createTime, state)."""
    tick = _get_json("https://contract.mexc.com/api/v1/contract/ticker")["data"]
    det = _get_json("https://contract.mexc.com/api/v1/contract/detail")["data"]
    detail = {d["symbol"]: d for d in det}
    out = []
    for t in tick:
        sym = t.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue
        base = sym[:-5]
        d = detail.get(sym, {})
        if d.get("state", 0) != 0 or d.get("isHidden"):
            continue
        if not _is_crypto(base):
            continue
        rr = t.get("riseFallRates") or {}
        out.append({
            "symbol": sym, "ticker": base,
            "price": float(t.get("lastPrice") or 0),
            "r24": float(t.get("riseFallRate") or 0) * 100,
            "r7": float(rr.get("r7") or 0) * 100,
            "r30": float(rr.get("r30") or 0) * 100,
            "turnover": float(t.get("amount24") or 0),
            "oi": float(t.get("holdVol") or 0),
            "funding": float(t.get("fundingRate") or 0) * 100,   # % per 8h
            "high24": float(t.get("high24Price") or 0), "low24": float(t.get("lower24Price") or 0),
            "created": (d.get("createTime") or 0) / 1000,
            "is_new": bool(d.get("isNew")),
            "max_lev": d.get("maxLeverage"),
        })
    return out


def fetch_mcap_map():
    """symbol → market cap (USD) z CoinGecko top-500. Przy 429 zwraca {} (filtr wyłączony)."""
    m = {}
    for page in (1, 2):
        try:
            rows = _get_json(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false", retries=2)
            for r in rows:
                s = (r.get("symbol") or "").upper()
                if s and s not in m and r.get("market_cap"):
                    m[s] = float(r["market_cap"])
            time.sleep(2)
        except Exception as e:
            print(f"[picks] CoinGecko page {page} failed ({e}) — mcap filter partial")
    return m


def fetch_daily(symbol, days=70):
    end = int(time.time())
    j = _get_json(f"https://contract.mexc.com/api/v1/contract/kline/{symbol}?interval=Day1&start={end - days * 86400}&end={end}")
    d = j.get("data") or {}
    n = len(d.get("close") or [])
    return [{"t": d["time"][i], "o": d["open"][i], "h": d["high"][i], "l": d["low"][i], "c": d["close"][i], "v": (d.get("amount") or d.get("vol") or [0] * n)[i]} for i in range(n)]


def fetch_fng():
    try:
        x = _get_json("https://api.alternative.me/fng/?limit=1")["data"][0]
        return int(x["value"]), x["value_classification"]
    except Exception:
        return None, None


# ─── Wskaźniki ───────────────────────────────────────────────────────────────

def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n; al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n; al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def analyze(row, k):
    """Wskaźniki z dziennych świec (ostatnia świeca = dziś, niedomknięta)."""
    if len(k) < 25:
        return None
    closes = [c["c"] for c in k]
    vols = [c["v"] for c in k]
    done = k[:-1]                      # domknięte
    px = row["price"] or closes[-1]
    e20, e50 = ema(closes, 20), ema(closes, 50) if len(closes) >= 50 else None
    last20 = done[-20:]
    hi20 = max(c["h"] for c in last20); lo20 = min(c["l"] for c in last20)
    vol_avg30 = sum(vols[-31:-1]) / max(1, len(vols[-31:-1]))
    vol_ratio = (row["turnover"] / vol_avg30) if vol_avg30 else None
    # higher highs: 3 ostatnie domknięte szczyty tygodniowe rosną
    hh = all(max(c["h"] for c in done[-7 * (i + 1):len(done) - 7 * i]) > max(c["h"] for c in done[-7 * (i + 2):len(done) - 7 * (i + 1)]) for i in range(2)) if len(done) >= 21 else False
    return {"px": px, "ema20": e20, "ema50": e50, "hi20": hi20, "lo20": lo20, "rsi": rsi(closes[:-1]), "vol_ratio": vol_ratio, "hh": hh,
            "dist_lo20": (px - lo20) / px * 100 if px else None, "dist_hi20": (hi20 - px) / px * 100 if px else None}


# ─── Scoring per kategoria ───────────────────────────────────────────────────

def score_momentum(r, a):
    if not a or a["ema20"] is None:
        return None
    if r["r24"] < 3 or r["r7"] < 8 or not (a["px"] > a["ema20"]) or (a["ema50"] and not a["ema20"] > a["ema50"]):
        return None
    s = min(r["r7"], 40) + min(r["r24"], 15) * 1.5 + (min(a["vol_ratio"], 4) * 8 if a["vol_ratio"] else 0) + (10 if a["hh"] else 0)
    if r["r24"] > 25:
        s *= 0.7   # pościg
    return s


def score_derivatives(r, a):
    f = r["funding"]
    if abs(f) < 0.03:
        return None
    if f <= -0.03 and r["r24"] > -2:
        # shorty płacą, cena nie spada = paliwo na squeeze
        return 20 + min(-f, 0.3) * 150 + max(r["r24"], 0) * 2
    if f >= 0.05 and r["r24"] < 3:
        # crowded long i cena nie idzie = ryzyko flushu (kandydat short / watch)
        return 15 + min(f, 0.3) * 120
    return None


def score_new_listing(r, a):
    if not r["created"]:
        return None
    age = (time.time() - r["created"]) / 86400
    if age > NEW_LISTING_DAYS or r["turnover"] < NEW_LISTING_MIN_TURNOVER:
        return None
    return 20 + min(r["turnover"] / 1e6, 200) * 0.2 + max(r["r24"], 0) + (10 if age < 7 else 0)


def score_mean_reversion(r, a, mcap):
    if not a or a["rsi"] is None or (mcap or 0) < QUALITY_MCAP:
        return None
    if a["rsi"] >= 32 or r["r7"] > -10 or a["dist_lo20"] is None or a["dist_lo20"] > 4:
        return None
    return (32 - a["rsi"]) * 2 + min(-r["r7"], 30) + (10 if r["r24"] > 0 else 0)   # r24 > 0 = pierwsze odbicie


# ─── Budowa picka ────────────────────────────────────────────────────────────

def fmtp(v):
    if v is None:
        return None
    return round(v, 6 if v < 0.01 else 4 if v < 1 else 2)


def build_pick(cat, r, a, mcap, score):
    px = r["price"]
    lo, hi = (a["lo20"], a["hi20"]) if a else (r["low24"], r["high24"])
    f = r["funding"]
    flags = []
    if r["turnover"] < 20e6:
        flags.append(f"płytka płynność — obrót 24h ${r['turnover']/1e6:.0f}M")
    if f >= 0.05:
        flags.append(f"crowded long, funding {f:+.3f}%/8h")
    if f <= -0.03:
        flags.append(f"funding ujemny {f:+.3f}%/8h (shorty płacą)")
    if r["r24"] > 15:
        flags.append(f"+{r['r24']:.0f}% w 24h — pościg, czekaj na retest")
    if datetime.now(timezone.utc).weekday() >= 5:
        flags.append("weekend — cienka książka")

    if cat == "Momentum":
        direction = "long"
        thesis = (f"7d {r['r7']:+.1f}%, 24h {r['r24']:+.1f}%, cena nad EMA20{' > EMA50' if a and a['ema50'] else ''}"
                  f"{', wolumen ' + format(a['vol_ratio'], '.1f') + '× śr. 30d' if a and a['vol_ratio'] else ''}"
                  f"{', wyższe szczyty tygodniowe' if a and a['hh'] else ''}. Trend z paliwem — wejście na cofnięciu do EMA20, nie na świecy.")
        support, resistance, inval = (a["ema20"] if a else lo), hi, lo
    elif cat == "Derivatives":
        if f <= -0.03:
            direction = "long"
            thesis = (f"Funding {f:+.3f}%/8h — shorty płacą za utrzymanie pozycji, a cena się trzyma ({r['r24']:+.1f}% 24h). "
                      f"Klasyczny układ pod short squeeze: wybicie nad {fmtp(hi)} zmusza shorty do zamknięcia.")
            support, resistance, inval = lo, hi, lo * 0.97
        else:
            direction = "short" if r["r24"] < 0 else "watch"
            thesis = (f"Funding {f:+.3f}%/8h — longi przepłacają, a cena nie idzie ({r['r24']:+.1f}% 24h). "
                      f"Crowded long = paliwo na flush; utrata {fmtp(lo)} uruchamia likwidacje.")
            support, resistance, inval = lo, hi, hi * 1.03
    elif cat == "New listing":
        age = (time.time() - r["created"]) / 86400
        direction = "watch" if r["r24"] < 0 else "long"
        thesis = (f"Listing perp na MEXC {age:.0f} dni temu, obrót 24h ${r['turnover']/1e6:.0f}M, {r['r24']:+.1f}% 24h. "
                  f"Nowe listingi mają największą zmienność w pierwszych 2 tygodniach — grać małym rozmiarem, tylko z jasnym poziomem.")
        support, resistance, inval = r["low24"], r["high24"], r["low24"] * 0.95
        flags.append("nowy listing — brak historii, ekstremalna zmienność")
    else:  # Mean reversion
        direction = "long"
        thesis = (f"RSI14 {a['rsi']:.0f} (wyprzedany), 7d {r['r7']:+.1f}%, {a['dist_lo20']:.1f}% nad 20-dniowym dołkiem, mcap ${mcap/1e9:.1f}B. "
                  f"Quality large-cap na wsparciu{' — pierwsza zielona świeca' if r['r24'] > 0 else ' — jeszcze bez potwierdzenia'}. Cel: powrót do EMA20 {fmtp(a['ema20'])}.")
        support, resistance, inval = lo, (a["ema20"] if a else hi), lo * 0.97

    return {
        "ticker": r["ticker"], "name": None, "exchange": "MEXC perp",
        "category": cat, "direction": direction,
        "price": fmtp(px), "change_24h_pct": round(r["r24"], 2), "change_7d_pct": round(r["r7"], 2),
        "thesis": thesis,
        "support": fmtp(support), "resistance": fmtp(resistance), "invalidation": fmtp(inval),
        "risk_flag": "; ".join(flags) if flags else None,
        "source_url": f"https://www.mexc.com/futures/{r['symbol']}",
        "funding_pct_8h": round(f, 4), "turnover_24h_usd": round(r["turnover"]), "mcap_usd": round(mcap) if mcap else None,
        "rsi14": round(a["rsi"], 1) if a and a["rsi"] is not None else None,
        "score": round(score, 1),
    }


def market_context(universe, fng):
    btc = next((r for r in universe if r["ticker"] == "BTC"), None)
    eth = next((r for r in universe if r["ticker"] == "ETH"), None)
    liquid = [r for r in universe if r["turnover"] >= MIN_TURNOVER_USD]
    up = sum(1 for r in liquid if r["r24"] > 0)
    breadth = up / len(liquid) * 100 if liquid else 0
    parts = []
    if btc:
        parts.append(f"BTC ${btc['price']:,.0f} ({btc['r24']:+.1f}% 24h, {btc['r7']:+.1f}% 7d)")
    if eth:
        parts.append(f"ETH ${eth['price']:,.0f} ({eth['r24']:+.1f}%)")
    parts.append(f"szerokość rynku: {breadth:.0f}% płynnych perpów na plusie 24h")
    if fng[0] is not None:
        parts.append(f"F&G {fng[0]} ({fng[1]})")
    mood = "risk-on" if breadth > 60 else "risk-off" if breadth < 40 else "mieszany"
    return ". ".join(parts) + f". Nastrój: {mood}."


# ─── Główna procedura ────────────────────────────────────────────────────────

def generate(force=False, telegram=False):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if OUT_PATH.exists() and not force:
        try:
            if json.loads(OUT_PATH.read_text()).get("date") == today:
                print(f"[picks] {today} już wygenerowane — pomijam"); return False
        except Exception:
            pass
    if datetime.now(timezone.utc).hour < PICKS_HOUR_UTC and not force:
        print(f"[picks] przed {PICKS_HOUR_UTC:02d}:00 UTC — czekam"); return False

    print("[picks] pobieram uniwersum MEXC...")
    universe = fetch_universe()
    print(f"[picks] {len(universe)} perpów USDT")
    mcap = fetch_mcap_map()
    print(f"[picks] mcap map: {len(mcap)} symboli")
    fng = fetch_fng()

    liquid = [r for r in universe if r["turnover"] >= MIN_TURNOVER_USD]
    # Prefiltr kandydatów — klines tylko dla tych, którzy mają szansę w którejś kategorii
    cands = {}
    for r in sorted(liquid, key=lambda x: -x["r7"])[:25]: cands[r["symbol"]] = r
    for r in sorted(liquid, key=lambda x: -x["r24"])[:15]: cands[r["symbol"]] = r
    for r in sorted(liquid, key=lambda x: x["r7"])[:20]: cands[r["symbol"]] = r
    for r in sorted(liquid, key=lambda x: x["funding"])[:10]: cands[r["symbol"]] = r
    for r in sorted(liquid, key=lambda x: -x["funding"])[:10]: cands[r["symbol"]] = r
    for r in liquid:
        if r["created"] and (time.time() - r["created"]) / 86400 <= NEW_LISTING_DAYS:
            cands[r["symbol"]] = r
    print(f"[picks] {len(cands)} kandydatów → klines")

    scored = []
    for sym, r in cands.items():
        mc = mcap.get(r["ticker"]) or mcap.get(r["ticker"].replace("1000", "", 1))
        is_new = bool(r["created"]) and (time.time() - r["created"]) / 86400 <= NEW_LISTING_DAYS
        # Gdy mamy mapę mcap: brak wpisu = nie-crypto (akcja/surowiec) albo mikro-cap → pomijamy, chyba że nowy listing
        if mcap and not is_new and (mc is None or mc < MIN_MCAP_USD):
            continue
        try:
            a = analyze(r, fetch_daily(sym))
        except Exception as e:
            print(f"[picks] {sym} klines failed: {e}"); a = None
        time.sleep(0.15)
        for cat, fn in (("Momentum", lambda: score_momentum(r, a)), ("Derivatives", lambda: score_derivatives(r, a)),
                        ("New listing", lambda: score_new_listing(r, a)), ("Mean reversion", lambda: score_mean_reversion(r, a, mc))):
            s = fn()
            if s:
                scored.append((s, cat, r, a, mc))

    scored.sort(key=lambda x: -x[0])
    picks, radar, per_cat, used = [], [], {}, set()
    for s, cat, r, a, mc in scored:
        if r["ticker"] in used:
            continue
        if len(picks) < TOP_N and per_cat.get(cat, 0) < MAX_PER_CATEGORY:
            picks.append(build_pick(cat, r, a, mc, s)); per_cat[cat] = per_cat.get(cat, 0) + 1; used.add(r["ticker"])
        elif len(radar) < RADAR_N:
            radar.append({"ticker": r["ticker"], "note": f"{cat}: {r['r24']:+.1f}% 24h, {r['r7']:+.1f}% 7d, funding {r['funding']:+.3f}%"}); used.add(r["ticker"])
        if len(picks) >= TOP_N and len(radar) >= RADAR_N:
            break

    out = {
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "auto_picks.py (MEXC perps · algorytmiczny screener, bez Claude)",
        "market_context": market_context(universe, fng),
        "universe": {"perps": len(universe), "liquid": len(liquid), "candidates": len(cands), "scored": len(scored)},
        "picks": picks,
        "radar": radar,
        "disclaimer": "Analiza informacyjna z danych rynkowych, nie porada inwestycyjna. Stop loss i sizing przed wejściem.",
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[picks] zapisano {OUT_PATH.name}: {len(picks)} picks, {len(radar)} radar")
    for p in picks:
        print(f"  {p['category']:15s} {p['direction']:5s} {p['ticker']:8s} {p['change_24h_pct']:+.1f}% score {p['score']}")
    if telegram:
        _send_telegram(out)
    return True


def _send_telegram(out):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(); chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return
    icon = {"Momentum": "🚀", "Derivatives": "📊", "New listing": "🆕", "Mean reversion": "↩️"}
    arrow = {"long": "▲", "short": "▼", "watch": "👀"}
    lines = [f"🎯 <b>Crypto Picks {out['date']}</b>", out["market_context"], ""]
    for p in out["picks"]:
        lines.append(f"{icon.get(p['category'], '📌')} <b>{p['ticker']}</b> {arrow.get(p['direction'], '')} {p['direction']} · {p['change_24h_pct']:+.1f}% 24h · S {p['support']} / R {p['resistance']}")
        if p.get("risk_flag"):
            lines.append(f"   ⚠ {p['risk_flag']}")
    if out["radar"]:
        lines.append(""); lines.append("📡 " + " · ".join(f"{r['ticker']}" for r in out["radar"]))
    lines.append(""); lines.append("<i>Szczegóły w terminalu → panel Crypto Picks</i>")
    import urllib.parse as up
    try:
        data = up.urlencode({"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
        with ur.urlopen(ur.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=10, context=SSL_CTX) as r:
            print(f"[picks] telegram {r.status}")
    except Exception as e:
        print(f"[picks] telegram failed: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TVC Auto-Picks (MEXC screener, no Claude)")
    ap.add_argument("--force", action="store_true", help="generuj mimo istniejącego pliku z dziś / przed godziną")
    ap.add_argument("--telegram", action="store_true", help="wyślij podsumowanie na Telegram (wymaga env)")
    args = ap.parse_args()
    try:
        generate(force=args.force, telegram=args.telegram)
    except Exception as e:
        print(f"[picks] FAILED: {e}")
        sys.exit(0)   # picks nie mogą wywracać cyklu autopilota
