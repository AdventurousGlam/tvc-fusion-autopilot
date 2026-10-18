#!/usr/bin/env python3
"""
test_no_peeking.py — czy jakakolwiek decyzja dotknęła danych z przyszłości.

Po co to istnieje
─────────────────
Wyniki konta modelowego są warte tyle, ile warte jest założenie, że bot w chwili
decyzji widział wyłącznie przeszłość. Jeśli gdziekolwiek przecieka przyszłość,
wszystkie dotychczasowe liczby są zawyżone i nie ma o tym żadnego sygnału —
wygląda to po prostu jak dobra strategia.

To nie jest hipotetyczne ryzyko. `sl_rescan_bug` (naprawiony 7.09.2026, commit
a4d814c) robił dokładnie to: podniesiony stop loss trafiał w stare świece, więc
pozycje zamykały się po cenach z przeszłości. Zostało wykryte przypadkiem, nie
przez test. Ten plik jest tym testem.

Co sprawdza
───────────
  1. KOLEJNOŚĆ WEJŚCIA  — migawka fusion użyta do decyzji musi być STARSZA niż
     moment otwarcia pozycji. Źródło: "Auto-generated HH:MM" w risk_flag.
  2. KOLEJNOŚĆ WYJŚCIA  — wyjście musi nastąpić PO wejściu, nigdy przed ani w tej
     samej sekundzie.
  3. CENA WYJŚCIA W ZAKRESIE — (tryb --online) świeca, w której zapisano wyjście,
     musi istnieć i faktycznie obejmować cenę wyjścia. Łapie zamknięcia po cenach,
     których nigdy nie było.
  4. SL/TP PO WEJŚCIU — (tryb --online) cena wyjścia nie mogła być osiągalna
     wyłącznie przed wejściem. To jest wprost test na powrót sl_rescan_bug.

Użycie
──────
    python3 test_no_peeking.py            # same sprawdzenia offline, bez sieci
    python3 test_no_peeking.py --online   # dokłada weryfikację po świecach Binance

Kod wyjścia 1 = znaleziono przeciek. W CI to ma zatrzymać pipeline.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import re
import urllib.request as ur
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "paper_trades.db"
ONLINE = "--online" in sys.argv

# v1.1 (14.10.2026) — okno dni dla trybu online.
# Tryb --online robi zapytanie o świece dla KAŻDEJ zamkniętej pozycji. Przy 101
# pozycjach przebieg autopilota urósł z ~3 min do 6 min 48 s i nałożył się na
# następny cykl (cron co 5 min). Dwa przebiegi zaczęły walczyć o push, pętla
# ponawiania się wyczerpała i 14.10 o 04:11 oraz 04:20 UTC poszedł alarm
# awarii infrastruktury.
# Codziennie wystarczy sprawdzać świeże zamknięcia — defekt wykrywa się w dniu
# powstania, a nie po sześciu tygodniach, i o to w tym teście chodzi.
# Pełne skanowanie historii: `--online --days 0` (ręcznie, nie w autopilocie).
def _arg_int(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return default

DAYS = _arg_int("--days", 7)   # 0 = bez ograniczenia

BINANCE = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT",
    "SUI": "SUIUSDT", "DOGE": "DOGEUSDT", "ADA": "ADAUSDT", "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT", "DOT": "DOTUSDT", "LTC": "LTCUSDT", "BCH": "BCHUSDT",
    "TRX": "TRXUSDT", "NEAR": "NEARUSDT", "APT": "APTUSDT",
}

failures: list[str] = []
# v1.2 (14.10.2026): pozycje z flagą excluded=1 w bazie są znane, opisane
# w REJESTR_WYKLUCZEN.md i mają udokumentowaną przyczynę. Dalej je sprawdzamy
# i WYPISUJEMY, ale nie liczą się jako nowy defekt — inaczej audyt tygodniowy
# świeciłby na czerwono co tydzień z tego samego powodu, a alarm, który dzwoni
# zawsze, przestaje cokolwiek znaczyć.
known: list[str] = []
warnings: list[str] = []


def fail(msg):
    failures.append(msg)
    print(f"  ✗ {msg}")


def warn(msg):
    warnings.append(msg)
    print(f"  ! {msg}")


def parse_ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def snapshot_time(row):
    """Czas migawki fusion z risk_flag: 'Auto-generated 17:22. ...' → datetime tego dnia."""
    m = re.search(r"Auto-generated (\d{2}):(\d{2})", str(row["risk_flag"] or ""))
    opened = parse_ts(row["opened_at"])
    if not m or not opened:
        return None
    snap = opened.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                          second=0, microsecond=0)
    # Migawka tuż przed północą, pozycja tuż po — cofamy o dobę zamiast zgłaszać przeciek.
    if snap > opened + timedelta(minutes=5):
        snap -= timedelta(days=1)
    return snap


def klines(ticker, start_ms, end_ms, interval="1m"):
    """
    Świece z całego okna, stronicowane.

    Binance oddaje maksymalnie 1000 świec na zapytanie, czyli przy interwale 1m
    tylko 16 h 40 min. Wcześniej pobieraliśmy jedną stronę i milcząco gubiliśmy
    resztę okna — przy pozycji trzymanej dłużej sprawdzenie [5] widziało sam
    początek i orzekało "poziom nigdy nie dotknięty", choć został dotknięty
    później. Sześć z ośmiu zgłoszeń z 18.10.2026 miało właśnie taką przyczynę,
    w tym #102 NEAR, gdzie TP2 padło w świecy 10:45 — poza pobranym zakresem.
    Fałszywy alarm w audycie jest groźniejszy niż brak alarmu: podważa zaufanie
    do rejestru i każe wykluczać pozycje, z którymi nic nie było nie tak.
    """
    sym = BINANCE.get(ticker)
    if not sym:
        return []
    MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}.get(interval, 60_000)
    out, kursor, start_ms, end_ms = [], int(start_ms), int(start_ms), int(end_ms)
    # Zabezpieczenie przed pętlą bez końca, gdyby API przestało przesuwać kursor.
    for _ in range(50):
        if kursor > end_ms:
            break
        url = (f"https://data-api.binance.vision/api/v3/klines?symbol={sym}"
               f"&interval={interval}&startTime={kursor}&endTime={end_ms}&limit=1000")
        try:
            with ur.urlopen(url, timeout=20) as r:
                strona = json.loads(r.read())
        except Exception as e:
            warn(f"klines {ticker}: {e}")
            break
        if not strona:
            break
        out.extend(strona)
        nowy = int(strona[-1][0]) + MS
        if nowy <= kursor:          # API nie posuwa się dalej — kończymy
            break
        kursor = nowy
        if len(strona) < 1000:      # ostatnia strona
            break
    return out


def main():
    if not DB_PATH.exists():
        print(f"Brak bazy: {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM positions WHERE status='closed' ORDER BY id"
    ).fetchall()
    total = len(rows)
    # Okno dotyczy TYLKO trybu online — sprawdzenia offline są darmowe, więc
    # lecą na całej historii zawsze.
    if ONLINE and DAYS > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
        rows = [r for r in rows if (r["closed_at"] or "") >= cutoff]
    okno = f", okno {DAYS} dni" if (ONLINE and DAYS > 0) else ""
    print(f"Sprawdzam {len(rows)} z {total} zamkniętych pozycji "
          f"({'online + offline' if ONLINE else 'tylko offline'}{okno}).\n")
    if ONLINE and DAYS > 0 and len(rows) < total:
        print(f"    (pominięto {total - len(rows)} starszych — pełne skanowanie: --online --days 0)\n")

    # ── 1. Kolejność wejścia ──────────────────────────────────────────────
    print("[1] Migawka fusion starsza niż otwarcie pozycji")
    checked = 0
    for r in rows:
        snap, opened = snapshot_time(r), parse_ts(r["opened_at"])
        if not snap or not opened:
            continue
        checked += 1
        if snap > opened:
            fail(f"#{r['id']} {r['ticker']}: decyzja z migawki {snap:%H:%M}, "
                 f"pozycja otwarta {opened:%H:%M} — dane z PRZYSZŁOŚCI")
    print(f"  sprawdzono {checked}, bez znacznika {len(rows)-checked}\n")

    # ── 2. Kolejność wyjścia ──────────────────────────────────────────────
    print("[2] Wyjście po wejściu")
    for r in rows:
        opened = parse_ts(r["opened_at"])
        closed = parse_ts(r["exit_date"]) or parse_ts(r["closed_at"])
        if not opened or not closed:
            continue
        if closed <= opened:
            fail(f"#{r['id']} {r['ticker']}: wyjście {closed.isoformat()} "
                 f"nie jest późniejsze niż wejście {opened.isoformat()}")
    print("  gotowe\n")

    if not ONLINE:
        print("Pomijam sprawdzenia 3 i 4 (wymagają sieci). Uruchom z --online.\n")
    else:
        # ── 3. Opóźnienie zapisu: ile minut przed zapisanym czasem wyjścia
        #      poziom był faktycznie dotknięty. To MIARA, nie błąd — bot skanuje
        #      co kilka minut, więc jakieś opóźnienie jest nieuniknione.
        #      Duże opóźnienie oznacza, że znacznik czasu wyjścia jest fikcją,
        #      ale sam wynik finansowy może być poprawny (rozstrzyga [5]).
        print("[3] Opóźnienie między dotknięciem poziomu a zapisanym wyjściem")
        lags = []
        for r in rows:
            opened = parse_ts(r["opened_at"])
            closed = parse_ts(r["exit_date"]) or parse_ts(r["closed_at"])
            px = r["exit_price"]
            if not opened or not closed or not px or closed <= opened:
                continue
            if r["hit_or_miss"] not in ("hit_sl", "hit_tp1", "hit_tp2"):
                continue
            ks = klines(r["ticker"], int(opened.timestamp() * 1000),
                        int(closed.timestamp() * 1000) + 60_000, interval="1m")
            if not ks:
                continue
            pxf = float(px)
            touch = None
            for k in ks:
                if float(k[3]) * 0.9995 <= pxf <= float(k[2]) * 1.0005:
                    touch = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
                    break
            if touch is None:
                continue  # brak dotknięcia obsługuje [5]
            lag = (closed - touch).total_seconds() / 60
            lags.append((lag, r["id"], r["ticker"]))
        if lags:
            lags.sort(reverse=True)
            med = sorted(l[0] for l in lags)[len(lags) // 2]
            print(f"  pozycji zmierzonych: {len(lags)} · mediana opóźnienia: {med:.0f} min")
            print("  największe opóźnienia:")
            for lag, pid, tick in lags[:5]:
                print(f"    #{pid} {tick}: {lag:.0f} min")
            if med > 15:
                warn(f"mediana opóźnienia {med:.0f} min — znaczniki czasu wyjść są "
                     f"orientacyjne, nie podawaj ich jako dokładnych")
        print("  gotowe\n")

        # ── 4. Poziom wyjścia osiągalny dopiero PO wejściu ────────────────
        print("[4] SL/TP nie trafiony świecą sprzed wejścia (powrót sl_rescan_bug)")
        for r in rows:
            if r["hit_or_miss"] not in ("hit_sl", "hit_tp1", "hit_tp2"):
                continue
            opened = parse_ts(r["opened_at"])
            closed = parse_ts(r["exit_date"]) or parse_ts(r["closed_at"])
            if not opened or not closed or closed <= opened:
                continue
            s = int((opened - timedelta(hours=6)).timestamp() * 1000)
            e = int(opened.timestamp() * 1000)
            before = klines(r["ticker"], s, e)
            if not before:
                continue
            px = float(r["exit_price"])
            lo = min(float(k[3]) for k in before)
            hi = max(float(k[2]) for k in before)
            long_ = (r["direction"] or "long").lower() == "long"
            sl_hit = r["hit_or_miss"] == "hit_sl"
            # Poziom osiągalny PRZED wejściem to podejrzenie, nie dowód — stąd ostrzeżenie.
            reachable = (lo <= px) if (long_ == sl_hit) else (hi >= px)
            if reachable:
                warn(f"#{r['id']} {r['ticker']} {r['hit_or_miss']}: poziom {px} był "
                     f"w zasięgu już przed wejściem (6h przed: {lo}–{hi}) — sprawdź ręcznie")
        print("  gotowe\n")

        # ── 5. Czy poziom wyjścia był KIEDYKOLWIEK dotknięty ─────────────
        # Sprawdzenie [3] łapie rozjazd ceny i czasu, ale nie odróżnia dwóch
        # bardzo różnych rzeczy:
        #   (a) poziom był dotknięty, bot zauważył to przy późniejszym skanie
        #       i zapisał czas wykrycia zamiast czasu dotknięcia — wada zapisu,
        #   (b) poziom nigdy nie został dotknięty, a pozycja i tak się zamknęła
        #       po tej cenie — wynik wyssany z palca.
        # Różnica decyduje o tym, czy publikowane liczby są prawdziwe.
        print("[5] Czy poziom wyjścia był w ogóle osiągnięty (wejście → wyjście)")
        for r in rows:
            opened = parse_ts(r["opened_at"])
            closed = parse_ts(r["exit_date"]) or parse_ts(r["closed_at"])
            px = r["exit_price"]
            if not opened or not closed or not px or closed <= opened:
                continue
            if r["hit_or_miss"] not in ("hit_sl", "hit_tp1", "hit_tp2"):
                continue
            # Okno do +10 min po zapisanym wyjściu, nie +1 min. Powód: bot sprawdza
            # świece 5-minutowe i zapisuje czas wykrycia, więc dotknięcie poziomu
            # mogło nastąpić w ogonie świecy już po tym znaczniku.
            # Tolerancja 0,15% zamiast 0,05% — różnice ticku i opóźnienia feedu.
            s = int(opened.timestamp() * 1000)
            e = int(closed.timestamp() * 1000) + 600_000
            ks = klines(r["ticker"], s, e, interval="1m")
            if not ks:
                continue
            lo = min(float(k[3]) for k in ks)
            hi = max(float(k[2]) for k in ks)
            px = float(px)
            TOL = 0.0015
            if not (lo * (1 - TOL) <= px <= hi * (1 + TOL)):
                gap = (lo - px) / px * 100 if px < lo else (px - hi) / px * 100
                opis = (f"#{r['id']} {r['ticker']} {r['hit_or_miss']}: poziom {px} nigdy nie "
                        f"dotknięty (zakres {lo}–{hi}, luka {gap:.2f}%)")
                try:
                    wykluczona = bool(r["excluded"])
                except (KeyError, IndexError):
                    wykluczona = False
                if wykluczona:
                    known.append(opis)
                    print(f"  (znane, wykluczone) {opis}")
                else:
                    fail(opis)
        print("  gotowe\n")

    # ── Podsumowanie ─────────────────────────────────────────────────────
    print("─" * 60)
    if known:
        print(f"Znane i wykluczone (nie wpływają na wynik testu): {len(known)}.")
        for k in known:
            print(f"  · {k}")
        print("  Opis i przyczyna: REJESTR_WYKLUCZEN.md pkt 2b.")
        print("─" * 60)
    if failures:
        print(f"PRZECIEK PRZYSZŁOŚCI: {len(failures)} problemów.")
        print("Wyniki konta modelowego są niewiarygodne, dopóki to nie zostanie naprawione.")
    else:
        print("Brak przecieku przyszłości w sprawdzanych punktach.")
    if warnings:
        print(f"Ostrzeżeń do ręcznego przejrzenia: {len(warnings)}.")
    print("─" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
