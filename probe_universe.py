#!/usr/bin/env python3
"""
probe_universe.py — sprawdza, dla których tokenów dostępne są wszystkie warstwy danych,
których używa Fusion Score. Uruchom lokalnie:  python3 probe_universe.py

Warstwy sprawdzane:
  HL   — Hyperliquid perp (Smart Money: pozycje wielorybów, OI, funding)
  FUT  — Binance USDT-M futures (perp taker flow, funding, OI, likwidacje)
  SPOT — Binance spot (CVD spot, klines do TA i poziomów)

Token bez kompletu 3/3 trafia do koszyka tylko wtedy, gdy świadomie zdecydujesz,
co scoring robi z brakującą warstwą.
"""
import json
import urllib.request

# Kandydaci — płynne perpetuale, kolejność wg wielkości i popularności.
# Dopisz / usuń wedle uznania przed uruchomieniem.
CANDIDATES = [
    # obecny koszyk
    "BTC", "ETH", "SOL", "XRP", "SUI",
    # large cap
    "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "BCH", "TRX", "TON", "ETC",
    # L1 / L2
    "NEAR", "APT", "ARB", "OP", "SEI", "TIA", "ATOM", "INJ",
    # DeFi
    "AAVE", "UNI", "MKR", "LDO",
    # meme / high beta
    "PEPE", "WIF", "BONK", "HYPE",
]

UA = {"User-Agent": "Mozilla/5.0 (TVC Fusion universe probe)"}
TIMEOUT = 25


def _get(url, data=None):
    req = urllib.request.Request(url, data=data, headers=dict(UA))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def hyperliquid_universe():
    payload = json.dumps({"type": "meta"}).encode()
    data = _get("https://api.hyperliquid.xyz/info", payload)
    return {a["name"].upper() for a in data["universe"]}


def binance_universe(base_url):
    data = _get(f"{base_url}/exchangeInfo")
    return {
        s["baseAsset"].upper()
        for s in data["symbols"]
        if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
    }


def main():
    print("Pobieram listy instrumentów...\n")
    try:
        hl = hyperliquid_universe()
    except Exception as e:
        print(f"  Hyperliquid: BLAD ({e})"); hl = set()
    try:
        fut = binance_universe("https://fapi.binance.com/fapi/v1")
    except Exception as e:
        print(f"  Binance futures: BLAD ({e})"); fut = set()
    try:
        spot = binance_universe("https://api.binance.com/api/v3")
    except Exception as e:
        print(f"  Binance spot: BLAD ({e})"); spot = set()

    print(f"{'TOKEN':8} {'HL':>4} {'FUT':>5} {'SPOT':>6}   status")
    print("-" * 44)

    full, partial, none_ = [], [], []
    for t in CANDIDATES:
        a, b, c = t in hl, t in fut, t in spot
        n = sum((a, b, c))
        mark = lambda x: " +" if x else " -"
        if n == 3:
            status, bucket = "komplet", full
        elif n == 0:
            status, bucket = "BRAK DANYCH", none_
        else:
            status, bucket = f"niepelne ({n}/3)", partial
        bucket.append(t)
        print(f"{t:8} {mark(a):>4} {mark(b):>5} {mark(c):>6}   {status}")

    print()
    print(f"KOMPLET 3/3 ({len(full)}):")
    print("  " + ", ".join(full))
    if partial:
        print(f"\nNIEPELNE ({len(partial)}): " + ", ".join(partial))
        print("  → wymagaja decyzji: pominac warstwe czy pominac token")
    if none_:
        print(f"\nBRAK ({len(none_)}): " + ", ".join(none_))

    print("\nGotowa lista do wklejenia w auto_fusion.py:")
    print("TICKERS = " + json.dumps(full))


if __name__ == "__main__":
    main()
