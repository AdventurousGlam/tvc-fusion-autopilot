#!/usr/bin/env python3
"""
TVC Fusion Weekly Recap v1.1 — Sunday compilation of the week's paper trades.

v1.1 (v4.4, tryb edukacyjny): treści dwujęzyczne PL + EN, bez cen wejścia/wyjścia,
bez CTA do kanałów sygnałowych, bez cennika PRO,
bez linku do kanału sygnałów; wszędzie jasno „paper trading / symulacja”.

Generates:
  - X thread (5-7 tweets with weekly stats, best/worst trade, lessons)
  - LinkedIn Newsletter draft (long-form, professional)
  - YouTube Shorts script (60s narration)
  - Graphics list per post (what to screenshot/create)

Reads:
  - paper_trades.db — all closed trades this week
  - content_queue.json — what was already posted
  - fusion_latest.json — current state for equity curve

Writes:
  - weekly_recap.json — complete recap with all content
  - Sends digest to personal Telegram

Runs every Sunday via GitHub Actions (scheduled or manual).
"""

from __future__ import annotations
import json
import os
import sqlite3
import ssl
import urllib.request as ur
import urllib.parse as up
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- config ----------------------------------------------------------------

HOME = Path.home()
FUSION_DIR = HOME / "Claude" / "TVCFusion"
DB_PATH = FUSION_DIR / "paper_trades.db"
RECAP_PATH = FUSION_DIR / "weekly_recap.json"
FUSION_JSON = FUSION_DIR / "fusion_latest.json"
QUEUE_PATH = FUSION_DIR / "content_queue.json"

STATS_SINCE = "2026-09-02T19:00:00"
STATS_WHERE = "status='closed' AND opened_at >= ? AND COALESCE(excluded,0)=0"

TG_PERSONAL_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# --- helpers ---------------------------------------------------------------

def log(msg):
    print(f"[weekly] {msg}")


def _now():
    return datetime.now(timezone.utc)


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_px(v):
    if v is None:
        return "—"
    v = float(v)
    if v >= 1000:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:.2f}"
    return f"${v:.4f}"


def _tg_send(text):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = TG_PERSONAL_CHAT
    if not token or not chat:
        return
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
        data = up.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": "true"}).encode()
        req = ur.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with ur.urlopen(req, timeout=10, context=ctx) as r:
            log(f"telegram: {r.status}")
    except Exception as e:
        log(f"telegram failed: {e}")


# --- data fetchers ---------------------------------------------------------

def get_week_trades():
    """All trades closed in the last 7 days."""
    if not DB_PATH.exists():
        return []
    conn = db()
    since = (_now() - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status='closed' AND closed_at >= ? "
        "AND COALESCE(excluded,0)=0 ORDER BY closed_at ASC",
        (since,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_week_opens():
    """Trades opened this week (still open)."""
    if not DB_PATH.exists():
        return []
    conn = db()
    since = (_now() - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status='open' AND opened_at >= ?",
        (since,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_cumulative_stats():
    if not DB_PATH.exists():
        return {"total": 0, "wins": 0, "pnl": 0, "wr": 0}
    conn = db()
    rows = conn.execute(f"SELECT pnl_usd FROM positions WHERE {STATS_WHERE}", (STATS_SINCE,)).fetchall()
    conn.close()
    total = len(rows)
    wins = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
    pnl = sum((r["pnl_usd"] or 0) for r in rows)
    return {"total": total, "wins": wins, "pnl": round(pnl, 2), "wr": round(wins / total * 100, 1) if total else 0}


# --- generators ------------------------------------------------------------
# v1.1 (v4.4): treści dwujęzyczne PL + EN, ton edukacyjny, bez cen wejścia/wyjścia,
# bez CTA do sygnałów. Kwoty = wirtualny kapitał paper tradingu ($10K).

X_MAX = 280          # limit znaków posta na X (konto bez Premium)
TG_MAX_LEN = 4000    # Telegram: 4096 znaków; zapas na encje HTML

EXIT_PL_EN = {
    "hit_sl": ("stop loss", "stop loss"),
    "hit_tp1": ("pierwszy cel (TP1)", "first target (TP1)"),
    "hit_tp2": ("drugi cel (TP2)", "second target (TP2)"),
    "hit_breakeven": ("stop na cenie wejścia po TP1", "break-even stop after TP1"),
    "hit_trailing_sl": ("stop kroczący", "trailing stop"),
    "max_hold": ("limit czasu", "time limit"),
    "zombie_expired": ("limit czasu", "time limit"),
    "short_killswitch": ("bezpiecznik dla shortów", "short safety switch"),
    "manual_close": ("zamknięcie ręczne", "manual close"),
}


def _exit_pl_en(t):
    r = t.get("hit_or_miss") or ""
    return EXIT_PL_EN.get(r, ("zamknięcie według reguł", "rule-based exit"))


def _pct_pl(v, nd=1):
    return f"{(v or 0):+.{nd}f}%".replace(".", ",")


def _pct_en(v, nd=1):
    return f"{(v or 0):+.{nd}f}%"


def _num_pl(v):
    return f"{v}".replace(".", ",")


def _week_numbers(trades):
    total = len(trades)
    wins = [t for t in trades if (t.get("pnl_usd") or 0) > 0]
    week_pnl = sum(t.get("pnl_usd", 0) or 0 for t in trades)
    week_wr = round(len(wins) / total * 100, 1) if total else 0
    best = max(trades, key=lambda t: t.get("pnl_pct", 0) or 0) if trades else None
    worst = min(trades, key=lambda t: t.get("pnl_pct", 0) or 0) if trades else None
    return total, wins, week_pnl, week_wr, best, worst


def _x_len(text):
    """Ostrożna długość wg X: znaki spoza BMP/emoji liczone podwójnie, link nie skracany do 23."""
    return sum(2 if ord(c) >= 0x2000 else 1 for c in text)


def _pair(num, pl, en, graphic=""):
    """Jeden wpis wątku PL+EN, jeśli mieści się w limicie X; inaczej dwa kolejne (PL, potem EN)."""
    both = f"🇵🇱 {pl}\n\n🇬🇧 {en}"
    if _x_len(both) <= X_MAX:
        return [{"tweet_num": num, "lang": "pl+en", "text": both, "graphic": graphic}]
    return [{"tweet_num": num, "lang": "pl", "text": f"🇵🇱 {pl}", "graphic": graphic},
            {"tweet_num": num, "lang": "en", "text": f"🇬🇧 {en}", "graphic": ""}]


def generate_x_thread(trades, stats, cum):
    """Wątek X: każda myśl po polsku i po angielsku (jeden post albo para PL→EN)."""
    total, wins, week_pnl, week_wr, best, worst = _week_numbers(trades)
    emoji = "🟢" if week_pnl >= 0 else "🔴"
    raw = []

    raw.append((1,
        f"{emoji} TVC Fusion: tydzień paper tradingu (symulacja)\n\n"
        f"Transakcje: {total}\nTrafione: {_num_pl(week_wr)}%\nWynik: ${week_pnl:+.0f} na wirtualnych $10K\n\n"
        f"Zapisujemy każdą transakcję, także straty. Omówienie niżej 🧵👇",
        f"{emoji} TVC Fusion: paper trading week (simulation)\n\n"
        f"Trades: {total}\nWin rate: {week_wr}%\nResult: ${week_pnl:+.0f} on $10K virtual capital\n\n"
        f"Every trade is logged, losses included. Breakdown below 🧵👇",
        "📸 GRAPHIC: Equity curve from P&L History panel (last 7 days), 1200x675px\n"
        "  URL: tradingventureclub.com/terminal/"))

    if best:
        ex_pl, ex_en = _exit_pl_en(best)
        d = best.get("direction", "long").upper()
        raw.append((2,
            f"Najlepsza transakcja: {best['ticker']} {d}\n"
            f"Wynik: {_pct_pl(best.get('pnl_pct'))}\nWyjście: {ex_pl}\n"
            f"Fusion Score przy wejściu: {best.get('fusion_score', '?')}/100\n\n"
            f"Lekcja: cel i stop były znane przed wejściem.",
            f"Best trade: {best['ticker']} {d}\n"
            f"Result: {_pct_en(best.get('pnl_pct'))}\nExit: {ex_en}\n"
            f"Fusion Score at entry: {best.get('fusion_score', '?')}/100\n\n"
            f"Lesson: target and stop were set before entry.",
            f"📸 GRAPHIC: Terminal chart of {best['ticker']} (no price labels needed)\n"
            f"  URL: tradingventureclub.com/terminal/?token={best['ticker']}"))

    if worst and (worst.get("pnl_usd") or 0) < 0:
        ex_pl, ex_en = _exit_pl_en(worst)
        d = worst.get("direction", "long").upper()
        raw.append((3,
            f"Najsłabsza transakcja: {worst['ticker']} {d}\n"
            f"Wynik: {_pct_pl(worst.get('pnl_pct'))}\nWyjście: {ex_pl}\n\n"
            f"Straty też pokazujemy. Stop ograniczył je do poziomu znanego przed wejściem.",
            f"Weakest trade: {worst['ticker']} {d}\n"
            f"Result: {_pct_en(worst.get('pnl_pct'))}\nExit: {ex_en}\n\n"
            f"We show losses too. The stop capped it at a level known before entry.",
            f"📸 GRAPHIC: Terminal chart of {worst['ticker']} with the stop marked\n"
            f"  URL: tradingventureclub.com/terminal/?token={worst['ticker']}"))

    by_dir = {}
    for t in trades:
        s = by_dir.setdefault(t.get("direction", "long"), {"count": 0, "pnl": 0.0, "wins": 0})
        s["count"] += 1
        s["pnl"] += t.get("pnl_usd", 0) or 0
        s["wins"] += 1 if (t.get("pnl_usd") or 0) > 0 else 0
    pl_l, en_l = [], []
    for d, s in by_dir.items():
        wr = round(s["wins"] / s["count"] * 100) if s["count"] else 0
        pl_l.append(f"{d.upper()}: {s['count']} transakcji, {wr}% trafionych, ${s['pnl']:+.0f}")
        en_l.append(f"{d.upper()}: {s['count']} trades, {wr}% WR, ${s['pnl']:+.0f}")
    raw.append((4,
        "Podział według kierunku:\n" + "\n".join(pl_l) +
        "\n\nBot rozpoznaje reżim rynku (trend albo konsolidacja) i do niego dopasowuje filtry wejścia.",
        "By direction:\n" + "\n".join(en_l) +
        "\n\nThe bot detects the market regime (trend or range) and adapts its entry filters.",
        "📸 GRAPHIC: 'What Works' panel (WR/PnL by direction, regime, exit type)\n"
        "  URL: tradingventureclub.com/terminal/"))

    raw.append((5,
        f"Od 2 września:\n📊 {cum['total']} transakcji\n✅ {_num_pl(cum['wr'])}% trafionych\n"
        f"💰 ${cum['pnl']:+.0f} łącznie\n\nKapitał wirtualny $10K. Pełny dziennik: tradingventureclub.com/results.html",
        f"Since Sep 2:\n📊 {cum['total']} trades\n✅ {cum['wr']}% win rate\n"
        f"💰 ${cum['pnl']:+.0f} total\n\n$10K virtual capital. Full journal: tradingventureclub.com/results.html",
        "📸 GRAPHIC: Full equity curve from Sep 2 (P&L History, zoomed out)\n"
        "  URL: tradingventureclub.com/results.html"))

    raw.append((6,
        "To dziennik edukacyjnego paper tradingu, a nie sygnały ani porada inwestycyjna.\n\nDo zobaczenia za tydzień 🫡",
        "This is an educational paper-trading log, not trade signals and not investment advice.\n\n"
        "See you next week 🫡\n\n#crypto #trading #algotrading",
        "📸 GRAPHIC: results page (tradingventureclub.com/results.html), 1200x675px"))

    thread = []
    for num, pl, en, g in raw:
        thread += _pair(num, pl, en, g)
    return thread


def _trade_log(trades, lang):
    out = []
    for t in trades:
        emoji = "✅" if (t.get("pnl_usd", 0) or 0) > 0 else "❌"
        ex_pl, ex_en = _exit_pl_en(t)
        pct = _pct_pl(t.get("pnl_pct")) if lang == "pl" else _pct_en(t.get("pnl_pct"))
        out.append(f"  {emoji} {t['ticker']} {t.get('direction', 'long').upper()}: {pct} "
                   f"(${t.get('pnl_usd', 0) or 0:+.0f}), score {t.get('fusion_score', '?')}, "
                   f"{ex_pl if lang == 'pl' else ex_en}")
    return "\n".join(out)


def generate_linkedin_newsletter(trades, stats, cum):
    """Newsletter LinkedIn: pełna wersja PL, potem pełna wersja EN, w jednym tekście."""
    total, wins, week_pnl, week_wr, best, worst = _week_numbers(trades)
    now = _now()
    ws = (now - timedelta(days=7))
    pl_range = f"{ws.strftime('%d.%m')}–{now.strftime('%d.%m.%Y')}"
    en_range = f"{ws.strftime('%b %d')} — {now.strftime('%b %d, %Y')}"

    pl = (f"🇵🇱 TVC Fusion: tygodniowy raport paper tradingu, {pl_range}\n\n"
          f"Kolejny tydzień w pełni automatycznego, jawnego paper tradingu (symulacja, wirtualne $10 000). "
          f"Oto co zrobił algorytm i czego to uczy.\n\n"
          f"LICZBY\n\nZamknięte transakcje: {total}\nZyskowne: {len(wins)} | Stratne: {total - len(wins)}\n"
          f"Trafność: {_num_pl(week_wr)}%\nWynik tygodnia: ${week_pnl:+.0f}\n\n"
          f"DZIENNIK TRANSAKCJI\n\n{_trade_log(trades, 'pl')}\n\n")
    en = (f"🇬🇧 TVC Fusion Weekly Paper-Trading Report: {en_range}\n\n"
          f"Another week of fully automated, transparent paper trading (simulation, $10,000 virtual capital). "
          f"Here's what the algorithm did and what it teaches.\n\n"
          f"THE NUMBERS\n\nTrades closed: {total}\nWins: {len(wins)} | Losses: {total - len(wins)}\n"
          f"Win rate: {week_wr}%\nWeek result: ${week_pnl:+.0f}\n\n"
          f"TRADE LOG\n\n{_trade_log(trades, 'en')}\n\n")
    if best:
        ex_pl, ex_en = _exit_pl_en(best)
        pl += (f"NAJLEPSZA TRANSAKCJA: {best['ticker']} {best.get('direction', 'long').upper()}\n"
               f"Wynik: {_pct_pl(best.get('pnl_pct'))}, wyjście: {ex_pl}, Fusion Score {best.get('fusion_score', '?')}/100.\n"
               f"Cel i stop były ustalone przed wejściem. Dobry wynik to efekt planu, nie zgadywania szczytu.\n\n")
        en += (f"BEST TRADE: {best['ticker']} {best.get('direction', 'long').upper()}\n"
               f"Result: {_pct_en(best.get('pnl_pct'))}, exit: {ex_en}, Fusion Score {best.get('fusion_score', '?')}/100.\n"
               f"Target and stop were set before entry. A good result comes from the plan, not from guessing the top.\n\n")
    if worst and (worst.get("pnl_usd") or 0) < 0:
        ex_pl, ex_en = _exit_pl_en(worst)
        pl += (f"NAJSŁABSZA TRANSAKCJA: {worst['ticker']} {worst.get('direction', 'long').upper()}\n"
               f"Wynik: {_pct_pl(worst.get('pnl_pct'))}, wyjście: {ex_pl}.\n"
               f"Strata była ograniczona stopem ustalonym przed wejściem. Żadna pojedyncza transakcja nie może zniszczyć konta.\n\n")
        en += (f"WEAKEST TRADE: {worst['ticker']} {worst.get('direction', 'long').upper()}\n"
               f"Result: {_pct_en(worst.get('pnl_pct'))}, exit: {ex_en}.\n"
               f"The loss was capped by a stop set before entry. No single trade can blow up the account.\n\n")
    pl += (f"JAK TO DZIAŁA\n\nCo 5 minut w GitHub Actions działa 7 skryptów Pythona:\n"
           f"→ Fusion Score (on-chain + technika + derywaty + sentyment + momentum)\n"
           f"→ Weryfikacja Smart Money (wieloryby Hyperliquid, top traderzy Binance, przepływ takerów)\n"
           f"→ Filtry wejścia zależne od reżimu rynku\n"
           f"→ Automatyczne zarządzanie ryzykiem (SL/TP1/TP2, maks. 5 dni w pozycji)\n\n"
           f"OD POCZĄTKU (od 2 września)\n\nTransakcje: {cum['total']}\nTrafność: {_num_pl(cum['wr'])}%\n"
           f"Wynik łącznie: ${cum['pnl']:+.0f}\nKapitał: $10 000 (wirtualny)\n\n"
           f"O TYM DZIENNIKU\n\nTo edukacyjna symulacja paper tradingu, a nie sygnały ani porada inwestycyjna.\n"
           f"Pełny dziennik razem ze stratami: tradingventureclub.com/results.html\n")
    en += (f"HOW IT WORKS\n\nThe algorithm runs 7 Python scripts every 5 minutes via GitHub Actions:\n"
           f"→ Fusion scoring (on-chain + technical + derivatives + sentiment + momentum)\n"
           f"→ Smart Money verification (Hyperliquid whales + Binance top traders + taker flow)\n"
           f"→ Regime-aware entry filters\n"
           f"→ Automated risk management (SL/TP1/TP2, max 5-day hold)\n\n"
           f"CUMULATIVE (since Sep 2)\n\nTotal trades: {cum['total']}\nWin rate: {cum['wr']}%\n"
           f"Total result: ${cum['pnl']:+.0f}\nCapital: $10,000 (virtual)\n\n"
           f"ABOUT THIS LOG\n\nThis is an educational paper-trading simulation, not trade signals and not investment advice.\n"
           f"Full trade journal, including losses: tradingventureclub.com/results.html\n\n"
           f"#CryptoTrading #AlgoTrading #PaperTrading #TradingEducation #BuildInPublic")
    text = pl + "\n———\n\n" + en

    graphic = (
        "📸 GRAPHICS FOR LINKEDIN NEWSLETTER:\n"
        "  1. Cover: terminal equity curve with the week highlighted + \"Weekly Report / Raport tygodniowy\"\n"
        "     Format: 1200x627px (newsletter header)\n"
        "  2. Trade log: Transaction History panel (no entry prices needed)\n"
        "  3. System diagram (optional): GitHub Actions → Python → Gist → Terminal\n"
        "  4. Cumulative equity curve from Sep 2\n"
        "  URL: tradingventureclub.com/results.html"
    )
    return {"text": text, "graphic": graphic, "chars": len(text), "lang": "pl+en"}


def generate_youtube_script(trades, stats, cum):
    """Skrypt YouTube Shorts: wersja PL i wersja EN (dwa nagrania po ~60 s)."""
    total, wins, week_pnl, week_wr, best, worst = _week_numbers(trades)
    b_pl = (f"Najlepsza transakcja: {best['ticker']}, {_pct_pl(best.get('pnl_pct'))} w symulacji." if best
            else "Szczegóły w dzienniku.")
    b_en = (f"Best trade: {best['ticker']}, {_pct_en(best.get('pnl_pct'))} simulated." if best
            else "Details in the journal.")
    script = f"""YOUTUBE SHORTS — Weekly Recap (2 × ~60 s: PL, potem EN)

=== 🇵🇱 WERSJA POLSKA ===
[HOOK 0–5 s] (krzywa kapitału w terminalu)
"Mój bot do paper tradingu: ${week_pnl:+.0f} w tym tygodniu na wirtualnych 10 tysiącach dolarów. Zobacz, jak działa."
[LICZBY 5–15 s]
"{total} transakcji, {_num_pl(week_wr)}% trafionych. Każda zapisana publicznie, także straty."
[PRZYKŁAD 15–30 s]
"{b_pl} Cel i stop były ustalone przed wejściem."
[JAK TO DZIAŁA 30–45 s] (GitHub Actions, przewijany kod)
"Siedem skryptów w Pythonie co pięć minut analizuje on-chain, pozycje wielorybów i derywaty."
"Gdy Fusion Score przekroczy 62, a Smart Money to potwierdzi, bot otwiera transakcję paper."
[ZAKOŃCZENIE 45–60 s] (strona wyników)
"To symulacja do nauki, a nie sygnały. Pełny dziennik: link w bio."

=== 🇬🇧 ENGLISH VERSION ===
[HOOK 0–5s] (terminal equity curve)
"My paper-trading bot: ${week_pnl:+.0f} this week on $10K virtual capital. Here's how it works."
[STATS 5–15s]
"{total} trades this week. {week_wr}% win rate. Every one logged publicly, losses included."
[EXAMPLE 15–30s]
"{b_en} Target and stop were set before entry."
[HOW IT WORKS 30–45s] (GitHub Actions, code scrolling)
"Seven Python scripts run every five minutes, scanning on-chain data, whale positions and derivatives."
"If the Fusion Score hits 62 and Smart Money confirms, the bot opens a paper trade."
[OUTRO 45–60s] (results page)
"It's a simulation for learning, not signals. Full journal: link in bio."
"""
    graphic = (
        "📸 VISUALS FOR YOUTUBE SHORT:\n"
        "  - Screen recording of TVC Terminal (scrolling through panels)\n"
        "  - Equity curve zoom-in with week highlighted\n"
        f"  - {best['ticker'] + ' chart' if best else 'BTC chart'} (no entry price labels)\n"
        "  - GitHub Actions workflow runs page (quick flash)\n"
        "  - Results page screenshot (tradingventureclub.com/results.html)\n"
        "  Format: 1080x1920 (9:16 vertical), 55–60 s per language version\n"
        "  URL: tradingventureclub.com/terminal/"
    )
    return {"script": script, "graphic": graphic, "lang": "pl+en"}


def _tg_split(text, limit=TG_MAX_LEN):
    """Dzieli tekst na części ≤ limit po akapitach (tagi HTML nie przechodzą przez nową linię)."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            parts.append(para[:limit]); para = para[limit:]
        if cur and len(cur) + 2 + len(para) > limit:
            parts.append(cur); cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts


# --- main ------------------------------------------------------------------

def run():
    log(f"Weekly Recap v1.1 (PL+EN) — {_now().isoformat()}")

    trades = get_week_trades()
    if not trades:
        log("No closed trades this week — skipping recap.")
        return

    cum = get_cumulative_stats()
    opens = get_week_opens()

    log(f"Week: {len(trades)} closed, {len(opens)} still open")

    # Generate all content
    x_thread = generate_x_thread(trades, {}, cum)
    li_newsletter = generate_linkedin_newsletter(trades, {}, cum)
    yt_script = generate_youtube_script(trades, {}, cum)

    now = _now()
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    week_end = now.strftime("%Y-%m-%d")

    recap = {
        "week": f"{week_start} — {week_end}",
        "generated_at": now.isoformat(),
        "summary": {
            "trades_closed": len(trades),
            "wins": sum(1 for t in trades if (t.get("pnl_usd", 0)) > 0),
            "losses": sum(1 for t in trades if (t.get("pnl_usd", 0)) <= 0),
            "week_pnl": round(sum(t.get("pnl_usd", 0) for t in trades), 2),
            "week_wr": round(sum(1 for t in trades if (t.get("pnl_usd", 0)) > 0) / len(trades) * 100, 1),
            "cumulative": cum,
        },
        "x_thread": x_thread,
        "linkedin_newsletter": li_newsletter,
        "youtube_script": yt_script,
    }

    RECAP_PATH.write_text(json.dumps(recap, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    log(f"✓ Saved weekly_recap.json")

    # Telegram digest (prywatny czat) — PL + EN, z pilnowaniem limitu 4096 znaków
    sm = recap["summary"]
    singles = sum(1 for t in x_thread if t.get("lang") == "pl+en")
    msg = (
        f"📊 <b>Weekly Recap {week_start} → {week_end}</b>\n\n"
        f"🇵🇱 Transakcje: {len(trades)} | trafione: {_num_pl(sm['week_wr'])}% | wynik: ${sm['week_pnl']:+.0f} (paper)\n"
        f"Treści (PL + EN): wątek X: {len(x_thread)} postów (PL+EN w jednym poście: {singles}, "
        f"pozostałe w parach PL→EN), newsletter LinkedIn ~{li_newsletter['chars']} znaków, skrypt Shorts PL i EN.\n"
        f"Wszystko w <code>weekly_recap.json</code>, do skopiowania.\n\n"
        f"🇬🇧 Trades: {len(trades)} | WR: {sm['week_wr']}% | result: ${sm['week_pnl']:+.0f} (paper)\n"
        f"Content (PL + EN): X thread {len(x_thread)} posts, LinkedIn newsletter ~{li_newsletter['chars']} chars, "
        f"Shorts script in PL and EN. All in <code>weekly_recap.json</code>."
    )
    for part in _tg_split(msg):
        _tg_send(part)
    log("Done!")


if __name__ == "__main__":
    run()
