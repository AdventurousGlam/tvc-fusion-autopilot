#!/usr/bin/env python3
"""v4.4.2 — R od razu przy max_hold / short_killswitch / manual_close,
migracja r_multiple_reconstructed (idempotentna, tylko puste komórki,
tylko zgodny entry_price) i recap tygodnia bez zamknięć (bez wysyłki).

Nie dotyka paper_trades.db w repo: pracuje na kopii albo na bazie tymczasowej.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paper_bot
import weekly_recap

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "r_multiple_reconstructed.csv"
FAILS: list[str] = []


def fail(msg: str):
    FAILS.append(msg)
    print(f"  ✗ {msg}")


def ok(msg: str):
    print(f"  ✓ {msg}")


def _insert(conn, **kw):
    cols = ", ".join(kw)
    qs = ", ".join("?" * len(kw))
    conn.execute(f"INSERT INTO positions ({cols}) VALUES ({qs})", tuple(kw.values()))
    conn.commit()


def _one(db_path, ticker):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM positions WHERE ticker = ?", (ticker,)).fetchone()
    conn.close()
    return row


def _expected_r(row):
    risk = abs(row["entry_price"] - row["sl_initial"]) / row["entry_price"] * 100.0
    return round(row["pnl_pct"] / risk, 3)


def _with_temp_db():
    td = tempfile.TemporaryDirectory()
    db_path = Path(td.name) / "paper_trades.db"
    old = paper_bot.DB_PATH
    paper_bot.DB_PATH = db_path
    paper_bot.db_init()
    return td, db_path, old


def _restore_db(td, old):
    paper_bot.DB_PATH = old
    td.cleanup()


def test_rules_version_unchanged():
    print("\n[rules] RULES_VERSION")
    if paper_bot.RULES_VERSION != "v4.4":
        fail(f"RULES_VERSION = {paper_bot.RULES_VERSION!r}, oczekiwano 'v4.4'")
    else:
        ok("RULES_VERSION zostaje v4.4")


def test_r_stamped_on_close_paths():
    print("\n[R] max_hold, short_killswitch, manual_close")
    td, db_path, old_db = _with_temp_db()
    old_price = paper_bot._fetch_current_price
    old_notify = paper_bot._notify_close
    old_digest = paper_bot._maybe_daily_digest
    old_upload = paper_bot.cmd_upload
    notify_r = {}

    def price(_ticker):
        return 110.0

    def notify(*_a, **_k):
        row = _one(db_path, _a[0] if _a else "")
        if row is not None:
            notify_r[row["ticker"]] = row["r_multiple"]

    def digest(_conn):
        return None

    try:
        paper_bot._fetch_current_price = price
        paper_bot._notify_close = notify
        paper_bot._maybe_daily_digest = digest

        # Jedna pozycja starsza niż MAX_HOLD — cmd_check wraca wcześniej
        # ("all positions closed"), więc końcowy _stamp_r_multiple się nie wykona.
        opened = (datetime.now(timezone.utc) - timedelta(days=paper_bot.MAX_HOLD_DAYS + 3)).isoformat()
        conn = paper_bot.db()
        _insert(conn, date="2026-10-01", ticker="SOL", exchange="binance", action="BUY",
                direction="long", regime="RANGING", entry_price=100.0, size_usd=1000.0,
                sl_price=95.0, sl_initial=95.0, status="open", opened_at=opened,
                r_multiple=None)
        # Już policzone R nie może się zmienić przy stemplu cudzego zamknięcia.
        _insert(conn, date="2026-09-01", ticker="ETH", exchange="binance", action="BUY",
                direction="long", entry_price=200.0, size_usd=500.0, sl_initial=190.0,
                status="closed", opened_at="2026-09-01T00:00:00+00:00",
                closed_at="2026-09-02T00:00:00+00:00", pnl_pct=1.0, pnl_usd=5.0,
                r_multiple=3.333, hit_or_miss="hit_tp2")
        conn.close()

        paper_bot.cmd_check(argparse.Namespace())
        row = _one(db_path, "SOL")
        if row["status"] != "closed" or row["hit_or_miss"] != "max_hold":
            fail(f"max_hold nie zamknął SOL: status={row['status']} hit={row['hit_or_miss']}")
        elif row["r_multiple"] is None:
            fail("max_hold: r_multiple NULL mimo wczesnego return (stempel dopiero w następnym przebiegu)")
        elif abs(row["r_multiple"] - _expected_r(row)) > 0.001:
            fail(f"max_hold: r_multiple {row['r_multiple']} != {_expected_r(row)}")
        elif notify_r.get("SOL") is None:
            fail("max_hold: R nie było zapisane w momencie powiadomienia")
        else:
            ok(f"max_hold stempel od razu, R={row['r_multiple']}")
        eth = _one(db_path, "ETH")
        if eth["r_multiple"] != 3.333:
            fail(f"istniejące r_multiple zmienione: {eth['r_multiple']}")
        else:
            ok("istniejące r_multiple nietknięte")

        # short w zablokowanym reżimie, młodsza niż max_hold — też wczesny return.
        blocked = [k for k, v in paper_bot.MAX_SHORT_SCORE_BY_REGIME.items() if v <= 0]
        if not blocked:
            fail("brak zablokowanego reżimu short — test killswitcha nie ma czego sprawdzić")
        else:
            young = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
            conn = paper_bot.db()
            _insert(conn, date="2026-11-01", ticker="XRP", exchange="binance", action="SELL",
                    direction="short", regime=blocked[0], entry_price=100.0, size_usd=800.0,
                    sl_price=105.0, sl_initial=105.0, status="open", opened_at=young)
            conn.close()
            paper_bot.cmd_check(argparse.Namespace())
            row = _one(db_path, "XRP")
            if row["hit_or_miss"] != "short_killswitch" or row["r_multiple"] is None:
                fail(f"short_killswitch: hit={row['hit_or_miss']} r={row['r_multiple']}")
            elif abs(row["r_multiple"] - _expected_r(row)) > 0.001:
                fail(f"short_killswitch: r_multiple {row['r_multiple']} != {_expected_r(row)}")
            elif notify_r.get("XRP") is None:
                fail("short_killswitch: R nie było zapisane w momencie powiadomienia")
            else:
                ok(f"short_killswitch ({blocked[0]}) stempel od razu, R={row['r_multiple']}")

        # manual_close: R musi być w bazie ZANIM cmd_upload pójdzie na Gist.
        uploaded = {}

        def fake_upload(_args):
            seen = _one(db_path, "BTC")
            uploaded["r"] = None if seen is None else seen["r_multiple"]
            uploaded["hit"] = None if seen is None else seen["hit_or_miss"]

        paper_bot.cmd_upload = fake_upload
        conn = paper_bot.db()
        _insert(conn, date="2026-11-02", ticker="BTC", exchange="binance", action="BUY",
                direction="long", regime="RANGING", entry_price=100.0, size_usd=1000.0,
                sl_price=90.0, sl_initial=90.0, status="open",
                opened_at=datetime.now(timezone.utc).isoformat())
        conn.close()
        paper_bot.cmd_close(argparse.Namespace(ticker="BTC"))
        row = _one(db_path, "BTC")
        if uploaded.get("hit") != "manual_close":
            fail(f"manual_close nie doszedł do uploadu jako manual_close: {uploaded}")
        elif uploaded.get("r") is None:
            fail("manual_close: Gist upload wystartował z r_multiple NULL")
        elif abs(row["r_multiple"] - _expected_r(row)) > 0.001:
            fail(f"manual_close: r_multiple {row['r_multiple']} != {_expected_r(row)}")
        else:
            ok(f"manual_close: R={row['r_multiple']} zapisane przed uploadem Gista")
    finally:
        paper_bot._fetch_current_price = old_price
        paper_bot._notify_close = old_notify
        paper_bot._maybe_daily_digest = old_digest
        paper_bot.cmd_upload = old_upload
        _restore_db(td, old_db)


def _read_csv(path: Path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(line for line in fh if not line.startswith("#")))


def test_migration_idempotent():
    print("\n[migrate] idempotencja i granice zapisu")
    td = tempfile.TemporaryDirectory()
    db_path = Path(td.name) / "t.db"
    try:
        conn = sqlite3.connect(db_path)
        conn.executescript(paper_bot.SCHEMA)
        paper_bot._migrate_add_column(conn, "positions", "r_multiple", "REAL")
        paper_bot._migrate_add_column(conn, "positions", "r_multiple_reconstructed", "REAL")
        # id 1: zamknięta, entry zgodny, r_multiple już ustawione — wolno ruszyć tylko nową kolumnę
        # id 2: entry się nie zgadza
        # id 3: reconstructed już wypełnione
        # id 4: otwarta, entry zgodny
        # id 5: nie ma jej w CSV
        rows = [
            (1, "closed", 100.0, 1.5, None, 12.0),
            (2, "closed", 999.0, -0.4, None, -3.0),
            (3, "closed", 50.0, 0.2, 0.123, 4.0),
            (4, "open", 100.0, None, None, None),
            (5, "closed", 10.0, 2.0, None, 8.0),
        ]
        for pid, status, entry, rm, recon, pnl in rows:
            conn.execute(
                "INSERT INTO positions (id, date, ticker, exchange, action, opened_at, status, "
                "entry_price, r_multiple, r_multiple_reconstructed, pnl_usd, pnl_pct) "
                "VALUES (?, '2026-08-01', 'SOL', 'binance', 'BUY', '2026-08-01T00:00:00+00:00', "
                "?, ?, ?, ?, ?, ?)",
                (pid, status, entry, rm, recon, pnl, pnl),
            )
        conn.commit()
        snap = conn.execute(
            "SELECT id, status, entry_price, r_multiple, pnl_usd, pnl_pct, r_multiple_reconstructed "
            "FROM positions ORDER BY id"
        ).fetchall()

        csv_path = Path(td.name) / "r.csv"
        csv_path.write_text(
            "id,ticker,entry_price,sl0,r_multiple_reconstructed,sl0_source\n"
            "1,SOL,100.0,95.0,-1.234,git:test\n"
            "2,SOL,100.0,95.0,9.999,git:test\n"
            "3,SOL,50.0,48.0,7.777,git:test\n"
            "4,SOL,100.0,95.0,3.333,git:test\n"
            "bogus,SOL,1,1,1,x\n"
            "6,SOL,not-a-number,1,1,x\n",
            encoding="utf-8",
        )
        n1 = paper_bot._migrate_r_multiple_reconstructed(conn, csv_path)
        after = conn.execute(
            "SELECT id, status, entry_price, r_multiple, pnl_usd, pnl_pct, r_multiple_reconstructed "
            "FROM positions ORDER BY id"
        ).fetchall()
        if n1 != 1:
            fail(f"pierwszy przebieg zapisał {n1} wierszy, oczekiwano 1")
        else:
            ok("pierwszy przebieg: 1 wiersz")
        by_id = {r[0]: r for r in after}
        if by_id[1][6] != -1.234:
            fail(f"id 1 reconstructed = {by_id[1][6]}, oczekiwano -1.234")
        else:
            ok("id 1: pusta komórka uzupełniona")
        if by_id[1][3] != 1.5 or by_id[1][4] != 12.0:
            fail(f"id 1: ruszono r_multiple/pnl ({by_id[1][3]}, {by_id[1][4]})")
        else:
            ok("id 1: r_multiple i pnl bez zmian")
        if by_id[2][6] is not None:
            fail("id 2: zapis mimo innego entry_price")
        else:
            ok("id 2: entry się nie zgadza — pominięte")
        if by_id[3][6] != 0.123:
            fail(f"id 3: nadpisano istniejące reconstructed ({by_id[3][6]})")
        else:
            ok("id 3: istniejąca wartość nietknięta")
        if by_id[4][6] is not None or by_id[4][1] != "open":
            fail("id 4: ruszono otwartą pozycję")
        else:
            ok("id 4: otwarta pozycja pominięta")
        if by_id[5][6] is not None:
            fail("id 5: zapis pozycji spoza CSV")
        else:
            ok("id 5: brak w CSV — pominięte")
        # nic poza reconstructed
        for before, now in zip(snap, after):
            if before[:6] != now[:6]:
                fail(f"zmieniono inne pola id {before[0]}: {before[:6]} -> {now[:6]}")
        n2 = paper_bot._migrate_r_multiple_reconstructed(conn, csv_path)
        again = conn.execute(
            "SELECT r_multiple_reconstructed FROM positions WHERE id = 1"
        ).fetchone()[0]
        if n2 != 0 or again != -1.234:
            fail(f"drugi przebieg nie jest idempotentny: n={n2} value={again}")
        else:
            ok("drugi przebieg: 0 wierszy, wartość bez zmian")
        missing = Path(td.name) / "nope.csv"
        if paper_bot._migrate_r_multiple_reconstructed(conn, missing) != 0:
            fail("brak pliku CSV powinien zwrócić 0")
        else:
            ok("brak pliku CSV: 0")
        conn.close()
    finally:
        td.cleanup()


def test_bundled_csv_fills_40_on_db_copy():
    print("\n[migrate] kopia paper_trades.db + CSV z repo")
    src = ROOT / "paper_trades.db"
    if not src.exists():
        fail("brak paper_trades.db — nie da się sprawdzić 40 wierszy")
        return
    rows = _read_csv(CSV_PATH)
    if len(rows) != 40:
        fail(f"CSV ma {len(rows)} wierszy danych, oczekiwano 40")
    else:
        ok("CSV: 40 wierszy")
    td = tempfile.TemporaryDirectory()
    try:
        dst = Path(td.name) / "paper_trades.db"
        shutil.copy(src, dst)
        conn = sqlite3.connect(dst)
        conn.row_factory = sqlite3.Row
        paper_bot._migrate_add_column(conn, "positions", "r_multiple_reconstructed", "REAL")
        before = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()}
        n1 = paper_bot._migrate_r_multiple_reconstructed(conn, CSV_PATH)
        after = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()}
        if n1 != 40:
            reasons = []
            for rec in rows:
                pid = int(rec["id"])
                row = after.get(pid)
                if row is None:
                    reasons.append(f"#{pid} brak w bazie")
                    continue
                entry = float(rec["entry_price"])
                db_entry = row["entry_price"]
                tol = 1e-9 * max(1.0, abs(entry))
                filled = row["r_multiple_reconstructed"]
                if filled is None:
                    reasons.append(
                        f"#{pid} {row['ticker']} status={row['status']} "
                        f"entry db={db_entry} csv={entry} |d|={None if db_entry is None else abs(db_entry - entry)} tol={tol}"
                    )
            fail(f"na kopii bazy zapisano {n1}, oczekiwano 40. " + "; ".join(reasons[:12]))
        else:
            ok("zapisano r_multiple_reconstructed dla 40 pozycji")
        dirty = []
        for pid, prev in before.items():
            now = after[pid]
            for k, v in prev.items():
                if k == "r_multiple_reconstructed":
                    continue
                if now.get(k) != v:
                    dirty.append(f"#{pid}.{k}: {v!r} -> {now.get(k)!r}")
        if dirty:
            fail("migracja ruszyła inne pola: " + "; ".join(dirty[:8]))
        else:
            ok("r_multiple i pozostałe pola bez zmian")
        # tylko wiersze z CSV i tylko gdy entry się zgadza
        csv_ids = {int(r["id"]) for r in rows}
        extra = [pid for pid, row in after.items()
                 if row["r_multiple_reconstructed"] is not None and pid not in csv_ids]
        if extra:
            fail(f"reconstructed poza CSV: {extra[:8]}")
        else:
            ok("poza CSV nic nie wypełnione")
        n2 = paper_bot._migrate_r_multiple_reconstructed(conn, CSV_PATH)
        after2 = {r["id"]: r["r_multiple_reconstructed"]
                  for r in conn.execute("SELECT id, r_multiple_reconstructed FROM positions")}
        same = all(after[i]["r_multiple_reconstructed"] == after2[i] for i in after)
        if n2 != 0 or not same:
            fail(f"drugi przebieg na kopii bazy: n={n2} bez zmian={same}")
        else:
            ok("drugi przebieg na kopii bazy: 0 wierszy")
        conn.close()
    finally:
        td.cleanup()
    # guard: źródłowa baza nie dostała kolumny
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    cols = [r[1] for r in src_conn.execute("PRAGMA table_info(positions)")]
    src_conn.close()
    if "r_multiple_reconstructed" in cols:
        fail("test zapisał kolumnę w paper_trades.db w repozytorium")
    else:
        ok("paper_trades.db w repozytorium bez zmian")


def _recap_db(path: Path, rows: list[dict]):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE positions (
            id INTEGER PRIMARY KEY,
            ticker TEXT, opened_at TEXT, closed_at TEXT, status TEXT,
            direction TEXT, entry_price REAL, exit_price REAL, sl_price REAL,
            pnl_usd REAL, pnl_pct REAL, excluded INTEGER, hit_or_miss TEXT,
            fusion_score INTEGER, size_usd REAL
        )"""
    )
    for r in rows:
        cols = ", ".join(r)
        qs = ", ".join("?" * len(r))
        conn.execute(f"INSERT INTO positions ({cols}) VALUES ({qs})", tuple(r.values()))
    conn.commit()
    conn.close()


_DIR_WORDS = re.compile(r"\b(long|short|buy|sell|bullish|bearish)\b", re.I)


def _scan_public(recap: dict, forbidden_numbers: list[str]):
    blob = json.dumps(recap, ensure_ascii=False)
    hit = _DIR_WORDS.search(blob)
    if hit:
        fail(f"recap zawiera słowo kierunku: {hit.group(0)!r}")
        return False
    for num in forbidden_numbers:
        if num in blob:
            fail(f"recap zawiera cenę/poziom {num}")
            return False
    return True


def test_empty_week_recap():
    print("\n[recap] tydzień bez zamknięć")
    td = tempfile.TemporaryDirectory()
    db_path = Path(td.name) / "paper_trades.db"
    recap_path = Path(td.name) / "weekly_recap.json"
    old_db, old_recap = weekly_recap.DB_PATH, weekly_recap.RECAP_PATH
    sent = []
    old_send = weekly_recap._tg_send

    def no_send(*_a, **_k):
        sent.append(True)
        raise AssertionError("empty week must not send")

    weekly_recap.DB_PATH = db_path
    weekly_recap.RECAP_PATH = recap_path
    weekly_recap._tg_send = no_send
    try:
        _recap_db(db_path, [
            {
                "ticker": "SOL",
                "opened_at": "2026-10-20T15:04:00+00:00",
                "status": "open",
                "direction": "short",
                "entry_price": 187.6543,
                "sl_price": 180.1111,
                "size_usd": 4321.5,
            },
            {
                "ticker": "ETH",
                "opened_at": "2026-08-01T00:00:00+00:00",
                "closed_at": "2026-08-02T00:00:00+00:00",
                "status": "closed",
                "direction": "long",
                "entry_price": 2500.25,
                "exit_price": 2600.5,
                "pnl_usd": 40.0,
                "pnl_pct": 4.0,
                "excluded": 0,
                "hit_or_miss": "hit_tp2",
            },
        ])
        weekly_recap.run()
        if sent:
            fail("pusty tydzień wywołał Telegram")
        elif not recap_path.exists():
            fail("brak weekly_recap.json przy tygodniu bez zamknięć")
        else:
            recap = json.loads(recap_path.read_text(encoding="utf-8"))
            sm = recap.get("summary") or {}
            opens = sm.get("open_positions")
            if sm.get("note_pl") != "brak zamkniętych transakcji w tym tygodniu":
                fail(f"note_pl = {sm.get('note_pl')!r}")
            elif sm.get("note_en") != "no closed trades this week":
                fail(f"note_en = {sm.get('note_en')!r}")
            elif recap.get("no_closed_trades") is not True or sm.get("trades_closed") != 0:
                fail("flaga pustego tygodnia nieustawiona")
            elif opens != [{"ticker": "SOL", "opened": "2026-10-20"}]:
                fail(f"otwarte pozycje: {opens}")
            elif recap.get("linkedin_newsletter") is not None or recap.get("youtube_script") is not None:
                fail("pusty tydzień wygenerował newsletter albo skrypt")
            elif not _scan_public(recap, ["187.6543", "180.1111", "4321.5", "2500.25", "2600.5"]):
                pass
            else:
                ok("json z notą PL/EN, ticker+data, bez kierunku, cen i wysyłki")

        # tydzień z zamknięciem dalej idzie starą ścieżką (i woła wysyłkę — tu podstawioną)
        sent.clear()
        calls = []

        def record(text):
            calls.append(text)

        weekly_recap._tg_send = record
        conn = sqlite3.connect(db_path)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO positions (ticker, opened_at, closed_at, status, direction, pnl_usd, pnl_pct, "
            "excluded, hit_or_miss, fusion_score, entry_price) VALUES "
            "(?, ?, ?, 'closed', 'long', 10.0, 1.5, 0, 'hit_tp1', 70, 111.222)",
            ("BTC", now, now),
        )
        conn.commit()
        conn.close()
        recap_path.unlink()
        weekly_recap.run()
        if not calls:
            fail("tydzień z zamknięciem nie woła wysyłki (ta ścieżka ma zostać)")
        elif not recap_path.exists():
            fail("tydzień z zamknięciem nie zapisał recapa")
        else:
            recap = json.loads(recap_path.read_text(encoding="utf-8"))
            if recap.get("no_closed_trades"):
                fail("tydzień z zamknięciem dostał recap pustego tygodnia")
            else:
                ok("tydzień z zamknięciem: zwykły recap, wysyłka wywołana")
    finally:
        weekly_recap.DB_PATH = old_db
        weekly_recap.RECAP_PATH = old_recap
        weekly_recap._tg_send = old_send
        td.cleanup()


def main():
    test_rules_version_unchanged()
    test_r_stamped_on_close_paths()
    test_migration_idempotent()
    test_bundled_csv_fills_40_on_db_copy()
    test_empty_week_recap()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}")
        for m in FAILS:
            print(f"  - {m}")
        raise SystemExit(1)
    print("OK test_v442")


if __name__ == "__main__":
    main()
