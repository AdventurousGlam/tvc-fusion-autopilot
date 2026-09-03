#!/usr/bin/env python3
"""
TVC Fusion Paper Trading Bot v0.1.1

Automates the entire paper trading loop:
  - `open`   Reads today's fusion-decisions .md file, extracts JSON, opens
             paper positions in SQLite for every BUY decision.
  - `check`  For every open position, fetches OHLC from Bybit (public API)
             since opened_at and closes if SL/TP was touched.
  - `eod`    Runs `check`, then prints an EOD summary (opened today, closed
             today, currently open, cumulative PnL).
  - `week`   Dumps last 7 days of closed trades as JSON for weekly-review
             scheduled task to consume.
  - `init`   Creates the SQLite schema if not present.

Zero broker API keys required — reads public market data only. No real
orders. This is a simulation layer that treats every BUY from fusion as a
virtual position sized against a configurable paper capital base.

Requirements:
  pip install ccxt

Config (edit at top or override via env vars):
  TVC_FUSION_DIR       — where fusion .md files are saved
                         default: ~/Claude/Daily notes/
  TVC_DB_PATH          — SQLite database file location
                         default: ~/Claude/TVCFusion/paper_trades.db
  TVC_PAPER_CAPITAL    — virtual starting capital in USD (base for size %)
                         default: 10000
  TVC_EXCHANGE         — ccxt exchange id for price data
                         default: bybit
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- config ------------------------------------------------------------

HOME = Path.home()
DAILY_NOTES_DIR = Path(
    os.environ.get("TVC_FUSION_DIR", HOME / "Claude" / "Projects" / "Daily notes")
)
DB_PATH = Path(
    os.environ.get("TVC_DB_PATH", HOME / "Claude" / "TVCFusion" / "paper_trades.db")
)
PAPER_CAPITAL = float(os.environ.get("TVC_PAPER_CAPITAL", "10000"))
EXCHANGE_ID = os.environ.get("TVC_EXCHANGE", "bybit")

# v0.2 — asymmetric sizing caps
LONG_SIZE_CAP_PCT = 3.0     # max % capital per LONG trade
SHORT_SIZE_CAP_PCT = 0.5    # max % capital per SHORT trade (unlimited upside risk)
# v0.2 — SHORT regime constraint: only allow SHORTs in these regimes
SHORT_ALLOWED_REGIMES = {"TRENDING_DOWN", "TRENDING_DOWN_VOLATILE", "CRASH"}

# v0.3 — QUALITY GATES. Wyprowadzone z audytu 50 zamkniętych paper trade'ów
# (paper_trades.db, 2026-09-02):
#   fusion score >= 70 → 7 trade'ów, 86% WR, +$39.26
#   fusion score 60-69 → 10 trade'ów, 10% WR, -$2.19
#   fusion score  < 60 → 33 trade'ów,  3% WR, -$7.60
#   regime RANGING     → 39 trade'ów,  5% WR  |  TRENDING_UP → 4 trade'y, 100% WR
#   exit = flip_choch  → 31 trade'ów,  3% WR (ping-pong long↔short co kilka minut)
#   SHORT              → 20 trade'ów,  0 wygranych
# Wniosek: system działa świetnie przy wysokiej konwikcji, a traci wyłącznie na
# niskiej jakości wejściach. Gates poniżej odcinają szum, nie zmieniają sygnału.
MIN_LONG_SCORE = 65            # minimalny score dla LONG w regime trendowym
MIN_LONG_SCORE_RANGING = 70    # w RANGING wymagamy jeszcze wyższej konwikcji
MAX_SHORT_SCORE = 40           # SHORT tylko gdy score jest faktycznie bearish
MIN_HOLD_MINUTES = 240         # nie flipuj pozycji młodszej niż 4h (anty ping-pong)
REOPEN_COOLDOWN_MINUTES = 120  # po zamknięciu tickera 2h przerwy przed nowym wejściem
MAX_NEW_TRADES_PER_DAY = 3     # bezpiecznik anty-overtrading (Sep 1: 14 trade'ów/dzień)

# --- ccxt lazy import (bot still runs `init` without it) ---------------

def get_exchange():
    try:
        import ccxt
    except ImportError:
        sys.exit(
            "ERROR: ccxt not installed. Run:\n"
            "  python3 -m pip install --user ccxt\n"
        )
    ex_cls = getattr(ccxt, EXCHANGE_ID)
    return ex_cls({"enableRateLimit": True})


# --- schema ------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    date              TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    exchange          TEXT NOT NULL,
    action            TEXT NOT NULL,
    direction         TEXT DEFAULT 'long',
    fusion_score      INTEGER,
    regime            TEXT,
    onchain_score     INTEGER,
    technical_score   INTEGER,
    news_score        INTEGER,
    momentum_score    INTEGER,
    sentiment_score   INTEGER,
    entry_price       REAL,
    size_pct          REAL,
    size_usd          REAL,
    sl_price          REAL,
    tp1_price         REAL,
    tp2_price         REAL,
    status            TEXT DEFAULT 'open',
    exit_price        REAL,
    exit_date         TEXT,
    pnl_pct           REAL,
    pnl_usd           REAL,
    hit_or_miss       TEXT,
    thesis            TEXT,
    invalidation      TEXT,
    risk_flag         TEXT,
    onchain_data_thin INTEGER DEFAULT 0,
    opened_at         TEXT NOT NULL,
    closed_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_date   ON positions(date);

-- P&L equity snapshots — tracked co check() run, dla equity curve chart
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    total_equity   REAL NOT NULL,
    open_positions INTEGER,
    unrealized_pnl REAL,
    realized_pnl   REAL,
    open_exposure  REAL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts);
"""


def _migrate_add_column(conn, table, column, coldef):
    """Add a column if it doesn't exist yet (idempotent SQLite migration)."""
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
        conn.commit()


def db_init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # v0.2 migrations (idempotent)
    _migrate_add_column(conn, "positions", "direction", "TEXT DEFAULT 'long'")
    _migrate_add_column(conn, "positions", "sentiment_score", "INTEGER")
    conn.commit()
    conn.close()
    print(f"[init] db ready at {DB_PATH}")


def _direction_from_action(action: str) -> str | None:
    """Map fusion action to trade direction; returns None for non-actionable."""
    a = (action or "").upper()
    if a in ("BUY", "STRONG_BUY", "SCALE_IN", "ADD", "LONG"):
        return "long"
    if a in ("SELL", "STRONG_SELL", "SHORT", "SCALE_OUT"):
        return "short"
    return None  # HOLD, WATCH, SKIP, REDUCE, CASH, EXIT — not opened


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# --- fusion JSON extraction --------------------------------------------

TVC_DIR = HOME / "Claude" / "TVCFusion"


def find_fusion_input(target_date: str | None = None) -> tuple[Path, str]:
    """
    Look for today's fusion input. Search order (interactive Variant B first, scheduled backup second):
      1. ~/Claude/TVCFusion/fusion_YYYY-MM-DD.json      (interactive Claude session)
      2. ~/Claude/Projects/Daily notes/YYYY-MM-DD-fusion-decisions.md  (scheduled task)
      3. Newest fusion_*.json in TVCFusion/
      4. Newest *fusion-decisions.md in Daily notes/

    Returns (path, format) where format is 'json' or 'md'.
    """
    d = target_date or datetime.now().strftime("%Y-%m-%d")
    ordered = [
        (TVC_DIR / f"fusion_{d}.json", "json"),
        (DAILY_NOTES_DIR / f"{d}-fusion-decisions.md", "md"),
    ]
    for path, fmt in ordered:
        if path.exists():
            return path, fmt
    # fallback: newest of either type
    json_matches = sorted(TVC_DIR.glob("fusion_*.json"))
    md_matches = sorted(DAILY_NOTES_DIR.glob("*fusion-decisions.md"))
    newest = None
    if json_matches and md_matches:
        newest = json_matches[-1] if json_matches[-1].stat().st_mtime > md_matches[-1].stat().st_mtime else md_matches[-1]
    elif json_matches:
        newest = json_matches[-1]
    elif md_matches:
        newest = md_matches[-1]
    if newest:
        fmt = "json" if newest.suffix == ".json" else "md"
        print(f"[warn] today's fusion file not found; using newest: {newest.name}")
        return newest, fmt
    sys.exit(
        f"ERROR: no fusion input found. Expected either:\n"
        f"  {TVC_DIR}/fusion_{d}.json  (interactive)\n"
        f"  {DAILY_NOTES_DIR}/{d}-fusion-decisions.md  (scheduled)"
    )


def extract_fusion_json(path: Path, fmt: str) -> dict:
    text = path.read_text(encoding="utf-8")
    if fmt == "json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            sys.exit(f"ERROR: {path.name} is malformed JSON: {e}")
    # markdown: extract JSON block between ```json ... ```
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not m:
        sys.exit(f"ERROR: no JSON block found in {path}")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: fusion JSON is malformed: {e}")


# --- open --------------------------------------------------------------

def _mid_entry(entry_low, entry_high, ticker, ex) -> float:
    """Use midpoint of entry_zone; fall back to current market price.
    Fallback uses Binance public REST (sprawdzone jako niezawodne z GH Actions)
    zamiast ccxt/Bybit, który bywał blokowany dla IP data-center."""
    if entry_low and entry_high:
        return (float(entry_low) + float(entry_high)) / 2
    try:
        return _fetch_current_price(ticker)
    except Exception as e:
        print(f"[warn] could not fetch {ticker} price: {e}")
        return 0.0


# --- Telegram notifications (v0.3) --------------------------------------
# Wymaga env: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (GitHub Secrets w workflow).
# Bez nich funkcja jest no-op — bot działa jak dotąd, tylko bez powiadomień.

def _telegram_send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    import urllib.request as ur
    import urllib.parse as up
    import ssl
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
    try:
        data = up.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": "true"}).encode()
        req = ur.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with ur.urlopen(req, timeout=10, context=ssl_ctx) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[telegram] send failed: {e}")
        return False


def _fmt_px(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.0f}" if v >= 100 else f"{v:.2f}" if v >= 1 else f"{v:.4f}"


def _notify_open(ticker, direction, entry, size_usd, sl, tp1, tp2, score, regime, override=False):
    arrow = "🟢 LONG" if direction == "long" else "🔴 SHORT"
    _telegram_send(
        f"<b>{arrow} {ticker}</b> otwarty @ {_fmt_px(entry)}\n"
        f"Size ${size_usd:.0f} · score {score} · {regime}{' · ⚡CHoCH' if override else ''}\n"
        f"SL {_fmt_px(sl)} · TP1 {_fmt_px(tp1)} · TP2 {_fmt_px(tp2)}"
    )


def _notify_close(ticker, direction, entry, exit_price, pnl_pct, pnl_usd, reason):
    icon = {"hit_tp1": "🎯 TP1", "hit_tp2": "🎯🎯 TP2", "hit_sl": "🛑 SL",
            "hit_trailing_sl": "📈🛑 Trailing SL", "flip_choch": "🔁 Flip",
            "manual_close": "✋ Manual"}.get(reason, reason)
    res = "✅" if pnl_usd > 0 else "❌" if pnl_usd < 0 else "➖"
    _telegram_send(
        f"{res} <b>{ticker} {direction.upper()}</b> zamknięty — {icon}\n"
        f"{_fmt_px(entry)} → {_fmt_px(exit_price)} · <b>{pnl_pct:+.2f}%</b> (${pnl_usd:+.2f})"
    )


def _meta_get(conn, key, default=None):
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(conn, key, value):
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


DAILY_DIGEST_HOUR_UTC = 6   # 08:00 CEST / 07:00 CET

def _maybe_daily_digest(conn):
    """Raz dziennie (po 06:00 UTC) wysyła podsumowanie ostatnich 24h na Telegram."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour < DAILY_DIGEST_HOUR_UTC or _meta_get(conn, "last_digest_date") == today:
        return
    since = (now - timedelta(hours=24)).isoformat()
    closed = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE status='closed' AND closed_at >= ?", (since,)).fetchall()]
    opened = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE opened_at >= ?", (since,)).fetchall()]
    open_now = [dict(r) for r in conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()]
    pnl = sum(r.get("pnl_usd") or 0 for r in closed)
    wins = sum(1 for r in closed if (r.get("pnl_usd") or 0) > 0)
    stats = _compute_equity_stats(conn) or {}
    lines = [f"📊 <b>TVC Fusion — raport dzienny {today}</b>",
             f"Ostatnie 24h: {len(opened)} otwartych · {len(closed)} zamkniętych"
             + (f" ({wins}W/{len(closed)-wins}L, ${pnl:+.2f})" if closed else ""),
             f"Otwarte teraz: {len(open_now)}"
             + (" — " + ", ".join(f"{r['ticker']} {r['direction'][0].upper()}" for r in open_now) if open_now else "")]
    if stats and stats.get("total_closed"):
        tot_pnl = stats.get("total_pnl_usd", 0) or 0
        wr_all = (stats.get("wins", 0) / stats["total_closed"] * 100) if stats["total_closed"] else 0
        pf = stats.get("profit_factor")
        lines.append(f"Od startu: ${PAPER_CAPITAL + tot_pnl:,.0f} ({tot_pnl / PAPER_CAPITAL * 100:+.2f}%) "
                     f"· WR {wr_all:.0f}% ({stats['total_closed']} trade'ów)"
                     f"{' · PF ' + format(pf, '.2f') if pf else ''}"
                     f" · maxDD {stats.get('max_drawdown_pct', 0) or 0:.1f}%")
    if _telegram_send("\n".join(lines)):
        _meta_set(conn, "last_digest_date", today)


def cmd_open(args):
    db_init()
    path, fmt = find_fusion_input()
    data = extract_fusion_json(path, fmt)
    trade_date = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    regime = data.get("regime", "UNKNOWN")
    print(f"[open] using fusion input: {path.name} ({fmt})")

    # Freshness guard — skip if file is > 4 hours old
    file_age_h = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 3600
    if file_age_h > 4:
        print(f"[skip] fusion file is {file_age_h:.1f}h old — skipping open (stale)")
        return

    ex = get_exchange()
    conn = db()
    opened = 0
    skipped = 0
    now_utc = datetime.now(timezone.utc)

    # v0.3 — dzienny limit nowych wejść (bezpiecznik anty-overtrading)
    day_start = now_utc.strftime("%Y-%m-%dT00:00:00")
    opened_today = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE opened_at >= ?", (day_start,)
    ).fetchone()[0]

    for dec in data.get("decisions", []):
        direction = _direction_from_action(dec.get("action"))
        if direction is None:
            continue  # HOLD/WATCH/SKIP — not actionable

        ticker = dec["ticker"]
        score = int(dec.get("score") or 0)

        # v0.3 — SCORE GATE. Dane: score>=70 → 86% WR, score<70 → ~5% WR.
        if direction == "long":
            min_long = MIN_LONG_SCORE_RANGING if regime == "RANGING" else MIN_LONG_SCORE
            if score < min_long:
                print(f"[skip] {ticker} LONG score {score} < {min_long} (regime {regime}) — za niska konwikcja")
                skipped += 1
                continue
        else:
            if score > MAX_SHORT_SCORE:
                print(f"[skip] {ticker} SHORT score {score} > {MAX_SHORT_SCORE} — score nie jest bearish")
                skipped += 1
                continue

        # v0.2 — SHORT regime constraint, z wyjątkiem CHoCH override (świeży bearish
        # change of character na 1h dla TEGO tokena omija globalny BTC-regime gate)
        if direction == "short" and regime not in SHORT_ALLOWED_REGIMES and not dec.get("choch_override"):
            print(f"[skip] {ticker} SHORT blocked — regime {regime} not in {SHORT_ALLOWED_REGIMES}")
            skipped += 1
            continue
        if direction == "short" and dec.get("choch_override"):
            print(f"[open] {ticker} SHORT via CHoCH override — regime {regime} bypassed")

        # v0.3 — DAILY LIMIT
        if opened_today >= MAX_NEW_TRADES_PER_DAY:
            print(f"[skip] {ticker} — dzienny limit {MAX_NEW_TRADES_PER_DAY} nowych trade'ów wyczerpany")
            skipped += 1
            continue

        # v0.3 — REOPEN COOLDOWN. Sep 2: SOL miał 4 trade'y w 35 minut (short→long→
        # short→long), każdy stratny. Po zamknięciu tickera czekamy zanim wejdziemy znowu.
        last_closed = conn.execute(
            "SELECT closed_at FROM positions WHERE ticker=? AND status='closed' AND closed_at IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if last_closed and last_closed["closed_at"]:
            try:
                closed_dt = datetime.fromisoformat(last_closed["closed_at"])
                if closed_dt.tzinfo is None:
                    closed_dt = closed_dt.replace(tzinfo=timezone.utc)
                mins_since_close = (now_utc - closed_dt).total_seconds() / 60
                if mins_since_close < REOPEN_COOLDOWN_MINUTES:
                    print(f"[skip] {ticker} — cooldown: zamknięty {mins_since_close:.0f} min temu "
                          f"(< {REOPEN_COOLDOWN_MINUTES} min)")
                    skipped += 1
                    continue
            except (ValueError, TypeError):
                pass

        # De-dupe / flip logic — patrzymy na KAŻDĄ otwartą pozycję tego tickera,
        # niezależnie od daty otwarcia (nie tylko "dziś"), żeby nie trzymać
        # jednocześnie long+short na tym samym tokenie.
        existing_any = conn.execute(
            "SELECT * FROM positions WHERE ticker=? AND status='open' ORDER BY opened_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()

        if existing_any and existing_any["direction"] == direction:
            # ta sama strona już otwarta — nic do zrobienia
            skipped += 1
            continue

        if existing_any and existing_any["direction"] != direction:
            if not dec.get("choch_override"):
                # przeciwny kierunek, ale bez silnego sygnału CHoCH — nie flipuj,
                # zostaw starą pozycję żeby SL/TP zrobiły swoje
                skipped += 1
                continue
            # v0.3 — MIN HOLD. Nie flipuj pozycji, która nie miała szansy zadziałać.
            # Dane: mediana życia trade'u zamkniętego przez flip_choch była w porządku,
            # ale 8 z 31 żyło < 60 min — czysty szum. SL/TP mają pierwszeństwo.
            try:
                opened_dt = datetime.fromisoformat(existing_any["opened_at"])
                if opened_dt.tzinfo is None:
                    opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                age_min = (now_utc - opened_dt).total_seconds() / 60
            except (ValueError, TypeError):
                age_min = MIN_HOLD_MINUTES  # brak daty → nie blokuj
            if age_min < MIN_HOLD_MINUTES:
                print(f"[skip] {ticker} flip zablokowany — pozycja ma {age_min:.0f} min "
                      f"(< {MIN_HOLD_MINUTES} min), SL/TP niech zadziałają")
                skipped += 1
                continue

            # FLIP — CHoCH override daje sygnał przeciwny do otwartej pozycji:
            # zamknij starą po aktualnej cenie rynkowej, otwórz nową w nowym kierunku
            try:
                flip_price = _fetch_current_price(ticker)
            except Exception as e:
                print(f"[flip] {ticker} price fetch failed ({e}) — skipping flip this cycle")
                skipped += 1
                continue
            old_dir = existing_any["direction"]
            old_entry = existing_any["entry_price"]
            if old_dir == "long":
                pnl_pct = (flip_price - old_entry) / old_entry * 100
            else:
                pnl_pct = (old_entry - flip_price) / old_entry * 100
            pnl_usd = existing_any["size_usd"] * (pnl_pct / 100)
            exit_dt = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """UPDATE positions SET status='closed', exit_price=?, exit_date=?,
                   pnl_pct=?, pnl_usd=?, hit_or_miss=?, closed_at=? WHERE id=?""",
                (flip_price, exit_dt, pnl_pct, pnl_usd, "flip_choch", exit_dt, existing_any["id"]),
            )
            conn.commit()
            print(f"[flip] {ticker} closed old {old_dir} @ {flip_price:.4f} PnL {pnl_pct:+.2f}% "
                  f"(${pnl_usd:+.2f}) — opening new {direction} (CHoCH override)")
            _notify_close(ticker, old_dir, old_entry, flip_price, pnl_pct, pnl_usd, "flip_choch")

        entry_price = _mid_entry(dec.get("entry_low"), dec.get("entry_high"), ticker, ex)
        if entry_price == 0.0:
            skipped += 1
            continue

        # v0.2 — asymmetric sizing caps
        size_pct_requested = float(dec.get("size_pct", 0))
        max_cap = SHORT_SIZE_CAP_PCT if direction == "short" else LONG_SIZE_CAP_PCT
        size_pct = min(size_pct_requested, max_cap)
        if size_pct < size_pct_requested:
            print(f"[cap] {ticker} {direction.upper()} size {size_pct_requested}% capped to {size_pct}% (v0.2 asymmetric cap)")
        size_usd = PAPER_CAPITAL * (size_pct / 100)

        sources = dec.get("sources", {})
        row = (
            trade_date,
            ticker,
            EXCHANGE_ID,
            dec.get("action"),
            direction,
            dec.get("score"),
            regime,
            sources.get("onchain"),
            sources.get("technical"),
            sources.get("news"),
            sources.get("momentum"),
            sources.get("sentiment"),
            entry_price,
            size_pct,
            size_usd,
            dec.get("sl"),
            dec.get("tp1"),
            dec.get("tp2"),
            "open",
            None, None, None, None, None,
            dec.get("risk_flag", ""),
            dec.get("invalidation_note", ""),
            dec.get("risk_flag", ""),
            1 if dec.get("onchain_data_thin") else 0,
            datetime.now(timezone.utc).isoformat(),
            None,
        )
        conn.execute(
            """INSERT INTO positions (
                date, ticker, exchange, action, direction, fusion_score, regime,
                onchain_score, technical_score, news_score, momentum_score, sentiment_score,
                entry_price, size_pct, size_usd, sl_price, tp1_price, tp2_price,
                status, exit_price, exit_date, pnl_pct, pnl_usd, hit_or_miss,
                thesis, invalidation, risk_flag, onchain_data_thin,
                opened_at, closed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )
        opened += 1
        opened_today += 1
        arrow = "↗" if direction == "long" else "↘"
        print(f"[open] {arrow} {direction.upper():5s} {ticker} @ {entry_price:.4f}  size ${size_usd:.2f}  "
              f"SL {dec.get('sl')} TP1 {dec.get('tp1')} TP2 {dec.get('tp2')}  "
              f"score {dec.get('score')}")
        _notify_open(ticker, direction, entry_price, size_usd, dec.get("sl"), dec.get("tp1"),
                     dec.get("tp2"), dec.get("score"), regime, bool(dec.get("choch_override")))

    conn.commit()
    conn.close()
    print(f"[open] done — opened {opened}, skipped {skipped} (dupes/no-price/regime-blocked)")


# --- check -------------------------------------------------------------

def _ticker_symbol(ticker: str) -> tuple[str, str]:
    """Return (perp_symbol, spot_symbol) candidates for ccxt."""
    t = ticker.upper()
    return (f"{t}/USDT:USDT", f"{t}/USDT")


def _fetch_ohlc_since(ex, ticker: str, since_iso: str, timeframe: str = "5m"):
    since_ms = int(datetime.fromisoformat(since_iso).timestamp() * 1000)
    perp, spot = _ticker_symbol(ticker)
    for sym in (perp, spot):
        try:
            return ex.fetch_ohlcv(sym, timeframe=timeframe, since=since_ms, limit=1000)
        except Exception:
            continue
    return None


# --- Trailing SL config -----------------------------------------------
# BREAKEVEN_TRIGGER: profit % kiedy SL auto-move to breakeven+
# TRAILING_TRIGGER: profit % kiedy zaczynamy trailing SL
# TRAILING_DISTANCE: SL follows current price at this distance %
BREAKEVEN_TRIGGER = 1.5   # +1.5% profit → SL = entry × 1.001 (breakeven+)
TRAILING_TRIGGER = 3.0    # +3% profit → start trailing
TRAILING_DISTANCE = 1.5   # SL follows 1.5% below current price


def _update_trailing_sl(conn, row, current_price):
    """
    Update SL na row jeśli warunki trailing spełnione.
    Zwraca (new_sl, reason) jeśli zmienione, None w.p.p.
    """
    direction = (row["direction"] or "long").lower()
    entry = float(row["entry_price"])
    current_sl = float(row["sl_price"] or 0)

    if direction == "long":
        profit_pct = (current_price - entry) / entry * 100

        # Stage 1: Breakeven+ (po +1.5% profit)
        breakeven_sl = entry * 1.001  # entry + 0.1%
        if profit_pct >= BREAKEVEN_TRIGGER and current_sl < breakeven_sl:
            conn.execute(
                "UPDATE positions SET sl_price = ? WHERE id = ?",
                (breakeven_sl, row["id"])
            )
            return (breakeven_sl, f"breakeven+ (profit {profit_pct:+.2f}%)")

        # Stage 2: Trailing SL (po +3% profit)
        if profit_pct >= TRAILING_TRIGGER:
            trailing_sl = current_price * (1 - TRAILING_DISTANCE / 100)
            if trailing_sl > current_sl:
                conn.execute(
                    "UPDATE positions SET sl_price = ? WHERE id = ?",
                    (trailing_sl, row["id"])
                )
                return (trailing_sl, f"trailing +{profit_pct:.1f}% profit, SL={trailing_sl:.4f}")

    else:  # SHORT (rzadkie w regime TRENDING_UP)
        profit_pct = (entry - current_price) / entry * 100
        breakeven_sl = entry * 0.999
        if profit_pct >= BREAKEVEN_TRIGGER and (current_sl > breakeven_sl or current_sl == 0):
            conn.execute("UPDATE positions SET sl_price = ? WHERE id = ?", (breakeven_sl, row["id"]))
            return (breakeven_sl, f"SHORT breakeven+ (profit {profit_pct:+.2f}%)")
        if profit_pct >= TRAILING_TRIGGER:
            trailing_sl = current_price * (1 + TRAILING_DISTANCE / 100)
            if trailing_sl < current_sl or current_sl == 0:
                conn.execute("UPDATE positions SET sl_price = ? WHERE id = ?", (trailing_sl, row["id"]))
                return (trailing_sl, f"SHORT trailing +{profit_pct:.1f}% profit")

    return None


def _snapshot_equity(conn, open_rows):
    """Zapisz current equity state do equity_snapshots table."""
    now_iso = datetime.now(timezone.utc).isoformat()
    realized = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) FROM positions WHERE status='closed'"
    ).fetchone()[0] or 0
    unrealized = 0
    exposure = 0
    for r in open_rows:
        try:
            current_price = _fetch_current_price(r["ticker"])
            direction = (r["direction"] or "long").lower()
            if direction == "long":
                pct = (current_price - r["entry_price"]) / r["entry_price"]
            else:
                pct = (r["entry_price"] - current_price) / r["entry_price"]
            unrealized += r["size_usd"] * pct
            exposure += r["size_usd"]
        except Exception:
            continue
    total_equity = PAPER_CAPITAL + realized + unrealized
    conn.execute(
        """INSERT INTO equity_snapshots (ts, total_equity, open_positions, unrealized_pnl, realized_pnl, open_exposure)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (now_iso, total_equity, len(open_rows), unrealized, realized, exposure),
    )
    conn.commit()
    print(f"[equity] snapshot: ${total_equity:.2f} · realized ${realized:+.2f} · unrealized ${unrealized:+.2f}")


def cmd_check(args):
    db_init()
    conn = db()
    open_rows = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()

    # Equity snapshot (regardless czy są open positions czy nie)
    _snapshot_equity(conn, open_rows)

    if not open_rows:
        print("[check] no open positions")
        _maybe_daily_digest(conn)
        conn.close()
        return

    for r in open_rows:
        try:
            ohlc = _fetch_klines_binance(r["ticker"], r["opened_at"], "5m")
        except Exception as e:
            print(f"[check] {r['ticker']}: klines fetch failed ({e}) — skip this cycle")
            continue
        if not ohlc:
            print(f"[check] {r['ticker']}: no ohlc data")
            continue

        direction = (r["direction"] or "long").lower()  # default LONG for legacy rows
        sl = float(r["sl_price"] or 0)
        tp1 = float(r["tp1_price"] or 0)
        tp2 = float(r["tp2_price"] or 0)

        exit_price = None
        exit_ts = None
        hit_or_miss = None

        for candle in ohlc:  # [ts, o, h, l, c, v]
            ts, _o, h, l, _c, _v = candle
            if direction == "long":
                # LONG: SL = price falls to SL (low <= sl). TP = price rises to TP (high >= tp).
                # Check SL first (conservative — if SL and TP hit same candle, assume SL first).
                if sl and l <= sl:
                    exit_price = sl; exit_ts = ts; hit_or_miss = "hit_sl"; break
                if tp2 and h >= tp2:
                    exit_price = tp2; exit_ts = ts; hit_or_miss = "hit_tp2"; break
                if tp1 and h >= tp1:
                    exit_price = tp1; exit_ts = ts; hit_or_miss = "hit_tp1"; break
            else:  # SHORT
                # SHORT: SL = price rises to SL (high >= sl). TP = price falls to TP (low <= tp).
                # Check SL first (conservative).
                if sl and h >= sl:
                    exit_price = sl; exit_ts = ts; hit_or_miss = "hit_sl"; break
                if tp2 and l <= tp2:
                    exit_price = tp2; exit_ts = ts; hit_or_miss = "hit_tp2"; break
                if tp1 and l <= tp1:
                    exit_price = tp1; exit_ts = ts; hit_or_miss = "hit_tp1"; break

        if exit_price is None:
            # Position still open — check if trailing SL should update
            last_price = ohlc[-1][4]
            r_dict = dict(r)  # convert for _update_trailing_sl
            trail_result = _update_trailing_sl(conn, r_dict, last_price)
            if trail_result:
                new_sl, reason = trail_result
                conn.commit()
                print(f"[trail] {r['ticker']} SL updated: {r['sl_price']:.4f} → {new_sl:.4f} ({reason})")
                # Re-fetch updated row for immediate re-check on next iteration
                r = conn.execute("SELECT * FROM positions WHERE id = ?", (r["id"],)).fetchone()
                sl = float(r["sl_price"] or 0)
                # Re-check if the new trailing SL is already hit
                if direction == "long" and sl and last_price <= sl:
                    exit_price = sl; exit_ts = ohlc[-1][0]; hit_or_miss = "hit_trailing_sl"
                elif direction != "long" and sl and last_price >= sl:
                    exit_price = sl; exit_ts = ohlc[-1][0]; hit_or_miss = "hit_trailing_sl"
                else:
                    print(f"[check] {r['ticker']} ({direction}): still open  entry {r['entry_price']:.4f}  "
                          f"last {last_price:.4f}  trailing SL {sl:.4f}")
                    continue
            else:
                print(f"[check] {r['ticker']} ({direction}): still open  entry {r['entry_price']:.4f}  "
                      f"last {last_price:.4f}  SL {r['sl_price']:.4f}")
                continue

        # PnL: LONG profits from price going up, SHORT profits from price going down
        if direction == "long":
            pnl_pct = (exit_price - r["entry_price"]) / r["entry_price"] * 100
        else:
            pnl_pct = (r["entry_price"] - exit_price) / r["entry_price"] * 100
        pnl_usd = r["size_usd"] * (pnl_pct / 100)
        exit_dt = datetime.fromtimestamp(exit_ts / 1000, tz=timezone.utc).isoformat()

        conn.execute(
            """UPDATE positions SET status='closed', exit_price=?, exit_date=?,
               pnl_pct=?, pnl_usd=?, hit_or_miss=?, closed_at=? WHERE id=?""",
            (exit_price, exit_dt, pnl_pct, pnl_usd, hit_or_miss, exit_dt, r["id"]),
        )
        conn.commit()
        arrow = "↗" if direction == "long" else "↘"
        print(f"[close] {arrow} {direction.upper():5s} {r['ticker']} {hit_or_miss} @ {exit_price:.4f}  "
              f"PnL {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
        _notify_close(r["ticker"], direction, r["entry_price"], exit_price, pnl_pct, pnl_usd, hit_or_miss)

    _maybe_daily_digest(conn)
    conn.close()


# --- eod ---------------------------------------------------------------

def cmd_eod(args):
    db_init()
    cmd_check(args)  # final check for the day
    conn = db()
    today = datetime.now().strftime("%Y-%m-%d")

    opened_today = conn.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE date=?", (today,)
    ).fetchone()["n"]
    closed_today = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(pnl_usd),0) AS pnl "
        "FROM positions WHERE status='closed' AND substr(closed_at,1,10)=?",
        (today,),
    ).fetchone()
    open_now = conn.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE status='open'"
    ).fetchone()["n"]
    total_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd),0) AS pnl FROM positions WHERE status='closed'"
    ).fetchone()["pnl"]
    total_closed = conn.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE status='closed'"
    ).fetchone()["n"]
    wins = conn.execute(
        "SELECT COUNT(*) AS n FROM positions "
        "WHERE status='closed' AND hit_or_miss IN ('hit_tp1','hit_tp2')"
    ).fetchone()["n"]

    win_rate = (wins / total_closed * 100) if total_closed else 0.0

    print("=" * 60)
    print(f"TVC Fusion Paper — EOD  {today}")
    print("=" * 60)
    print(f"Opened today:   {opened_today}")
    print(f"Closed today:   {closed_today['n']}  (PnL ${closed_today['pnl']:+.2f})")
    print(f"Open now:       {open_now}")
    print("-" * 60)
    print(f"Cumulative closed: {total_closed}")
    print(f"Cumulative PnL:    ${total_pnl:+.2f}")
    print(f"Cumulative winrate: {win_rate:.1f}%")
    print("=" * 60)

    # also write EOD to markdown for future scheduled tasks
    eod_dir = DAILY_NOTES_DIR
    eod_dir.mkdir(parents=True, exist_ok=True)
    eod_path = eod_dir / f"{today}-paper-eod.md"
    with eod_path.open("w", encoding="utf-8") as f:
        f.write(f"# Paper EOD — {today}\n\n")
        f.write(f"- Opened today: **{opened_today}**\n")
        f.write(f"- Closed today: **{closed_today['n']}** (PnL **${closed_today['pnl']:+.2f}**)\n")
        f.write(f"- Currently open: **{open_now}**\n")
        f.write(f"- Cumulative closed: **{total_closed}**\n")
        f.write(f"- Cumulative PnL: **${total_pnl:+.2f}**\n")
        f.write(f"- Cumulative winrate: **{win_rate:.1f}%**\n")

    # detail today's closes
    detail = conn.execute(
        "SELECT ticker, entry_price, exit_price, pnl_pct, pnl_usd, hit_or_miss, "
        "fusion_score, onchain_score, technical_score, news_score, momentum_score "
        "FROM positions WHERE status='closed' AND substr(closed_at,1,10)=?",
        (today,),
    ).fetchall()
    if detail:
        with eod_path.open("a", encoding="utf-8") as f:
            f.write("\n## Closed today\n\n")
            f.write("| Ticker | Entry | Exit | PnL % | PnL $ | Result | Score (OC/T/N/M) |\n")
            f.write("|---|---|---|---|---|---|---|\n")
            for d in detail:
                sub = f"{d['onchain_score']}/{d['technical_score']}/{d['news_score']}/{d['momentum_score']}"
                f.write(f"| {d['ticker']} | {d['entry_price']:.4f} | {d['exit_price']:.4f} | "
                        f"{d['pnl_pct']:+.2f}% | ${d['pnl_usd']:+.2f} | {d['hit_or_miss']} | "
                        f"{d['fusion_score']} ({sub}) |\n")
    conn.close()
    print(f"[eod] wrote summary to {eod_path}")


# --- week dump for weekly-review ---------------------------------------

def cmd_week(args):
    db_init()
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    conn = db()
    rows = conn.execute(
        "SELECT * FROM positions WHERE date >= ? ORDER BY opened_at", (since,)
    ).fetchall()
    conn.close()
    out = [dict(r) for r in rows]
    print(json.dumps(out, indent=2, default=str))


# --- upload (GitHub Gist for terminal widget) --------------------------

def _fetch_news_for_tickers(decisions):
    """Fetch fresh news from FREE public sources (no auth needed):
      - CoinDesk RSS
      - Cointelegraph RSS
      - Decrypt RSS
      - Reddit r/CryptoCurrency top (public JSON)
    Filters per-ticker by title/body mention. Python has no CORS issue.
    Returns dict {ticker: [{title, url, source, published_on}, ...]}."""
    import urllib.request as ur
    import xml.etree.ElementTree as ET
    import ssl
    import re
    from email.utils import parsedate_to_datetime

    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()

    tickers = ['BTC', 'ETH', 'SOL', 'XRP', 'SUI']
    for d in decisions:
        t = d.get('ticker')
        if t and t not in tickers:
            tickers.append(t)

    UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15 tvc-fusion-bot/0.2"

    def fetch(url, timeout=8):
        req = ur.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with ur.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            return resp.read()

    def parse_rss(xml_bytes, source_name):
        items = []
        try:
            root = ET.fromstring(xml_bytes)
            for item in root.iter('item'):
                title = (item.findtext('title') or '').strip()
                link = (item.findtext('link') or '').strip()
                pub = (item.findtext('pubDate') or '').strip()
                desc = (item.findtext('description') or '').strip()
                try:
                    ts = int(parsedate_to_datetime(pub).timestamp())
                except Exception:
                    ts = 0
                items.append({
                    "title": title, "url": link, "source": source_name,
                    "published_on": ts, "body": desc[:400],
                })
        except Exception as e:
            print(f"[news] RSS parse error ({source_name}): {e}")
        return items

    all_items = []

    # Source 1 — CoinDesk RSS
    try:
        raw = fetch("https://www.coindesk.com/arc/outboundfeeds/rss/")
        items = parse_rss(raw, "CoinDesk")
        print(f"[news] CoinDesk: {len(items)} items")
        all_items.extend(items)
    except Exception as e:
        print(f"[news] CoinDesk fetch failed: {e}")

    # Source 2 — Cointelegraph RSS
    try:
        raw = fetch("https://cointelegraph.com/rss")
        items = parse_rss(raw, "Cointelegraph")
        print(f"[news] Cointelegraph: {len(items)} items")
        all_items.extend(items)
    except Exception as e:
        print(f"[news] Cointelegraph fetch failed: {e}")

    # Source 3 — Decrypt RSS
    try:
        raw = fetch("https://decrypt.co/feed")
        items = parse_rss(raw, "Decrypt")
        print(f"[news] Decrypt: {len(items)} items")
        all_items.extend(items)
    except Exception as e:
        print(f"[news] Decrypt fetch failed: {e}")

    # Source 4 — Reddit r/CryptoCurrency top posts (public JSON, no auth)
    try:
        raw = fetch("https://www.reddit.com/r/CryptoCurrency/top.json?t=day&limit=25", timeout=10)
        data = json.loads(raw)
        reddit_items = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            reddit_items.append({
                "title": post.get("title", ""),
                "url": "https://reddit.com" + post.get("permalink", ""),
                "source": "r/CryptoCurrency",
                "published_on": int(post.get("created_utc", 0)),
                "body": (post.get("selftext") or "")[:400],
            })
        print(f"[news] Reddit: {len(reddit_items)} items")
        all_items.extend(reddit_items)
    except Exception as e:
        print(f"[news] Reddit fetch failed: {e}")

    # Sort by newest first
    all_items.sort(key=lambda x: x.get("published_on", 0), reverse=True)
    print(f"[news] total raw items: {len(all_items)}")

    # Market-wide top news (no ticker filter) — for widget's market news panel
    result = {}
    result['_market'] = [{
        "title": n.get("title", ""),
        "url": n.get("url", ""),
        "source": n.get("source", ""),
        "published_on": n.get("published_on", 0),
    } for n in all_items[:15]]
    print(f"[news] _market: {len(result['_market'])} items")

    # Filter per ticker
    for ticker in tickers:
        pattern = re.compile(rf"\b{re.escape(ticker)}\b", re.IGNORECASE)
        filtered = []
        for item in all_items:
            haystack = (item.get("title", "") + " " + item.get("body", ""))
            if pattern.search(haystack):
                filtered.append(item)
                if len(filtered) >= 8:
                    break
        # pad with newest general news if < 3
        if len(filtered) < 3:
            for item in all_items[:8]:
                if item not in filtered:
                    filtered.append(item)
                if len(filtered) >= 6:
                    break
        result[ticker] = [{
            "title": n.get("title", ""),
            "url": n.get("url", ""),
            "source": n.get("source", ""),
            "published_on": n.get("published_on", 0),
        } for n in filtered[:8]]
        print(f"[news] {ticker}: {len(result[ticker])} items")
    return result


def _fetch_live_whales():
    """Fetch recent large transactions per chain (whale activity).
    Uses per-chain free public APIs — no keys, no auth.
    Returns dict {ticker: [{value_usd, amount, time, hash, chain}]}."""
    import urllib.request as ur
    import ssl
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()

    UA_BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

    def _get_json(url, timeout=15):
        req = ur.Request(url, headers={"User-Agent": UA_BROWSER, "Accept": "application/json"})
        with ur.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            return json.loads(resp.read())

    def _get_text(url, timeout=12):
        req = ur.Request(url, headers={"User-Agent": UA_BROWSER})
        with ur.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            return resp.read().decode().strip()

    def _post_json(url, payload, timeout=15):
        data = json.dumps(payload).encode()
        req = ur.Request(url, data=data, headers={
            "User-Agent": UA_BROWSER,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }, method="POST")
        with ur.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            return json.loads(resp.read())

    result = {}

    # ─── Fetch prices upfront (Binance) for BTC + ETH → USD conversion ───
    try:
        btc_usd = float(_get_json("https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT").get("price", 0))
    except Exception:
        btc_usd = 0
    try:
        eth_usd = float(_get_json("https://data-api.binance.vision/api/v3/ticker/price?symbol=ETHUSDT").get("price", 0))
    except Exception:
        eth_usd = 0
    try:
        xrp_usd = float(_get_json("https://data-api.binance.vision/api/v3/ticker/price?symbol=XRPUSDT").get("price", 0))
    except Exception:
        xrp_usd = 0

    # ─── BTC: mempool.space — scan last 3 blocks × first 4 pages (~300 txs) ───
    try:
        if btc_usd <= 0:
            raise Exception("BTC price fetch failed — skip whales")
        # Get last 3 block hashes
        blocks_meta = _get_json("https://mempool.space/api/v1/blocks")  # returns list of recent 15 blocks
        block_hashes = [b.get("id") for b in blocks_meta[:3] if b.get("id")]
        processed = []
        seen_hashes = set()
        for bh in block_hashes:
            for page_start in [0, 25, 50, 75]:  # first 100 txs each block
                try:
                    page_txs = _get_json(f"https://mempool.space/api/block/{bh}/txs/{page_start}")
                except Exception:
                    break  # end of block
                if not page_txs:
                    break
                for tx in page_txs:
                    if tx.get("txid") in seen_hashes:
                        continue
                    # Skip coinbase (miner reward, not whale activity)
                    if any(vin.get("is_coinbase") for vin in tx.get("vin", [])):
                        continue
                    total_sats = sum(v.get("value", 0) for v in tx.get("vout", []))
                    btc_val = total_sats / 1e8
                    usd_val = btc_val * btc_usd
                    if usd_val < 1_000_000:  # BTC whales > $1M
                        continue
                    block_time_ts = tx.get("status", {}).get("block_time", 0)
                    time_str = datetime.fromtimestamp(block_time_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if block_time_ts else ""
                    processed.append({
                        "value_usd": round(usd_val, 0),
                        "amount": round(btc_val, 4),
                        "time": time_str,
                        "hash": tx.get("txid", ""),
                        "chain": "bitcoin",
                    })
                    seen_hashes.add(tx.get("txid"))
        result["BTC"] = sorted(processed, key=lambda x: -x["value_usd"])[:8]
        print(f"[whales] BTC: {len(result['BTC'])} large txs (mempool.space, 3 blocks scanned)")
    except Exception as e:
        print(f"[whales] BTC fetch failed: {e}")
        result["BTC"] = []

    # ─── ETH: public Ethereum JSON-RPC with fallback pool (no key, no rate limits) ───
    # Some RPC providers reject eth_getBlockByNumber(true) — try multiple in order.
    ETH_RPCS = [
        "https://ethereum-rpc.publicnode.com",
        "https://eth.llamarpc.com",
        "https://rpc.ankr.com/eth",
        "https://eth.public-rpc.com",
        "https://cloudflare-eth.com",
    ]
    try:
        if eth_usd <= 0:
            raise Exception("ETH price fetch failed — skip whales")
        processed = []
        seen_hashes = set()
        working_rpc = None
        latest_num = None
        # Find working RPC that supports eth_blockNumber
        for rpc_url in ETH_RPCS:
            try:
                r = _post_json(rpc_url, {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
                lh = r.get("result")
                if isinstance(lh, str) and lh.startswith("0x"):
                    working_rpc = rpc_url
                    latest_num = int(lh, 16)
                    print(f"[whales] ETH RPC working: {rpc_url} @ block {latest_num}")
                    break
            except Exception:
                continue
        if not working_rpc:
            raise Exception("no working ETH RPC found in fallback pool")
        # Scan last 8 blocks (~96s window) with full txs — native ETH whales są rzadsze niż BTC/XRP
        for offset in range(8):
            block_num_hex = hex(latest_num - offset)
            try:
                block_resp = _post_json(working_rpc, {
                    "jsonrpc": "2.0", "method": "eth_getBlockByNumber",
                    "params": [block_num_hex, True], "id": 1
                })
            except Exception as e:
                print(f"[whales] ETH block {latest_num - offset} fetch failed: {e}")
                continue
            block = block_resp.get("result")
            if not isinstance(block, dict):
                # RPC rejected fullTx=true on this endpoint — try next RPC
                print(f"[whales] ETH RPC {working_rpc} rejected fullTx — trying fallback")
                # switch to fallback with fullTx support
                for alt_rpc in ETH_RPCS:
                    if alt_rpc == working_rpc:
                        continue
                    try:
                        alt_resp = _post_json(alt_rpc, {
                            "jsonrpc": "2.0", "method": "eth_getBlockByNumber",
                            "params": [block_num_hex, True], "id": 1
                        })
                        alt_block = alt_resp.get("result")
                        if isinstance(alt_block, dict):
                            block = alt_block
                            working_rpc = alt_rpc
                            print(f"[whales] ETH switched to {alt_rpc}")
                            break
                    except Exception:
                        continue
                if not isinstance(block, dict):
                    continue
            txs = block.get("transactions", []) or []
            block_ts = int(block.get("timestamp", "0x0"), 16)
            time_str = datetime.fromtimestamp(block_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if block_ts else ""
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                tx_hash = tx.get("hash", "")
                if tx_hash in seen_hashes:
                    continue
                try:
                    value_wei = int(tx.get("value", "0x0"), 16)
                except (ValueError, TypeError):
                    continue
                eth_val = value_wei / 1e18
                usd_val = eth_val * eth_usd
                if usd_val < 250_000:  # ETH whales > $250K (native ETH; most whale flow is via ERC-20 tokens)
                    continue
                processed.append({
                    "value_usd": round(usd_val, 0),
                    "amount": round(eth_val, 4),
                    "time": time_str,
                    "hash": tx_hash,
                    "chain": "ethereum",
                })
                seen_hashes.add(tx_hash)
        result["ETH"] = sorted(processed, key=lambda x: -x["value_usd"])[:8]
        print(f"[whales] ETH: {len(result['ETH'])} large txs (public RPC, 8 blocks)")
    except Exception as e:
        print(f"[whales] ETH fetch failed: {e}")
        result["ETH"] = []

    # ─── XRP: xrplcluster.com public rippled JSON-RPC (free, very stable) ───
    try:
        if xrp_usd <= 0:
            raise Exception("XRP price fetch failed — skip whales")
        # Get latest validated ledger with transactions expanded
        rpc_resp = _post_json("https://xrplcluster.com/", {
            "method": "ledger",
            "params": [{
                "ledger_index": "validated",
                "transactions": True,
                "expand": True,
            }],
        })
        ledger = rpc_resp.get("result", {}).get("ledger", {}) or {}
        txs = ledger.get("transactions", []) or []
        close_time_iso = ledger.get("close_time_human") or ledger.get("close_time_iso", "")
        # Fetch a few more validated ledgers by decreasing ledger_index for wider window
        current_idx = ledger.get("ledger_index") or ledger.get("seqNum")
        try:
            current_idx = int(current_idx)
        except (TypeError, ValueError):
            current_idx = None
        processed = []
        seen_hashes = set()
        def _process_ledger_txs(tx_list, time_str):
            for tx in tx_list:
                # In expanded form tx can be dict with tx fields at top level
                if not isinstance(tx, dict):
                    continue
                tx_hash = tx.get("hash") or tx.get("Hash", "")
                if tx_hash in seen_hashes:
                    continue
                if tx.get("TransactionType") != "Payment":
                    continue
                amount = tx.get("Amount")
                if not amount:
                    continue
                if isinstance(amount, str):
                    try:
                        xrp_val = int(amount) / 1e6
                    except (ValueError, TypeError):
                        continue
                else:
                    continue  # issued currency, skip
                usd_val = xrp_val * xrp_usd
                if usd_val < 100_000:
                    continue
                processed.append({
                    "value_usd": round(usd_val, 0),
                    "amount": round(xrp_val, 2),
                    "time": time_str,
                    "hash": tx_hash,
                    "chain": "ripple",
                })
                seen_hashes.add(tx_hash)
        _process_ledger_txs(txs, close_time_iso)
        # Scan 3 more ledgers back for better coverage (XRP has ~5s ledger close = only ~30s window otherwise)
        if current_idx:
            for back in range(1, 20):  # scan 20 ledgers back = ~100s window
                try:
                    r = _post_json("https://xrplcluster.com/", {
                        "method": "ledger",
                        "params": [{"ledger_index": current_idx - back, "transactions": True, "expand": True}],
                    })
                    l = r.get("result", {}).get("ledger", {}) or {}
                    _process_ledger_txs(l.get("transactions", []) or [], l.get("close_time_human", ""))
                except Exception:
                    pass
        result["XRP"] = sorted(processed, key=lambda x: -x["value_usd"])[:8]
        print(f"[whales] XRP: {len(result['XRP'])} large txs (xrplcluster, ~20 ledgers)")
    except Exception as e:
        print(f"[whales] XRP fetch failed: {e}")
        result["XRP"] = []

    return result


def _get_equity_history(conn, days=30):
    """Fetch equity snapshots dla ostatnich N dni — dla equity curve chart."""
    try:
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT ts, total_equity, unrealized_pnl, realized_pnl, open_exposure FROM equity_snapshots WHERE ts >= ? ORDER BY ts",
            (since,)
        ).fetchall()
        return [
            {
                "ts": r["ts"],
                "equity": round(float(r["total_equity"]), 2),
                "unrealized": round(float(r["unrealized_pnl"] or 0), 2),
                "realized": round(float(r["realized_pnl"] or 0), 2),
                "exposure": round(float(r["open_exposure"] or 0), 2),
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[equity_history] fetch failed: {e}")
        return []


def _compute_equity_stats(conn):
    """Compute P&L stats — max drawdown, avg win, avg loss, sharpe proxy."""
    try:
        # Closed positions stats
        closed = conn.execute(
            "SELECT pnl_usd, pnl_pct, hit_or_miss FROM positions WHERE status='closed' AND pnl_usd IS NOT NULL"
        ).fetchall()
        if not closed:
            return {}
        wins = [r["pnl_usd"] for r in closed if r["pnl_usd"] > 0]
        losses = [r["pnl_usd"] for r in closed if r["pnl_usd"] < 0]
        total_pnl = sum(r["pnl_usd"] for r in closed)
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        best_trade = max((r["pnl_usd"] for r in closed), default=0)
        worst_trade = min((r["pnl_usd"] for r in closed), default=0)
        # Streak calculation (current)
        streak = 0
        streak_type = "none"
        for r in reversed(closed):
            if r["pnl_usd"] > 0 and streak_type in ("none", "win"):
                streak_type = "win"; streak += 1
            elif r["pnl_usd"] < 0 and streak_type in ("none", "loss"):
                streak_type = "loss"; streak += 1
            else:
                break

        # Max drawdown z equity history
        equity_rows = conn.execute(
            "SELECT total_equity FROM equity_snapshots ORDER BY ts"
        ).fetchall()
        max_dd = 0
        max_dd_pct = 0
        if equity_rows:
            peak = float(equity_rows[0]["total_equity"])
            for r in equity_rows:
                eq = float(r["total_equity"])
                if eq > peak:
                    peak = eq
                dd = peak - eq
                dd_pct = (dd / peak * 100) if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd; max_dd_pct = dd_pct

        # Result breakdown
        tp1_hits = sum(1 for r in closed if r["hit_or_miss"] == "hit_tp1")
        tp2_hits = sum(1 for r in closed if r["hit_or_miss"] == "hit_tp2")
        sl_hits = sum(1 for r in closed if r["hit_or_miss"] == "hit_sl")
        trailing_hits = sum(1 for r in closed if r["hit_or_miss"] == "hit_trailing_sl")
        manual_closes = sum(1 for r in closed if r["hit_or_miss"] == "manual_close")

        return {
            "total_closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "total_pnl_usd": round(total_pnl, 2),
            "avg_win_usd": round(avg_win, 2),
            "avg_loss_usd": round(avg_loss, 2),
            "best_trade_usd": round(best_trade, 2),
            "worst_trade_usd": round(worst_trade, 2),
            "current_streak": streak,
            "current_streak_type": streak_type,
            "max_drawdown_usd": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None,
            "result_breakdown": {
                "tp1_hits": tp1_hits,
                "tp2_hits": tp2_hits,
                "sl_hits": sl_hits,
                "trailing_sl_hits": trailing_hits,
                "manual_closes": manual_closes,
            }
        }
    except Exception as e:
        print(f"[equity_stats] compute failed: {e}")
        return {}


def _compute_performance_breakdown(conn):
    """v0.3 — analityka 'co działa': WR / PnL / profit factor w rozbiciu po score,
    regime, kierunku, typie wyjścia i wersji bramek. To ten sam audyt, który
    wykrył degradację v0.2 — teraz liczony automatycznie co cykl, żeby spadek
    jakości było widać po 2 dniach, nie po 40 stratnych trade'ach."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE status='closed' AND closed_at IS NOT NULL ORDER BY closed_at"
    ).fetchall()]
    if not rows:
        return {}

    def agg(rs):
        n = len(rs)
        wins = [r["pnl_usd"] for r in rs if (r["pnl_usd"] or 0) > 0]
        losses = [r["pnl_usd"] for r in rs if (r["pnl_usd"] or 0) < 0]
        pnl = sum((r["pnl_usd"] or 0) for r in rs)
        pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (None if not wins else 99.0)
        return {"n": n, "wins": len(wins), "wr": round(len(wins) / n * 100, 1) if n else 0,
                "pnl": round(pnl, 2), "avg_pnl_pct": round(sum((r["pnl_pct"] or 0) for r in rs) / n, 2) if n else 0,
                "pf": round(pf, 2) if pf is not None else None}

    def bucketize(keyfn, order=None):
        groups = {}
        for r in rows:
            k = keyfn(r)
            groups.setdefault(k, []).append(r)
        keys = order if order else sorted(groups.keys(), key=lambda x: str(x))
        return [{"label": k, **agg(groups[k])} for k in keys if k in groups]

    def score_bucket(r):
        s = r.get("fusion_score") or 0
        return ">=70" if s >= 70 else "60-69" if s >= 60 else "50-59" if s >= 50 else "<50"

    exit_labels = {"hit_tp2": "TP2", "hit_tp1": "TP1", "hit_trailing_sl": "Trailing SL",
                   "hit_sl": "SL", "flip_choch": "Flip CHoCH", "manual_close": "Manual"}
    now = datetime.now(timezone.utc)
    cut7 = (now - timedelta(days=7)).isoformat()
    cut14 = (now - timedelta(days=14)).isoformat()
    last7 = [r for r in rows if r["closed_at"] >= cut7]
    prev7 = [r for r in rows if cut14 <= r["closed_at"] < cut7]
    v03_since = "2026-09-02T19:00:00"  # deploy bramek v0.3 (commit c890e34)
    return {
        "computed_at": now.isoformat(),
        "all": agg(rows),
        "by_score": bucketize(score_bucket, [">=70", "60-69", "50-59", "<50"]),
        "by_regime": bucketize(lambda r: r.get("regime") or "?"),
        "by_direction": bucketize(lambda r: (r.get("direction") or "long").upper(), ["LONG", "SHORT"]),
        "by_exit": bucketize(lambda r: exit_labels.get(r.get("hit_or_miss"), str(r.get("hit_or_miss")))),
        "by_ticker": bucketize(lambda r: r.get("ticker") or "?"),
        "trend": {"last7": agg(last7) if last7 else None, "prev7": agg(prev7) if prev7 else None},
        "by_version": [
            {"label": "v0.2 (przed bramkami)", **agg([r for r in rows if r["opened_at"] < v03_since])},
            {"label": "v0.3 (bramki)", **agg([r for r in rows if r["opened_at"] >= v03_since])},
        ],
    }


def cmd_upload(args):
    """Push today's fusion JSON to a GitHub Gist so the terminal widget can fetch it.

    Requires env vars:
        GITHUB_TOKEN — Personal Access Token with 'gist' scope
        TVC_GIST_ID  — the target Gist ID (from gist URL after your username)

    The gist file will be named `fusion_latest.json` (always overwritten so the
    widget's fetch URL is stable). Also uploads today's dated copy for history.
    """
    import urllib.request
    import urllib.error
    import ssl

    # SSL context — try system defaults first, fall back to certifi if available
    # (fixes Python-on-Mac "CERTIFICATE_VERIFY_FAILED" from python.org installer)
    ssl_ctx = None
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()

    token = os.environ.get("GITHUB_TOKEN")
    gist_id = os.environ.get("TVC_GIST_ID")
    if not token:
        sys.exit("ERROR: set GITHUB_TOKEN env var (Personal Access Token with 'gist' scope)")
    if not gist_id:
        sys.exit("ERROR: set TVC_GIST_ID env var (from your Gist URL)")

    today = datetime.now().strftime("%Y-%m-%d")
    path = TVC_DIR / f"fusion_{today}.json"
    if not path.exists():
        sys.exit(f"ERROR: {path.name} not found — run 'odpal fusion' first, then retry upload")

    # Base fusion data
    fusion_data = json.loads(path.read_text(encoding="utf-8"))

    # Enrich with live state from SQLite (open positions, recent closed, PnL)
    db_init()
    conn = db()
    open_positions = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
    ).fetchall()]
    recent_closed = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 20"
    ).fetchall()]
    total_pnl = sum((r.get("pnl_usd") or 0) for r in recent_closed)
    # v0.3 — wygrana = dodatni PnL (spójnie z panelem P&L History). Wcześniej liczono
    # tylko hit_tp*, więc terminal pokazywał "WIN RATE 0%" mimo zyskownych trade'ów
    # zamkniętych trailing stopem / ręcznie.
    wins = sum(1 for r in recent_closed if (r.get("pnl_usd") or 0) > 0)
    win_rate = (wins / len(recent_closed) * 100) if recent_closed else 0.0
    equity_history = _get_equity_history(conn, days=30)
    equity_stats = _compute_equity_stats(conn)
    performance = _compute_performance_breakdown(conn)
    last_open_row = conn.execute("SELECT MAX(opened_at) FROM positions").fetchone()[0]
    last_close_row = conn.execute("SELECT MAX(closed_at) FROM positions").fetchone()[0]
    conn.close()

    # v0.3 — health block: terminal pokazuje "Autopilot: X min temu" i ostrzega gdy
    # cykl milczy. generated_at = heartbeat tego cyklu.
    health = {
        "autopilot_last_cycle": datetime.now(timezone.utc).isoformat(),
        "cycle_interval_min": 5,
        "bot_version": "0.3",
        "telegram_enabled": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
        "last_position_opened": last_open_row,
        "last_position_closed": last_close_row,
        "open_count": len(open_positions),
        "gates": {
            "min_long_score": MIN_LONG_SCORE,
            "min_long_score_ranging": MIN_LONG_SCORE_RANGING,
            "max_short_score": MAX_SHORT_SCORE,
            "min_hold_min": MIN_HOLD_MINUTES,
            "reopen_cooldown_min": REOPEN_COOLDOWN_MINUTES,
            "max_trades_per_day": MAX_NEW_TRADES_PER_DAY,
        },
    }

    # Fetch per-token news server-side (Python has no CORS issue)
    news_by_token = _fetch_news_for_tickers(fusion_data.get("decisions", []))
    # Fetch live whale transactions per chain (Blockchair public API, no auth)
    whales_by_token = _fetch_live_whales()

    fusion_data["state"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "open_positions": open_positions,
        "recent_closed": recent_closed,
        "total_pnl_usd": round(total_pnl, 2),
        "cumulative_win_rate": round(win_rate, 1),
        "cumulative_closed": len(recent_closed),
        "paper_capital": PAPER_CAPITAL,
        "news": news_by_token,
        "whales_live": whales_by_token,
        "equity_history": equity_history,
        "equity_stats": equity_stats,
        "health": health,
        "performance": performance,
    }

    content = json.dumps(fusion_data, indent=2, default=str)

    payload = json.dumps({
        "description": f"TVC Fusion latest ({today})",
        "files": {
            "fusion_latest.json": {"content": content},
            f"fusion_{today}.json": {"content": content},
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "tvc-fusion-bot/0.2",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            data = json.loads(resp.read())
            print(f"[upload] gist updated at {data.get('updated_at', '?')}")
            latest = data.get("files", {}).get("fusion_latest.json", {})
            raw_url_with_sha = latest.get("raw_url", "?")
            # Compute STABLE URL (no SHA — always resolves to latest version).
            # Format: https://gist.githubusercontent.com/{user}/{gist_id}/raw/{filename}
            owner = data.get("owner", {}).get("login", "")
            stable_url = f"https://gist.githubusercontent.com/{owner}/{gist_id}/raw/fusion_latest.json"
            print(f"[upload] STABLE widget URL (use this — no SHA, always latest):")
            print(f"         {stable_url}")
            print(f"[upload] versioned URL (this one changes every upload — ignore):")
            print(f"         {raw_url_with_sha}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.exit(f"ERROR: GitHub API {e.code}\n{body}")
    except urllib.error.URLError as e:
        sys.exit(
            f"ERROR: network failure — {e}\n\n"
            "Jeśli to jest SSL CERTIFICATE_VERIFY_FAILED:\n"
            "  1. Uruchom: /Applications/Python\\ 3.14/Install\\ Certificates.command\n"
            "  2. Lub zainstaluj certifi: python3 -m pip install --user certifi\n"
        )


# --- CLI ---------------------------------------------------------------

def cmd_close(args):
    """Manually close a specific ticker position (for take partial / manual exit)."""
    db_init()
    ticker = getattr(args, 'ticker', None)
    if not ticker:
        print("[close] Podaj ticker: python3 paper_bot.py close BTC")
        return
    ticker = ticker.upper()
    conn = db()
    rows = conn.execute("SELECT * FROM positions WHERE status='open' AND ticker=?", (ticker,)).fetchall()
    if not rows:
        print(f"[close] Brak open position dla {ticker}")
        conn.close()
        return
    # Get current price
    try:
        exit_price = _fetch_current_price(ticker)
    except Exception as e:
        print(f"[close] Price fetch failed dla {ticker}: {e}")
        conn.close()
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    for r in rows:
        direction = r['direction'] if 'direction' in r.keys() else 'long'
        if direction == 'long':
            pnl_pct = (exit_price - r['entry_price']) / r['entry_price'] * 100
        else:
            pnl_pct = (r['entry_price'] - exit_price) / r['entry_price'] * 100
        pnl_usd = r['size_usd'] * (pnl_pct / 100)
        conn.execute(
            """UPDATE positions SET status='closed', exit_price=?, exit_date=?,
               pnl_pct=?, pnl_usd=?, hit_or_miss=?, closed_at=? WHERE id=?""",
            (exit_price, now_iso, pnl_pct, pnl_usd, 'manual_close', now_iso, r['id'])
        )
        conn.commit()
        arrow = "↗" if direction == "long" else "↘"
        print(f"[close] {arrow} MANUAL {direction.upper():5s} {ticker} @ {exit_price:.4f}  "
              f"PnL {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
        _notify_close(ticker, direction, r['entry_price'], exit_price, pnl_pct, pnl_usd, "manual_close")
    conn.close()
    # AUTO-UPLOAD po close żeby widget widział świeże dane
    print("[close] Auto-uploading do Gist...")
    try:
        cmd_upload(args)
    except Exception as e:
        print(f"[close] Upload failed: {e}")


def cmd_refresh(args):
    """Szybki refresh — check + upload (dla auto-loop lub manual)."""
    db_init()
    print("[refresh] check pozycji...")
    try:
        cmd_check(args)
    except Exception as e:
        print(f"[refresh] check failed: {e}")
    print("[refresh] upload do Gist...")
    try:
        cmd_upload(args)
    except Exception as e:
        print(f"[refresh] upload failed: {e}")


def cmd_loop(args):
    """Background daemon — auto-refresh co N minut. Zostaw Terminal open."""
    interval_min = getattr(args, 'interval', None) or 15
    interval_sec = int(interval_min) * 60
    print(f"[loop] Auto-refresh co {interval_min} min. Ctrl+C żeby zatrzymać.")
    print(f"[loop] Data flow: check pozycji → upload do Gist → widget refresh")
    print(f"[loop] Zostaw ten Terminal otwarty!\n")
    import time
    iteration = 0
    while True:
        iteration += 1
        now = datetime.now().strftime('%H:%M:%S')
        print(f"\n─── [loop #{iteration}] {now} ───")
        try:
            cmd_check(args)
        except Exception as e:
            print(f"[loop] check failed: {e}")
        try:
            cmd_upload(args)
            print(f"[loop] ✅ refresh done. Next za {interval_min} min ({datetime.fromtimestamp(time.time() + interval_sec).strftime('%H:%M')})...")
        except Exception as e:
            print(f"[loop] upload failed: {e}")
        try:
            time.sleep(interval_sec)
        except KeyboardInterrupt:
            print("\n[loop] Stopped by Ctrl+C. Widget przestanie się odświeżać.")
            break


BINANCE_SYMBOL_MAP = {'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'SOL': 'SOLUSDT', 'XRP': 'XRPUSDT', 'SUI': 'SUIUSDT'}


def _binance_symbol(ticker: str) -> str:
    t = ticker.upper()
    return BINANCE_SYMBOL_MAP.get(t, f"{t}USDT")


def _fetch_current_price(ticker):
    """Fetch current price for ticker from Binance."""
    import urllib.request as ur
    import ssl
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
    sym = _binance_symbol(ticker)
    url = f"https://data-api.binance.vision/api/v3/ticker/price?symbol={sym}"
    req = ur.Request(url, headers={"User-Agent": "Mozilla/5.0 tvc-fusion-bot/0.2"})
    with ur.urlopen(req, timeout=10, context=ssl_ctx) as resp:
        return float(json.loads(resp.read()).get('price', 0))


def _fetch_klines_binance(ticker: str, since_iso: str, interval: str = "5m", limit: int = 1000):
    """Fetch OHLCV klines z Binance public REST API (data-api.binance.vision) —
    zero-auth, wysokie rate limity, sprawdzone jako niezawodne z GitHub Actions.
    Zastępuje wcześniejsze ccxt/Bybit fetche w cmd_check, które bywały blokowane
    dla IP data-center GitHub-hosted runnerów (cichy fail → pozycje nigdy się
    nie aktualizowały/zamykały, bo _fetch_ohlc_since zwracał None bez żadnego
    widocznego błędu w logach).
    Zwraca listę [ts, open, high, low, close, volume] (format zgodny z ccxt OHLCV)."""
    import urllib.request as ur
    import ssl
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
    sym = _binance_symbol(ticker)
    since_ms = int(datetime.fromisoformat(since_iso).timestamp() * 1000)
    url = (f"https://data-api.binance.vision/api/v3/klines?symbol={sym}"
           f"&interval={interval}&startTime={since_ms}&limit={limit}")
    req = ur.Request(url, headers={"User-Agent": "Mozilla/5.0 tvc-fusion-bot/0.2"})
    with ur.urlopen(req, timeout=15, context=ssl_ctx) as resp:
        raw = json.loads(resp.read())
    # Binance kline: [openTime, open, high, low, close, volume, closeTime, ...] (wszystko stringi poza openTime)
    return [[k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in raw]


def main():
    p = argparse.ArgumentParser(description="TVC Fusion Paper Trading Bot")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("init", help="create SQLite schema")
    sub.add_parser("open", help="open paper positions from today's fusion")
    sub.add_parser("check", help="check open positions vs current prices")
    sub.add_parser("eod", help="check + EOD summary")
    sub.add_parser("week", help="dump last 7d as JSON")
    sub.add_parser("upload", help="push today's fusion JSON to GitHub Gist (for terminal widget)")
    sub.add_parser("refresh", help="check + upload (szybki manual refresh)")
    close_p = sub.add_parser("close", help="manual close specific ticker (auto-uploads after)")
    close_p.add_argument("ticker", help="Ticker to close (BTC/ETH/SOL/XRP/SUI)")
    loop_p = sub.add_parser("loop", help="background daemon — auto-refresh co N min")
    loop_p.add_argument("--interval", type=int, default=15, help="Minutes between refreshes (default 15)")
    args = p.parse_args()

    handlers = {"init": lambda a: db_init(), "open": cmd_open,
                "check": cmd_check, "eod": cmd_eod, "week": cmd_week,
                "upload": cmd_upload, "close": cmd_close, "refresh": cmd_refresh, "loop": cmd_loop}
    if not args.cmd:
        p.print_help()
        sys.exit(1)
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
