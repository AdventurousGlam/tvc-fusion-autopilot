#!/usr/bin/env python3
"""
TVC Fusion Paper Trading Bot v1.0

v1.0 (2026-09-22) — EFFECTIVENESS UPGRADE
  - Regime-aware score gate: TRENDING_UP→55, RANGING→58, TRENDING_DOWN→62
  - MAX_NEW_TRADES_PER_DAY: 3→5 (more opportunities)
  - Fib threshold RANGING: 0.70→0.80 (less restrictive)
  - REOPEN_COOLDOWN_MINUTES: 120→90 (faster re-entry)
  - Tiered sizing extended: 55-60→5% micro positions
  - STATS_SINCE reset to 2026-09-22 (clean slate)
  - Works with auto_fusion v1.0 wider score spread

v0.9 (2026-09-22) — REGIME-AWARE FIBONACCI + ZOMBIE CLEANUP
  - Fibonacci pullback threshold depends on market regime:
    TRENDING_UP→0.90, RANGING→0.70, TRENDING_DOWN→0.50
  - MAX_HOLD_DAYS=5: auto-close zombie positions older than 5 days
  - Fixes bot being completely blocked in trending markets

v0.8 (2026-09-16) — TIERED SIZING: position size scales with conviction
  - Score 60-65→8%, 65-75→20%, 75-85→40%, 85+→80% of capital
  - MIN_LONG_SCORE back to 60 (tiered sizing handles risk)
  - Replaces flat 3% LONG_SIZE_CAP_PCT

v0.7 (2026-09-16) — STRATEGY UPGRADE: Fibonacci + rebalanced weights
  - Fibonacci pullback filter: skip LONG if price >70% of 30d range
  - MIN_LONG_SCORE 60→65 (score 60-64 = 0% WR)
  - TRAILING_DISTANCE 1.5%→2.5%

v0.6 (2026-09-14) — AUDIT FIX: threshold alignment + R:R guard
  - MIN_LONG_SCORE 65→60 (aligned with auto_fusion BUY threshold)
  - MAX_SHORT_SCORE 35→40 (symmetric, formula-achievable)
  - Added MIN_RR_AT_ENTRY=1.0 guard: checks R:R at live market price
    before opening (fixes stale-TP bug, e.g. SOL 11.09 R:R=0.0)
  - Root cause: 93% of BUY signals (13/14) were rejected Sep 2-12
    because auto_fusion marks ≥60 as BUY but paper_bot required ≥65.

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
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _sanitize_nan(obj):
    """Recursively replace NaN/Inf floats with None — json.dumps allows NaN by default
    but JavaScript's JSON.parse does NOT, crashing the terminal."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_nan(v) for v in obj]
    return obj

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

# v0.8 — TIERED SIZING by fusion score (higher conviction = bigger position)
# Replaces flat 3% cap. Shorts stay conservative (unlimited upside risk).
TIERED_LONG_SIZES = {        # (min_score, max_score): size_pct
    (55, 60): 5.0,           # v1.0: micro position (trending regime only)
    (60, 65): 8.0,           # low conviction — probe position
    (65, 75): 20.0,          # normal conviction
    (75, 85): 40.0,          # high conviction
    (85, 101): 80.0,         # very high conviction — full send
}
SHORT_SIZE_CAP_PCT = 0.5    # max % capital per SHORT trade (unlimited upside risk)

# ═══════════════════════════════════════════════════════════════════════════
# v0.5 — SIMPLIFIED 5-RULE SYSTEM (Tushar Chande: <10 rules)
#
# Diagnoza: analysis paralysis z 12+ paneli + 5-6 skomplikowanych bramek.
# System działa najlepiej przy wysokiej konwikcji (score≥70: 86% WR),
# a traci na niskiej jakości wejściach i nadmiarze reguł (CHoCH flip,
# entry_quality wait/skip, regime constraint, pending orders).
#
# 5 REGUŁ:
#   1. Fusion Score ≥60 → LONG  /  ≤40 → SHORT  (v0.8: back to 60, tiered sizing)
#   1b. R:R at live price ≥ 1.0 (v0.6: execution-time guard)
#   6. Fibonacci pullback filter (v0.7)
#   7. Tiered sizing by score: 60-65→8%, 65-75→20%, 75-85→40%, 85+→80% (v0.8)
#   2. Smart Money verdict ≠ opposes (layers.veto is None)
#   3. Brak makro blackout (3h przed / 1h po tier-1 evencie)
#   4. SL ze struktury (auto_fusion levels)
#   5. TP ze struktury (auto_fusion levels)
#
# Wszystko inne (regime, CVD, flush/pump, OI, funding) → KONTEKST, nie decyzja.
# Usunięte: CHoCH flip, entry_quality wait/skip, pending orders, regime gate
# dla shortów, MIN_HOLD flip protection.
# Zachowane: trailing SL, reopen cooldown, daily limit, Telegram, equity.
# ═══════════════════════════════════════════════════════════════════════════
MIN_LONG_SCORE = 58            # Reguła #1: bazowy minimalny score dla LONG (v1.0: 60→58)
MAX_SHORT_SCORE = 42           # Reguła #1: SHORT gdy score jest bearish (v1.0: 40→42, symmetric)
MIN_RR_AT_ENTRY = 1.0          # Reguła #1b: min R:R w momencie wejścia (ochrona przed stale TP)
REOPEN_COOLDOWN_MINUTES = 90   # anty-overtrading: po zamknięciu tickera 90min przerwy (v1.0: 120→90)
MAX_NEW_TRADES_PER_DAY = 5     # anty-overtrading: max 5 nowych trade'ów dziennie (v1.0: 3→5)

# v1.0 — Regime-aware score thresholds: w TRENDING_UP łatwiej otworzyć LONG,
# w TRENDING_DOWN trudniej (kontrt-trendowe pozycje wymagają wyższej konwikcji)
MIN_LONG_SCORE_BY_REGIME = {
    "TRENDING_UP":          55,
    "TRENDING_UP_VOLATILE": 55,
    "RANGING":              58,
    "TRENDING_DOWN":        62,
    "TRENDING_DOWN_VOLATILE": 65,
}
MAX_HOLD_DAYS = 5              # v0.9 — auto-close zombie pozycji po 5 dniach

# v0.9 — Fibonacci pullback threshold zależny od reżimu rynku.
# W trendzie wzrostowym bot akceptuje wejścia bliżej szczytu 30d zakresu,
# bo pullback do 50% może nie nadejść przez tygodnie. W konsolidacji
# i downtrend — bardziej restrykcyjny (wymaga głębszego pullbacku).
FIB_THRESHOLD_BY_REGIME = {
    "TRENDING_UP":          0.90,
    "TRENDING_UP_VOLATILE": 0.90,
    "RANGING":              0.80,   # v1.0: 0.70→0.80 (mniej restrykcyjny w konsolidacji)
    "TRENDING_DOWN":        0.50,
    "TRENDING_DOWN_VOLATILE": 0.50,
}
FIB_THRESHOLD_DEFAULT = 0.80  # v1.0: fallback 0.70→0.80

# STATYSTYKI — liczone od wdrożenia bramek v0.3 (trade'y v0.2 = archiwum).
STATS_SINCE = "2026-09-22T00:00:00"  # v1.0 — czysty start (stare trade'y = archiwum)
STATS_WHERE = "status='closed' AND opened_at >= ? AND COALESCE(excluded,0)=0"

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
    # v0.4 migrations
    _migrate_add_column(conn, "positions", "sl_since", "INTEGER")   # ms epoch ostatniej zmiany SL
    _migrate_add_column(conn, "positions", "excluded", "INTEGER DEFAULT 0")
    _migrate_add_column(conn, "positions", "context_tags", "TEXT")      # v0.4: 'extended,weekend,…' (audyt: co działa w jakim kontekście)
    _migrate_add_column(conn, "positions", "entry_zone_low", "REAL")
    _migrate_add_column(conn, "positions", "entry_zone_high", "REAL")
    _migrate_add_column(conn, "positions", "pending_until", "TEXT")
    _migrate_add_column(conn, "positions", "rr", "REAL")
    conn.commit()
    _migrate_sl_rescan_bug(conn)
    conn.close()
    print(f"[init] db ready at {DB_PATH}")


def _migrate_sl_rescan_bug(conn):
    """Jednorazowa migracja danych (idempotentna przez meta key).
    Bug (do 07.09.2026): cmd_check skanował świece od opened_at z AKTUALNYM SL.
    Gdy trailing podniósł SL na breakeven (+0.1%), następny cykl znajdował starą
    świecę sprzed podniesienia z low <= nowy SL i zamykał pozycję wstecznie na
    +0.10% — mimo że realnie była +1.5% i więcej. Sygnatura: hit_sl, pnl_pct == +0.10%,
    zamknięcie < 60 min od otwarcia. Takie trade'y oznaczamy sl_rescan_bug i wykluczamy
    ze statystyk (wyniku nie rekonstruujemy — nie wiemy, gdzie realnie by wyszły)."""
    try:
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT value FROM meta WHERE key='mig_sl_rescan_v1'").fetchone():
            return
    except sqlite3.OperationalError:
        return  # brak tabeli meta (świeża baza) — nie ma czego migrować
    rows = conn.execute(
        "SELECT id, opened_at, closed_at FROM positions WHERE status='closed' AND hit_or_miss='hit_sl' "
        "AND pnl_pct IS NOT NULL AND ABS(pnl_pct - 0.1) < 0.005"
    ).fetchall()
    hit = []
    for r in rows:
        try:
            o = datetime.fromisoformat(r["opened_at"].replace("Z", "+00:00"))
            c = datetime.fromisoformat(r["closed_at"].replace("Z", "+00:00"))
            if o.tzinfo is None: o = o.replace(tzinfo=timezone.utc)
            if c.tzinfo is None: c = c.replace(tzinfo=timezone.utc)
            if (c - o).total_seconds() < 3600:
                hit.append(r["id"])
        except Exception:
            continue
    if hit:
        conn.executemany("UPDATE positions SET hit_or_miss='sl_rescan_bug', excluded=1 WHERE id=?",
                         [(i,) for i in hit])
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('mig_sl_rescan_v1', ?)",
                 (f"{datetime.now(timezone.utc).isoformat()} ids={hit}",))
    conn.commit()
    print(f"[migrate] sl_rescan_bug: oznaczono {len(hit)} trade'ów {hit}")


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


# --- Telegram notifications (v0.6 — dual-channel) -------------------------
# Env: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (GitHub Secrets w workflow).
# Kanały hardcoded (to publiczne ID, nie sekrety):
TG_PRO_CHANNEL = "-1004436927192"   # TVC Fusion PRO (płatny)
TG_FREE_CHANNEL = "-1004341989751"  # TVC Fusion Signals (darmowy)
# PRO = pełny sygnał natychmiast, FREE = stripped (bez cen/SL/TP/score).

_TELEGRAM_LAST_ERROR = None   # ostatni błąd wysyłki — trafia do health w gist (widoczny w terminalu)
_TELEGRAM_LAST_OK = None      # ISO czasu ostatniej udanej wysyłki

def _tg_ssl():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()

def _telegram_send(text: str, chat_id: str | None = None) -> bool:
    """Wyślij wiadomość na jeden chat/kanał. Domyślnie: osobisty chat z env."""
    global _TELEGRAM_LAST_ERROR, _TELEGRAM_LAST_OK
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not chat_id:
        chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return False
    import urllib.request as ur
    import urllib.parse as up
    import urllib.error as ue
    ssl_ctx = _tg_ssl()
    try:
        data = up.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": "true"}).encode()
        req = ur.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with ur.urlopen(req, timeout=10, context=ssl_ctx) as resp:
            ok = resp.status == 200
            if ok:
                _TELEGRAM_LAST_OK = datetime.now(timezone.utc).isoformat()
                _TELEGRAM_LAST_ERROR = None
            return ok
    except ue.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        _TELEGRAM_LAST_ERROR = f"HTTP {e.code}: {body}"
    except Exception as e:
        _TELEGRAM_LAST_ERROR = f"{type(e).__name__}: {e}"
    print(f"[telegram] send failed ({chat_id[-4:]}): {_TELEGRAM_LAST_ERROR}")
    return False


def _telegram_broadcast(pro_text: str, free_text: str | None = None):
    """Wyślij na 3 kanały: osobisty + PRO (pełny) + FREE (jeśli podany)."""
    ok = _telegram_send(pro_text)           # osobisty chat — pełna wersja
    _telegram_send(pro_text, TG_PRO_CHANNEL)  # PRO kanał — pełna wersja
    if free_text:
        _telegram_send(free_text, TG_FREE_CHANNEL)  # FREE kanał — ograniczona wersja
    return ok


def _telegram_api(method: str, params: dict | None = None):
    """Wywołanie Bot API bez wysyłania wiadomości (getMe / getChat). Zwraca (ok, info)."""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return False, "brak TELEGRAM_BOT_TOKEN"
    import urllib.request as ur
    import urllib.parse as up
    import urllib.error as ue
    import ssl
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
    try:
        url = f"https://api.telegram.org/bot{token}/{method}"
        if params:
            url += "?" + up.urlencode(params)
        with ur.urlopen(url, timeout=10, context=ssl_ctx) as resp:
            data = json.loads(resp.read())
            return bool(data.get("ok")), data.get("result")
    except ue.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _fmt_px(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.0f}" if v >= 100 else f"{v:.2f}" if v >= 1 else f"{v:.4f}"


def _notify_open(ticker, direction, entry, size_usd, sl, tp1, tp2, score, regime, override=False):
    """Telegram: otwarcie pozycji. PRO = natychmiast (pełny). FREE = kolejkowany z 15-min opóźnieniem."""
    try:
        dir_label = "🟢 LONG" if direction == "long" else "🔴 SHORT"
        # R:R ratio
        risk = abs(entry - sl) if sl else 0
        reward1 = abs(tp1 - entry) if tp1 else 0
        reward2 = abs(tp2 - entry) if tp2 else 0
        rr1 = f"1:{reward1/risk:.1f}" if risk > 0 else "—"
        rr2 = f"1:{reward2/risk:.1f}" if risk > 0 else "—"
        # Regime context
        regime_label = {
            "TRENDING_UP": "Trending Up ▲", "TRENDING_UP_VOLATILE": "Trending Up ⚡",
            "TRENDING_DOWN": "Trending Down ▼", "TRENDING_DOWN_VOLATILE": "Trending Down ⚡",
            "RANGING": "Range-bound ↔", "CRASH": "Crash ⛔",
        }.get(regime, regime or "—")
        pro_lines = [
            f"📈 <b>NEW TRADE — {ticker}</b>",
            "",
            f"<b>{dir_label}</b> @ <code>{_fmt_px(entry)}</code>",
            f"Size: <b>${size_usd:.0f}</b> · Score: <b>{score}</b>",
            "",
            f"SL: <code>{_fmt_px(sl)}</code>",
            f"TP1: <code>{_fmt_px(tp1)}</code>  (R:R {rr1})",
            f"TP2: <code>{_fmt_px(tp2)}</code>  (R:R {rr2})",
            "",
            f"Regime: {regime_label}{' · ⚡ CHoCH override' if override else ''}",
            f"✅ Smart Money confirmed (≥2/3 layers aligned)",
        ]
        pro_text = "\n".join(pro_lines)
        _telegram_broadcast(pro_text)  # osobisty + PRO — natychmiast

        # FREE: kolejkuj z 15-min opóźnieniem (pełne dane, max 1/dzień)
        try:
            conn = db()
            _queue_free_signal(conn, {
                "ticker": ticker, "direction": direction,
                "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
                "score": score, "regime": regime, "size_usd": size_usd,
            })
            conn.close()
        except Exception as qe:
            print(f"[telegram] queue_free failed: {qe}")
    except Exception as e:
        print(f"[telegram] notify_open failed: {e}")


def _notify_close(ticker, direction, entry, exit_price, pnl_pct, pnl_usd, reason):
    """Telegram: zamknięcie pozycji — osobisty + PRO (pełny) + FREE (wynik + CTA)."""
    try:
        exit_label = {"hit_tp1": "🎯 TP1 Hit", "hit_tp2": "🎯🎯 TP2 Hit", "hit_sl": "🛑 Stop Loss",
                      "hit_trailing_sl": "📈 Trailing Stop", "flip_choch": "🔁 Flip Exit", "sl_rescan_bug": "🐛 SL re-scan",
                      "manual_close": "✋ Manual Close", "zombie_expired": "⏰ Max Hold Expired"}.get(reason, reason)
        res = "✅" if pnl_usd > 0 else "❌" if pnl_usd < 0 else "➖"
        pro_lines = [
            f"{res} <b>CLOSED — {ticker} {direction.upper()}</b>",
            "",
            f"Exit reason: <b>{exit_label}</b>",
            f"Entry: <code>{_fmt_px(entry)}</code> → Exit: <code>{_fmt_px(exit_price)}</code>",
            f"P&L: <b>{pnl_pct:+.2f}%</b> (${pnl_usd:+.2f})",
        ]
        pro_text = "\n".join(pro_lines)
        _telegram_broadcast(pro_text)  # osobisty + PRO

        # FREE: wynik zamknięcia + statystyki + CTA
        _send_free_close_report(ticker, direction, pnl_pct, pnl_usd, exit_label, res)
    except Exception as e:
        print(f"[telegram] notify_close failed: {e}")


def _send_free_close_report(ticker, direction, pnl_pct, pnl_usd, exit_label, res_emoji):
    """Send trade close result to FREE channel — shows PnL as social proof + CTA to join PRO."""
    try:
        # Pobierz aktualne statystyki z DB
        conn = db()
        stats = _compute_equity_stats(conn) or {}
        conn.close()

        total_closed = stats.get("total_closed", 0)
        wins = stats.get("wins", 0)
        wr = (wins / total_closed * 100) if total_closed else 0
        total_pnl = stats.get("total_pnl_usd", 0) or 0
        pf = stats.get("profit_factor")

        lines = [
            f"{res_emoji} <b>TRADE CLOSED — {ticker} {direction.upper()}</b>",
            "",
            f"Result: <b>{exit_label}</b>",
            f"P&L: <b>{pnl_pct:+.2f}%</b> (${pnl_usd:+.2f})",
        ]

        # Dodaj statystyki jako social proof
        if total_closed >= 5:
            stats_line = f"📊 Track record: <b>{total_closed} trades</b> · WR <b>{wr:.0f}%</b>"
            if pf and pf > 0:
                stats_line += f" · PF <b>{pf:.2f}</b>"
            lines.append("")
            lines.append(stats_line)
            if total_pnl != 0:
                lines.append(f"💰 Total P&L: <b>${total_pnl:+,.2f}</b>")

        lines.extend([
            "",
            "🔓 <b>Get all signals instantly → TVC Fusion PRO</b>",
            "<i>$29/mo · Cancel anytime · Full access</i>",
        ])

        free_text = "\n".join(lines)
        _telegram_send(free_text, TG_FREE_CHANNEL)
        print(f"[free-close] ✅ sent to FREE: {ticker} {pnl_pct:+.2f}%")
    except Exception as e:
        print(f"[free-close] ❌ send failed: {e}")


def cmd_telegram_test(args):
    """Weryfikacja Telegrama. Domyślnie CICHA (getMe + getChat — nic nie wysyła);
    z flagą --send wysyła wiadomość testową. Wynik trafia do meta → health → terminal.
    Testuje WSZYSTKIE 3 kanały: osobisty, PRO, FREE."""
    global _TELEGRAM_LAST_ERROR
    tok = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    cid = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    print(f"[telegram-test] token: {'OK (' + str(len(tok)) + ' znaków)' if tok else 'BRAK'} · chat_id: {cid or 'BRAK'}")
    if getattr(args, "send", False):
        ok = _telegram_send("🧪 <b>TVC Alerts — test z bota</b>\nJeśli to widzisz, powiadomienia działają.")
        result = "OK (wysłano)" if ok else f"FAIL {_TELEGRAM_LAST_ERROR}"
    else:
        ok_me, me = _telegram_api("getMe")
        if not ok_me:
            ok, result = False, f"FAIL token: {me}"
        else:
            bot_user = (me or {}).get("username", "?")
            # Test osobisty chat
            ok_chat, chat = _telegram_api("getChat", {"chat_id": cid})
            if not ok_chat:
                ok, result = False, f"FAIL personal chat: {chat}"
            else:
                who = (chat or {}).get("username") or (chat or {}).get("first_name") or cid
                result = f"OK (@{bot_user} → {who})"
                ok = True

            # Test PRO channel
            ok_pro, pro_info = _telegram_api("getChat", {"chat_id": TG_PRO_CHANNEL})
            pro_name = (pro_info or {}).get("title", "?") if ok_pro else f"FAIL: {pro_info}"
            print(f"[telegram-test] PRO channel ({TG_PRO_CHANNEL}): {'✅ ' + pro_name if ok_pro else '❌ ' + pro_name}")

            # Test FREE channel
            ok_free, free_info = _telegram_api("getChat", {"chat_id": TG_FREE_CHANNEL})
            free_name = (free_info or {}).get("title", "?") if ok_free else f"FAIL: {free_info}"
            print(f"[telegram-test] FREE channel ({TG_FREE_CHANNEL}): {'✅ ' + free_name if ok_free else '❌ ' + free_name}")

            # Append channel status to result
            result += f" | PRO: {'OK' if ok_pro else 'FAIL'} | FREE: {'OK' if ok_free else 'FAIL'}"

    print(f"[telegram-test] {result}")
    try:
        db_init()
        conn = db()
        _meta_set(conn, "telegram_test_result", f"{datetime.now(timezone.utc).strftime('%H:%M')}Z {result}")
        conn.close()
    except Exception as e:
        print(f"[telegram-test] meta write failed: {e}")


def _meta_get(conn, key, default=None):
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(conn, key, value):
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


# --- FREE channel: opóźnione sygnały (15 min) z pełnymi danymi, max 2/dzień ---

FREE_DELAY_SECONDS = 15 * 60   # 15 minut opóźnienia vs PRO
FREE_MAX_SIGNALS_PER_DAY = 1   # max 1 trade signal dziennie na FREE


def _queue_free_signal(conn, signal_data: dict):
    """Dodaj sygnał do kolejki FREE (meta key = free_pending). Wysyłka po 15 min."""
    raw = _meta_get(conn, "free_pending", "[]")
    try:
        queue = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        queue = []
    signal_data["queued_at"] = datetime.now(timezone.utc).isoformat()
    queue.append(signal_data)
    _meta_set(conn, "free_pending", json.dumps(queue))
    print(f"[free-queue] dodano sygnał {signal_data.get('ticker')} — w kolejce: {len(queue)}")


def _process_free_queue(conn):
    """Sprawdź kolejkę FREE i wyślij sygnały starsze niż 15 min (max 1/dzień)."""
    raw = _meta_get(conn, "free_pending", "[]")
    try:
        queue = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        queue = []
    if not queue:
        print("[free-queue] kolejka pusta — nic do przetworzenia")
        return
    print(f"[free-queue] znaleziono {len(queue)} sygnał(ów) w kolejce")

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    # Licznik wysłanych dziś
    sent_date = _meta_get(conn, "free_sent_date", "")
    sent_count = int(_meta_get(conn, "free_sent_count", "0")) if sent_date == today else 0

    remaining = []
    for sig in queue:
        queued_at_str = sig.get("queued_at", "")
        try:
            queued_at = datetime.fromisoformat(queued_at_str)
            if queued_at.tzinfo is None:
                queued_at = queued_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            # Nie da się sparsować — wyrzuć z kolejki
            print(f"[free-queue] pominięto sygnał z nieprawidłowym queued_at: {queued_at_str}")
            continue

        elapsed = (now - queued_at).total_seconds()
        if elapsed < FREE_DELAY_SECONDS:
            # Za wcześnie — zostaje w kolejce
            remaining.append(sig)
            continue

        if sent_count >= FREE_MAX_SIGNALS_PER_DAY:
            # Limit dzienny osiągnięty — wyrzuć z kolejki (nie wysyłaj, nie kumuluj)
            print(f"[free-queue] limit {FREE_MAX_SIGNALS_PER_DAY}/dzień osiągnięty — pomijam {sig.get('ticker')}")
            continue

        # Wyślij na FREE kanał z pełnymi danymi + notą o opóźnieniu
        _send_free_delayed_signal(sig)
        sent_count += 1
        _meta_set(conn, "free_sent_date", today)
        _meta_set(conn, "free_sent_count", str(sent_count))

    # Zaktualizuj kolejkę (zostają tylko te, które jeszcze nie minęły 15 min)
    _meta_set(conn, "free_pending", json.dumps(remaining))
    if queue:
        print(f"[free-queue] przetworzone: {len(queue) - len(remaining)}, pozostało: {len(remaining)}, wysłane dziś: {sent_count}")


def _send_free_delayed_signal(sig):
    """Send delayed signal to FREE channel — same format as PRO + delay note + CTA."""
    ticker = sig.get("ticker", "???")
    direction = sig.get("direction", "long")
    entry = sig.get("entry", 0)
    sl = sig.get("sl", 0)
    tp1 = sig.get("tp1", 0)
    tp2 = sig.get("tp2", 0)
    score = sig.get("score", 0)
    regime = sig.get("regime", "")
    size_usd = sig.get("size_usd", 0)

    dir_label = "🟢 LONG" if direction == "long" else "🔴 SHORT"

    # R:R ratios
    risk = abs(entry - sl) if sl else 0
    reward1 = abs(tp1 - entry) if tp1 else 0
    reward2 = abs(tp2 - entry) if tp2 else 0
    rr1 = f"1:{reward1/risk:.1f}" if risk > 0 else "—"
    rr2 = f"1:{reward2/risk:.1f}" if risk > 0 else "—"

    regime_label = {
        "TRENDING_UP": "Trending Up ▲", "TRENDING_UP_VOLATILE": "Trending Up ⚡",
        "TRENDING_DOWN": "Trending Down ▼", "TRENDING_DOWN_VOLATILE": "Trending Down ⚡",
        "RANGING": "Range-bound ↔", "CRASH": "Crash ⛔",
    }.get(regime, regime or "—")

    size_str = f"${size_usd:,.0f}" if size_usd else ""
    size_line = f"Size: <b>{size_str}</b> · Score: <b>{score}</b>" if size_str else f"Score: <b>{score}</b>"

    lines = [
        f"📈 <b>NEW TRADE — {ticker}</b>",
        "",
        f"<b>{dir_label}</b> @ <code>{_fmt_px(entry)}</code>",
        size_line,
        "",
        f"SL: <code>{_fmt_px(sl)}</code>",
        f"TP1: <code>{_fmt_px(tp1)}</code>  (R:R {rr1})",
        f"TP2: <code>{_fmt_px(tp2)}</code>  (R:R {rr2})",
        "",
        f"Regime: {regime_label}",
        f"✅ Smart Money confirmed (≥2/3 layers aligned)",
        "",
        f"⏱ <i>Signal delayed 15 min vs PRO channel</i>",
        f"🔓 <b>Get all signals instantly → TVC Fusion PRO</b>",
        f"<i>$29/mo · Cancel anytime · Full access</i>",
    ]
    free_text = "\n".join(lines)
    try:
        _telegram_send(free_text, TG_FREE_CHANNEL)
        print(f"[free-queue] ✅ sent to FREE: {ticker} {direction}")
    except Exception as e:
        print(f"[free-queue] ❌ send failed: {e}")


DAILY_DIGEST_HOUR_UTC = 6   # 08:00 CEST / 07:00 CET

def _maybe_daily_digest(conn):
    """Raz dziennie (po 06:00 UTC) wysyła podsumowanie ostatnich 24h na Telegram."""
    try:
        _maybe_daily_digest_inner(conn)
    except Exception as e:
        print(f"[digest] failed: {type(e).__name__}: {e}")
        try:
            _meta_set(conn, "digest_last_error", f"{type(e).__name__}: {e}")
        except Exception:
            pass


def _maybe_daily_digest_inner(conn):
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour < DAILY_DIGEST_HOUR_UTC or _meta_get(conn, "last_digest_date") == today:
        return
    since = (now - timedelta(hours=24)).isoformat()
    closed = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE status='closed' AND closed_at >= ? AND COALESCE(excluded,0)=0", (since,)).fetchall()]
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
    # FREE kanał dostaje wyniki PRO jako social proof + CTA
    free_lines = [f"📊 <b>TVC Fusion PRO — Daily Results</b>", ""]
    if closed:
        free_lines.append(f"Closed today: <b>{len(closed)}</b> trades ({wins}W/{len(closed)-wins}L)")
        free_lines.append(f"Today's P&L: <b>${pnl:+,.2f}</b>")
        # Pokaż top 3 zamknięcia jako social proof
        top_closed = sorted(closed, key=lambda r: abs(r.get("pnl_usd") or 0), reverse=True)[:3]
        for tc in top_closed:
            tc_res = "✅" if (tc.get("pnl_usd") or 0) > 0 else "❌"
            free_lines.append(f"  {tc_res} {tc['ticker']} {tc.get('direction','long')[0].upper()} → <b>{tc.get('pnl_pct', 0) or 0:+.2f}%</b>")
    else:
        free_lines.append("No trades closed today.")
    if stats and stats.get("total_closed"):
        tot_pnl_free = stats.get("total_pnl_usd", 0) or 0
        wr_free = (stats.get("wins", 0) / stats["total_closed"] * 100) if stats["total_closed"] else 0
        pf_free = stats.get("profit_factor")
        free_lines.append("")
        free_lines.append(f"📈 All-time: <b>{stats['total_closed']} trades</b> · WR <b>{wr_free:.0f}%</b>"
                          + (f" · PF <b>{pf_free:.2f}</b>" if pf_free else ""))
    free_lines.extend([
        "",
        "🔓 <b>Get all signals instantly → TVC Fusion PRO</b>",
        "<i>$29/mo · Cancel anytime · Full access</i>",
    ])
    try:
        pro_text = "\n".join(lines)
        free_text = "\n".join(free_lines)
        if _telegram_broadcast(pro_text, free_text):
            _meta_set(conn, "last_digest_date", today)
            _meta_set(conn, "digest_last_error", "")
        else:
            _meta_set(conn, "digest_last_error", f"send failed: {_TELEGRAM_LAST_ERROR}")
    except Exception as e:
        _meta_set(conn, "digest_last_error", f"{type(e).__name__}: {e}")
        raise


MACRO_BLACKOUT_BEFORE_H = 3.0
MACRO_BLACKOUT_AFTER_H = 1.0


def _macro_blackout(data, now_utc):
    """Zwraca opis eventu jeśli jesteśmy w oknie blackoutu tier-1, inaczej None."""
    for ev in data.get("macro_events") or []:
        if int(ev.get("tier", 2)) != 1:
            continue
        try:
            ts = datetime.fromisoformat(ev["ts_utc"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        delta_h = (ts - now_utc).total_seconds() / 3600
        if -MACRO_BLACKOUT_AFTER_H <= delta_h <= MACRO_BLACKOUT_BEFORE_H:
            when = f"za {delta_h * 60:.0f} min" if delta_h >= 0 else f"{-delta_h * 60:.0f} min temu"
            return f"{ev.get('name', 'tier-1 event')} ({when})"
    return None


def _context_tags(flags, now_utc, data):
    """Tagi kontekstu wejścia — do audytu 'co działa' (WR wg kontekstu w panelu)."""
    tags = list(flags or [])
    if now_utc.weekday() in (5, 6) and "weekend" not in tags:
        tags.append("weekend")
    h = now_utc.hour
    tags.append("us_session" if 13 <= h < 21 else "eu_session" if 7 <= h < 13 else "asia_session")
    # makro w ciągu 24h (nie w blackoucie, ale blisko)
    try:
        for ev in data.get("macro_events") or []:
            if int(ev.get("tier", 2)) != 1:
                continue
            ts = datetime.fromisoformat(str(ev.get("ts_utc")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if 0 < (ts - now_utc).total_seconds() < 24 * 3600:
                tags.append("macro_24h")
                break
    except Exception:
        pass
    return tags


def _cancel_pending(conn, pid, reason):
    conn.execute("UPDATE positions SET status='cancelled', hit_or_miss=?, closed_at=?, excluded=1 WHERE id=?",
                 (reason, datetime.now(timezone.utc).isoformat(), pid))
    conn.commit()
    print(f"[pending] #{pid} anulowane: {reason}")


def _notify_pending(ticker, direction, limit_px, zone_low, zone_high, sl, tp1, note):
    try:
        _telegram_send(f"⏳ <b>OCZEKUJĄCE {direction.upper()} {ticker}</b> limit {_fmt_px(limit_px)} "
                       f"(strefa {_fmt_px(zone_low)}–{_fmt_px(zone_high)})\nSL {_fmt_px(sl)} · TP1 {_fmt_px(tp1)}\n{note}")
    except Exception as e:
        print(f"[telegram] pending notify failed: {e}")


def _fill_pending(conn):
    """v0.4 — realizacja wejść oczekujących: long wypełnia się, gdy low świecy ≤ limit;
    short, gdy high ≥ limit. Po TTL — wygasa (poza statystykami)."""
    rows = conn.execute("SELECT * FROM positions WHERE status='pending'").fetchall()
    now = datetime.now(timezone.utc)
    for r in rows:
        try:
            until = datetime.fromisoformat(r["pending_until"]) if r["pending_until"] else None
            if until and until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except Exception:
            until = None
        try:
            ohlc = _fetch_klines_binance(r["ticker"], r["opened_at"], "5m")
        except Exception as e:
            print(f"[pending] {r['ticker']}: klines failed ({e})")
            continue
        limit_px = float(r["entry_price"])
        direction = (r["direction"] or "long").lower()
        fill_ts = None
        for ts, _o, h, l, _c, _v in ohlc or []:
            if (direction == "long" and l <= limit_px) or (direction == "short" and h >= limit_px):
                fill_ts = ts
                break
        if fill_ts:
            fill_dt = datetime.fromtimestamp(fill_ts / 1000, tz=timezone.utc).isoformat()
            conn.execute("UPDATE positions SET status='open', opened_at=?, sl_since=?, pending_until=NULL WHERE id=?",
                         (fill_dt, fill_ts, r["id"]))
            conn.commit()
            print(f"[fill] {direction.upper()} {r['ticker']} @ {limit_px:.4f} (oczekujące zrealizowane {fill_dt[:16]})")
            _notify_open(r["ticker"], direction, limit_px, r["size_usd"], r["sl_price"], r["tp1_price"],
                         r["tp2_price"], r["fusion_score"], r["regime"])
        elif until and now > until:
            _cancel_pending(conn, r["id"], "expired")


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

    # v0.4 — MAKRO BLACKOUT. 4.09.2026: bot otworzył BTC long na dźwigniowej pompie, dzień
    # później NFP zrobił −2.5% w jednej świecy. Nowe wejścia (oba kierunki) zablokowane
    # od 3h przed do 1h po tier-1 evencie (NFP/CPI/PCE/FOMC z macro_events w fusion).
    # Otwarte pozycje żyją dalej — SL/TP/trailing robią swoje.
    blackout = _macro_blackout(data, now_utc)
    if blackout:
        print(f"[skip] MAKRO BLACKOUT — {blackout} — brak nowych wejść w tym cyklu")
        conn.close()
        return

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

        # v1.0 — REGUŁA #1: REGIME-AWARE SCORE GATE
        # W TRENDING_UP łatwiej otworzyć LONG (55), w TRENDING_DOWN trudniej (62+)
        # v1.0 fix: read regime from decision dict first, fall back to top-level data.get("regime")
        regime = dec.get("regime") or data.get("regime") or "RANGING"
        min_long = MIN_LONG_SCORE_BY_REGIME.get(regime, MIN_LONG_SCORE)
        if direction == "long":
            if score < min_long:
                print(f"[skip] {ticker} LONG score {score} < {min_long} (regime={regime}) — za niska konwikcja")
                skipped += 1
                continue
        else:
            if score > MAX_SHORT_SCORE:
                print(f"[skip] {ticker} SHORT score {score} > {MAX_SHORT_SCORE} — score nie jest bearish")
                skipped += 1
                continue

        # v0.5 — REGUŁA #2: SMART MONEY VETO (layers.veto != None → skip)
        layers = dec.get("layers") or {}
        veto = layers.get("veto")
        if veto:
            print(f"[skip] {ticker} {direction.upper()} — Smart Money VETO: {veto}")
            skipped += 1
            continue

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

        # v0.5 — DE-DUPE: jeśli ticker ma otwartą pozycję (dowolny kierunek) → skip.
        # Żadnych flipów — SL/TP zamkną starą pozycję, nowa wejdzie w następnym cyklu.
        existing_any = conn.execute(
            "SELECT id, direction FROM positions WHERE ticker=? AND status='open' LIMIT 1",
            (ticker,),
        ).fetchone()
        if existing_any:
            skipped += 1
            continue

        # v0.5 — WEJŚCIE PO CENIE RYNKOWEJ (bez pending orders / entry_quality)
        try:
            entry_price = _fetch_current_price(ticker)
        except Exception as e:
            print(f"[warn] {ticker} live price failed ({e}) — fallback na środek strefy")
            entry_price = _mid_entry(dec.get("entry_low"), dec.get("entry_high"), ticker, ex)
        if not entry_price:
            skipped += 1
            continue

        # v0.6 — REGUŁA #1b: R:R GUARD w momencie wejścia.
        # Fusion generuje TP/SL na podstawie ceny w momencie skanu, ale bot wchodzi
        # po cenie rynkowej (która może być wyższa/niższa). Jeśli cena przeszła
        # bliżej TP niż SL, R:R spada poniżej 1:1 → skip. (Fix: SOL 11.09, R:R=0.0)
        sl_from_json = dec.get("sl")
        tp1_from_json = dec.get("tp1")
        if sl_from_json and tp1_from_json:
            try:
                _sl = float(sl_from_json)
                _tp = float(tp1_from_json)
                _risk = abs(entry_price - _sl)
                _reward = abs(_tp - entry_price)
                _live_rr = _reward / max(1e-12, _risk)
                if _live_rr < MIN_RR_AT_ENTRY:
                    print(f"[skip] {ticker} {direction.upper()} — R:R at live price = {_live_rr:.2f} "
                          f"(< {MIN_RR_AT_ENTRY}) entry={entry_price} SL={_sl} TP1={_tp}")
                    skipped += 1
                    continue
                # Also check TP is on the right side of entry
                if direction == "long" and _tp <= entry_price:
                    print(f"[skip] {ticker} LONG — TP1 {_tp} <= entry {entry_price} (stale levels)")
                    skipped += 1
                    continue
                if direction == "short" and _tp >= entry_price:
                    print(f"[skip] {ticker} SHORT — TP1 {_tp} >= entry {entry_price} (stale levels)")
                    skipped += 1
                    continue
            except (TypeError, ValueError):
                pass  # missing/bad levels — proceed, SL/TP will be set to None

        # v0.9 — REGUŁA #6: FIBONACCI PULLBACK FILTER (regime-aware)
        # Don't chase! Threshold depends on market regime: trending markets
        # allow entries closer to 30d high (0.90), ranging markets require
        # deeper pullback (0.70), downtrends even more (0.50).
        fib_pos = dec.get("fib_position")
        if fib_pos is not None:
            try:
                fib_pos = float(fib_pos)
                fib_long_thresh = FIB_THRESHOLD_BY_REGIME.get(regime, FIB_THRESHOLD_DEFAULT)
                fib_short_thresh = 1.0 - fib_long_thresh  # mirror: 0.90→0.10, 0.70→0.30, 0.50→0.50
                if direction == "long" and fib_pos > fib_long_thresh:
                    print(f"[skip] {ticker} LONG — chasing: price at {fib_pos:.0%} of 30d range "
                          f"(above {fib_long_thresh:.0%} Fib threshold for {regime})")
                    skipped += 1
                    continue
                if direction == "short" and fib_pos < fib_short_thresh:
                    print(f"[skip] {ticker} SHORT — chasing bottom: price at {fib_pos:.0%} of 30d range "
                          f"(below {fib_short_thresh:.0%} Fib threshold for {regime})")
                    skipped += 1
                    continue
            except (TypeError, ValueError):
                pass

        # v1.0 — REGUŁA #7: 5-MINUTE MICRO-CONFIRMATION
        # Don't enter blind! Check 5m candles for momentum confirmation:
        # ≥1 green candle in last 2 (bounce) + RSI(14) > 35 (not freefall).
        try:
            confirm_ok, confirm_reason = _check_5m_confirmation(ticker, direction)
            if not confirm_ok:
                print(f"[skip] {ticker} {direction.upper()} — no 5m confirmation: {confirm_reason}")
                skipped += 1
                continue
        except Exception as e:
            print(f"[5m-confirm] {ticker} — exception: {e}, proceeding anyway")

        # v0.8 — Tiered sizing: score determines position size
        if direction == "short":
            size_pct = SHORT_SIZE_CAP_PCT
        else:
            size_pct = 8.0  # fallback
            for (lo, hi), pct in TIERED_LONG_SIZES.items():
                if lo <= score < hi:
                    size_pct = pct
                    break
        print(f"[size] {ticker} {direction.upper()} score={score} → {size_pct}% of capital")
        size_usd = PAPER_CAPITAL * (size_pct / 100)

        sources = dec.get("sources", {})
        row = (
            trade_date, ticker, EXCHANGE_ID, dec.get("action"), direction,
            dec.get("score"), regime,
            sources.get("onchain"), sources.get("technical"), sources.get("news"),
            sources.get("momentum"), sources.get("sentiment"),
            entry_price, size_pct, size_usd,
            dec.get("sl"), dec.get("tp1"), dec.get("tp2"),
            "open",
            None, None, None, None, None,  # exit_price, exit_date, pnl_pct, pnl_usd, hit_or_miss
            dec.get("risk_flag", ""), dec.get("invalidation_note", ""), dec.get("risk_flag", ""),
            1 if dec.get("onchain_data_thin") else 0,
            datetime.now(timezone.utc).isoformat(),
            None,  # closed_at
            None,  # context_tags (v0.5: removed entry_quality tags)
            None, None,  # entry_zone_low, entry_zone_high (v0.5: no pending)
            (dec.get("levels") or {}).get("rr"),
        )
        conn.execute(
            """INSERT INTO positions (
                date, ticker, exchange, action, direction, fusion_score, regime,
                onchain_score, technical_score, news_score, momentum_score, sentiment_score,
                entry_price, size_pct, size_usd, sl_price, tp1_price, tp2_price,
                status, exit_price, exit_date, pnl_pct, pnl_usd, hit_or_miss,
                thesis, invalidation, risk_flag, onchain_data_thin,
                opened_at, closed_at, context_tags, entry_zone_low, entry_zone_high, rr
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )
        opened += 1
        opened_today += 1
        arrow = "↗" if direction == "long" else "↘"
        print(f"[open] {arrow} {direction.upper():5s} {ticker} @ {entry_price:.4f}  size ${size_usd:.2f}  "
              f"SL {dec.get('sl')} TP1 {dec.get('tp1')} TP2 {dec.get('tp2')}  "
              f"score {dec.get('score')}")
        _notify_open(ticker, direction, entry_price, size_usd, dec.get("sl"), dec.get("tp1"),
                     dec.get("tp2"), dec.get("score"), regime, False)

    conn.commit()
    conn.close()
    print(f"[open] done — opened {opened}, skipped {skipped}")


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
TRAILING_DISTANCE = 2.5   # SL follows 2.5% below current price (v0.7: widened from 1.5%, crypto moves 2% on a sneeze)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _iso_to_ms(iso: str) -> int:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


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
                "UPDATE positions SET sl_price = ?, sl_since = ? WHERE id = ?",
                (breakeven_sl, _now_ms(), row["id"])
            )
            return (breakeven_sl, f"breakeven+ (profit {profit_pct:+.2f}%)")

        # Stage 2: Trailing SL (po +3% profit)
        if profit_pct >= TRAILING_TRIGGER:
            trailing_sl = current_price * (1 - TRAILING_DISTANCE / 100)
            if trailing_sl > current_sl:
                conn.execute(
                    "UPDATE positions SET sl_price = ?, sl_since = ? WHERE id = ?",
                    (trailing_sl, _now_ms(), row["id"])
                )
                return (trailing_sl, f"trailing +{profit_pct:.1f}% profit, SL={trailing_sl:.4f}")

    else:  # SHORT (rzadkie w regime TRENDING_UP)
        profit_pct = (entry - current_price) / entry * 100
        breakeven_sl = entry * 0.999
        if profit_pct >= BREAKEVEN_TRIGGER and (current_sl > breakeven_sl or current_sl == 0):
            conn.execute("UPDATE positions SET sl_price = ?, sl_since = ? WHERE id = ?", (breakeven_sl, _now_ms(), row["id"]))
            return (breakeven_sl, f"SHORT breakeven+ (profit {profit_pct:+.2f}%)")
        if profit_pct >= TRAILING_TRIGGER:
            trailing_sl = current_price * (1 + TRAILING_DISTANCE / 100)
            if trailing_sl < current_sl or current_sl == 0:
                conn.execute("UPDATE positions SET sl_price = ?, sl_since = ? WHERE id = ?", (trailing_sl, _now_ms(), row["id"]))
                return (trailing_sl, f"SHORT trailing +{profit_pct:.1f}% profit")

    return None


def _snapshot_equity(conn, open_rows):
    """Zapisz current equity state do equity_snapshots table."""
    now_iso = datetime.now(timezone.utc).isoformat()
    realized = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) FROM positions WHERE status='closed' AND COALESCE(excluded,0)=0"
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
    _fill_pending(conn)   # v0.4 — najpierw realizacja wejść oczekujących
    open_rows = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()

    # Equity snapshot (regardless czy są open positions czy nie)
    _snapshot_equity(conn, open_rows)

    if not open_rows:
        print("[check] no open positions")
        _maybe_daily_digest(conn)
        conn.close()
        return

    # v0.9 — MAX HOLD TIME: zamknij zombie pozycje starsze niż MAX_HOLD_DAYS.
    # Pozycja, która nie trafiła SL ani TP przez 5 dni, prawdopodobnie ma
    # nieaktualne poziomy — zamykamy po aktualnej cenie rynkowej.
    now_utc = datetime.now(timezone.utc)
    for r in open_rows:
        try:
            opened = datetime.fromisoformat(r["opened_at"])
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            age_days = (now_utc - opened).total_seconds() / 86400
        except Exception:
            continue
        if age_days > MAX_HOLD_DAYS:
            direction = (r["direction"] or "long").lower()
            try:
                exit_price = _fetch_current_price(r["ticker"])
            except Exception:
                print(f"[zombie] {r['ticker']} — can't fetch price, skipping auto-close")
                continue
            if direction == "long":
                pnl_pct = (exit_price - r["entry_price"]) / r["entry_price"] * 100
            else:
                pnl_pct = (r["entry_price"] - exit_price) / r["entry_price"] * 100
            pnl_usd = r["size_usd"] * (pnl_pct / 100)
            close_ts = now_utc.isoformat()
            conn.execute(
                """UPDATE positions SET status='closed', exit_price=?, exit_date=?,
                   pnl_pct=?, pnl_usd=?, hit_or_miss=?, closed_at=? WHERE id=?""",
                (exit_price, close_ts, pnl_pct, pnl_usd, "max_hold", close_ts, r["id"]),
            )
            conn.commit()
            print(f"[zombie] {r['ticker']} ({direction}) auto-closed after {age_days:.0f} days  "
                  f"entry {r['entry_price']:.4f} → exit {exit_price:.4f}  PnL {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
            _notify_close(r["ticker"], direction, r["entry_price"], exit_price, pnl_pct, pnl_usd, "max_hold")

    # Re-fetch open rows after zombie cleanup
    open_rows = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    if not open_rows:
        print("[check] all positions closed (zombie cleanup)")
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
        # v0.4 — SL obowiązuje tylko dla świec otwartych PO ostatniej zmianie SL.
        # Bez tego podniesiony (breakeven/trailing) SL "trafiał" w stare świece sprzed
        # podniesienia i zamykał wygrane pozycje wstecznie na +0.1% (sl_rescan_bug).
        try:
            sl_since_ms = int(r["sl_since"]) if r["sl_since"] else _iso_to_ms(r["opened_at"])
        except Exception:
            sl_since_ms = 0

        exit_price = None
        exit_ts = None
        hit_or_miss = None

        for candle in ohlc:  # [ts, o, h, l, c, v]
            ts, _o, h, l, _c, _v = candle
            if direction == "long":
                # LONG: SL = price falls to SL (low <= sl). TP = price rises to TP (high >= tp).
                # Check SL first (conservative — if SL and TP hit same candle, assume SL first).
                if sl and l <= sl and ts >= sl_since_ms:
                    exit_price = sl; exit_ts = ts; hit_or_miss = "hit_sl"; break
                if tp2 and h >= tp2:
                    exit_price = tp2; exit_ts = ts; hit_or_miss = "hit_tp2"; break
                if tp1 and h >= tp1:
                    exit_price = tp1; exit_ts = ts; hit_or_miss = "hit_tp1"; break
            else:  # SHORT
                # SHORT: SL = price rises to SL (high >= sl). TP = price falls to TP (low <= tp).
                # Check SL first (conservative).
                if sl and h >= sl and ts >= sl_since_ms:
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
        f"SELECT COALESCE(SUM(pnl_usd),0) AS pnl FROM positions WHERE {STATS_WHERE}", (STATS_SINCE,)
    ).fetchone()["pnl"]
    total_closed = conn.execute(
        f"SELECT COUNT(*) AS n FROM positions WHERE {STATS_WHERE}", (STATS_SINCE,)
    ).fetchone()["n"]
    wins = conn.execute(
        f"SELECT COUNT(*) AS n FROM positions WHERE {STATS_WHERE} AND pnl_usd > 0", (STATS_SINCE,)
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
            f"SELECT pnl_usd, pnl_pct, hit_or_miss FROM positions WHERE {STATS_WHERE} AND pnl_usd IS NOT NULL "
            "ORDER BY closed_at", (STATS_SINCE,)
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
    all_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE status='closed' AND closed_at IS NOT NULL ORDER BY closed_at"
    ).fetchall()]
    # v0.4 — bucketowanie tylko po trade'ach v0.3+ i niewykluczonych; pełna historia
    # (v0.2 ping-pong CHoCH, sl_rescan_bug) zostaje wyłącznie w by_version/excluded.
    rows = [r for r in all_rows if r["opened_at"] >= STATS_SINCE and not (r.get("excluded") or 0)]
    excluded_rows = [r for r in all_rows if (r.get("excluded") or 0)]
    if not all_rows:
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
                   "hit_sl": "SL", "flip_choch": "Flip CHoCH", "manual_close": "Manual",
                   "sl_rescan_bug": "SL re-scan bug (wykluczone)"}
    now = datetime.now(timezone.utc)
    cut7 = (now - timedelta(days=7)).isoformat()
    cut14 = (now - timedelta(days=14)).isoformat()
    last7 = [r for r in rows if r["closed_at"] >= cut7]
    prev7 = [r for r in rows if cut14 <= r["closed_at"] < cut7]
    v03_since = STATS_SINCE
    empty = {"n": 0, "wins": 0, "wr": 0, "pnl": 0, "avg_pnl_pct": 0, "pf": None}
    return {
        "computed_at": now.isoformat(),
        "stats_since": STATS_SINCE,
        "excluded": {"n": len(excluded_rows), "reason": "sl_rescan_bug"},
        "all": agg(rows) if rows else empty,
        "by_score": bucketize(score_bucket, [">=70", "60-69", "50-59", "<50"]),
        "by_regime": bucketize(lambda r: r.get("regime") or "?"),
        "by_direction": bucketize(lambda r: (r.get("direction") or "long").upper(), ["LONG", "SHORT"]),
        "by_exit": bucketize(lambda r: exit_labels.get(r.get("hit_or_miss"), str(r.get("hit_or_miss")))),
        "by_ticker": bucketize(lambda r: r.get("ticker") or "?"),
        # v0.4 — WR wg kontekstu wejścia (tagi z entry_quality + sesja + weekend + makro 24h);
        # trade może mieć kilka tagów, więc sumy nie muszą się zgadzać z "all".
        "by_context": [{"label": tag, **agg(rs)} for tag, rs in sorted(
            ((tag, [r for r in rows if tag in (r.get("context_tags") or "").split(",")])
             for tag in sorted({t for r in rows for t in (r.get("context_tags") or "").split(",") if t})),
            key=lambda x: -len(x[1]))] + (
            [{"label": "bez_tagów (pre-v0.4)", **agg([r for r in rows if not r.get("context_tags")])}]
            if any(not r.get("context_tags") for r in rows) else []),
        "trend": {"last7": agg(last7) if last7 else None, "prev7": agg(prev7) if prev7 else None},
        "by_version": [
            {"label": "v0.2 (przed bramkami, archiwum)", **agg([r for r in all_rows if r["opened_at"] < v03_since and not (r.get("excluded") or 0)])},
            {"label": "v0.3+ (bramki)", **(agg(rows) if rows else empty)},
            {"label": "wykluczone (sl_rescan_bug)", **(agg(excluded_rows) if excluded_rows else empty)},
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
    pending_positions = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE status='pending' ORDER BY opened_at DESC"
    ).fetchall()]
    recent_closed = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE status='closed' AND COALESCE(excluded,0)=0 ORDER BY closed_at DESC LIMIT 20"
    ).fetchall()]
    # v0.4 — nagłówek (PnL / WR / liczba) liczony od bramek v0.3 i bez wykluczonych,
    # a nie z "ostatnich 20" (które były zdominowane przez ping-pong CHoCH z 01–02.09).
    stat_rows = [dict(r) for r in conn.execute(
        f"SELECT pnl_usd FROM positions WHERE {STATS_WHERE}", (STATS_SINCE,)).fetchall()]
    total_pnl = sum((r.get("pnl_usd") or 0) for r in stat_rows)
    # v0.3 — wygrana = dodatni PnL (spójnie z panelem P&L History).
    wins = sum(1 for r in stat_rows if (r.get("pnl_usd") or 0) > 0)
    win_rate = (wins / len(stat_rows) * 100) if stat_rows else 0.0
    equity_history = _get_equity_history(conn, days=30)
    equity_stats = _compute_equity_stats(conn)
    performance = _compute_performance_breakdown(conn)
    last_open_row = conn.execute("SELECT MAX(opened_at) FROM positions").fetchone()[0]
    last_close_row = conn.execute("SELECT MAX(closed_at) FROM positions").fetchone()[0]
    tg_meta = {k: _meta_get(conn, k) for k in ("telegram_test_result", "last_digest_date", "digest_last_error")}
    conn.close()

    # v0.3 — health block: terminal pokazuje "Autopilot: X min temu" i ostrzega gdy
    # cykl milczy. generated_at = heartbeat tego cyklu.
    health = {
        "autopilot_last_cycle": datetime.now(timezone.utc).isoformat(),
        "cycle_interval_min": 5,
        "bot_version": "0.6",
        "telegram_enabled": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
        "telegram_chat_id_tail": (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()[-4:] or None,
        "telegram_pro_channel": TG_PRO_CHANNEL,
        "telegram_free_channel": TG_FREE_CHANNEL,
        "telegram_last_error": _TELEGRAM_LAST_ERROR,
        "telegram_last_ok": _TELEGRAM_LAST_OK,
        "telegram_test_result": tg_meta.get("telegram_test_result"),
        "digest_last_date": tg_meta.get("last_digest_date"),
        "digest_last_error": tg_meta.get("digest_last_error"),
        "last_position_opened": last_open_row,
        "last_position_closed": last_close_row,
        "open_count": len(open_positions),
        "gates": {
            "min_long_score": MIN_LONG_SCORE,
            "max_short_score": MAX_SHORT_SCORE,
            "smart_money_veto": True,
            "macro_blackout": True,
            "reopen_cooldown_min": REOPEN_COOLDOWN_MINUTES,
            "max_trades_per_day": MAX_NEW_TRADES_PER_DAY,
            "tiered_sizing": {str(k): v for k, v in TIERED_LONG_SIZES.items()},
        },
    }

    # Fetch per-token news server-side (Python has no CORS issue)
    news_by_token = _fetch_news_for_tickers(fusion_data.get("decisions", []))
    # Fetch live whale transactions per chain (Blockchair public API, no auth)
    whales_by_token = _fetch_live_whales()

    fusion_data["state"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "open_positions": open_positions,
        "pending_positions": pending_positions,
        "recent_closed": recent_closed,
        "total_pnl_usd": round(total_pnl, 2),
        "cumulative_win_rate": round(win_rate, 1),
        "cumulative_closed": len(stat_rows),
        "stats_since": STATS_SINCE,
        "paper_capital": PAPER_CAPITAL,
        "news": news_by_token,
        "whales_live": whales_by_token,
        "equity_history": equity_history,
        "equity_stats": equity_stats,
        "health": health,
        "performance": performance,
    }

    content = json.dumps(_sanitize_nan(fusion_data), indent=2, default=str)

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
    """Szybki refresh — check + upload + FREE queue (dla auto-loop lub manual)."""
    db_init()
    print("[refresh] check pozycji...")
    try:
        cmd_check(args)
    except Exception as e:
        print(f"[refresh] check failed: {e}")
    # Przetwórz kolejkę opóźnionych sygnałów FREE (15 min delay, max 2/dzień)
    try:
        conn = db()
        _process_free_queue(conn)
        conn.close()
    except Exception as e:
        print(f"[refresh] free queue failed: {e}")
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


def _check_5m_confirmation(ticker: str, direction: str = "long") -> tuple[bool, str]:
    """v1.0 — 5-minute micro-confirmation gate.
    Fetches last 20 candles (5m) from Binance and checks:
      1) At least 1 of the last 2 candles is green (close > open) for longs
         (red for shorts)
      2) RSI(14) on 5m is > 35 for longs (< 65 for shorts) — not in freefall/overshoot
    Returns (passed: bool, reason: str).
    """
    import urllib.request as ur
    import ssl
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()

    sym = _binance_symbol(ticker)
    # Fetch last 20 candles on 5m (no startTime → returns most recent)
    url = (f"https://data-api.binance.vision/api/v3/klines?symbol={sym}"
           f"&interval=5m&limit=20")
    try:
        req = ur.Request(url, headers={"User-Agent": "Mozilla/5.0 tvc-fusion-bot/0.2"})
        with ur.urlopen(req, timeout=10, context=ssl_ctx) as resp:
            raw = json.loads(resp.read())
        candles = [[float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in raw]
        # candles = [open, high, low, close]
    except Exception as e:
        # Network error — don't block trade, just warn
        print(f"[5m-confirm] {ticker} — fetch error: {e}, skipping check (pass)")
        return True, "5m fetch error (pass-through)"

    if len(candles) < 15:
        print(f"[5m-confirm] {ticker} — only {len(candles)} candles, need ≥15 (pass)")
        return True, "insufficient 5m data (pass-through)"

    # --- Check 1: Green candle confirmation ---
    last_2 = candles[-2:]  # last 2 completed candles
    if direction == "long":
        green_count = sum(1 for c in last_2 if c[3] > c[0])  # close > open
        candle_ok = green_count >= 1
        candle_reason = f"{green_count}/2 green candles"
    else:  # short
        red_count = sum(1 for c in last_2 if c[3] < c[0])  # close < open
        candle_ok = red_count >= 1
        candle_reason = f"{red_count}/2 red candles"

    # --- Check 2: RSI(14) on 5m ---
    closes = [c[3] for c in candles]
    rsi_period = 14
    if len(closes) < rsi_period + 1:
        rsi_val = 50.0  # not enough data — neutral
    else:
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        # Initial SMA for first `rsi_period` deltas
        avg_gain = sum(gains[:rsi_period]) / rsi_period
        avg_loss = sum(losses[:rsi_period]) / rsi_period
        # Smoothed (Wilder's) for remaining
        for i in range(rsi_period, len(deltas)):
            avg_gain = (avg_gain * (rsi_period - 1) + gains[i]) / rsi_period
            avg_loss = (avg_loss * (rsi_period - 1) + losses[i]) / rsi_period
        if avg_loss == 0:
            rsi_val = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_val = 100.0 - (100.0 / (1.0 + rs))

    if direction == "long":
        rsi_ok = rsi_val > 35
        rsi_reason = f"RSI(14)={rsi_val:.1f} {'>' if rsi_ok else '≤'} 35"
    else:  # short
        rsi_ok = rsi_val < 65
        rsi_reason = f"RSI(14)={rsi_val:.1f} {'<' if rsi_ok else '≥'} 65"

    passed = candle_ok and rsi_ok
    reason = f"5m confirm: {candle_reason}, {rsi_reason} → {'PASS' if passed else 'FAIL'}"
    print(f"[5m-confirm] {ticker} {direction.upper()} — {reason}")
    return passed, reason


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
    tt = sub.add_parser("telegram-test", help="cicha weryfikacja Telegrama (getMe+getChat); --send wysyła wiadomość testową")
    tt.add_argument("--send", action="store_true", help="wyślij wiadomość testową zamiast cichej weryfikacji")
    close_p = sub.add_parser("close", help="manual close specific ticker (auto-uploads after)")
    close_p.add_argument("ticker", help="Ticker to close (BTC/ETH/SOL/XRP/SUI)")
    loop_p = sub.add_parser("loop", help="background daemon — auto-refresh co N min")
    loop_p.add_argument("--interval", type=int, default=15, help="Minutes between refreshes (default 15)")
    args = p.parse_args()

    handlers = {"init": lambda a: db_init(), "open": cmd_open,
                "check": cmd_check, "eod": cmd_eod, "week": cmd_week,
                "upload": cmd_upload, "close": cmd_close, "refresh": cmd_refresh, "loop": cmd_loop,
                "telegram-test": cmd_telegram_test}
    if not args.cmd:
        p.print_help()
        sys.exit(1)
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
