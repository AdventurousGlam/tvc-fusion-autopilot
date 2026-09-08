#!/usr/bin/env python3
"""
TVC Pump Radar — skan całego rynku perpów co 5 min pod krótkoterminowe pumpy.

Nie "przewiduje" pumpów. Mierzy trzy rzeczy, które je poprzedzają, i jedną, która je
zaczyna:
  PALIWO      — skok OI przy płaskiej cenie (ktoś buduje pozycję po cichu), funding
                ujemny (shorty płacą), kompresja (ciasny range vs ATR).
  WIELORYBY   — top-30 portfeli Hyperliquid (te same co Smart Money) kupuje token
                netto w ostatniej godzinie (userFillsByTime).
  KATALIZATOR — nowy listing: Upbit (notices API), Hyperliquid (nowy perp w universe),
                MEXC (nowy kontrakt), Binance (CMS API przez jina). Bybit geo-blokuje runnery — pominięty.
  ZAPŁON      — wolumen 15m ≥ 4σ vs 24h + cena > +1.5% w 15m: pump już ruszył,
                masz 5–15 min przewagi. Oznaczane `ignited`, nie liczone jako "przed".

Każdy alert (score ≥ WATCH) trafia do pump_radar_alerts.json z ceną i czasem; kolejne
cykle dopisują zwrot +1h / +4h / +24h. Statystyki (hit rate, średni zwrot, per
składnik) są w pump_radar.json → Gist → panel w terminalu. To jest cały sens:
po 2 tygodniach wiemy, które składniki mają edge, a które to szum.

Źródła dostępne z runnera GitHub: Hyperliquid (info), MEXC contract API, Upbit
notices, jina (Binance announcements), Bybit (może być 403 — z fallbackiem).
Uruchamiane z workflow przed auto_fusion; wynik ładowany do fusion json.
Nigdy nie wywraca cyklu (exit 0).
"""
import json
import os
import ssl
import sys
import time
import argparse
import re
import urllib.request as ur
import urllib.parse as up
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

UA = "Mozilla/5.0 tvc-pump-radar/1.0"
FUSION_DIR = Path.home() / "Claude" / "TVCFusion"
OUT_PATH = FUSION_DIR / "pump_radar.json"
ALERTS_PATH = FUSION_DIR / "pump_radar_alerts.json"
CACHE_PATH = FUSION_DIR / "radar_cache.json"
LAYERS_CACHE = FUSION_DIR / "layers_cache.json"     # wieloryby (top-30) z auto_fusion — współdzielone

HL_INFO = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

MIN_TURNOVER_USD = 3_000_000    # poniżej — brak płynności, "pump" to 2 zlecenia
SCORE_WATCH = 35
SCORE_HIGH = 55
MAX_KLINE_CANDIDATES = 40       # ile tokenów dostaje 5m klines (kompresja + zapłon) w cyklu
ALERT_COOLDOWN_H = 6            # ten sam token nie alertuje częściej niż co 6h (chyba że HIGH po WATCH)
HIT_1H_PCT = 2.0                # "trafienie" = +2% w 1h lub +3% w 4h
HIT_4H_PCT = 3.0
EXCLUDE = {"USDT", "USDC", "USDE", "DAI", "FDUSD", "TUSD", "USD1", "PYUSD", "BUSD", "EUR", "GOLD", "XAUT", "PAXG",
           "BTC", "ETH"}       # majors nie "pompują" w tym sensie; są w Fusion
EXCLUDE_SUBSTR = ("STOCK", "3L", "3S", "5L", "5S", "2L", "2S")
ERRORS = []


def log(msg):
    print(f"[radar] {msg}")


def _get_json(url, timeout=20, retries=2, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = ur.Request(url, headers=h)
    last = None
    for attempt in range(1, retries + 1):
        try:
            with ur.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(2 * attempt)
    raise last


def _post_json(url, payload, timeout=20):
    req = ur.Request(url, data=json.dumps(payload).encode(), headers={"User-Agent": UA, "Content-Type": "application/json"})
    with ur.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read())


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save(path, obj):
    path.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


def _is_crypto(base):
    return base not in EXCLUDE and not any(s in base for s in EXCLUDE_SUBSTR)


# ─── Dane rynkowe ───────────────────────────────────────────────────────────

def fetch_hl_universe():
    """coin → {price, oi_usd, funding_8h, vol24, px24}. HL funding jest godzinowy → ×8."""
    meta, ctxs = _post_json(HL_INFO, {"type": "metaAndAssetCtxs"})
    out = {}
    for u, c in zip(meta.get("universe", []), ctxs):
        name = u.get("name")
        if not name or u.get("isDelisted"):
            continue
        px = float(c.get("markPx") or 0)
        prev = float(c.get("prevDayPx") or 0)
        out[name] = {
            "src": "HL", "price": px, "oi_usd": float(c.get("openInterest") or 0) * px,
            "funding_8h": float(c.get("funding") or 0) * 100 * 8, "vol24": float(c.get("dayNtlVlm") or 0),
            "r24": ((px / prev - 1) * 100) if prev else 0.0,
        }
    return out


def fetch_mexc_universe():
    tick = _get_json("https://contract.mexc.com/api/v1/contract/ticker")["data"]
    det = {d["symbol"]: d for d in _get_json("https://contract.mexc.com/api/v1/contract/detail")["data"]}
    out = {}
    for t in tick:
        sym = t.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue
        base = sym[:-5]
        d = det.get(sym, {})
        if d.get("state", 0) != 0 or d.get("isHidden") or not _is_crypto(base):
            continue
        px = float(t.get("lastPrice") or 0)
        out[base] = {
            "src": "MEXC", "symbol": sym, "price": px,
            "oi_usd": float(t.get("holdVol") or 0) * float(d.get("contractSize") or 1) * px,
            "funding_8h": float(t.get("fundingRate") or 0) * 100, "vol24": float(t.get("amount24") or 0),
            "r24": float(t.get("riseFallRate") or 0) * 100,
            "created": (d.get("createTime") or 0) / 1000, "is_new": bool(d.get("isNew")),
        }
    return out


def fetch_mexc_klines(symbol, interval="Min5", bars=300):
    end = int(time.time())
    step = {"Min5": 300, "Min15": 900}[interval]
    j = _get_json(f"https://contract.mexc.com/api/v1/contract/kline/{symbol}?interval={interval}&start={end - bars * step}&end={end}", retries=1)
    d = j.get("data") or {}
    n = len(d.get("close") or [])
    vol = d.get("amount") or d.get("vol") or [0] * n
    return [{"t": d["time"][i], "o": float(d["open"][i]), "h": float(d["high"][i]), "l": float(d["low"][i]),
             "c": float(d["close"][i]), "v": float(vol[i])} for i in range(n)]


def fetch_hl_klines(coin, interval="5m", hours=25):
    end = int(time.time() * 1000)
    rows = _post_json(HL_INFO, {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": end - hours * 3600 * 1000, "endTime": end}})
    return [{"t": r["t"] // 1000, "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"]) * float(r["c"])} for r in rows]


# ─── Wieloryby HL: fills top-30 w ostatniej godzinie ────────────────────────

def top_wallets():
    """Adresy top-30 z cache warstw (auto_fusion odświeża co 30 min); fallback: leaderboard."""
    lc = _load(LAYERS_CACHE, {})
    addrs = (lc.get("sm") or {}).get("addrs")
    if addrs:
        return addrs
    lb = _get_json(HL_LEADERBOARD, timeout=40)
    rows = []
    for r in lb.get("leaderboardRows", []):
        w = next((x[1] for x in r.get("windowPerformances", []) if x[0] == "week"), {}) or {}
        v = float(r.get("accountValue") or 0)
        if 5e6 <= v <= 4e8 and float(w.get("vlm") or 0) > 1e7:
            rows.append((float(w.get("pnl") or 0), r.get("ethAddress")))
    rows.sort(key=lambda x: -x[0])
    return [a for _p, a in rows[:30]]


def fetch_whale_flow(cache):
    """Netto USD (buy − sell) per coin z fills top-30 w ostatnich 60 min. Cache trzyma
    fills 2h, dociąga tylko od ostatniego ts (userFillsByTime)."""
    now_ms = int(time.time() * 1000)
    fills = [f for f in cache.get("fills", []) if now_ms - f[0] < 2 * 3600 * 1000]
    since = cache.get("fills_since") or (now_ms - 60 * 60 * 1000)
    try:
        addrs = top_wallets()
    except Exception as e:
        ERRORS.append(f"whales.addrs: {e}"); addrs = []
    got = 0
    for a in addrs:
        try:
            rows = _post_json(HL_INFO, {"type": "userFillsByTime", "user": a, "startTime": since - 60_000}, timeout=15)
        except Exception:
            continue
        for r in rows or []:
            coin = r.get("coin"); px = float(r.get("px") or 0); sz = float(r.get("sz") or 0)
            if not coin or coin.startswith("@") or not px:
                continue
            side = 1 if r.get("side") == "B" else -1
            fills.append([int(r.get("time") or now_ms), coin, side * px * sz, a[:6]])
            got += 1
    # dedupe (ts, coin, usd, addr)
    seen = set(); ded = []
    for f in fills:
        k = (f[0], f[1], round(f[2], 2), f[3])
        if k not in seen:
            seen.add(k); ded.append(f)
    cache["fills"] = ded; cache["fills_since"] = now_ms
    flow = {}
    for ts, coin, usd, addr in ded:
        if now_ms - ts < 3600 * 1000:
            d = flow.setdefault(coin, {"net": 0.0, "buy": 0.0, "sell": 0.0, "wallets": set()})
            d["net"] += usd; d["buy" if usd > 0 else "sell"] += abs(usd); d["wallets"].add(addr)
    for c in flow.values():
        c["wallets"] = len(c["wallets"])
    log(f"whales: {len(addrs)} portfeli, {got} nowych fills, {len(flow)} coinów z flow 1h")
    return flow


# ─── Listingi / katalizatory ────────────────────────────────────────────────

def fetch_listings(cache, hl_coins, mexc_uni):
    """Zwraca {coin: [źródła]} dla katalizatorów z ostatnich 6h."""
    now = time.time()
    seen = cache.setdefault("listings", {})      # key → ts pierwszego zobaczenia
    events = []
    # Hyperliquid — nowy perp w universe
    known = set(cache.get("hl_universe") or [])
    if known:
        for c in hl_coins:
            if c not in known:
                events.append((f"HL:{c}", c, "Hyperliquid perp"))
    cache["hl_universe"] = sorted(hl_coins)
    # MEXC — nowy kontrakt (createTime < 24h) lub isNew
    for base, m in mexc_uni.items():
        if m.get("created") and now - m["created"] < 24 * 3600:
            events.append((f"MEXC:{base}", base, "MEXC perp"))
    # Upbit — announcements API (stary /notices zwraca 404). Tytuły KR: "… 신규 거래지원 안내 (XXX)"
    try:
        j = _get_json("https://api-manager.upbit.com/api/v1/announcements?os=web&page=1&per_page=20&category=trade", timeout=15,
                      headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"})
        d = j.get("data") or {}
        for n in d.get("notices") or d.get("list") or []:
            title = n.get("title") or ""
            if "거래지원" in title or "Market Support" in title or "listing" in title.lower():
                for tk in re.findall(r"\(([A-Z0-9]{2,10})\)", title):
                    events.append((f"UPBIT:{n.get('id')}:{tk}", tk, "Upbit listing"))
    except Exception as e:
        ERRORS.append(f"listings.upbit: {str(e)[:60]}")
    # Binance — CMS API przez jina (binance.com bezpośrednio jest geo-blokowany dla runnerów US;
    # api.bybit.com blokuje CloudFront po kraju nawet przez jina — Bybit pominięty)
    try:
        req = ur.Request("https://r.jina.ai/https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=20&catalogId=48",
                         headers={"User-Agent": UA, "Accept": "application/json"})
        with ur.urlopen(req, timeout=25, context=SSL_CTX) as r:
            wrap = json.loads(r.read())
        inner = json.loads((wrap.get("data") or {}).get("content") or "{}")
        for cat in ((inner.get("data") or {}).get("catalogs") or []):
            for a in cat.get("articles") or []:
                title = a.get("title") or ""
                rel = (a.get("releaseDate") or 0) / 1000
                if rel and now - rel > 6 * 3600:
                    continue
                kind = "Binance Alpha" if "Alpha" in title else "Binance Launchpool" if "Launchpool" in title else "Binance Futures" if "Futures" in title or "Perpetual" in title else "Binance listing"
                tks = set(re.findall(r"\(([A-Z0-9]{2,10})\)", title)) | set(re.findall(r"\b([A-Z0-9]{2,10})USDT\b", title))
                for tk in tks:
                    events.append((f"BINANCE:{a.get('id')}:{tk}", tk, kind))
    except Exception as e:
        ERRORS.append(f"listings.binance: {str(e)[:60]}")
    out = {}
    for key, coin, src in events:
        if key not in seen:
            seen[key] = now
        if now - seen[key] < 6 * 3600 and _is_crypto(coin):
            out.setdefault(coin, [])
            if src not in out[coin]:
                out[coin].append(src)
    # prune
    for k in [k for k, ts in seen.items() if now - ts > 7 * 86400]:
        del seen[k]
    return out


# ─── Serie w cache: cena + OI per coin (26h) ────────────────────────────────

def _sig(v, digits=6):
    if not v:
        return 0
    from math import log10, floor
    return round(v, max(0, digits - 1 - floor(log10(abs(v)))))


def update_series(cache, universe):
    """Serie cena/OI per coin. Rozdzielczość: 5 min przez ostatnie 70 min, potem tylko
    punkty przy pełnej godzinie (do 26h) — cache trzymany w actions/cache, ma być mały."""
    now = int(time.time())
    ser = cache.setdefault("series", {})
    for coin, u in universe.items():
        s = [x for x in ser.get(coin, []) if now - x[0] < 26 * 3600 and (now - x[0] < 70 * 60 or x[0] % 3600 < 330)]
        s.append([now, _sig(u["price"]), int(u["oi_usd"])])
        ser[coin] = s
    for k in [k for k in ser if k not in universe]:
        del ser[k]


def _at(series, hours_ago, now):
    target = now - hours_ago * 3600
    if not series:
        return None
    p = min(series, key=lambda x: abs(x[0] - target))
    return p if abs(p[0] - target) < max(600, hours_ago * 3600 * 0.35) else None


def pct(a, b):
    return (a / b - 1) * 100 if a and b else None


# ─── Scoring ────────────────────────────────────────────────────────────────

def score_coin(coin, u, series, flow, listings, klines):
    now = int(time.time())
    parts = {}; tags = []
    p1 = _at(series, 1, now); p4 = _at(series, 4, now)
    px1 = pct(u["price"], p1[1]) if p1 else None
    px4 = pct(u["price"], p4[1]) if p4 else None
    oi1 = pct(u["oi_usd"], p1[2]) if p1 and p1[2] else None
    oi4 = pct(u["oi_usd"], p4[2]) if p4 and p4[2] else None

    # PALIWO: OI rośnie, cena stoi
    if oi1 is not None and px1 is not None:
        if oi1 >= 15 and abs(px1) < 1.5:
            parts["oi_surge"] = 30
        elif oi1 >= 8 and abs(px1) < 1.0:
            parts["oi_surge"] = 22
    if oi4 is not None and px4 is not None and oi4 >= 15 and abs(px4) < 3:
        parts["oi_build_4h"] = 10
    f = u.get("funding_8h") or 0.0
    if f <= -0.10:
        parts["funding"] = 25
    elif f <= -0.03:
        parts["funding"] = 15
    elif f <= -0.015:
        parts["funding"] = 8

    # WIELORYBY: netto kupno top-30 w 1h
    fl = flow.get(coin)
    if fl and fl["net"] >= 2e6:
        parts["whales"] = 30
    elif fl and fl["net"] >= 5e5:
        parts["whales"] = 20
    elif fl and fl["net"] >= 1.5e5 and fl["wallets"] >= 2:
        parts["whales"] = 12

    # KATALIZATOR
    if coin in listings:
        parts["listing"] = 30
        tags.append("listing:" + "/".join(listings[coin]))

    # KOMPRESJA + ZAPŁON (tylko kandydaci z klines)
    if klines and len(klines) >= 60:
        closes = [k["c"] for k in klines]; highs = [k["h"] for k in klines]; lows = [k["l"] for k in klines]
        trs = [max(klines[i]["h"] - klines[i]["l"], abs(klines[i]["h"] - closes[i - 1]), abs(klines[i]["l"] - closes[i - 1])) for i in range(1, len(klines))]
        atr = mean(trs[-72:]) if len(trs) >= 72 else mean(trs)
        rng3h = max(highs[-36:]) - min(lows[-36:])
        if atr and rng3h < 3.5 * atr:
            parts["compression"] = 10
        vols = [k["v"] for k in klines]
        v15 = sum(vols[-3:]); hist = [sum(vols[i:i + 3]) for i in range(0, len(vols) - 3, 3)]
        if len(hist) >= 20:
            mu, sd = mean(hist), pstdev(hist)
            z = (v15 - mu) / sd if sd else 0
            px15 = pct(closes[-1], closes[-4]) or 0
            if z >= 4 and px15 >= 1.5:
                parts["ignition"] = 20; tags.append(f"ignited z{z:.0f} +{px15:.1f}%15m")
            elif z >= 3 and px15 >= 0.8:
                parts["ignition"] = 10

    # PÓŹNO: już po pumpie
    r24 = u.get("r24") or 0
    if r24 >= 15 or (px1 is not None and px1 >= 5):
        parts["late"] = -15; tags.append("late")
    score = max(0, min(100, sum(parts.values())))
    return {
        "ticker": coin, "src": u["src"], "score": score,
        "level": "HIGH" if score >= SCORE_HIGH else "WATCH" if score >= SCORE_WATCH else "none",
        "parts": parts, "tags": tags, "price": u["price"], "r24": round(r24, 2),
        "px_1h": round(px1, 2) if px1 is not None else None, "oi_1h": round(oi1, 1) if oi1 is not None else None,
        "oi_4h": round(oi4, 1) if oi4 is not None else None, "funding_8h": round(f, 4),
        "vol24": round(u.get("vol24") or 0), "oi_usd": round(u["oi_usd"]),
        "whale_net_1h": round(fl["net"]) if fl else 0, "whale_wallets": fl["wallets"] if fl else 0,
    }


# ─── Track record ───────────────────────────────────────────────────────────

def update_alerts(alerts, universe, scored):
    now = int(time.time())
    prices = {c: u["price"] for c, u in universe.items()}
    # uzupełnij zwroty
    for a in alerts:
        p0 = a["price"]; px = prices.get(a["ticker"])
        if not px or not p0:
            continue
        age = now - a["ts"]
        a["max_fwd_pct"] = round(max(a.get("max_fwd_pct", 0), (px / p0 - 1) * 100), 2)
        for key, h in (("fwd_1h", 1), ("fwd_4h", 4), ("fwd_24h", 24)):
            if a.get(key) is None and age >= h * 3600:
                a[key] = round((px / p0 - 1) * 100, 2)
    # nowe alerty (cooldown 6h per token; HIGH może nadpisać WATCH)
    new = []
    last = {}
    for a in alerts:
        if a["ticker"] not in last or a["ts"] > last[a["ticker"]]["ts"]:
            last[a["ticker"]] = a
    for s in scored:
        if s["level"] == "none":
            continue
        prev = last.get(s["ticker"])
        if prev and now - prev["ts"] < ALERT_COOLDOWN_H * 3600 and not (s["level"] == "HIGH" and prev["level"] == "WATCH"):
            continue
        a = {"ts": now, "ticker": s["ticker"], "level": s["level"], "score": s["score"], "price": s["price"],
             "parts": s["parts"], "tags": s["tags"], "fwd_1h": None, "fwd_4h": None, "fwd_24h": None, "max_fwd_pct": 0}
        alerts.append(a); new.append(a)
    # prune 30 dni
    alerts[:] = [a for a in alerts if now - a["ts"] < 30 * 86400]
    return new


def stats(alerts):
    def agg(rows):
        n = len(rows)
        h1 = [r for r in rows if r.get("fwd_1h") is not None]; h4 = [r for r in rows if r.get("fwd_4h") is not None]; h24 = [r for r in rows if r.get("fwd_24h") is not None]
        return {"n": n,
                "n_1h": len(h1), "hit_1h": round(100 * sum(1 for r in h1 if r["fwd_1h"] >= HIT_1H_PCT) / len(h1), 1) if h1 else None,
                "avg_1h": round(mean(r["fwd_1h"] for r in h1), 2) if h1 else None,
                "n_4h": len(h4), "hit_4h": round(100 * sum(1 for r in h4 if r["fwd_4h"] >= HIT_4H_PCT) / len(h4), 1) if h4 else None,
                "avg_4h": round(mean(r["fwd_4h"] for r in h4), 2) if h4 else None,
                "avg_24h": round(mean(r["fwd_24h"] for r in h24), 2) if h24 else None,
                "avg_max": round(mean(r.get("max_fwd_pct", 0) for r in rows), 2) if rows else None}
    parts = sorted({p for a in alerts for p in a.get("parts", {}) if p != "late"})
    return {"all": agg(alerts), "high": agg([a for a in alerts if a["level"] == "HIGH"]),
            "watch": agg([a for a in alerts if a["level"] == "WATCH"]),
            "by_part": {p: agg([a for a in alerts if p in a.get("parts", {})]) for p in parts},
            "hit_def": f"+{HIT_1H_PCT}% w 1h / +{HIT_4H_PCT}% w 4h"}


# ─── Telegram ───────────────────────────────────────────────────────────────

def send_telegram(new_alerts, st):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(); chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    highs = [a for a in new_alerts if a["level"] == "HIGH"]
    if not token or not chat or not highs:
        return
    lines = ["🚀 <b>Pump Radar — HIGH</b>"]
    for a in highs[:5]:
        parts = " · ".join(f"{k} {v:+d}" for k, v in a["parts"].items())
        lines.append(f"<b>{a['ticker']}</b> {a['score']}/100 @ {a['price']:.6g} — {parts}" + (f"\n   {' · '.join(a['tags'])}" if a["tags"] else ""))
    if st["all"]["n_1h"]:
        lines.append(f"\n<i>Track record: {st['all']['n']} alertów · hit 1h {st['all']['hit_1h']}% · hit 4h {st['all']['hit_4h']}%</i>")
    lines.append("<i>To nie jest sygnał wejścia. Sprawdź strukturę w terminalu → Pump Radar.</i>")
    try:
        data = up.urlencode({"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
        with ur.urlopen(ur.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=10, context=SSL_CTX) as r:
            log(f"telegram {r.status}")
    except Exception as e:
        log(f"telegram failed: {e}")


# ─── Main ───────────────────────────────────────────────────────────────────

def run(telegram=False):
    t0 = time.time()
    cache = _load(CACHE_PATH, {})
    alerts = _load(ALERTS_PATH, [])
    universe = {}
    try:
        mexc = fetch_mexc_universe()
        universe.update({k: v for k, v in mexc.items() if v["vol24"] >= MIN_TURNOVER_USD})
        log(f"MEXC: {len(mexc)} perpów, {len(universe)} płynnych")
    except Exception as e:
        ERRORS.append(f"mexc: {e}"); mexc = {}
    try:
        hl = fetch_hl_universe()
        for k, v in hl.items():
            if v["vol24"] >= MIN_TURNOVER_USD and _is_crypto(k):
                if k in universe:
                    # HL ma lepszy OI/funding (godzinowy) — zostaw MEXC price/symbol do klines, OI/funding z HL
                    universe[k]["oi_usd"] = max(universe[k]["oi_usd"], v["oi_usd"]); universe[k]["funding_8h"] = v["funding_8h"]; universe[k]["hl"] = True
                else:
                    universe[k] = dict(v, hl=True)
        log(f"HL: {len(hl)} coinów")
    except Exception as e:
        ERRORS.append(f"hl: {e}"); hl = {}
    if not universe:
        raise RuntimeError("brak universe (MEXC i HL niedostępne)")

    update_series(cache, universe)
    series = cache["series"]
    try:
        flow = fetch_whale_flow(cache)
    except Exception as e:
        ERRORS.append(f"whales: {e}"); flow = {}
    try:
        listings = fetch_listings(cache, list(hl.keys()), mexc)
    except Exception as e:
        ERRORS.append(f"listings: {e}"); listings = {}

    # 1. wstępny scoring bez klines → kandydaci na klines (kompresja/zapłon)
    pre = {c: score_coin(c, u, series.get(c, []), flow, listings, None) for c, u in universe.items()}
    cand = sorted(pre.values(), key=lambda s: (-s["score"], -(s["vol24"] or 0)))
    cand = [s for s in cand if s["score"] >= 15][:MAX_KLINE_CANDIDATES]
    # + kilka najbardziej "gorących" po wolumenie 24h vs OI (zapłon może przyjść bez paliwa)
    hot = sorted([s for s in pre.values() if s["score"] < 15 and s["r24"] > 3], key=lambda s: -s["r24"])[:10]
    scored = []
    for s in cand + hot:
        u = universe[s["ticker"]]
        kl = None
        try:
            if u["src"] == "MEXC":
                kl = fetch_mexc_klines(u["symbol"], "Min5", 300)
            elif u.get("hl"):
                kl = fetch_hl_klines(s["ticker"], "5m", 25)
        except Exception:
            kl = None
        scored.append(score_coin(s["ticker"], u, series.get(s["ticker"], []), flow, listings, kl))
    done = {s["ticker"] for s in scored}
    scored += [s for c, s in pre.items() if c not in done]
    scored.sort(key=lambda s: -s["score"])

    new_alerts = update_alerts(alerts, universe, scored)
    st = stats(alerts)
    top = [s for s in scored if s["level"] != "none"][:10]
    watch = [s for s in scored if s["level"] == "none" and s["score"] >= 20][:10]
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "universe_n": len(universe),
        "top": top, "near": watch, "new_alerts": [a["ticker"] for a in new_alerts],
        "stats": st, "recent_alerts": sorted(alerts, key=lambda a: -a["ts"])[:30],
        "errors": ERRORS, "elapsed_s": round(time.time() - t0, 1),
        "hit_def": st["hit_def"],
    }
    _save(OUT_PATH, out); _save(ALERTS_PATH, alerts); _save(CACHE_PATH, cache)
    log(f"universe {len(universe)} · top {[(s['ticker'], s['score']) for s in top[:5]]} · nowe alerty {out['new_alerts']} · {out['elapsed_s']}s · błędy {len(ERRORS)}")
    if telegram:
        send_telegram(new_alerts, st)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TVC Pump Radar")
    ap.add_argument("--telegram", action="store_true")
    args = ap.parse_args()
    try:
        run(telegram=args.telegram)
    except Exception as e:
        print(f"[radar] FAILED: {e}")
        sys.exit(0)
