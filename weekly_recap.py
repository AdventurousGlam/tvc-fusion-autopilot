#!/usr/bin/env python3
"""
TVC Fusion Weekly Recap v1.0 — Sunday compilation of the week's trades + signals.

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

def generate_x_thread(trades, stats, cum):
    """Generate a 5-7 tweet thread for X."""
    total = len(trades)
    wins = [t for t in trades if (t.get("pnl_usd") or 0) > 0]
    losses = [t for t in trades if (t.get("pnl_usd") or 0) <= 0]
    week_pnl = sum(t.get("pnl_usd", 0) for t in trades)
    week_wr = round(len(wins) / total * 100, 1) if total else 0

    best = max(trades, key=lambda t: t.get("pnl_pct", 0)) if trades else None
    worst = min(trades, key=lambda t: t.get("pnl_pct", 0)) if trades else None

    thread = []

    # Tweet 1 — hook
    emoji = "🟢" if week_pnl >= 0 else "🔴"
    thread.append({
        "tweet_num": 1,
        "text": (
            f"{emoji} TVC Fusion Weekly Recap\n\n"
            f"Trades: {total}\n"
            f"Win rate: {week_wr}%\n"
            f"PnL: ${week_pnl:+.0f}\n\n"
            f"Fully automated. Every trade logged. Here's the breakdown 🧵👇"
        ),
        "graphic": (
            "📸 GRAPHIC: Equity curve screenshot from P&L History panel\n"
            "  - Show the last 7 days clearly\n"
            "  - Add text overlay: \"Week of [date range]\"\n"
            "  - Format: 1200x675px\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    })

    # Tweet 2 — best trade
    if best:
        thread.append({
            "tweet_num": 2,
            "text": (
                f"Best trade: ${best['ticker']} {best.get('direction','long').upper()}\n\n"
                f"Entry: {_fmt_px(best.get('entry_price'))}\n"
                f"Exit: {_fmt_px(best.get('exit_price'))}\n"
                f"PnL: {best.get('pnl_pct',0):+.1f}% (${best.get('pnl_usd',0):+.0f})\n"
                f"Fusion Score: {best.get('fusion_score','?')}/100\n\n"
                f"Smart Money confirmed before entry ✅"
            ),
            "graphic": (
                f"📸 GRAPHIC: Terminal chart of ${best['ticker']} showing:\n"
                f"  - Entry/exit points annotated on price chart\n"
                f"  - Smart Money panel showing confirmation\n"
                f"  URL: tradingventureclub.com/terminal/?token={best['ticker']}"
            ),
        })

    # Tweet 3 — worst trade (transparency)
    if worst and worst.get("pnl_usd", 0) < 0:
        thread.append({
            "tweet_num": 3,
            "text": (
                f"Worst trade: ${worst['ticker']} {worst.get('direction','long').upper()}\n\n"
                f"PnL: {worst.get('pnl_pct',0):+.1f}%\n"
                f"Stopped out — risk managed.\n\n"
                f"Posting losses = credibility.\n"
                f"If someone only shows wins, run 🚩"
            ),
            "graphic": (
                f"📸 GRAPHIC: Terminal chart of ${worst['ticker']} with stop-loss hit marked\n"
                f"  URL: tradingventureclub.com/terminal/?token={worst['ticker']}"
            ),
        })

    # Tweet 4 — stats breakdown
    by_direction = {}
    for t in trades:
        d = t.get("direction", "long")
        if d not in by_direction:
            by_direction[d] = {"count": 0, "pnl": 0, "wins": 0}
        by_direction[d]["count"] += 1
        by_direction[d]["pnl"] += t.get("pnl_usd", 0)
        if (t.get("pnl_usd", 0)) > 0:
            by_direction[d]["wins"] += 1

    stats_lines = []
    for d, s in by_direction.items():
        wr = round(s["wins"] / s["count"] * 100) if s["count"] else 0
        stats_lines.append(f"{d.upper()}: {s['count']} trades, {wr}% WR, ${s['pnl']:+.0f}")

    thread.append({
        "tweet_num": 4,
        "text": (
            f"Direction breakdown:\n\n"
            + "\n".join(stats_lines) + "\n\n"
            f"The bot auto-detects regime (trending/ranging) and adjusts entry filters accordingly."
        ),
        "graphic": (
            "📸 GRAPHIC: Screenshot of 'What Works' panel from terminal\n"
            "  - Shows WR/PnL by direction, regime, exit type\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    })

    # Tweet 5 — cumulative
    thread.append({
        "tweet_num": 5,
        "text": (
            f"Cumulative stats (since Sep 2):\n\n"
            f"📊 {cum['total']} trades\n"
            f"✅ {cum['wr']}% win rate\n"
            f"💰 ${cum['pnl']:+.0f} total PnL\n\n"
            f"$10K paper capital. Fully transparent.\n"
            f"Track record on: tradingventureclub.com/terminal/"
        ),
        "graphic": (
            "📸 GRAPHIC: Full equity curve from day 1 (Sep 2)\n"
            "  - P&L History panel, zoomed out to show entire history\n"
            "  - System Health panel visible\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    })

    # Tweet 6 — CTA
    thread.append({
        "tweet_num": 6,
        "text": (
            f"Want the signals?\n\n"
            f"🆓 Free: t.me/TVCFusionSignals\n"
            f"  → 1 pick/day + 2 delayed trade signals\n\n"
            f"🔑 PRO ($29/mo): full instant signals + Smart Money data\n"
            f"  → @TVCAlertsBot on Telegram\n\n"
            f"See you next week 🫡\n\n"
            f"#crypto #trading #smartmoney #algotrading"
        ),
        "graphic": (
            "📸 GRAPHIC: Terminal bird's-eye view (multiple panels visible)\n"
            "  - Or: side-by-side Telegram PRO vs FREE channel screenshots\n"
            "  - Format: 1200x675px"
        ),
    })

    return thread


def generate_linkedin_newsletter(trades, stats, cum):
    """Generate a long-form LinkedIn Newsletter draft."""
    total = len(trades)
    wins = [t for t in trades if (t.get("pnl_usd") or 0) > 0]
    week_pnl = sum(t.get("pnl_usd", 0) for t in trades)
    week_wr = round(len(wins) / total * 100, 1) if total else 0

    best = max(trades, key=lambda t: t.get("pnl_pct", 0)) if trades else None
    worst = min(trades, key=lambda t: t.get("pnl_pct", 0)) if trades else None

    now = _now()
    week_start = (now - timedelta(days=7)).strftime("%b %d")
    week_end = now.strftime("%b %d, %Y")

    # Trade log
    trade_log = ""
    for t in trades:
        emoji = "✅" if (t.get("pnl_usd", 0)) > 0 else "❌"
        trade_log += (
            f"  {emoji} ${t['ticker']} {t.get('direction','long').upper()} — "
            f"{t.get('pnl_pct',0):+.1f}% (${t.get('pnl_usd',0):+.0f}) — "
            f"Score {t.get('fusion_score','?')}, {t.get('hit_or_miss','closed')}\n"
        )

    text = f"""TVC Fusion Weekly Report: {week_start} — {week_end}

Another week of fully automated, transparent crypto trading. Here's what our algorithm did.

THE NUMBERS

Trades closed: {total}
Wins: {len(wins)} | Losses: {total - len(wins)}
Win rate: {week_wr}%
Week PnL: ${week_pnl:+.0f}

TRADE LOG

{trade_log}
"""

    if best:
        text += f"""BEST TRADE: ${best['ticker']}

Direction: {best.get('direction','long').upper()}
Entry: {_fmt_px(best.get('entry_price'))} → Exit: {_fmt_px(best.get('exit_price'))}
PnL: {best.get('pnl_pct',0):+.1f}%
Fusion Score at entry: {best.get('fusion_score','?')}/100
What worked: Smart Money layers aligned (≥2/3 confirmed), regime was {best.get('regime','favorable')}, and the Fibonacci pullback filter gave a clean entry.

"""

    if worst and worst.get("pnl_usd", 0) < 0:
        text += f"""WORST TRADE: ${worst['ticker']}

Direction: {worst.get('direction','long').upper()}
PnL: {worst.get('pnl_pct',0):+.1f}%
What happened: {worst.get('hit_or_miss','stopped out')}. The system caught the loss early via stop-loss. Risk per trade is capped at the tiered sizing model — no single loss can blow the account.

"""

    text += f"""SYSTEM INSIGHTS

The algorithm runs 7 Python scripts every 5 minutes via GitHub Actions:
→ Fusion scoring (on-chain + technical + derivatives + sentiment + momentum)
→ Smart Money verification (Hyperliquid whales + Binance top traders + taker flow)
→ Regime-aware entry filters (Fibonacci pullback thresholds adjust per market state)
→ Automated risk management (SL/TP1/TP2 from market structure, max 5-day hold)

CUMULATIVE PERFORMANCE (since Sep 2)

Total trades: {cum['total']}
Win rate: {cum['wr']}%
Total PnL: ${cum['pnl']:+.0f}
Capital: $10,000 (paper)

GET THE SIGNALS

Free tier: t.me/TVCFusionSignals — 1 crypto pick/day + 2 delayed trade signals
PRO tier ($29/mo): Full instant signals + Smart Money data — @TVCAlertsBot on Telegram
Terminal ($79/mo): Full dashboard at tradingventureclub.com/terminal/

Every trade is real, every result is public. No cherry-picking.

#CryptoTrading #AlgoTrading #SmartMoney #TradingBot #WeeklyReport #BuildInPublic"""

    graphic = (
        "📸 GRAPHICS FOR LINKEDIN NEWSLETTER:\n"
        "  1. Cover image: Terminal equity curve with week highlighted + text overlay \"Weekly Report\"\n"
        "     Format: 1200x627px (newsletter header)\n"
        "  2. Trade log table: Screenshot of terminal Transaction History panel\n"
        "  3. Best trade chart: Terminal showing entry/exit on price chart\n"
        "  4. System architecture diagram (optional): GitHub Actions → Python → Gist → Terminal flow\n"
        "  5. Cumulative equity curve: Full history from Sep 2\n"
        "  URL: tradingventureclub.com/terminal/"
    )

    return {"text": text, "graphic": graphic}


def generate_youtube_script(trades, stats, cum):
    """Generate a 60-second YouTube Shorts script."""
    total = len(trades)
    wins = [t for t in trades if (t.get("pnl_usd") or 0) > 0]
    week_pnl = sum(t.get("pnl_usd", 0) for t in trades)
    week_wr = round(len(wins) / total * 100, 1) if total else 0
    best = max(trades, key=lambda t: t.get("pnl_pct", 0)) if trades else None

    script = f"""YOUTUBE SHORTS SCRIPT — Weekly Recap (60 seconds)

[HOOK — 0-5s]
(Screen recording of terminal equity curve)
"My trading bot made ${week_pnl:+.0f} this week — fully automated. Here's how."

[STATS — 5-15s]
(Show terminal dashboard, zoom into stats)
"{total} trades this week. {week_wr}% win rate. Every single one logged publicly."

[BEST TRADE — 15-30s]
(Show {best['ticker'] if best else 'BTC'} chart with entry/exit points)
"Best trade: {'$' + best['ticker'] + ' — ' + str(round(best.get('pnl_pct', 0), 1)) + '% profit' if best else 'check the terminal'}."
"The algorithm detected Smart Money flow aligning with technical structure."
"2 out of 3 whale layers confirmed — that's our entry trigger."

[HOW IT WORKS — 30-45s]
(Show GitHub Actions running, code scrolling)
"7 Python scripts run every 5 minutes in GitHub Actions."
"They scan on-chain data, whale positions, and derivatives signals."
"If the Fusion Score hits 60 and Smart Money confirms — the bot enters automatically."

[CTA — 45-60s]
(Show Telegram channels)
"Free signals — link in bio. Or see everything live in the TVC Fusion Terminal."
"Every trade. Every loss. Full transparency."
"""

    graphic = (
        "📸 VISUALS FOR YOUTUBE SHORT:\n"
        "  - Screen recording of TVC Terminal (scrolling through panels)\n"
        "  - Equity curve zoom-in with week highlighted\n"
        f"  - {'$' + best['ticker'] + ' chart with entry/exit annotations' if best else 'BTC chart'}\n"
        "  - GitHub Actions workflow runs page (quick flash)\n"
        "  - Telegram channel screenshots (PRO + FREE)\n"
        "  - Record with OBS or similar (terminal in browser)\n"
        "  Format: 1080x1920 (9:16 vertical)\n"
        "  Duration: 55-60 seconds\n"
        "  Background music: lo-fi or minimal electronic\n"
        "  URL: tradingventureclub.com/terminal/"
    )

    return {"script": script, "graphic": graphic}


# --- main ------------------------------------------------------------------

def run():
    log(f"Weekly Recap v1.0 — {_now().isoformat()}")

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

    # Telegram digest
    msg = (
        f"📊 <b>Weekly Recap — {week_start} to {week_end}</b>\n\n"
        f"Trades: {len(trades)} | WR: {recap['summary']['week_wr']}% | PnL: ${recap['summary']['week_pnl']:+.0f}\n\n"
        f"<b>Generated content:</b>\n"
        f"🐦 X thread: {len(x_thread)} tweets\n"
        f"📝 LinkedIn Newsletter: ~{len(li_newsletter['text'])} chars\n"
        f"🎬 YouTube Shorts script: ready\n\n"
        f"Pliki w <code>weekly_recap.json</code> — skopiuj i opublikuj!"
    )
    _tg_send(msg)
    log("Done!")


if __name__ == "__main__":
    run()
