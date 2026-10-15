#!/usr/bin/env python3
"""Eksport publicznych wyników paper tradingu do results.json (dla results.html).

Jedno źródło prawdy: REJESTR_WYKLUCZEN.md. Ten skrypt ma dawać DOKŁADNIE te
liczby, co rejestr — 46 pozycji, 13/33, 28%, −0,94 $ — i nic innego.

Dlaczego od 28.08, a nie od 22.09:
  28.08 (commit b92888d0c) to data, od której wyjścia liczą się po świecach.
  Wcześniej ceny wejścia były oderwane od rynku, więc liczby nie znaczyły nic.
  22.09 to data STROJENIA (v1.0: szerszy rozrzut score, inne progi) — zmieniły
  się parametry, nie sposób pomiaru. Reset rekordu przy każdym strojeniu to
  sposób na wieczny czysty start, więc granica stoi na pomiarze.
  Uwaga: 28.08 NIE jest wyborem pod ładniejsze liczby. Daje gorszy win rate
  (28% vs 30%) i lepszy PnL (−0,94 $ vs −23,54 $) niż okno od 22.09.

Wykluczenia bierzemy WYŁĄCZNIE z kolumny `excluded` w bazie. Jeśli rejestr
wyklucza klasę, której baza nie ma oznaczonej, skrypt się zatrzymuje — bo
wtedy strona i rejestr pokazałyby różne liczby. 15.10.2026 dokładnie to się
stało z klasą `flip_choch` (32 pozycje opisane w .md, `excluded` = 0 w bazie).

Blokada publikacji: do STATS_PUBLIC_FROM plik NIE jest zapisywany. Data musi
być równa dacie z rejestru pkt 6. Wymuszenie: --force (wypisze ostrzeżenie).
"""
import argparse
import datetime
import json
import pathlib
import sqlite3
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
        "exit_price", "opened_at", "closed_at", "pnl_pct", "pnl_usd", "hit_or_miss"]
trades = [{k: r.get(k) for k in keep} for r in rows]

pnl = [t["pnl_usd"] or 0 for t in trades]
wins = sum(1 for p in pnl if p > 0)
eq, curve = 0.0, []
for t in trades:
    eq += t["pnl_usd"] or 0
    curve.append({"t": t["closed_at"], "pnl": round(eq, 2)})

open_n = conn.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'").fetchone()[0]
excl_n = conn.execute("SELECT COUNT(*) FROM positions WHERE COALESCE(excluded,0) = 1").fetchone()[0]

out = {
    "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "since": STATS_SINCE[:10],
    "summary": {
        "trades": len(trades),
        "wins": wins,
        "win_rate": round(100 * wins / len(trades), 1) if trades else None,
        "pnl_usd": round(sum(pnl), 2),
        "avg_pct": round(sum((t["pnl_pct"] or 0) for t in trades) / len(trades), 2) if trades else None,
        "open": open_n,
        # jawnie, żeby każdy widział, że zbiór jest filtrowany i ile wypadło
        "excluded": excl_n,
        "costs_note": "PnL netto: 0,05% opłata + 0,02% poślizg na stronę, 0,14% na obrót",
    },
    "equity": curve,
    "trades": trades[::-1],
}

today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
summary = (f"{len(trades)} pozycji, {wins}/{len(trades) - wins}, "
           f"win {out['summary']['win_rate']}%, PnL {out['summary']['pnl_usd']} $ "
           f"(wykluczonych {excl_n})")

if today < STATS_PUBLIC_FROM and not args.force:
    print(f"NIE ZAPISANO — publikacja wyników od {STATS_PUBLIC_FROM} (rejestr pkt 6). "
          f"Dziś {today}.\nWyliczone: {summary}\nWymuszenie: --force")
    sys.exit(0)

if today < STATS_PUBLIC_FROM:
    print(f"⚠️  --force: zapisuję {today}, czyli przed terminem publikacji "
          f"{STATS_PUBLIC_FROM} z rejestru pkt 6.")

db.with_name("results.json").write_text(json.dumps(out, indent=1, default=str))
print(f"results.json: {summary}")
