#!/usr/bin/env python3
"""
TVC Pump Radar v2.4 — skan rynku perpów co 5 min pod krótkoterminowe pumpy.

v2.2 (2026-10-31, audyt — NIE wdrożone bez zgody):
  - funding MEXC normalizowany do 8h wg collectCycle (4h/1h kontrakty były zaniżane 2×/8×)
  - wykluczone kontrakty TradFi z MEXC (akcje, ETF, ropa, metale) — 71/165 historycznych alertów
  - HL kPEPE/kBONK/kSHIB/kFLOKI łączone z MEXC PEPE/BONK/... (bez duplikatów)
  - zwroty +1h/+4h/+24h liczone także, gdy token wypadł z płynnego universe (survivorship)
  - max_fwd_pct liczony tylko w oknie 24h (wcześniej rósł przez 30 dni)
  - dump_warning logowany osobno (dump_watch + track record), żeby dało się go zmierzyć
  - Upbit: fallback przez r.jina.ai (bezpośrednio 403 z runnerów GitHub)
  - progi w JSON (thresholds) — panel nie musi ich hardkodować

v2.3 (2026-10-31, Upbit):
  - Upbit: oficjalne api.upbit.com/v1/market/all + diff snapshotu (zamiast jina, którą Cloudflare też blokuje),
    poprawione rozpoznawanie tytułów ("디지털 자산 추가" tak, delisting/ostrzeżenia nie), fallback CoinGecko

v2.4 (2026-10-31, Binance):
  - (4.11.2026) log alertów 365 dni; test WATCH 45 do 2.12 w pump_radar_test45.json (bez Telegramu)
  - usunięte proxy r.jina.ai (obchodziło blokadę lokalizacji 451); listy rynków spot/futures z diffem snapshotu,
    źródła: Binance bezpośrednio → CoinPaprika (spot) / CoinGecko (futures); komunikaty tylko bezpośrednio

v2.1 changes (2026-09-29): po analizie 159 alertów — WATCH hit rate 6.1% = szum:
  - SCORE_WATCH podniesiony z 50 → 60 (filtr najsłabszych alertów)
  - SCORE_HIGH podniesiony z 65 → 70 (HIGH = naprawdę silny setup)
  - MIN_TURNOVER_USD podniesiony z 3M → 5M (lepsza płynność)
  - Dodany MIN_FUNDING_RATE_BPS = -5 (ignoruj mikro-funding < 5bps)
  - Wzmocniony whale signal: wymaga min 3 whale fills (było 1)

v2 changes (2026-09-12): po analizie 149 alertów z 10 dni:
  - oi_surge i oi_build_4h usunięte jako samodzielne sygnały (hit rate 3-5%, szum)
  - funding zostaje jako główny sygnał (13.6% hit 1h, avg_max 6.76%)
  - ignition przemianowany na dump warning (avg -6.61% 24h po "zapłonie")
  - próg minimalny podniesiony z 35 do 50
  - wymagane: funding + min. 1 inny czynnik (wieloryby/listing/kompresja)
  - OI wzrost i kompresja zostają jako wzmocnienia (+5-10), nie samodzielne alerty

Składniki:
  FUNDING     — ujemny funding rate (shorty płacą) — jedyny edge potwierdzony danymi
  WIELORYBY   — top-30 portfeli HL kupuje netto w 1h
  KATALIZATOR — nowy listing (Upbit, HL, MEXC, Binance)
  KOMPRESJA   — ciasny range vs ATR (wzmocnienie, nie samodzielny)
  OI WZROST   — skok OI przy płaskiej cenie (wzmocnienie, nie samodzielny)
  DUMP WARNING— wolumen 15m ≥ 4σ + cena > +1.5% → oznaczane jako dump risk
                (historycznie: avg +0.7% 1h, potem -6.61% 24h)

Źródła: Hyperliquid, MEXC, Upbit (Open API + komunikaty), Binance (listy rynków: Binance/CoinPaprika/CoinGecko + komunikaty bez proxy).
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
TEST45_PATH = FUSION_DIR / "pump_radar_test45.json"   # v2.4: test progu WATCH 45 (4.11-2.12.2026), tylko log
LAYERS_CACHE = FUSION_DIR / "layers_cache.json"     # wieloryby (top-30) z auto_fusion — współdzielone

HL_INFO = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

MIN_TURNOVER_USD = 5_000_000    # v2.1: 3M→5M, lepsza płynność
SCORE_WATCH = 60                # v2.1: 50→60, filtr szumu
SCORE_HIGH = 70                 # v2.1: 65→70, HIGH = silny setup
MIN_FUNDING_BPS = 5             # v2.1: ignoruj funding < 5bps (mikro-sygnał)
MAX_KLINE_CANDIDATES = 40       # ile tokenów dostaje 5m klines (kompresja + zapłon) w cyklu
TEST_WATCH = 45                 # v2.4: test 4 tyg. (4.11-2.12.2026), osobny log, bez Telegramu
TEST_END_TS = 1796252400        # 3.12.2026 00:00 Warszawa
ALERT_RETENTION_D = 365         # v2.4: log alertów 365 dni (było 30)
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
        if any("tradfi" in str(c).lower() for c in (d.get("conceptPlate") or [])):
            continue                     # v2.2: akcje/ETF/surowce MEXC — to nie jest rynek krypto
        px = float(t.get("lastPrice") or 0)
        out[base] = {
            "src": "MEXC", "symbol": sym, "price": px,
            "oi_usd": float(t.get("holdVol") or 0) * float(d.get("contractSize") or 1) * px,
            "funding_8h": float(t.get("fundingRate") or 0) * 100, "vol24": float(t.get("amount24") or 0),
            "r24": float(t.get("riseFallRate") or 0) * 100,
            "created": (d.get("createTime") or 0) / 1000, "is_new": bool(d.get("isNew")),
        }
    return out


def mexc_funding_cycle(symbol, cache):
    """v2.2: collectCycle (h) kontraktu MEXC — fundingRate z tickera jest NA CYKL, nie na 8h.
    Cache 24h, pytamy tylko o kontrakty z ujemnym fundingiem (kilkanaście zapytań/cykl)."""
    cc = cache.setdefault("mexc_cycle", {})
    hit = cc.get(symbol)
    if hit and time.time() - hit[1] < 86400:
        return hit[0]
    try:
        cyc = int((_get_json(f"https://contract.mexc.com/api/v1/contract/funding_rate/{symbol}", timeout=10, retries=1).get("data") or {}).get("collectCycle") or 8)
    except Exception:
        return 8
    cc[symbol] = [cyc, time.time()]
    return cyc


def normalize_mexc_funding(universe, cache):
    for u in universe.values():
        if u.get("src") == "MEXC" and not u.get("hl") and (u.get("funding_8h") or 0) < 0:
            cyc = mexc_funding_cycle(u["symbol"], cache)
            if cyc and cyc != 8:
                u["funding_raw"] = u["funding_8h"]; u["funding_cycle_h"] = cyc
                u["funding_8h"] = u["funding_8h"] * 8 / cyc


def hl_alias(name):
    """v2.2: HL notuje memecoiny w tysiącach (kPEPE = 1000 PEPE). Do łączenia z MEXC."""
    return name[1:] if len(name) > 1 and name[0] == "k" and name[1:].isupper() else name


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


# ─── Upbit (v2.3) ───────────────────────────────────────────────────────────
# Przyczyna awarii v2.0–v2.2: api-manager.upbit.com (komunikaty) stoi za Cloudflare WAF, który
# blokuje adresy centrów danych (runnery GitHub/Azure, r.jina.ai) stroną "Attention Required"
# (HTTP 403). To blokada skierowana do ruchu serwerowego, więc NIE obchodzimy jej proxy.
# Główne źródło: oficjalne publiczne Open API api.upbit.com/v1/market/all (bez klucza, limit per IP,
# nie za Cloudflare). Nowy rynek = baza, której nie było w poprzednim snapshocie (cache).
# Komunikaty pytamy bezpośrednio tylko jako wczesny sygnał; po 403 przerwa 1h.
# Fallback, gdy api.upbit.com nie odpowiada: CoinGecko /exchanges/upbit/tickers (rynki KRW).
UPBIT_MARKETS = "https://api.upbit.com/v1/market/all"
UPBIT_NOTICES = "https://api-manager.upbit.com/api/v1/announcements?os=web&page=1&per_page=20&category=trade"
CG_UPBIT = "https://api.coingecko.com/api/v3/exchanges/upbit/tickers?page={page}"
UPBIT_BURST_MAX = 8               # więcej nowych rynków naraz = utrata snapshotu/zmiana formatu → tylko baza
UPBIT_SNAPSHOT_TTL = 14 * 86400   # baza znika ze snapshotu dopiero po 14 dniach nieobecności (bez "flappingu")
UPBIT_NOTICE_PAUSE = 3600         # po 403 z komunikatów nie pytamy przez 1h
UPBIT_NOTICE_MAX_AGE = 6 * 3600
SOURCE_STATUS = {}
_UPBIT_ADD = re.compile(r"신규\s*거래지원|디지털\s*자산\s*추가|마켓\s*추가|new\s+listing|market\s+support\s+for", re.I)
_UPBIT_SKIP = re.compile(r"종료|유의|연장|중단|촉구|해제|delist|caution|suspen|termination|end\s+of", re.I)
_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def upbit_title_tickers(title):
    """Tickery z tytułu komunikatu o NOWYM rynku. Pomija delisting, ostrzeżenia, przedłużenia."""
    title = title or ""
    if not _UPBIT_ADD.search(title) or _UPBIT_SKIP.search(title):
        return []
    return [t for t in dict.fromkeys(re.findall(r"\(([A-Z0-9]{2,15})\)", title)) if t not in ("KRW", "BTC", "USDT")]


def upbit_markets_bases(rows):
    """market/all → {"KRW": {bazy}, "ALL": {bazy}}. Rynek ma format QUOTE-BASE (KRW-BTC)."""
    krw, allb = set(), set()
    for r in rows or []:
        m = (r or {}).get("market") or ""
        if "-" not in m:
            continue
        q, b = m.split("-", 1)
        allb.add(b)
        if q == "KRW":
            krw.add(b)
    return {"KRW": krw, "ALL": allb}


def upbit_diff(cache, slot, current, now, label):
    """Porównuje zbiory baz ze snapshotem w cache[slot]; zwraca listę nowych zdarzeń."""
    snap = cache.setdefault(slot, {})
    events = []
    for scope, bases in current.items():
        prev = snap.get(scope)
        if prev is None or not bases:
            if bases:
                snap[scope] = {b: now for b in bases}      # pierwszy przebieg: tylko baza
            continue
        new = sorted(b for b in bases if b not in prev)
        if len(new) > UPBIT_BURST_MAX:
            log(f"upbit[{label}/{scope}]: {len(new)} nowych naraz → nowa baza, bez alertów")
            new = []
        for b in new:
            events.append((f"UPBIT_MKT:{scope}:{b}", b, "Upbit listing"))
        for b in bases:
            prev[b] = now
        for b in [b for b, ts in prev.items() if now - ts > UPBIT_SNAPSHOT_TTL]:
            del prev[b]
    return events


def _iso_ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def fetch_upbit_events(cache, now):
    events, st = [], {}
    # 1) oficjalne Open API: lista rynków
    ok = False
    try:
        rows = _get_json(UPBIT_MARKETS, timeout=15, retries=2)
        if not isinstance(rows, list) or not rows:
            raise ValueError("market/all: pusta lub nie-lista")
        cur = upbit_markets_bases(rows)
        events += upbit_diff(cache, "upbit_markets", cur, now, "api")
        st["markets"] = f"ok {len(cur['KRW'])} KRW / {len(cur['ALL'])} baz"; ok = True
    except Exception as e:
        st["markets"] = f"fail: {str(e)[:60]}"
    # 2) fallback: CoinGecko (tylko gdy Open API padło; osobny snapshot, żeby nie mieszać źródeł)
    if not ok:
        try:
            krw = set()
            for page in range(1, 6):
                tk = (_get_json(CG_UPBIT.format(page=page), timeout=20, retries=1).get("tickers") or [])
                krw |= {t.get("base", "").upper() for t in tk if t.get("target") == "KRW" and t.get("base")}
                if len(tk) < 100:
                    break
            if not krw:
                raise ValueError("coingecko: brak rynków KRW")
            events += upbit_diff(cache, "upbit_markets_cg", {"KRW": krw}, now, "coingecko")
            st["markets_fallback"] = f"ok {len(krw)} KRW (CoinGecko)"; ok = True
        except Exception as e:
            st["markets_fallback"] = f"fail: {str(e)[:60]}"
    # 3) komunikaty: wczesny sygnał (przed startem handlu), bez proxy; po 403 przerwa
    pause = cache.get("upbit_notice_pause_until") or 0
    if now < pause:
        st["notices"] = "pauza po 403 (Cloudflare blokuje IP centrów danych)"
    else:
        try:
            j = _get_json(UPBIT_NOTICES, timeout=15, retries=1,
                          headers={"User-Agent": _BROWSER_UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"})
            d = j.get("data") or {}
            n_new = 0
            for n in d.get("notices") or d.get("list") or []:
                ts = _iso_ts(n.get("listed_at") or n.get("first_listed_at"))
                if ts and now - ts > UPBIT_NOTICE_MAX_AGE:
                    continue
                for tk in upbit_title_tickers(n.get("title")):
                    events.append((f"UPBIT:{n.get('id')}:{tk}", tk, "Upbit listing")); n_new += 1
            st["notices"] = f"ok ({n_new} świeżych)"
            cache.pop("upbit_notice_pause_until", None)
        except Exception as e:
            code = getattr(e, "code", None)
            if code in (403, 429):
                cache["upbit_notice_pause_until"] = now + UPBIT_NOTICE_PAUSE
            st["notices"] = f"niedostępne ({code or str(e)[:40]})"
    if not ok:
        ERRORS.append(f"listings.upbit: market/all i CoinGecko niedostępne")
    SOURCE_STATUS["upbit"] = st
    return events


# ─── Binance (v2.4) ─────────────────────────────────────────────────────────
# Do v2.3 komunikaty Binance szły przez r.jina.ai, bo binance.com odpowiada runnerom GitHub (USA)
# kodem 451 "restricted location" (warunki Binance, pkt "b. Eligibility"). To blokada skierowana
# do naszej lokalizacji, więc proxy było jej obchodzeniem i zostało usunięte.
# Teraz: najpierw bezpośrednio Binance (działa tylko z lokalizacji, które Binance obsługuje),
# po 451/403 przerwa 6h; zamiast tego publiczne agregatory, które same udostępniają te dane:
#   spot    → CoinPaprika /v1/exchanges/binance/markets (1 zapytanie, bez klucza)
#   futures → CoinGecko /derivatives/exchanges/binance_futures?include_tickers=all (1 zapytanie)
# Nowa baza względem snapshotu w cache = katalizator. Listy pytamy co 15 min (limity darmowych API).
BN_SPOT_DIRECT = "https://data-api.binance.vision/api/v3/ticker/price"
BN_FUT_DIRECT = "https://fapi.binance.com/fapi/v1/ticker/price"
BN_CMS = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=20&catalogId=48"
PAPRIKA_BN = "https://api.coinpaprika.com/v1/exchanges/binance/markets"
CG_BN_FUT = "https://api.coingecko.com/api/v3/derivatives/exchanges/binance_futures?include_tickers=all"
BN_POLL_S = 900
BN_BLOCK_PAUSE = 6 * 3600
SNAP_STALE_S = 24 * 3600          # snapshot nieodświeżany >24h = nowa baza (bez zaległych "nowości")
_BLOCK_CODES = (401, 403, 418, 429, 451)


def _paused(cache, name, now):
    return now < ((cache.get("src_pause") or {}).get(name) or 0)


def _pause_on_block(cache, name, e, now):
    code = getattr(e, "code", None)
    if code in _BLOCK_CODES:
        cache.setdefault("src_pause", {})[name] = now + (BN_BLOCK_PAUSE if code in (451, 403) else 3600)
    return code


def snapshot_diff(cache, slot, bases, now, key_prefix, src, label):
    """Diff zbioru baz z cache["snap"][slot]. Pierwszy przebieg, burst > UPBIT_BURST_MAX albo
    snapshot starszy niż 24h → tylko nowa baza. Klucz zdarzenia bez nazwy źródła (dedupe między źródłami)."""
    snaps = cache.setdefault("snap", {})
    snap = snaps.get(slot)
    events = []
    if not bases:
        return events
    if snap is None or now - (snap.get("_ts") or 0) > SNAP_STALE_S:
        snaps[slot] = {"_ts": now, "b": {x: now for x in bases}}
        return events
    prev = snap["b"]
    new = sorted(x for x in bases if x not in prev)
    if len(new) > UPBIT_BURST_MAX:
        log(f"{label}: {len(new)} nowych naraz → nowa baza, bez alertów")
        new = []
    events = [(f"{key_prefix}:{x}", x, src) for x in new]
    for x in bases:
        prev[x] = now
    for x in [x for x, ts in prev.items() if now - ts > UPBIT_SNAPSHOT_TTL]:
        del prev[x]
    snap["_ts"] = now
    return events


def _bases_usdt(symbols):
    return {x[:-4] for x in symbols if x.endswith("USDT") and len(x) > 4}


def _try_sources(cache, now, st, name, chain):
    """chain = [(nazwa_źródła, fn → set baz)]; zwraca (nazwa, bazy) z pierwszego działającego."""
    for sname, fn in chain:
        if _paused(cache, sname, now):
            st[sname] = "pauza po blokadzie"
            continue
        try:
            bases = fn()
            if not bases:
                raise ValueError("pusta lista")
            st[sname] = f"ok {len(bases)}"
            return sname, bases
        except Exception as e:
            code = _pause_on_block(cache, sname, e, now)
            st[sname] = f"fail ({code or str(e)[:40]})"
    return None, set()


def binance_title_events(articles, now, max_age=6 * 3600):
    ev = []
    for a in articles or []:
        title = a.get("title") or ""
        rel = (a.get("releaseDate") or 0) / 1000
        if rel and now - rel > max_age:
            continue
        if re.search(r"delist|remove|deprecat|will\s+cease", title, re.I):
            continue
        kind = "Binance Alpha" if "Alpha" in title else "Binance Launchpool" if "Launchpool" in title else "Binance Futures" if "Futures" in title or "Perpetual" in title else "Binance listing"
        tks = set(re.findall(r"\(([A-Z0-9]{2,10})\)", title)) | set(re.findall(r"\b([A-Z0-9]{2,10})USDT\b", title))
        for tk in sorted(tks - {"USDT", "USDC", "BTC", "BNB"}):
            ev.append((f"BINANCE:{a.get('id')}:{tk}", tk, kind))
    return ev


def fetch_binance_events(cache, now):
    st, events = {}, []
    if now >= (cache.get("binance_next_poll") or 0):
        cache["binance_next_poll"] = now + BN_POLL_S - 30
        spot_chain = [
            ("binance_spot", lambda: _bases_usdt(r.get("symbol", "") for r in _get_json(BN_SPOT_DIRECT, timeout=20, retries=1))),
            ("coinpaprika", lambda: {m["pair"].split("/")[0].upper() for m in _get_json(PAPRIKA_BN, timeout=30, retries=1)
                                     if m.get("category") == "Spot" and str(m.get("pair", "")).endswith("/USDT")}),
        ]
        fut_chain = [
            ("binance_futures", lambda: _bases_usdt(r.get("symbol", "") for r in _get_json(BN_FUT_DIRECT, timeout=20, retries=1))),
            ("coingecko_futures", lambda: {str(t.get("base", "")).upper() for t in (_get_json(CG_BN_FUT, timeout=30, retries=1).get("tickers") or [])
                                           if t.get("contract_type") == "perpetual" and t.get("target") == "USDT" and t.get("base")}),
        ]
        sname, bases = _try_sources(cache, now, st, "spot", spot_chain)
        if sname:
            events += snapshot_diff(cache, f"bn_spot_{sname}", bases, now, "BINANCE_SPOT", "Binance listing", sname)
        fname, fbases = _try_sources(cache, now, st, "futures", fut_chain)
        if fname:
            events += snapshot_diff(cache, f"bn_fut_{fname}", fbases, now, "BINANCE_FUT", "Binance Futures", fname)
        if not sname and not fname:
            ERRORS.append("listings.binance: żadne źródło listy rynków nie odpowiada")
    else:
        st["markets"] = "co 15 min (następny później)"
    # komunikaty: tylko bezpośrednio; z lokalizacji objętej ograniczeniem Binance → 451 i przerwa 6h
    if _paused(cache, "binance_cms", now):
        st["binance_cms"] = "pauza po blokadzie (lokalizacja)"
    else:
        try:
            j = _get_json(BN_CMS, timeout=20, retries=1)
            arts = [a for cat in ((j.get("data") or {}).get("catalogs") or []) for a in (cat.get("articles") or [])]
            ev = binance_title_events(arts, now)
            events += ev
            st["binance_cms"] = f"ok ({len(ev)} świeżych)"
        except Exception as e:
            code = _pause_on_block(cache, "binance_cms", e, now)
            st["binance_cms"] = f"niedostępne ({code or str(e)[:40]})"
    SOURCE_STATUS["binance"] = st
    return events


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
    # Upbit — v2.3: oficjalne Open API (market/all, diff) + komunikaty bez proxy, patrz fetch_upbit_events
    events.extend(fetch_upbit_events(cache, now))
    # Binance — v2.4: listy rynków (diff snapshotu) + komunikaty bez proxy, patrz fetch_binance_events
    events.extend(fetch_binance_events(cache, now))
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

    # ── FUNDING (główny sygnał — jedyny z potwierdzonym edge) ──
    # v2.1: usunięty najniższy tier (-0.015, 15pts) — micro-funding to szum
    f = u.get("funding_8h") or 0.0
    if f <= -0.10:
        parts["funding"] = 35          # bardzo ujemny — shorty mocno płacą
    elif f <= -0.03:
        parts["funding"] = 25
    # usunięto: elif f <= -0.015: parts["funding"] = 15  # v2.1: za słaby sygnał

    # ── WIELORYBY: netto kupno top-30 w 1h ──
    # v2.1: podniesione progi — wymagane min 3 portfele na najniższym tierze
    fl = flow.get(u.get("hl_coin") or coin)
    if fl and fl["net"] >= 2e6:
        parts["whales"] = 30
    elif fl and fl["net"] >= 5e5 and fl.get("wallets", 0) >= 2:
        parts["whales"] = 20
    elif fl and fl["net"] >= 2e5 and fl.get("wallets", 0) >= 3:
        parts["whales"] = 12

    # ── KATALIZATOR ──
    if coin in listings:
        parts["listing"] = 30
        tags.append("listing:" + "/".join(listings[coin]))

    # ── OI WZROST (wzmocnienie +5/+10, nie samodzielny sygnał) ──
    if oi1 is not None and px1 is not None:
        if oi1 >= 15 and abs(px1) < 1.5:
            parts["oi_surge"] = 10      # wzmocnienie, było 30
        elif oi1 >= 8 and abs(px1) < 1.0:
            parts["oi_surge"] = 5       # wzmocnienie, było 22

    # ── KOMPRESJA + DUMP WARNING (tylko kandydaci z klines) ──
    if klines and len(klines) >= 60:
        closes = [k["c"] for k in klines]; highs = [k["h"] for k in klines]; lows = [k["l"] for k in klines]
        trs = [max(klines[i]["h"] - klines[i]["l"], abs(klines[i]["h"] - closes[i - 1]), abs(klines[i]["l"] - closes[i - 1])) for i in range(1, len(klines))]
        atr = mean(trs[-72:]) if len(trs) >= 72 else mean(trs)
        rng3h = max(highs[-36:]) - min(lows[-36:])
        if atr and rng3h < 3.5 * atr:
            parts["compression"] = 10   # wzmocnienie (bez zmian)
        # DUMP WARNING: volume spike + price spike = historycznie dump po 1-4h
        vols = [k["v"] for k in klines]
        v15 = sum(vols[-3:]); hist = [sum(vols[i:i + 3]) for i in range(0, len(vols) - 3, 3)]
        if len(hist) >= 20:
            mu, sd = mean(hist), pstdev(hist)
            z = (v15 - mu) / sd if sd else 0
            px15 = pct(closes[-1], closes[-4]) or 0
            if z >= 4 and px15 >= 1.5:
                parts["dump_warning"] = -10; tags.append(f"⚠️DUMP z{z:.0f} +{px15:.1f}%15m")
            elif z >= 3 and px15 >= 0.8:
                parts["dump_warning"] = -5; tags.append(f"⚠️dump z{z:.0f} +{px15:.1f}%15m")

    # ── PÓŹNO: już po pumpie ──
    r24 = u.get("r24") or 0
    if r24 >= 15 or (px1 is not None and px1 >= 5):
        parts["late"] = -15; tags.append("late")

    raw_score = sum(parts.values())

    # ── GATE: wymagany funding + min. 1 inny czynnik (wieloryby/listing/kompresja) ──
    has_funding = "funding" in parts
    has_other = any(k in parts for k in ("whales", "listing", "compression"))
    # Listing + whales może alertować bez fundingu (inna logika)
    has_listing_whale = "listing" in parts and "whales" in parts

    gate_ok = (has_funding and has_other) or has_listing_whale
    if has_funding and has_other:
        score = max(0, min(100, raw_score))
    elif has_listing_whale:
        score = max(0, min(100, raw_score))
    else:
        # brak wymaganej kombinacji — score cappowany poniżej WATCH
        score = min(max(0, raw_score), SCORE_WATCH - 1)

    return {
        "ticker": coin, "src": u["src"], "score": score,
        "level": "HIGH" if score >= SCORE_HIGH else "WATCH" if score >= SCORE_WATCH else "none",
        "gate_ok": bool(gate_ok), "score_raw": max(0, min(100, raw_score)),
        "parts": parts, "tags": tags, "price": u["price"], "r24": round(r24, 2),
        "px_1h": round(px1, 2) if px1 is not None else None, "oi_1h": round(oi1, 1) if oi1 is not None else None,
        "oi_4h": round(oi4, 1) if oi4 is not None else None, "funding_8h": round(f, 4),
        "vol24": round(u.get("vol24") or 0), "oi_usd": round(u["oi_usd"]),
        "whale_net_1h": round(fl["net"]) if fl else 0, "whale_wallets": fl["wallets"] if fl else 0,
    }


# ─── Track record ───────────────────────────────────────────────────────────

def update_alerts(alerts, universe, scored, all_prices=None):
    now = int(time.time())
    prices = dict(all_prices or {}); prices.update({c: u["price"] for c, u in universe.items()})
    # uzupełnij zwroty
    for a in alerts:
        p0 = a["price"]; px = prices.get(a["ticker"])
        if not px or not p0:
            continue
        age = now - a["ts"]
        if age <= 24 * 3600:               # v2.2: max tylko w oknie 24h (wcześniej rosło 30 dni)
            a["max_fwd_pct"] = round(max(a.get("max_fwd_pct", 0), (px / p0 - 1) * 100), 2)
            a["min_fwd_pct"] = round(min(a.get("min_fwd_pct", 0), (px / p0 - 1) * 100), 2)
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
        if s["level"] == "none" and "dump_warning" not in s["parts"]:
            continue
        kind = "dump" if s["level"] == "none" else "pump"
        prev = last.get(s["ticker"])
        if prev and prev.get("kind", "pump") != kind:
            prev = None
        if prev and now - prev["ts"] < ALERT_COOLDOWN_H * 3600 and not (s["level"] == "HIGH" and prev["level"] == "WATCH"):
            continue
        a = {"ts": now, "kind": kind, "ticker": s["ticker"], "level": s["level"] if kind == "pump" else "DUMP_WATCH", "score": s["score"], "price": s["price"],
             "parts": s["parts"], "tags": s["tags"], "fwd_1h": None, "fwd_4h": None, "fwd_24h": None, "max_fwd_pct": 0}
        alerts.append(a); new.append(a)
    # prune (v2.4: 365 dni)
    alerts[:] = [a for a in alerts if now - a["ts"] < ALERT_RETENTION_D * 86400]
    return new


def update_test45(test, universe, scored, all_prices):
    """v2.4: test progu WATCH 45. Alert testowy = bramka spełniona i wynik >= 45 (ten sam scoring).
    Dla każdego alertu losowa kontrola: losowy token z tego samego universe w tej samej chwili.
    Tylko log, bez Telegramu i bez wpływu na top/WATCH/HIGH."""
    import random
    now = int(time.time())
    prices = dict(all_prices or {}); prices.update({c: u["price"] for c, u in universe.items()})
    rows = test.setdefault("alerts", [])
    for a in rows:
        for side in ("", "ctrl_"):
            t = a.get(side + "ticker") if side else a["ticker"]; p0 = a.get(side + "price") if side else a["price"]
            px = prices.get(t)
            if not px or not p0:
                continue
            age = now - a["ts"]
            for key, h in (("fwd_1h", 1), ("fwd_4h", 4), ("fwd_24h", 24)):
                if a.get(side + key) is None and age >= h * 3600:
                    a[side + key] = round((px / p0 - 1) * 100, 2)
    if now < TEST_END_TS:
        last = {}
        for a in rows:
            if a["ticker"] not in last or a["ts"] > last[a["ticker"]]:
                last[a["ticker"]] = a["ts"]
        pool = list(universe.keys())
        for s in scored:
            sc = s.get("score_raw", s["score"])
            if not s.get("gate_ok") or sc < TEST_WATCH:
                continue
            if s["ticker"] in last and now - last[s["ticker"]] < ALERT_COOLDOWN_H * 3600:
                continue
            others = [c for c in pool if c != s["ticker"]]
            ctrl = random.choice(others) if others else None
            rows.append({"ts": now, "ticker": s["ticker"], "score": sc, "band": "45-59" if sc < SCORE_WATCH else "60+",
                         "price": s["price"], "parts": s["parts"], "fwd_1h": None, "fwd_4h": None, "fwd_24h": None,
                         "ctrl_ticker": ctrl, "ctrl_price": universe[ctrl]["price"] if ctrl else None,
                         "ctrl_fwd_1h": None, "ctrl_fwd_4h": None, "ctrl_fwd_24h": None})
            last[s["ticker"]] = now
    test["meta"] = {"threshold": TEST_WATCH, "baseline": SCORE_WATCH, "start": "2026-11-04", "end": "2026-12-02",
                    "prereg": "pump_dump_radar_test45_PREREJESTRACJA.md", "updated": datetime.now(timezone.utc).isoformat(), "n": len(rows)}
    return test


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
    dumps = [a for a in alerts if a.get("kind") == "dump"]
    alerts = [a for a in alerts if a.get("kind", "pump") == "pump"]
    parts = sorted({p for a in alerts for p in a.get("parts", {}) if p != "late"})
    return {"all": agg(alerts), "dump_watch": agg(dumps), "high": agg([a for a in alerts if a["level"] == "HIGH"]),
            "watch": agg([a for a in alerts if a["level"] == "WATCH"]),
            "by_part": {p: agg([a for a in alerts if p in a.get("parts", {})]) for p in parts},
            "hit_def": f"+{HIT_1H_PCT}% w 1h / +{HIT_4H_PCT}% w 4h"}


# ─── Telegram (dual-channel v2) ────────────────────────────────────────────
TG_PRO_CHANNEL = "-1004436927192"   # dawny kanał PRO — v4.4: nic tu nie wysyłamy
TG_FREE_CHANNEL = "-1004341989751"  # kanał FREE — v4.4: bez picków/alertów
# v4.4 (tryb edukacyjny): kanały publiczne nie dostają picków ani alertów, tylko osobisty czat.
# Powrót do starego zachowania: TVC_EDUCATION_ONLY=0 (zmienna w workflow).
EDUCATION_ONLY = os.environ.get("TVC_EDUCATION_ONLY", "1") != "0"

def _tg_send(text, chat_id):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token or not chat_id:
        return
    if EDUCATION_ONLY and chat_id in (TG_PRO_CHANNEL, TG_FREE_CHANNEL):
        log(f"telegram({chat_id[-4:]}) education-only: pominięto kanał publiczny")
        return
    # 2026-10-05: kanały PRO/FREE przez tg_dedupe — bez podwójnych alertów po restarcie cyklu
    guarded = chat_id in (TG_PRO_CHANNEL, TG_FREE_CHANNEL)
    if guarded:
        try:
            from tg_dedupe import should_send, forget
            if not should_send(chat_id, text):
                log(f"telegram({chat_id[-4:]}) duplicate suppressed"); return
        except Exception as e:
            log(f"telegram dedupe unavailable ({e}) — sending"); guarded = False
    try:
        data = up.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
        with ur.urlopen(ur.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=10, context=SSL_CTX) as r:
            log(f"telegram({chat_id[-4:]}) {r.status}")
    except Exception as e:
        log(f"telegram({chat_id[-4:]}) failed: {e}")
        if guarded:
            try: forget(chat_id, text)
            except Exception: pass

def send_telegram(new_alerts, st):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(); chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    highs = [a for a in new_alerts if a["level"] == "HIGH" and a.get("kind", "pump") == "pump"]
    if not token or not chat or not highs:
        return
    # PRO + personal: pełny alert z cenami i score
    lines = ["🚀 <b>Pump Radar v2 — HIGH</b>"]
    for a in highs[:5]:
        parts = " · ".join(f"{k} {v:+d}" for k, v in a["parts"].items())
        lines.append(f"<b>{a['ticker']}</b> {a['score']}/100 @ {a['price']:.6g} — {parts}" + (f"\n   {' · '.join(a['tags'])}" if a["tags"] else ""))
    if st["all"]["n_1h"]:
        lines.append(f"\n<i>Track record: {st['all']['n']} alertów · hit 1h {st['all']['hit_1h']}% · hit 4h {st['all']['hit_4h']}%</i>")
    lines.append("<i>To nie jest sygnał wejścia. Sprawdź strukturę w terminalu → Pump Radar.</i>")
    pro_text = "\n".join(lines)
    # FREE kanał: wyłączony — FREE tier dostaje tylko Crypto Picks (auto_picks.py)
    _tg_send(pro_text, chat)           # osobisty
    _tg_send(pro_text, TG_PRO_CHANNEL) # kanał PRO — tylko gdy TVC_EDUCATION_ONLY=0


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
        for k0, v in hl.items():
            k = hl_alias(k0)
            if k != k0:                  # kPEPE = 1000 PEPE → cena za 1 token
                v = dict(v, hl_coin=k0, price=v["price"] / 1000)
            if v["vol24"] >= MIN_TURNOVER_USD and _is_crypto(k):
                if k in universe:
                    # HL ma lepszy OI/funding (godzinowy) — zostaw MEXC price/symbol do klines, OI/funding z HL
                    universe[k]["oi_usd"] = max(universe[k]["oi_usd"], v["oi_usd"]); universe[k]["funding_8h"] = v["funding_8h"]; universe[k]["hl"] = True
                else:
                    universe[k] = dict(v, hl=True)
                universe[k]["hl_coin"] = k0
        log(f"HL: {len(hl)} coinów")
    except Exception as e:
        ERRORS.append(f"hl: {e}"); hl = {}
    if not universe:
        raise RuntimeError("brak universe (MEXC i HL niedostępne)")
    normalize_mexc_funding(universe, cache)
    # v2.2: ceny WSZYSTKICH kontraktów (też poniżej progu płynności) do zwrotów po alertach
    all_prices = {k: v["price"] for k, v in mexc.items()}
    all_prices.update({k: v["price"] for k, v in hl.items()})   # też surowe nazwy HL (stare alerty "kPEPE")
    all_prices.update({hl_alias(k): v["price"] / 1000 for k, v in hl.items() if hl_alias(k) != k and hl_alias(k) not in mexc})
    all_prices.update({k: v["price"] for k, v in universe.items()})

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
                kl = fetch_hl_klines(u.get("hl_coin") or s["ticker"], "5m", 25)
        except Exception:
            kl = None
        scored.append(score_coin(s["ticker"], u, series.get(s["ticker"], []), flow, listings, kl))
    done = {s["ticker"] for s in scored}
    scored += [s for c, s in pre.items() if c not in done]
    scored.sort(key=lambda s: -s["score"])

    new_alerts = update_alerts(alerts, universe, scored, all_prices)
    try:
        test45 = update_test45(_load(TEST45_PATH, {}), universe, scored, all_prices)
        _save(TEST45_PATH, test45)
    except Exception as e:
        ERRORS.append(f"test45: {e}")
    st = stats(alerts)
    top = [s for s in scored if s["level"] != "none"][:10]
    watch = [s for s in scored if s["level"] == "none" and s["score"] >= 20][:10]
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "universe_n": len(universe),
        "top": top, "near": watch, "new_alerts": [a["ticker"] for a in new_alerts],
        "stats": st, "recent_alerts": sorted([a for a in alerts if a.get("kind", "pump") == "pump"], key=lambda a: -a["ts"])[:30],
        "dump_watch": [s for s in scored if "dump_warning" in s["parts"]][:10],
        "thresholds": {"watch": SCORE_WATCH, "high": SCORE_HIGH, "min_turnover_usd": MIN_TURNOVER_USD},
        "sources": SOURCE_STATUS,
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
