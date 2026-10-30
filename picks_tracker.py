#!/usr/bin/env python3
"""
picks_tracker.py — czy Crypto Picks cokolwiek dają.

PROBLEM
auto_picks.py nadpisuje crypto_picks.json każdego ranka. Pick żyje dobę i znika,
więc nie da się powiedzieć, czy kategoria "Momentum" wybiera lepiej niż
"Derivatives", ani czy zmiany w screenerze pomogły. Panel w terminalu pokazuje,
co screener wybrał — nie pokazuje, co z tego wyszło.

CO ROBI TEN SKRYPT
Prowadzi crypto_picks_history.json: każdy pick zapamiętany z ceną wejścia,
kategorią i poziomami z tezy, a potem domykany danymi z rynku.

Dla każdego picka liczy DWIE rzeczy, bo odpowiadają na dwa różne pytania:

  1. Zwrot po 24h / 3 dniach / 7 dniach od ceny z momentu picka.
     Pytanie: ile to dało. Nic nie zakłada o tym, kiedy ktoś by wyszedł.

  2. Czy cena dotknęła oporu z tezy (trafienie), czy invalidation (porażka),
     i co było pierwsze.
     Pytanie: czy teza się broniła.

Gdy obie granice padną w tej samej świecy dziennej, liczymy to jako porażkę.
Dane dzienne nie mówią, co było pierwsze w ciągu dnia, a zawyżanie wyniku
własnego screenera jest dokładnie tym błędem, którego ten plik ma unikać.

BACKFILL
Przy pierwszym uruchomieniu (brak crypto_picks_history.json) skrypt odtwarza
archiwum z historii gita — workflow commituje crypto_picks.json codziennie,
więc każdy dzień da się odczytać przez `git show <commit>:crypto_picks.json`.
Na 17.10.2026 to 41 dni i 117 picków wstecz do 5 września.

PODZIAŁ NA WERSJE SCREENERA
7.10.2026 doszedł risk_flag przy RSI > 70 i obniżenie score dla wykupionych.
Statystyka jest liczona osobno przed i po tej dacie — inaczej werdykt
o kategoriach miesza dwie różne wersje narzędzia.

UŻYCIE
    python3 picks_tracker.py            # dopisz dzisiejsze, domknij stare
    python3 picks_tracker.py --backfill # wymuś odtworzenie z gita
    python3 picks_tracker.py --dry-run  # nic nie zapisuj, pokaż co by zrobił
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request as ur
from datetime import datetime, timedelta, timezone
from pathlib import Path

FUSION_DIR = Path(__file__).resolve().parent
PICKS_PATH = FUSION_DIR / "crypto_picks.json"
HIST_PATH = FUSION_DIR / "crypto_picks_history.json"

# Data, od której screener ma filtr RSI > 70. Statystyka liczona osobno
# przed i po — żeby było widać, czy poprawka cokolwiek dała.
WERSJA_SCREENERA = "2026-10-07"

# Po tylu dniach pick jest zamknięty i już go nie przeliczamy.
HORYZONT_DNI = 7

# Cel bliżej niż tyle procent od wejścia nie jest celem — zostaje dotknięty
# w dniu zerowym samym szumem. Bez tego progu hit rate pokazywał 71% przy
# medianie zwrotu −6%, czyli mierzył kształt tezy, nie jakość wyboru.
MIN_DYSTANS_CELU = 3.0

# 30.10.2026 — uczciwe trafienia. Mimo progu 3% Momentum pokazywał 62,7% hit rate
# przy medianie zwrotu 7d −8%. Trzy przyczyny i trzy poprawki:
#  1. Świeca dzienna dnia zerowego zawiera godziny PRZED publikacją picka (05:00 UTC),
#     więc szczyt z nocy liczył się jako "trafienie". Teraz cel/stop sprawdzamy na
#     świecach godzinowych od godziny picka.
#  2. "expired_up" (7 dni bez celu, lekko na plusie) wliczało się do trafień.
#     Teraz trafienie = tylko dotknięty cel oddalony o ≥ MIN_DYSTANS_CELU.
#     expired_up raportujemy osobno.
#  3. Ceny < 0.01 były zapisane z obciętą precyzją (PEPE 4e-06), więc dystans do celu
#     liczył się od złej ceny. Przy wyraźnej rozbieżności bierzemy cenę z rynku
#     z godziny picka.
# Stare wpisy bez "metoda" == METODA są przeliczane, najwyżej MAX_PRZELICZEN_NA_CYKL
# na przebieg, żeby nie wydłużać 5-minutowego cyklu autopilota.
METODA = "h1-2026-10-30"
GODZINA_PICKA_UTC = 5
MAX_PRZELICZEN_NA_CYKL = 40

UA = "Mozilla/5.0 (compatible; TVCPicksTracker/1.0)"
SSL_CTX = ssl.create_default_context()


# ── sieć ─────────────────────────────────────────────────────────────────────

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


def symbol_z_picka(p):
    """MEXC chce 'WAL_USDT'. W picku jest ticker, a pełny symbol siedzi w source_url."""
    url = p.get("source_url") or ""
    if "/futures/" in url:
        s = url.rsplit("/futures/", 1)[1].strip()
        if s:
            return s
    t = (p.get("ticker") or "").upper()
    return f"{t}_USDT" if t else ""


_BTC_CACHE = {}


def swiece_btc(od_ts, do_ts):
    """
    Świece BTC do porównania. Pobierane raz na przebieg i trzymane w pamięci —
    przy 122 pickach bez cache'u byłoby 122 identyczne zapytania.
    """
    klucz = (int(od_ts) // 86400, int(do_ts) // 86400)
    if klucz not in _BTC_CACHE:
        _BTC_CACHE[klucz] = swiece_dzienne("BTC_USDT", od_ts, do_ts)
    return _BTC_CACHE[klucz]


def swiece_godzinowe(symbol, od_ts, do_ts):
    """Świece 1h z MEXC (publiczne API trzyma ~90 dni). Pusta lista zamiast wyjątku."""
    try:
        j = _get_json(
            f"https://contract.mexc.com/api/v1/contract/kline/{symbol}"
            f"?interval=Min60&start={int(od_ts)}&end={int(do_ts)}"
        )
    except Exception as e:
        print(f"[tracker] {symbol}: klines 1h nieosiągalne ({e})")
        return []
    d = (j or {}).get("data") or {}
    n = len(d.get("close") or [])
    out, seen = [], set()
    for i in range(n):
        t = int(d["time"][i])
        if t in seen:
            continue
        seen.add(t)
        out.append({"t": t, "o": float(d["open"][i]), "h": float(d["high"][i]),
                    "l": float(d["low"][i]), "c": float(d["close"][i])})
    out.sort(key=lambda s: s["t"])
    return out


def moment_picka(p, h1):
    """Unix ts początku godziny, od której liczymy pick.
    1) generated_at zapisany przy picku (od 30.10), 2) dla starych wpisów: 05:00 UTC,
    chyba że cena z tej godziny różni się od zapisanej o > 3% (pick generowany ręcznie
    później) — wtedy pierwsza godzina od 05:00, której open zgadza się z zapisaną ceną."""
    g = p.get("generated_at")
    if g:
        try:
            dt = datetime.fromisoformat(str(g).replace("Z", "+00:00"))
            return int(dt.timestamp()) // 3600 * 3600
        except ValueError:
            pass
    d0 = datetime.strptime(p["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    t5 = int(d0.timestamp()) + GODZINA_PICKA_UTC * 3600
    cena = p.get("price")
    if not cena or not h1 or cena < 0.01:      # ceny < 0.01 bywały obcięte — nie dopasowujemy
        return t5
    po = {s["t"]: s for s in h1}
    if t5 in po and abs(po[t5]["o"] / cena - 1) <= 0.03:
        return t5
    for t in range(t5, int(d0.timestamp()) + 24 * 3600, 3600):
        s = po.get(t)
        if s and abs(s["o"] / cena - 1) <= 0.03:
            return t
    return t5


def swiece_dzienne(symbol, od_ts, do_ts):
    """Świece dzienne z MEXC w zadanym oknie. Pusta lista zamiast wyjątku."""
    try:
        j = _get_json(
            f"https://contract.mexc.com/api/v1/contract/kline/{symbol}"
            f"?interval=Day1&start={int(od_ts)}&end={int(do_ts)}"
        )
    except Exception as e:
        print(f"[tracker] {symbol}: klines nieosiągalne ({e})")
        return []
    d = (j or {}).get("data") or {}
    n = len(d.get("close") or [])
    if not n:
        return []
    return [{"t": int(d["time"][i]), "o": float(d["open"][i]), "h": float(d["high"][i]),
             "l": float(d["low"][i]), "c": float(d["close"][i])} for i in range(n)]


# ── scoring jednego picka ────────────────────────────────────────────────────

def zwrot_btc(data_picka, dni):
    """
    Zwrot BTC w tym samym oknie co pick. None, gdy brak danych.

    Po co: 115 ze 117 picków to longi, a wrzesień–październik 2026 był risk-off.
    Bez tego punktu odniesienia nie da się odróżnić "screener źle wybiera"
    od "longi w spadającym rynku tracą, niezależnie od wyboru".
    """
    d0 = datetime.strptime(data_picka, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    sw = swiece_btc((d0 - timedelta(days=2)).timestamp(),
                    (d0 + timedelta(days=HORYZONT_DNI + 2)).timestamp())
    po = sorted((s for s in sw if s["t"] >= int(d0.timestamp())), key=lambda s: s["t"])
    if len(po) <= dni:
        return None
    baza = po[0]["o"]
    if not baza:
        return None
    return round((po[dni]["c"] - baza) / baza * 100.0, 2)


def policz_wynik(p, swiece, h1=None):
    """
    Zwraca słownik z wynikiem albo None, gdy brak danych.

    Konwencja dla longa: zysk gdy cena rośnie, cel = resistance, stop = invalidation.
    Dla shorta wszystko na odwrót, a celem jest support.
    """
    entry = p.get("price")
    if not entry or not swiece:
        return None

    short = (p.get("direction") or "long").lower() == "short"
    cel = p.get("support") if short else p.get("resistance")
    stop = p.get("invalidation")

    # Świece OD dnia wejścia włącznie — pick powstaje rano, więc ruch tego dnia
    # jeszcze się liczy.
    d0 = datetime.strptime(p["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    po = [s for s in swiece if s["t"] >= int(d0.timestamp())]
    if not po:
        return None
    po.sort(key=lambda s: s["t"])
    po = po[: HORYZONT_DNI + 1]

    def zwrot(close):
        r = (close - entry) / entry * 100.0
        return round(-r if short else r, 2)

    # zwroty po N dniach — close świecy z dnia wejścia + N
    zwroty = {}
    for n in (1, 3, 7):
        if len(po) > n:
            zwroty[f"return_{n}d_pct"] = zwrot(po[n]["c"])
        else:
            zwroty[f"return_{n}d_pct"] = None

    maks = max(s["h"] for s in po)
    mini = min(s["l"] for s in po)
    zwroty["max_favourable_pct"] = round(((mini - entry) / entry * -100.0) if short
                                         else ((maks - entry) / entry * 100.0), 2)
    zwroty["max_adverse_pct"] = round(((maks - entry) / entry * -100.0) if short
                                      else ((mini - entry) / entry * 100.0), 2)

    # Czy cel w ogóle jest celem.
    #
    # 25 picków na 112 ma opór USTAWIONY PONIŻEJ ceny wejścia, a 87% "trafień"
    # pada w dniu zerowym — w tej samej świecy, w której pick powstał. Taki cel
    # jest spełniony z definicji, nie przez ruch rynku, i zawyża hit rate do
    # bezużytecznych 71% przy medianie zwrotu −6%.
    #
    # Dlatego pick, którego cel leży mniej niż MIN_DYSTANS_CELU od wejścia,
    # dostaje target_mierzalny = False i wypada z liczenia trafień. Zwroty
    # liczymy mu normalnie — one są poprawne niezależnie od tego, gdzie ktoś
    # postawił opór.
    # 30.10: od kiedy i od jakiej ceny liczymy cel/stop
    t0 = moment_picka(p, h1) if h1 else None
    h1_po = [s for s in (h1 or []) if t0 is not None and t0 <= s["t"] < t0 + HORYZONT_DNI * 86400]
    entry_cel = entry
    if h1_po and entry and abs(h1_po[0]["o"] / entry - 1) > 0.03 and entry < 0.01:
        entry_cel = h1_po[0]["o"]          # obcięta precyzja ceny (np. PEPE 4e-06)

    dyst = None
    if cel is not None and entry_cel:
        dyst = (entry_cel - cel) / entry_cel * 100.0 if short else (cel - entry_cel) / entry_cel * 100.0
    mierzalny = dyst is not None and dyst >= MIN_DYSTANS_CELU

    # co padło pierwsze: cel czy stop — na świecach 1h od godziny picka, gdy są;
    # inaczej (starsze niż ~90 dni) na dziennych jak dotąd.
    wynik = "open"
    dzien = None
    seria = h1_po if h1_po else po
    for i, s in enumerate(seria):
        trafil_cel = cel is not None and (s["l"] <= cel if short else s["h"] >= cel)
        trafil_stop = stop is not None and (s["h"] >= stop if short else s["l"] <= stop)
        if trafil_stop:
            # Obie granice w tej samej świecy → liczymy jako porażkę.
            # Dane dzienne nie rozstrzygają kolejności, więc wybieramy wariant
            # niekorzystny dla nas zamiast zgadywać na swoją korzyść.
            wynik, dzien = "invalidated", i
            break
        if trafil_cel:
            wynik, dzien = "target_hit", i
            break
    if h1_po and dzien is not None:
        dzien = (seria[dzien]["t"] - t0) // 86400      # dzień liczony od godziny picka

    dni_minelo = len(po) - 1
    if wynik == "open" and dni_minelo >= HORYZONT_DNI:
        # Horyzont minął bez rozstrzygnięcia — decyduje zwrot po 7 dniach.
        z7 = zwroty.get("return_7d_pct")
        wynik = "expired_up" if (z7 or 0) > 0 else "expired_down"

    return {
        **zwroty,
        "outcome": wynik,
        "outcome_day": dzien,
        "days_elapsed": dni_minelo,
        "resolved": wynik != "open",
        "target_dist_pct": round(dyst, 2) if dyst is not None else None,
        "target_mierzalny": mierzalny,
        "trafiony_w_dniu_0": wynik == "target_hit" and dzien == 0,
        "metoda": METODA if h1_po else "d1",
        "entry_ts": t0 if h1_po else None,
        "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **btc_porownanie(p, zwroty, short),
    }


def btc_porownanie(p, zwroty, short):
    """
    Zwrot BTC w tych samych oknach + nadwyżka picka nad BTC (alfa).

    Dla shorta BTC też odwracamy — short, który stracił 5%, gdy BTC urósł 10%,
    zachował się zgodnie z oczekiwaniem i nie powinien wyglądać jak porażka.
    """
    out = {}
    for n in (1, 3, 7):
        b = zwrot_btc(p["date"], n)
        if short and b is not None:
            b = -b
        out[f"btc_{n}d_pct"] = b
        z = zwroty.get(f"return_{n}d_pct")
        out[f"alpha_{n}d_pct"] = round(z - b, 2) if (z is not None and b is not None) else None
    return out


# ── archiwum ─────────────────────────────────────────────────────────────────

def klucz(p):
    return f"{p['date']}|{p['ticker']}"


def zachowaj_z_picka(p, data, generated_at=None):
    """Tylko pola potrzebne do rozliczenia — archiwum ma nie puchnąć tezami."""
    return {
        "date": data,
        "ticker": p.get("ticker"),
        "symbol": symbol_z_picka(p),
        "category": p.get("category"),
        "direction": (p.get("direction") or "long").lower(),
        "price": p.get("price"),
        "support": p.get("support"),
        "resistance": p.get("resistance"),
        "invalidation": p.get("invalidation"),
        "score": p.get("score"),
        "rsi14": p.get("rsi14"),
        # Potrzebne do raportu progów i do analiz, których jeszcze nie znamy.
        # Kosztuje trzy liczby na pick, a bez nich trzeba znowu kopać w gicie.
        "mcap_usd": p.get("mcap_usd"),
        "turnover_24h_usd": p.get("turnover_24h_usd"),
        "funding_pct_8h": p.get("funding_pct_8h"),
        "change_24h_pct": p.get("change_24h_pct"),
        "change_7d_pct": p.get("change_7d_pct"),
        "risk_flag": (p.get("risk_flag") or "")[:160] or None,
        "screener": "v2" if data >= WERSJA_SCREENERA else "v1",
        "generated_at": generated_at,
    }


def backfill_z_gita():
    """Odtwarza archiwum z commitów crypto_picks.json. Bez gita zwraca pustkę."""
    try:
        coms = subprocess.run(
            ["git", "log", "--format=%H", "--", "crypto_picks.json"],
            cwd=FUSION_DIR, capture_output=True, text=True, timeout=60
        ).stdout.split()
    except Exception as e:
        print(f"[tracker] backfill: git niedostępny ({e})")
        return {}
    arch, dni = {}, set()
    for c in coms:
        try:
            raw = subprocess.run(["git", "show", f"{c}:crypto_picks.json"],
                                 cwd=FUSION_DIR, capture_output=True, text=True,
                                 timeout=30).stdout
            d = json.loads(raw)
        except Exception:
            continue
        data = d.get("date")
        if not data or data in dni:
            continue          # pierwszy commit danego dnia wygrywa
        dni.add(data)
        for p in d.get("picks") or []:
            if p.get("ticker") and p.get("price"):
                arch[f"{data}|{p['ticker']}"] = zachowaj_z_picka(p, data, d.get("generated_at"))
    print(f"[tracker] backfill z gita: {len(dni)} dni, {len(arch)} picków")
    return arch


def wczytaj_archiwum():
    if HIST_PATH.exists():
        try:
            d = json.loads(HIST_PATH.read_text())
            return {k: v for k, v in (d.get("entries") or {}).items()}
        except Exception as e:
            print(f"[tracker] crypto_picks_history.json nieczytelny ({e}) — zaczynam od gita")
    return None


def dopisz_dzisiejsze(arch):
    if not PICKS_PATH.exists():
        return 0
    try:
        d = json.loads(PICKS_PATH.read_text())
    except Exception as e:
        print(f"[tracker] crypto_picks.json nieczytelny: {e}")
        return 0
    data = d.get("date")
    if not data:
        return 0
    nowe = 0
    for p in d.get("picks") or []:
        k = f"{data}|{p.get('ticker')}"
        if p.get("ticker") and p.get("price") and k not in arch:
            arch[k] = zachowaj_z_picka(p, data, d.get("generated_at"))
            nowe += 1
    return nowe


# ── statystyka ───────────────────────────────────────────────────────────────

# Progi kapitalizacji do przetestowania — raport pokazuje, co by było, gdyby
# screener odcinał w każdym z tych miejsc. NIC nie zmienia w doborze picków.
#
# Po co osobny raport zamiast od razu progu w auto_picks.py: na 43 dniach
# próg 2 mld wyglądał najlepiej (+3,7% wobec BTC), ale 12/21 trafień nie różni
# się od rzutu monetą (p = 0,33), a w drugiej połowie okresu efekt zniknął
# (+6,3% wrzesień, −4,9% październik). Zanim wytniemy 4 na 5 picków, chcemy
# zobaczyć te same progi na danych, których nie oglądaliśmy przy wyborze.
PROGI_MCAP = [0, 500_000_000, 1_000_000_000, 2_000_000_000]


def mediana(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2, 2)


def raport_progow(arch, od_daty=None):
    """
    Co by było, gdyby screener odcinał przy danej kapitalizacji.

    od_daty pozwala policzyć to samo na danych świeższych niż te, na których
    próg był wybierany — bo próg dobrany i oceniony na tej samej próbce
    zawsze wygląda dobrze.
    """
    out = {}
    for prog in PROGI_MCAP:
        g = [e for e in arch.values()
             if (e.get("mcap_usd") or 0) >= prog
             and (e.get("wynik") or {}).get("alpha_7d_pct") is not None
             and (od_daty is None or e["date"] >= od_daty)]
        if not g:
            out[str(prog)] = {"n": 0}
            continue
        alfy = [e["wynik"]["alpha_7d_pct"] for e in g]
        zwroty = [e["wynik"]["return_7d_pct"] for e in g
                  if e["wynik"].get("return_7d_pct") is not None]
        lepsze = sum(1 for x in alfy if x > 0)
        out[str(prog)] = {
            "n": len(g),
            "median_alpha_7d_pct": mediana(alfy),
            "median_return_7d_pct": mediana(zwroty),
            "share_beat_btc_pct": round(lepsze / len(alfy) * 100, 1),
            "beat_btc": lepsze,
        }
    return out


def podsumuj(arch):
    """
    Statystyka per kategoria i per wersja screenera.

    Główną liczbą jest ZWROT PO 7 DNIACH, nie hit rate. Powód w komentarzu
    przy MIN_DYSTANS_CELU: trafienia liczone na oryginalnych poziomach z tezy
    mierzyły to, jak blisko ktoś postawił opór, a nie czy pick był dobry.

    Hit rate zostaje, ale liczony wyłącznie na pickach z mierzalnym celem
    (opór co najmniej MIN_DYSTANS_CELU powyżej wejścia). Obok podajemy,
    ile picków z tego wypadło — żeby wykluczenie było widoczne, nie ukryte.
    """
    def pusty():
        return {"n": 0, "rozstrzygnietych": 0, "mierzalnych": 0, "niemierzalnych": 0,
                "w_dniu_0": 0, "target_hit": 0, "invalidated": 0,
                "expired_up": 0, "expired_down": 0, "zwroty7": [], "zwroty1": [],
                "dodatnie7": 0, "alfa7": [], "alfa_dodatnia7": 0}

    wg = {}
    for e in arch.values():
        w = e.get("wynik") or {}
        for kl in (("all", e["category"]), (e["screener"], e["category"]),
                   ("all", "RAZEM"), (e["screener"], "RAZEM")):
            s = wg.setdefault(kl, pusty())
            s["n"] += 1
            if w.get("resolved"):
                s["rozstrzygnietych"] += 1
                if w.get("trafiony_w_dniu_0"):
                    s["w_dniu_0"] += 1
                if w.get("target_mierzalny"):
                    s["mierzalnych"] += 1
                    s[w["outcome"]] = s.get(w["outcome"], 0) + 1
                else:
                    s["niemierzalnych"] += 1
            z7 = w.get("return_7d_pct")
            if z7 is not None:
                s["zwroty7"].append(z7)
                if z7 > 0:
                    s["dodatnie7"] += 1
            if w.get("return_1d_pct") is not None:
                s["zwroty1"].append(w["return_1d_pct"])
            a7 = w.get("alpha_7d_pct")
            if a7 is not None:
                s["alfa7"].append(a7)
                if a7 > 0:
                    s["alfa_dodatnia7"] += 1

    out = {}
    for (wersja, kat), s in wg.items():
        mier = s["mierzalnych"]
        udane = s["target_hit"]          # 30.10: expired_up nie jest trafieniem celu
        n7 = len(s["zwroty7"])
        out.setdefault(wersja, {})[kat] = {
            "n": s["n"],
            "resolved": s["rozstrzygnietych"],
            # Główna miara: gdzie cena była po 7 dniach.
            "median_return_7d_pct": mediana(s["zwroty7"]),
            "median_return_1d_pct": mediana(s["zwroty1"]),
            "share_positive_7d_pct": round(s["dodatnie7"] / n7 * 100, 1) if n7 else None,
            "sample_7d": n7,
            # Nadwyżka nad BTC — czy screener dodaje cokolwiek ponad kierunek rynku.
            "median_alpha_7d_pct": mediana(s["alfa7"]),
            "share_beat_btc_7d_pct": (round(s["alfa_dodatnia7"] / len(s["alfa7"]) * 100, 1)
                                      if s["alfa7"] else None),
            "sample_alpha_7d": len(s["alfa7"]),
            # Miara pomocnicza, tylko na mierzalnych celach.
            "hit_rate_pct": round(udane / mier * 100, 1) if mier else None,
            "hit_rate_incl_expired_up_pct": (round((udane + s["expired_up"]) / mier * 100, 1)
                                             if mier else None),
            "hit_sample": mier,
            "excluded_unmeasurable": s["niemierzalnych"],
            "hits_on_day_0": s["w_dniu_0"],
            "target_hit": s["target_hit"],
            "invalidated": s["invalidated"],
            "expired_up": s["expired_up"],
            "expired_down": s["expired_down"],
        }
    return out


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    dry = "--dry-run" in sys.argv
    force_bf = "--backfill" in sys.argv

    arch = None if force_bf else wczytaj_archiwum()
    if arch is None:
        arch = backfill_z_gita()
        if not arch:
            print("[tracker] brak danych do odtworzenia — kończę bez zmian")
            return 0

    nowe = dopisz_dzisiejsze(arch)
    if nowe:
        print(f"[tracker] dopisano {nowe} nowych picków z crypto_picks.json")

    # Przeliczamy tylko to, co jeszcze nie jest domknięte — żeby nie bić
    # w MEXC po 117 symbolach każdego dnia.
    dzis = datetime.now(timezone.utc).date()
    do_liczenia = []
    wszystko = "--przelicz-wszystko" in sys.argv
    stare = 0
    for k, e in arch.items():
        w = e.get("wynik")
        wiek_dni = (dzis - datetime.strptime(e["date"], "%Y-%m-%d").date()).days
        if (w and w.get("resolved") and w.get("metoda") in (METODA, "d1")
                and (w.get("return_7d_pct") is not None or wiek_dni > HORYZONT_DNI + 2)):
            # 30.10: pick rozstrzygnięty wcześnie (cel/stop) liczymy dalej, aż będzie
            # zwrot 7d — wcześniej zostawał bez niego i wypadał ze statystyk zwrotu.
            continue
        if w and w.get("resolved"):
            # rozliczony starą metodą — przeliczamy, ale porcjami
            if not wszystko and stare >= MAX_PRZELICZEN_NA_CYKL:
                continue
            stare += 1
        wiek = (dzis - datetime.strptime(e["date"], "%Y-%m-%d").date()).days
        if wiek < 0:
            continue
        do_liczenia.append(k)

    print(f"[tracker] do przeliczenia: {len(do_liczenia)} z {len(arch)}")
    policzone, bez_danych = 0, 0
    for i, k in enumerate(do_liczenia):
        e = arch[k]
        d0 = datetime.strptime(e["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        sw = swiece_dzienne(e["symbol"],
                            (d0 - timedelta(days=2)).timestamp(),
                            (d0 + timedelta(days=HORYZONT_DNI + 2)).timestamp())
        h1 = swiece_godzinowe(e["symbol"], d0.timestamp(),
                              (d0 + timedelta(days=HORYZONT_DNI + 1)).timestamp())
        p = dict(e)
        w = policz_wynik(p, sw, h1)
        if w:
            e["wynik"] = w
            policzone += 1
        else:
            bez_danych += 1
        if i % 10 == 9:
            time.sleep(1)          # MEXC nie lubi serii bez oddechu

    print(f"[tracker] policzono {policzone}, bez danych {bez_danych}")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "horizon_days": HORYZONT_DNI,
        "screener_v2_since": WERSJA_SCREENERA,
        "min_target_dist_pct": MIN_DYSTANS_CELU,
        "note": ("Główna miara to zwrot po 7 dniach od ceny z momentu picka. "
                 f"Hit rate = dotknięty cel oddalony o ≥{MIN_DYSTANS_CELU}% od wejścia, sprawdzany "
                 "na świecach 1h od godziny picka (od 30.10.2026); bliższe cele i 'expired_up' "
                 "nie liczą się jako trafienie. "
                 "Gdy cel i invalidation padną w tej samej świecy, liczone jako invalidated."),
        "summary": podsumuj(arch),
        # Co by było, gdyby odcinać przy kolejnych progach kapitalizacji.
        # "od_17_10" liczy to samo tylko na pickach z dnia decyzji wzwyż —
        # czyli na danych, których nie oglądaliśmy, dobierając próg.
        "progi_mcap": raport_progow(arch),
        "progi_mcap_od_17_10": raport_progow(arch, od_daty="2026-10-17"),
        "entries": arch,
    }
    if dry:
        print(json.dumps(out["summary"], indent=2, ensure_ascii=False))
        print("[tracker] --dry-run: nic nie zapisano")
        return 0

    HIST_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[tracker] zapisano {HIST_PATH.name}: {len(arch)} picków")
    for wersja in ("all", "v1", "v2"):
        s = out["summary"].get(wersja, {}).get("RAZEM")
        if s:
            print(f"  {wersja:4s} n={s['n']:3d}  mediana7d={s['median_return_7d_pct']}%  "
                  f"vs BTC={s['median_alpha_7d_pct']}%  "
                  f"lepsze od BTC={s['share_beat_btc_7d_pct']}%  "
                  f"hit={s['hit_rate_pct']}% (z {s['hit_sample']})")
    print("\n  progi kapitalizacji — co by było, gdyby odcinać:")
    for prog, r in sorted(out["progi_mcap"].items(), key=lambda x: int(x[0])):
        if not r.get("n"):
            continue
        etykieta = "bez progu" if prog == "0" else f"≥ {int(prog)/1e6:.0f} mln"
        print(f"      {etykieta:>12s}  n={r['n']:3d}  vs BTC={r['median_alpha_7d_pct']:+6.2f}%  "
              f"lepsze od BTC {r['beat_btc']}/{r['n']} ({r['share_beat_btc_pct']}%)")
    swieze = {k: v for k, v in out["progi_mcap_od_17_10"].items() if v.get("n")}
    if swieze:
        print("\n  to samo, ale tylko na pickach od 17.10 (dane spoza doboru progu):")
        for prog, r in sorted(swieze.items(), key=lambda x: int(x[0])):
            etykieta = "bez progu" if prog == "0" else f"≥ {int(prog)/1e6:.0f} mln"
            print(f"      {etykieta:>12s}  n={r['n']:3d}  vs BTC={r['median_alpha_7d_pct']:+6.2f}%  "
                  f"lepsze od BTC {r['beat_btc']}/{r['n']}")
    else:
        print("\n  (brak jeszcze rozliczonych picków od 17.10 — wróć za tydzień)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
