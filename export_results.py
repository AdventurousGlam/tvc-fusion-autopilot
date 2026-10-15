#!/usr/bin/env python3
"""Eksport publicznych wyników paper tradingu do results.json (dla results.html).

Jedno źródło prawdy: REJESTR_WYKLUCZEN.md. Ten skrypt ma dawać DOKŁADNIE te
liczby, co rejestr, i nic innego.

════════════════════════════════════════════════════════════════════════════
DLACZEGO NAGŁÓWKIEM JEST PROCENT, A NIE SUMA W DOLARACH
════════════════════════════════════════════════════════════════════════════
Wielkość pozycji zmieniła się W TRAKCIE próby pięćdziesięciokrotnie:
sierpień średnio 21 $, wrzesień 1068 $ (max 4000 $), październik 633 $.
Suma dolarowa przez taki okres nie mierzy strategii — mierzy zmiany sizingu.
Jedna typowa transakcja z okresu po 22.09 to ~72 % całej sumy dolarowej,
więc ta suma potrafi zmienić znak po jednym zamknięciu. 15.10.2026 dokładnie
to się stało: o 10:28 zbiór dawał −0,94 $, o 10:45 zamknął się NEAR short na
TP2 (+56,74 $) i zbiór dał +55,80 $.

Dlatego `headline` to oczekiwana wartość w PROCENTACH na pozycję, liczba
pozycji i R:R. Suma dolarowa zostaje, ale z jawną adnotacją o zmianie sizingu
i z zakresem wielkości pozycji, żeby każdy mógł sam ocenić jej wagę.

DLACZEGO OD 28.08
  28.08 (commit b92888d0c) to data, od której wyjścia liczą się po świecach.
  Wcześniej ceny wejścia były oderwane od rynku. 22.09 to data STROJENIA
  (v1.0: szerszy rozrzut score, inne progi) — zmieniły się parametry, nie
  sposób pomiaru. Reset rekordu przy każdym strojeniu to sposób na wieczny
  czysty start, więc granica stoi na pomiarze, nie na progach.
  28.08 NIE jest wyborem pod ładniejsze liczby: daje gorszy win rate niż
  okno od 22.09.

WYKLUCZENIA bierzemy WYŁĄCZNIE z kolumny `excluded` w bazie. Jeśli rejestr
wyklucza klasę, której baza nie ma oznaczonej, skrypt się zatrzymuje — bo
wtedy strona i rejestr pokazałyby różne liczby.

BLOKADA PUBLIKACJI: do STATS_PUBLIC_FROM plik NIE jest zapisywany.
Wymuszenie: --force (wypisze ostrzeżenie).
"""
import argparse
import datetime
import json
import pathlib
import sqlite3
import statistics
import sys

STATS_SINCE = "2026-08-28T00:00:00"   # == rejestr pkt 2a; patrz docstring
STATS_PUBLIC_FROM = "2026-11-12"      # == rejestr pkt 6; trzymać zgodnie z content_engine.py

# Klasy wykluczone w rejestrze, które MUSZĄ być oznaczone w bazie kolumną
# `excluded`. Gdy któraś nie jest — przerywamy, zamiast opublikować inne liczby
# niż rejestr. Klucz = wartość hit_or_miss, wartość = punkt rejestru.
MUST_BE_FLAGGED = {"flip_choch": "pkt 2 — reguła wycofana 9.09 (ace0b98)"}

ap = argparse.ArgumentParser()
ap.add_argument("--force", action="store_true",
                help="zapisz results.json przed datą publikacji z rejestru")
args = ap.parse_args()

db = pathlib.Path(__file__).with_name("paper_trades.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}

if "excluded" not in cols:
    sys.exit("STOP: baza nie ma kolumny `excluded` — uruchom najpierw paper_bot.py init")

# ── Zgoda bazy z rejestrem ───────────────────────────────────────────────────
problems = []
for label, why in MUST_BE_FLAGGED.items():
    n = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE hit_or_miss = ? AND COALESCE(excluded,0) = 0",
        (label,)).fetchone()[0]
    if n:
        problems.append(f"  {label}: {n} pozycji bez flagi `excluded` ({why})")
if problems:
    print("STOP: baza nie zgadza się z rejestrem. Strona pokazałaby inne liczby.\n"
          + "\n".join(problems)
          + "\n\nNapraw: python3 paper_bot.py init  (migracja _migrate_flag_flip_choch)")
    sys.exit(1)

rows = [dict(r) for r in conn.execute(
    """SELECT * FROM positions
       WHERE status = 'closed' AND COALESCE(excluded,0) = 0 AND opened_at >= ?
       ORDER BY closed_at""", (STATS_SINCE,))]

keep = ["id", "ticker", "direction", "fusion_score", "regime", "entry_price",
        "exit_price", "size_usd", "opened_at", "closed_at", "pnl_pct", "pnl_usd",
        "hit_or_miss"]
trades = [{k: r.get(k) for k in keep} for r in rows]


def block(items):
    """Statystyka jednego zbioru. Procenty liczymy na pnl_pct (niezależne od
    wielkości pozycji), dolary na pnl_usd (zależne — dlatego osobno)."""
    n = len(items)
    if not n:
        return None
    pct = [t["pnl_pct"] or 0 for t in items]
    usd = [t["pnl_usd"] or 0 for t in items]
    w = [p for p in pct if p > 0]
    l = [p for p in pct if p <= 0]
    avg_w = round(sum(w) / len(w), 2) if w else None
    avg_l = round(sum(l) / len(l), 2) if l else None
    return {
        "trades": n,
        "wins": len(w),
        "losses": len(l),
        "win_rate": round(100 * len(w) / n, 1),
        # NAGŁÓWEK: średni wynik na pozycję w procentach
        "ev_pct": round(sum(pct) / n, 3),
        "median_pct": round(statistics.median(pct), 3),
        "avg_win_pct": avg_w,
        "avg_loss_pct": avg_l,
        "rr": round(abs(avg_w / avg_l), 2) if (avg_w and avg_l) else None,
        "best_pct": round(max(pct), 2),
        "worst_pct": round(min(pct), 2),
        # dolary — pomocniczo, patrz sizing_note
        "pnl_usd": round(sum(usd), 2),
        "avg_pnl_usd": round(sum(usd) / n, 2),
        "biggest_abs_usd": round(max(abs(u) for u in usd), 2),
    }


overall = block(trades)
by_dir = {d: block([t for t in trades if (t["direction"] or "") == d])
          for d in ("long", "short")}

sizes = [t["size_usd"] or 0 for t in trades if t["size_usd"]]
sizing = {
    "min_usd": round(min(sizes), 0) if sizes else None,
    "max_usd": round(max(sizes), 0) if sizes else None,
    "avg_usd": round(sum(sizes) / len(sizes), 0) if sizes else None,
}
# Jak dużo całej sumy dolarowej waży jedna typowa transakcja. Liczymy na
# WARTOŚCIACH BEZWZGLĘDNYCH: średnia ze znakiem jest mała, bo wygrane i straty
# się znoszą, i dawałaby fałszywie spokojny obraz (pierwsza wersja tego skryptu
# pokazywała tu 2 %, podczas gdy typowy ruch to ~43 % sumy).
if overall:
    avg_abs = round(sum(abs(t["pnl_usd"] or 0) for t in trades) / len(trades), 2)
    sizing["avg_abs_usd"] = avg_abs
    total = abs(overall["pnl_usd"])
    if total >= 0.01:
        sizing["typical_trade_share_of_total_pct"] = round(100 * avg_abs / total, 0)
        sizing["total_smaller_than_typical_trade"] = avg_abs > total

eq, curve = 0.0, []
for t in trades:
    eq += t["pnl_usd"] or 0
    curve.append({"t": t["closed_at"], "pnl": round(eq, 2)})

open_n = conn.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'").fetchone()[0]
excl_n = conn.execute("SELECT COUNT(*) FROM positions WHERE COALESCE(excluded,0) = 1").fetchone()[0]

now = datetime.datetime.now(datetime.timezone.utc)
out = {
    "updated": now.isoformat(timespec="seconds"),
    "since": STATS_SINCE[:10],
    # ── klucze, które czyta results.html (nie zmieniać nazw) ──
    "summary": {
        "trades": overall["trades"] if overall else 0,
        "wins": overall["wins"] if overall else 0,
        "win_rate": overall["win_rate"] if overall else None,
        "pnl_usd": overall["pnl_usd"] if overall else 0,
        "avg_pct": overall["ev_pct"] if overall else None,
        "open": open_n,
        "excluded": excl_n,
    },
    # ── nagłówek, na który patrzymy my i który jest uczciwy ──
    "headline": {
        "ev_pct": overall["ev_pct"] if overall else None,
        "trades": overall["trades"] if overall else 0,
        "rr": overall["rr"] if overall else None,
        "win_rate": overall["win_rate"] if overall else None,
        "as_of": now.strftime("%Y-%m-%d %H:%M UTC"),
    },
    "overall": overall,
    "by_direction": by_dir,
    "sizing": sizing,
    "notes": {
        "costs": "PnL netto: 0,05 % opłata + 0,02 % poślizg na stronę, 0,14 % na obrót",
        "sizing": ("Wielkość pozycji zmieniała się w trakcie okresu "
                   f"(od {sizing['min_usd']:.0f} $ do {sizing['max_usd']:.0f} $). "
                   "Suma dolarowa odzwierciedla te zmiany, nie tylko jakość decyzji — "
                   "dlatego nagłówkiem jest wynik w procentach na pozycję."),
        "sample": ("Wynik w dolarach potrafi zmienić znak po jednym zamknięciu: typowa "
                   f"transakcja to {sizing.get('avg_abs_usd', '?')} $ w jedną lub drugą stronę, "
                   f"czyli {sizing.get('typical_trade_share_of_total_pct', '?')} % całej sumy"
                   + (" — suma jest MNIEJSZA niż typowa transakcja."
                      if sizing.get("total_smaller_than_typical_trade") else ".")
                   + " Przy tej liczbie pozycji żadna pojedyncza zmiana reguł nie jest "
                     "rozstrzygalna statystycznie."),
        "excluded": (f"{excl_n} pozycji wykluczonych z przyczynami opisanymi w rejestrze "
                     "(zbugowane reguły, era przed 28.08, wyjścia spoza zakresu). "
                     "Pozostają w bazie i w audycie."),
    },
    "equity": curve,
    "trades": trades[::-1],
}

today = now.strftime("%Y-%m-%d")
head = (f"{out['headline']['trades']} pozycji, EV {out['headline']['ev_pct']:+.3f}%/poz, "
        f"WR {out['headline']['win_rate']}%, R:R {out['headline']['rr']}, "
        f"suma {out['summary']['pnl_usd']:+.2f} $ (wykluczonych {excl_n})")

if today < STATS_PUBLIC_FROM and not args.force:
    print(f"NIE ZAPISANO — publikacja wyników od {STATS_PUBLIC_FROM} (rejestr pkt 6). "
          f"Dziś {today}.\nWyliczone: {head}\nWymuszenie: --force")
    sys.exit(0)

if today < STATS_PUBLIC_FROM:
    print(f"⚠️  --force: zapisuję {today}, czyli przed terminem publikacji "
          f"{STATS_PUBLIC_FROM} z rejestru pkt 6.")

db.with_name("results.json").write_text(json.dumps(out, indent=1, default=str))
print(f"results.json: {head}")
for d, b in by_dir.items():
    if b:
        print(f"  {d:<6} n={b['trades']:<3} WR={b['win_rate']:<5}% EV={b['ev_pct']:+.3f}%/poz "
              f"R:R={b['rr']}  suma={b['pnl_usd']:+.2f} $")
