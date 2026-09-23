#!/usr/bin/env python3
"""
TVC Fusion Content Engine v3.0 — day-of-week content strategy

DAILY ROTATION:
  Monday    — Weekend Data Drop (what the system flagged)
  Tuesday   — Behind the Build (founder/builder narrative)
  Wednesday — Market Analysis (contrarian take + real data)
  Thursday  — System Results (weekly P&L from paper_trades.db)
  Friday    — Education (one trading concept explained)
  Saturday  — No posts (engagement-only day)
  Sunday    — Week Ahead Preview (macro events + key levels)

PLATFORM RULES:
  LinkedIn — ORIGINAL posts only. Personal angle ("I built...", "I noticed...").
             Never reshares. Target: 3,570 decision-maker followers.
  X/Twitter — Questions or contrarian observations. NEVER listicles.
              Cashtags ($BTC, $ETH, $SUI). Short and punchy.

CTA: t.me/TVCFusionSignals in every post.

Reads:  paper_trades.db, fusion_latest.json, crypto_picks.json, pump_radar_alerts.json
Writes: content_queue.json + sends to personal Telegram for copy-paste

Runs in GitHub Actions after paper_bot refresh. Non-destructive: appends to queue.
Zero external deps beyond stdlib + sqlite3.
"""

from __future__ import annotations
import json
import os
import sqlite3
import ssl
import sys
import urllib.request as ur
import urllib.parse as up
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

HOME = Path.home()
FUSION_DIR = HOME / "Claude" / "TVCFusion"
DB_PATH = FUSION_DIR / "paper_trades.db"
QUEUE_PATH = FUSION_DIR / "content_queue.json"
FUSION_JSON = FUSION_DIR / f"fusion_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
PICKS_JSON = FUSION_DIR / "crypto_picks.json"
RADAR_ALERTS = FUSION_DIR / "pump_radar_alerts.json"

STATS_SINCE = "2026-09-02T19:00:00"
STATS_WHERE = "status='closed' AND opened_at >= ? AND COALESCE(excluded,0)=0"

MAX_X_PER_DAY = 2
MAX_LI_PER_DAY = 2

TG_PERSONAL_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CTA = "t.me/TVCFusionSignals"

DAY_THEMES = {
    0: "Monday — Weekend Data Drop",
    1: "Tuesday — Behind the Build",
    2: "Wednesday — Market Analysis",
    3: "Thursday — System Results",
    4: "Friday — Education",
    5: "Saturday — No Posts",
    6: "Sunday — Week Ahead Preview",
}

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def log(msg):
    print(f"[content] {msg}")


def _now():
    return datetime.now(timezone.utc)


def _today():
    return _now().strftime("%Y-%m-%d")


def _week():
    return _now().isocalendar()[1]


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_queue():
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"posts": [], "meta": {}}
    return {"posts": [], "meta": {}}


def _save_queue(q):
    QUEUE_PATH.write_text(
        json.dumps(q, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
    )


def _posts_today(q, platform):
    today = _today()
    return sum(
        1 for p in q["posts"]
        if p.get("date") == today and p.get("platform") == platform
    )


def _has_type_today(q, post_type):
    today = _today()
    return any(
        p.get("type") == post_type and p.get("date") == today for p in q["posts"]
    )


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


def _post(platform, ptype, text, graphic, ticker=None, extra=None):
    """Build a post dict."""
    p = {
        "platform": platform,
        "type": ptype,
        "ticker": ticker,
        "date": _today(),
        "ts": _now().isoformat(),
        "text": text,
        "graphic": graphic,
        "posted": False,
    }
    if extra:
        p.update(extra)
    return p


# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_cumulative_stats():
    """Overall stats since tracking began."""
    if not DB_PATH.exists():
        return {"total": 0, "wins": 0, "pnl": 0, "wr": 0}
    conn = db()
    rows = conn.execute(
        f"SELECT pnl_usd FROM positions WHERE {STATS_WHERE}", (STATS_SINCE,)
    ).fetchall()
    conn.close()
    total = len(rows)
    wins = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
    pnl = sum((r["pnl_usd"] or 0) for r in rows)
    return {
        "total": total, "wins": wins, "pnl": round(pnl, 2),
        "wr": round(wins / total * 100, 1) if total else 0,
    }


def get_weekly_trades():
    """Trades closed in the last 7 days."""
    if not DB_PATH.exists():
        return []
    conn = db()
    since = (_now() - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status='closed' AND closed_at >= ? "
        "AND COALESCE(excluded,0)=0 ORDER BY closed_at DESC", (since,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_closes(hours=6):
    """Trades closed in the last N hours."""
    if not DB_PATH.exists():
        return []
    conn = db()
    since = (_now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status='closed' AND closed_at >= ? "
        "AND COALESCE(excluded,0)=0 ORDER BY closed_at DESC", (since,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_open_positions():
    """Currently open positions."""
    if not DB_PATH.exists():
        return []
    conn = db()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_todays_picks():
    """Today's auto picks."""
    data = _load_json(PICKS_JSON)
    if data.get("date") == _today():
        return data.get("picks", [])
    return []


def get_fusion_data():
    """Current fusion state."""
    return _load_json(FUSION_JSON)


def get_recent_pump_alerts(hours=24):
    """Recent HIGH pump alerts."""
    data = _load_json(RADAR_ALERTS)
    if not isinstance(data, list):
        data = data.get("alerts", [])
    since_ts = (_now() - timedelta(hours=hours)).timestamp()
    return [
        a for a in data
        if a.get("level") == "HIGH" and (a.get("ts") or 0) >= since_ts
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# MONDAY — WEEKEND DATA DROP
# ═══════════════════════════════════════════════════════════════════════════════

def gen_monday():
    """Data-driven: what the system flagged. Personal angle on LinkedIn."""
    fusion = get_fusion_data()
    picks = get_todays_picks()
    alerts = get_recent_pump_alerts(hours=48)
    stats = get_cumulative_stats()
    decisions = fusion.get("decisions", [])
    btc = next((d for d in decisions if d.get("ticker") == "BTC"), None)

    bullets = []
    if btc:
        score = btc.get("fusion_score", 50)
        regime = btc.get("regime", "UNKNOWN").replace("_", " ").lower()
        sm = btc.get("layers", {})
        bullets.append(f"$BTC Fusion Score at {score}/100 — regime: {regime}")
        if sm.get("verdict"):
            bullets.append(f"Smart Money verdict: {sm['verdict']}")

    if picks:
        top = picks[0]
        bullets.append(
            f"Screener flagged ${top['ticker']} ({top.get('category', 'momentum')}) "
            f"— {top.get('change_24h_pct', 0):+.1f}% in 24h"
        )

    for d in decisions:
        if d.get("ticker") != "BTC" and d.get("fusion_score", 0) >= 65:
            bullets.append(f"${d['ticker']} showing strength — score {d['fusion_score']}/100")
            break

    if alerts:
        a = alerts[0]
        bullets.append(
            f"Pump Radar fired on ${a.get('ticker', '?')} (score {a.get('score', 0)})"
        )

    bullets = bullets[:4] or [
        "200+ tokens scanned — no high-conviction setups today"
    ]

    bp = "\n".join(f"→ {b}" for b in bullets)
    bias = "conditions favor the long side"
    if btc and btc.get("fusion_score", 50) <= 40:
        bias = "caution is warranted"
    elif not btc or btc.get("fusion_score", 50) < 65:
        bias = "patience is the play"

    li = (
        f"Here's what my trading system flagged this morning:\n\n"
        f"{bp}\n\n"
        f"I scan 200+ perpetual contracts every 5 minutes — on-chain flow, "
        f"derivatives data, whale positions, price structure.\n\n"
        f"The system doesn't predict. It measures. Right now the data says: {bias}.\n\n"
        f"Running stats: {stats['total']} automated trades, "
        f"{stats['wr']}% win rate, ${stats['pnl']:+.0f} PnL.\n\n"
        f"I share these signals daily → {CTA}\n\n"
        f"What's on your watchlist this week?\n\n"
        f"#CryptoTrading #SmartMoney #AlgoTrading #BuildInPublic"
    )

    if picks:
        top = picks[0]
        x = (
            f"${top['ticker']} just hit our screener — "
            f"{top.get('category', 'momentum')} signal.\n\n"
            f"{top.get('change_24h_pct', 0):+.1f}% in 24h. "
            f"Compression or continuation?\n\n"
            f"Free signals: {CTA}"
        )
    elif btc:
        x = (
            f"$BTC Fusion Score: {btc.get('fusion_score', 50)}/100.\n"
            f"Smart Money: {btc.get('layers', {}).get('verdict', 'neutral')}.\n\n"
            f"Are you positioned for what's coming?\n\n"
            f"Free signals: {CTA}"
        )
    else:
        x = (
            f"200+ tokens scanned. Nothing screams conviction.\n\n"
            f"Sometimes the edge is staying flat.\n\n"
            f"What are you watching?\n\n"
            f"Free signals: {CTA}"
        )

    gr = (
        "📸 GRAPHIC: TVC Terminal dashboard — Fusion Score breakdown "
        "+ Smart Money panel + Pump Radar\n"
        "  URL: tradingventureclub.com/terminal/"
    )

    return [_post("linkedin", "data_drop", li, gr), _post("x", "data_drop", x, gr)]


# ═══════════════════════════════════════════════════════════════════════════════
# TUESDAY — BEHIND THE BUILD
# ═══════════════════════════════════════════════════════════════════════════════

_TUESDAY_TOPICS = [
    # 0 — Architecture
    {
        "li": (
            "Here's how my automated trading system actually works — no black box.\n\n"
            "7 Python scripts run every 5 minutes via GitHub Actions:\n\n"
            "→ auto_picks.py scans 200+ perp contracts across 4 signal categories\n"
            "→ auto_fusion.py computes a 0-100 score per token from 5 weighted factors\n"
            "→ paper_bot.py opens and manages trades with 12 rejection gates\n"
            "→ pump_radar.py watches for derivatives anomalies in real-time\n"
            "→ Plus correlation matrix, macro calendar, and content generation\n\n"
            "Total infrastructure cost: $0. GitHub Actions free tier.\n"
            "Total daily time: ~20 minutes publishing pre-written posts.\n\n"
            "The rest runs itself. That's the point.\n\n"
            "What part of your workflow could you automate but haven't yet?\n\n"
            "Free signals from this system → {cta}\n\n"
            "#BuildInPublic #Python #Automation #CryptoTrading"
        ),
        "x": (
            "7 Python scripts. GitHub Actions. $0/month.\n\n"
            "That's the entire infrastructure behind our 24/7 crypto trading system.\n\n"
            "No AWS. No servers. Just cron + code.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: GitHub Actions workflow runs page or TVC Terminal bird's eye view\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    },
    # 1 — Win rate journey
    {
        "li": (
            "My trading system's win rate started at 38%.\n\n"
            "Not great. But every losing trade taught the algorithm something:\n\n"
            "→ Got squeezed on a short → added funding rate gate\n"
            "→ Held a dead trade for 10 days → coded a 5-day zombie close\n"
            "→ Re-entered the same ticker 30 min after a stop → built a 2h cooldown\n"
            "→ Took a 0.8 R:R setup → added minimum 1.0 R:R gate\n\n"
            "Now: {wr}% win rate over {total} automated trades.\n\n"
            "The advantage of algorithmic trading: every fix is permanent. "
            "The bot can't forget its rules on a bad day.\n\n"
            "What's the most expensive lesson trading taught you?\n\n"
            "Free signals → {cta}\n\n"
            "#AlgoTrading #BuildInPublic #TradingLessons"
        ),
        "x": (
            "Win rate journey:\n\n"
            "Week 1: 38%\n"
            "After adding funding gate: 45%\n"
            "After zombie close: 52%\n"
            "After Smart Money veto: {wr}%\n\n"
            "Every loss = a permanent fix.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: TVC Terminal P&L History panel showing equity curve\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    },
    # 2 — Smart Money detection
    {
        "li": (
            "I noticed something while studying crypto markets: "
            "the biggest moves are preceded by specific footprints most traders never see.\n\n"
            "So I built a system that reads three layers of institutional flow:\n\n"
            "→ Hyperliquid whale wallets — top 30 addresses by realized PnL\n"
            "→ Binance top trader Long/Short ratio — what the top 20% are doing\n"
            "→ Taker flow + CVD — who's crossing the spread with conviction\n\n"
            "The rule: ≥2 out of 3 must confirm before the bot enters.\n"
            "When they diverge, the bot vetoes the trade — no matter how good the score looks.\n\n"
            "This veto system alone improved our results significantly.\n\n"
            "Do you track any form of Smart Money flow in your analysis?\n\n"
            "See it live → {cta}\n\n"
            "#SmartMoney #WhaleTracking #CryptoTrading"
        ),
        "x": (
            "Most retail watches price.\n"
            "Smart Money watches positioning.\n\n"
            "Our bot tracks 3 layers before any entry:\n"
            "→ HL whale wallets\n"
            "→ Binance top trader L/S\n"
            "→ Taker flow CVD\n\n"
            "≥2/3 must confirm. Otherwise: no trade.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: TVC Terminal Smart Money panel — whales + L/S ratio + taker CVD\n"
            "  URL: tradingventureclub.com/terminal/?token=BTC"
        ),
    },
    # 3 — Solo founder
    {
        "li": (
            "Trading Venture Club has no team. No investors. No funding.\n\n"
            "It has:\n"
            "→ A ~10,000-line trading terminal built from scratch\n"
            "→ 7 Python scripts running autonomously every 5 minutes\n"
            "→ Automated Stripe membership system\n"
            "→ Dual Telegram channels (free + premium)\n"
            "→ A content engine that writes posts from bot data\n\n"
            "I'm sharing this not to impress — but because the narrative that you need "
            "a team and funding to build something useful is wrong.\n\n"
            "You need a clear problem, willingness to learn, and discipline to ship weekly.\n\n"
            "The hardest part isn't building. It's publishing before it's perfect.\n\n"
            "If you're building something solo — keep going.\n\n"
            "→ {cta}\n\n"
            "#SoloFounder #BuildInPublic #Entrepreneurship"
        ),
        "x": (
            "Solo founder stats:\n"
            "→ 10,000+ lines of terminal code\n"
            "→ 7 scripts running 24/7\n"
            "→ $0 infrastructure cost\n"
            "→ Team size: 1\n\n"
            "You don't need a team. You need a system.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: Terminal bird's eye view showing multiple panels\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    },
    # 4 — Radical transparency
    {
        "li": (
            "When I built Trading Venture Club, I had a choice:\n\n"
            "Option A: Only show winning trades. Screenshot the best results. Scale fast.\n\n"
            "Option B: Show everything. Every win. Every loss. Every stopped-out trade.\n\n"
            "I chose B.\n\n"
            "The crypto signal industry is built on survivorship bias. "
            "Channels that only show wins attract short-term subscribers who churn "
            "the moment reality hits.\n\n"
            "Channels that show the full picture attract people who understand "
            "how trading actually works. Those people stay.\n\n"
            "Our real record: {total} trades, {wr}% win rate, ${pnl} PnL. "
            "Not every trade wins. That's exactly why the track record is credible.\n\n"
            "Ask anyone selling you signals: \"Can I see your full, unedited trade history?\"\n"
            "If the answer is no — you have your answer.\n\n"
            "Full transparency → {cta}\n\n"
            "#Transparency #CryptoSignals #TradingVentureClub"
        ),
        "x": (
            "If a signal channel won't show you their full history — "
            "including losses — you're not paying for signals.\n\n"
            "You're paying for fiction.\n\n"
            "Every TVC trade: logged, timestamped, public.\n\n"
            "Check for yourself: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: TVC Terminal P&L History — full equity curve with wins AND losses visible\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    },
    # 5 — Removing emotions from trading
    {
        "li": (
            "The hardest lesson in trading: you are the biggest risk.\n\n"
            "I studied my own mistakes and found the same pattern every time:\n\n"
            "→ Held losers too long because admitting a loss felt like failure\n"
            "→ Cut winners too early because taking profit felt safe\n"
            "→ Overtraded after a win streak — confidence became overconfidence\n"
            "→ Revenge-traded after a loss to \"get it back\"\n\n"
            "The solution wasn't more discipline. It was removing myself from the decision.\n\n"
            "I built an algorithm with hard-coded rules it physically cannot break. "
            "It doesn't care about yesterday's loss. Doesn't get excited about a streak. "
            "It just runs.\n\n"
            "After {total} automated trades at {wr}% win rate — the best thing I ever did "
            "for my trading was stop making trading decisions.\n\n"
            "What emotional trap do you fall into most?\n\n"
            "→ {cta}\n\n"
            "#TradingPsychology #AlgoTrading #TradingMindset"
        ),
        "x": (
            "\"I'll just hold a little longer.\"\n"
            "\"This time is different.\"\n"
            "\"I'll average down.\"\n\n"
            "Every blown account starts with one of these.\n\n"
            "That's why I removed myself from the equation. The bot follows the rules. I don't have to.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: TVC Terminal equity curve — smooth, systematic line\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    },
]


def gen_tuesday():
    """Behind the Build — founder narrative, rotates weekly."""
    stats = get_cumulative_stats()
    idx = _week() % len(_TUESDAY_TOPICS)
    topic = _TUESDAY_TOPICS[idx]

    fmt = {
        "cta": CTA, "total": stats["total"],
        "wr": stats["wr"], "pnl": f"{stats['pnl']:+.0f}",
    }

    li = topic["li"].format(**fmt)
    x = topic["x"].format(**fmt)
    gr = topic["graphic"]

    return [_post("linkedin", "behind_build", li, gr), _post("x", "behind_build", x, gr)]


# ═══════════════════════════════════════════════════════════════════════════════
# WEDNESDAY — MARKET ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def gen_wednesday():
    """Contrarian market take backed by terminal data."""
    fusion = get_fusion_data()
    stats = get_cumulative_stats()
    decisions = fusion.get("decisions", [])
    btc = next((d for d in decisions if d.get("ticker") == "BTC"), None)

    if btc:
        score = btc.get("fusion_score", 50)
        regime = btc.get("regime", "UNKNOWN")
        sm = btc.get("layers", {})
        sm_verdict = sm.get("verdict", "neutral")

        if score >= 70:
            angle = "bullish"
            contrarian = (
                f"Everyone's calling the top. Here's what the data actually says:\n\n"
                f"→ $BTC Fusion Score: {score}/100 — strong across all factors\n"
                f"→ Smart Money verdict: {sm_verdict}\n"
            )
        elif score <= 35:
            angle = "bearish"
            contrarian = (
                f"The crowd is still buying. Here's what my data shows:\n\n"
                f"→ $BTC Fusion Score: {score}/100 — weakness across multiple factors\n"
                f"→ Smart Money verdict: {sm_verdict}\n"
            )
        else:
            angle = "neutral"
            contrarian = (
                f"Everyone wants a direction call. Here's what the data actually says:\n\n"
                f"→ $BTC Fusion Score: {score}/100 — not enough conviction either way\n"
                f"→ Smart Money verdict: {sm_verdict}\n"
            )

        if sm.get("hl_whale_bias"):
            contrarian += f"→ Hyperliquid whales: {sm['hl_whale_bias']}\n"
        if sm.get("binance_top_trader"):
            contrarian += f"→ Binance top traders: {sm['binance_top_trader']}\n"

        regime_label = regime.replace("_", " ").title()
        contrarian += f"→ Market regime: {regime_label}\n"

        li = (
            f"{contrarian}\n"
            f"I built a system that scores every token 0-100 across on-chain, "
            f"technical structure, and sentiment. No opinions — just measurements.\n\n"
            f"The data doesn't care what CT thinks. "
            f"And right now it's saying: {angle}.\n\n"
            f"Running stats: {stats['total']} trades, {stats['wr']}% win rate.\n\n"
            f"What's your read on the current structure?\n\n"
            f"See the full breakdown → {CTA}\n\n"
            f"#Bitcoin #MarketAnalysis #SmartMoney #CryptoTrading"
        )

        x = (
            f"$BTC Fusion Score: {score}/100.\n"
            f"Regime: {regime_label}.\n"
            f"Smart Money: {sm_verdict}.\n\n"
            f"What's YOUR read?\n\n"
            f"Free signals: {CTA}"
        )
    else:
        li = (
            f"I noticed something most traders miss: they trade the narrative, not the data.\n\n"
            f"My system strips away opinions and measures 5 factors per token:\n"
            f"→ On-chain flow (40%)\n"
            f"→ Technical structure (40%)\n"
            f"→ Sentiment (15%)\n"
            f"→ News catalyst (5%)\n\n"
            f"When multiple factors align, the probability shifts. "
            f"When they don't — stay flat.\n\n"
            f"After {stats['total']} trades at {stats['wr']}% win rate, "
            f"I trust the numbers more than any CT thread.\n\n"
            f"Daily signals → {CTA}\n\n"
            f"#CryptoTrading #DataDriven #AlgoTrading"
        )
        x = (
            f"Opinions are free. Data costs effort.\n\n"
            f"That's why our system measures 5 factors before any trade.\n\n"
            f"What do you base YOUR entries on?\n\n"
            f"Free signals: {CTA}"
        )

    gr = (
        "📸 GRAPHIC: TVC Terminal — $BTC Fusion Score breakdown + Smart Money panel\n"
        "  URL: tradingventureclub.com/terminal/?token=BTC"
    )

    return [_post("linkedin", "market_analysis", li, gr, "BTC"),
            _post("x", "market_analysis", x, gr, "BTC")]


# ═══════════════════════════════════════════════════════════════════════════════
# THURSDAY — SYSTEM RESULTS (P&L)
# ═══════════════════════════════════════════════════════════════════════════════

def gen_thursday():
    """Weekly P&L from paper_trades.db — radical transparency."""
    weekly = get_weekly_trades()
    stats = get_cumulative_stats()

    if weekly:
        wins = [t for t in weekly if (t.get("pnl_usd") or 0) > 0]
        losses = [t for t in weekly if (t.get("pnl_usd") or 0) <= 0]
        week_pnl = sum((t.get("pnl_usd") or 0) for t in weekly)
        week_wr = round(len(wins) / len(weekly) * 100, 1) if weekly else 0

        trade_lines = []
        for t in weekly[:5]:
            ticker = t["ticker"]
            direction = t.get("direction", "long").upper()
            pnl_pct = t.get("pnl_pct", 0)
            emoji = "✅" if (t.get("pnl_usd") or 0) > 0 else "❌"
            reason = t.get("hit_or_miss", "closed")
            exit_labels = {
                "hit_tp1": "TP1", "hit_tp2": "TP2", "hit_sl": "stopped out",
                "hit_trailing_sl": "trailing stop", "zombie_close": "max hold",
            }
            exit_lbl = exit_labels.get(reason, reason or "closed")
            trade_lines.append(f"{emoji} ${ticker} {direction}: {pnl_pct:+.1f}% ({exit_lbl})")

        trades_str = "\n".join(trade_lines)
        extra_note = ""
        if len(weekly) > 5:
            extra_note = f"\n+ {len(weekly) - 5} more trades this week\n"

        li = (
            f"This week's automated trading results — no cherry-picking:\n\n"
            f"{trades_str}{extra_note}\n"
            f"Win rate: {len(wins)}/{len(weekly)} = {week_wr}%\n"
            f"Net P&L: ${week_pnl:+.1f}\n\n"
            f"Not backtested. Paper traded in real-time, every 5 minutes, 24/7.\n"
            f"Every signal posted to Telegram as it fires.\n\n"
            f"Cumulative: {stats['total']} trades, {stats['wr']}% win rate, "
            f"${stats['pnl']:+.0f} total PnL.\n\n"
            f"Want to see them live? → {CTA}\n\n"
            f"#CryptoTrading #Transparency #AlgoTrading #TradingResults"
        )

        best = max(weekly, key=lambda t: t.get("pnl_pct", 0))
        best_pct = best.get("pnl_pct", 0)
        best_ticker = best["ticker"]
        if best_pct > 0:
            x = (
                f"Best trade this week: ${best_ticker} {best.get('direction', 'long').upper()} "
                f"+{best_pct:.1f}%\n\n"
                f"Entry: {_fmt_px(best.get('entry_price'))} → "
                f"Exit: {_fmt_px(best.get('exit_price'))}\n\n"
                f"Not a prediction. A system.\n\n"
                f"Free signals: {CTA}"
            )
        else:
            x = (
                f"This week: {len(wins)}/{len(weekly)} wins. Net: ${week_pnl:+.1f}\n\n"
                f"Not every week is green. That's what a real track record looks like.\n\n"
                f"Free signals: {CTA}"
            )
    else:
        li = (
            f"No new trades closed this week.\n\n"
            f"The system has 12 rejection gates. Sometimes the best trade is no trade.\n\n"
            f"When setups aren't there, the algorithm waits. No FOMO. No forcing.\n\n"
            f"Cumulative record: {stats['total']} trades, {stats['wr']}% win rate, "
            f"${stats['pnl']:+.0f} PnL.\n\n"
            f"Patience > activity.\n\n"
            f"Follow the signals → {CTA}\n\n"
            f"#TradingDiscipline #AlgoTrading #CryptoTrading"
        )
        x = (
            f"Zero trades this week. 12 rejection gates said: not yet.\n\n"
            f"Patience IS the edge.\n\n"
            f"Free signals: {CTA}"
        )

    gr = (
        "📸 GRAPHIC: TVC Terminal P&L History panel — equity curve + trade log\n"
        "  URL: tradingventureclub.com/terminal/"
    )

    return [_post("linkedin", "weekly_results", li, gr),
            _post("x", "weekly_results", x, gr)]


# ═══════════════════════════════════════════════════════════════════════════════
# FRIDAY — EDUCATION
# ═══════════════════════════════════════════════════════════════════════════════

_FRIDAY_TOPICS = [
    # 0 — Multi-timeframe confluence
    {
        "li": (
            "Most traders check one timeframe. That's why most traders lose.\n\n"
            "They see a bullish candle on the 4h chart and go long. "
            "Then get stopped out because the daily trend is down.\n\n"
            "My system checks 5 timeframes simultaneously:\n"
            "→ 5m — micro-confirmation (is there momentum RIGHT NOW?)\n"
            "→ 15m — entry timing\n"
            "→ 1h — trend direction\n"
            "→ 4h — structure\n"
            "→ 1D — regime (trending up, ranging, or trending down)\n\n"
            "Only when 3+ align does it open a position.\n\n"
            "This single filter improved our win rate dramatically. "
            "Multi-timeframe confluence isn't optional. It's the edge.\n\n"
            "I post the signals this system generates → {cta}\n\n"
            "#TradingEducation #MultiTimeframe #CryptoTrading"
        ),
        "x": (
            "One filter that changed everything:\n\n"
            "Multi-timeframe confluence.\n\n"
            "5m confirms. 1h directs. 4h structures. 1D defines regime.\n\n"
            "3+ must align. Otherwise: no trade.\n\n"
            "Simple, but most skip it.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: TVC Terminal showing multi-timeframe analysis panel\n"
            "  URL: tradingventureclub.com/terminal/?token=BTC"
        ),
    },
    # 1 — Risk management
    {
        "li": (
            "Risk management isn't exciting. But it's the only reason we're still profitable.\n\n"
            "I coded these rules directly into the algorithm — they can't be overridden:\n\n"
            "→ Max 3 new trades per day — prevents overtrading in FOMO conditions\n"
            "→ 2h cooldown between same ticker — stops revenge trading\n"
            "→ Mandatory stop-loss at entry — no trade exists without an invalidation\n"
            "→ 5-day max hold — zombie positions get cut automatically\n"
            "→ Smart Money veto — even a perfect score gets rejected if institutional flow disagrees\n"
            "→ Minimum 1.0 R:R — no trade with more risk than reward\n\n"
            "The advantage of algorithmic trading: your rules don't bend when you're tired.\n\n"
            "Result: {total} trades, {wr}% win rate. Systems beat discipline.\n\n"
            "What's the one risk rule you wish you'd followed from day one?\n\n"
            "→ {cta}\n\n"
            "#RiskManagement #TradingPsychology #AlgoTrading"
        ),
        "x": (
            "Risk management isn't sexy.\n"
            "But it's the only reason we're still profitable.\n\n"
            "Our rules are hard-coded. The bot can't break them.\n\n"
            "Max 3 trades/day. Mandatory SL. Smart Money veto.\n\n"
            "{total} trades later: {wr}% WR.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: TVC Terminal positions panel + P&L History\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    },
    # 2 — Market regimes
    {
        "li": (
            "The same strategy that prints money in a trend will destroy you in a range.\n\n"
            "Most traders use one approach in all conditions. "
            "That's like wearing the same clothes in summer and winter.\n\n"
            "In our system, regime detection happens BEFORE any trade decision:\n\n"
            "→ TRENDING UP — Fibonacci filter loosens, giving room for pullback entries\n"
            "→ RANGING — filter tightens, only high-conviction setups near boundaries\n"
            "→ TRENDING DOWN — short bias, strictest filter, extremely selective\n\n"
            "The bot adapts automatically. No \"I think it'll bounce.\" Just data.\n\n"
            "Do you adapt your strategy to market conditions, or run the same playbook?\n\n"
            "→ {cta}\n\n"
            "#MarketRegimes #TradingStrategy #CryptoEducation"
        ),
        "x": (
            "The same strategy won't work in every market.\n\n"
            "Trend → wide TP, ride momentum\n"
            "Range → tight plays, boundaries only\n"
            "Downtrend → short bias, strict SL\n\n"
            "Our bot detects regime FIRST. Then adapts.\n\n"
            "Most traders skip this step entirely.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: TVC Terminal BTC chart with regime label + Fibonacci levels\n"
            "  URL: tradingventureclub.com/terminal/?token=BTC"
        ),
    },
    # 3 — Funding rates
    {
        "li": (
            "Funding rates: the signal most crypto traders overlook entirely.\n\n"
            "Quick primer on perpetual futures:\n\n"
            "Perps have no expiry. To keep them anchored to spot price, "
            "exchanges use a funding mechanism — longs pay shorts (or vice versa) every 8h.\n\n"
            "Why it matters:\n"
            "→ Extremely positive funding = everyone is long, paying a premium. "
            "Liquidation cascade risk is real.\n"
            "→ Extremely negative = shorts are paying. Squeeze risk.\n"
            "→ Near zero = balanced. Trend-following works best.\n\n"
            "Our Pump Radar checks funding across 200+ perpetual contracts daily. "
            "Extreme funding is one of the strongest contrarian signals we track.\n\n"
            "This is public data. The edge is knowing what to do with it.\n\n"
            "Did you know most exchanges show funding rates? Most traders never check.\n\n"
            "→ {cta}\n\n"
            "#FundingRates #CryptoDerivatives #TradingEducation"
        ),
        "x": (
            "Funding rates — one of crypto's best-kept secrets.\n\n"
            "Positive + extreme = flush incoming\n"
            "Negative + extreme = squeeze incoming\n"
            "Near zero = follow the trend\n\n"
            "We check 200+ tokens every run. Free alpha in plain sight.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: TVC Terminal Pump Radar panel — funding rate data visible\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    },
    # 4 — Derivatives data (OI, liquidations)
    {
        "li": (
            "Price is a lagging indicator. By the time you see a breakout on the chart, "
            "the move has already been positioned for in derivatives.\n\n"
            "Three derivatives signals I've found most predictive:\n\n"
            "1. Open Interest divergence — OI surges but price stays flat = "
            "someone is building a massive position. Direction TBD, but volatility is coming.\n\n"
            "2. Funding rate extremes — too crowded. The unwind becomes the trade.\n\n"
            "3. Liquidation clusters — large clusters act like magnets. "
            "Market makers know where the stops are.\n\n"
            "Our Pump Radar scans 200+ tokens every 5 minutes for exactly these patterns.\n\n"
            "The edge isn't the data (it's public). The edge is systematizing the interpretation.\n\n"
            "What derivatives metrics do you track?\n\n"
            "→ {cta}\n\n"
            "#Derivatives #TradingEducation #CryptoMarkets"
        ),
        "x": (
            "Derivatives tell you what's coming before price does.\n\n"
            "OI surging + price flat = big move loading\n"
            "Funding extreme = crowded trade unwinding\n"
            "Liquidation cluster = price magnet\n\n"
            "Our Pump Radar scans 200+ tokens for this. Every 5 min.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: TVC Terminal Pump Radar panel — OI + funding + factor breakdown\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    },
    # 5 — Holy grail myth
    {
        "li": (
            "Stop looking for the holy grail indicator. It doesn't exist.\n\n"
            "I spent months testing RSI, MACD, Bollinger, Ichimoku. "
            "None worked consistently. The breakthrough came when I stopped looking "
            "for one signal and started combining many.\n\n"
            "Our Fusion Score combines 5 weighted factors: on-chain flow, "
            "technical structure, derivatives, momentum, and sentiment. "
            "No single factor wins consistently. "
            "But when multiple weak signals align, the edge compounds.\n\n"
            "On top of that, 12 rejection gates filter out bad setups. "
            "The system says \"no\" far more often than \"yes.\"\n\n"
            "That selectivity IS the edge. Not the entry signal — the filtration.\n\n"
            "Result: {total} trades, {wr}% win rate. "
            "Not because we found the holy grail. "
            "Because we stacked imperfect signals correctly.\n\n"
            "What indicator did you rely on longest?\n\n"
            "→ {cta}\n\n"
            "#TradingMythBusted #QuantFinance #CryptoTrading"
        ),
        "x": (
            "There is no holy grail indicator.\n\n"
            "What works:\n"
            "→ Multiple weak signals > one \"perfect\" signal\n"
            "→ Rigid risk rules > flexible entry rules\n"
            "→ Adapting to regime > one-size-fits-all\n\n"
            "5 factors + 3 SM layers + 12 gates = {wr}% WR over {total} trades.\n\n"
            "Free signals: {cta}"
        ),
        "graphic": (
            "📸 GRAPHIC: TVC Terminal Fusion Score breakdown + Smart Money panel\n"
            "  URL: tradingventureclub.com/terminal/"
        ),
    },
]


def gen_friday():
    """Education — one concept explained. Rotates weekly."""
    stats = get_cumulative_stats()
    idx = _week() % len(_FRIDAY_TOPICS)
    topic = _FRIDAY_TOPICS[idx]

    fmt = {"cta": CTA, "total": stats["total"], "wr": stats["wr"]}

    li = topic["li"].format(**fmt)
    x = topic["x"].format(**fmt)
    gr = topic["graphic"]

    return [_post("linkedin", "education", li, gr), _post("x", "education", x, gr)]


# ═══════════════════════════════════════════════════════════════════════════════
# SUNDAY — WEEK AHEAD PREVIEW
# ═══════════════════════════════════════════════════════════════════════════════

def gen_sunday():
    """Forward-looking: what to watch next week."""
    fusion = get_fusion_data()
    stats = get_cumulative_stats()
    opens = get_open_positions()
    decisions = fusion.get("decisions", [])
    btc = next((d for d in decisions if d.get("ticker") == "BTC"), None)

    watch_items = []

    if btc:
        score = btc.get("fusion_score", 50)
        regime = btc.get("regime", "UNKNOWN").replace("_", " ").title()
        watch_items.append(f"$BTC regime: {regime} (score {score}/100)")

    for d in decisions:
        if d.get("ticker") != "BTC":
            s = d.get("fusion_score", 0)
            if s >= 60 or s <= 35:
                label = "bullish setup" if s >= 60 else "weakness detected"
                watch_items.append(f"${d['ticker']}: {label} (score {s}/100)")

    if opens:
        tickers = [p["ticker"] for p in opens[:3]]
        watch_items.append(f"Open positions: {', '.join('$' + t for t in tickers)} — managing risk")

    watch_items = watch_items[:4] or [
        "All eyes on $BTC — direction determines alt behavior next week"
    ]

    wp = "\n".join(f"→ {w}" for w in watch_items)

    li = (
        f"What I'm watching next week:\n\n"
        f"{wp}\n\n"
        f"My system adjusts automatically:\n"
        f"→ Macro blackout: no new trades 3h before / 1h after Tier-1 events\n"
        f"→ Regime detection adapts entry criteria in real-time\n"
        f"→ Smart Money veto stays active — no override possible\n\n"
        f"I'll share signals as they fire throughout the week.\n\n"
        f"What's on YOUR radar?\n\n"
        f"Free daily signals → {CTA}\n\n"
        f"#CryptoTrading #WeekAhead #SmartMoney #MarketAnalysis"
    )

    short_items = [w.split("—")[0].strip() for w in watch_items[:3]]
    x_list = "\n".join(f"{i+1}. {item}" for i, item in enumerate(short_items))
    x = (
        f"Next week — watching:\n\n"
        f"{x_list}\n\n"
        f"Signals fire when conditions align. Not before.\n\n"
        f"Free signals: {CTA}"
    )

    gr = (
        "📸 GRAPHIC: TVC Terminal dashboard — multi-token overview + macro calendar\n"
        "  URL: tradingventureclub.com/terminal/"
    )

    return [_post("linkedin", "week_preview", li, gr),
            _post("x", "week_preview", x, gr)]


# ═══════════════════════════════════════════════════════════════════════════════
# BONUS — TRADE CLOSE POSTS (any day, max 1/platform)
# ═══════════════════════════════════════════════════════════════════════════════

def gen_trade_close(trade):
    """Generate posts for a notable closed trade — bonus content."""
    ticker = trade["ticker"]
    direction = trade.get("direction", "long").upper()
    pnl_pct = trade.get("pnl_pct", 0)
    pnl_usd = trade.get("pnl_usd", 0)
    entry = trade.get("entry_price", 0)
    exit_px = trade.get("exit_price", 0)
    score = trade.get("fusion_score", 0)
    reason = trade.get("hit_or_miss", "")
    stats = get_cumulative_stats()

    is_win = pnl_usd > 0

    exit_labels = {
        "hit_tp1": "TP1 hit", "hit_tp2": "TP2 hit", "hit_sl": "stopped out",
        "hit_trailing_sl": "trailing stop", "zombie_close": "max hold reached",
    }
    exit_label = exit_labels.get(reason, reason or "closed")

    if is_win:
        x = (
            f"✅ ${ticker} {direction} closed +{pnl_pct:.1f}%\n\n"
            f"{_fmt_px(entry)} → {_fmt_px(exit_px)} ({exit_label})\n"
            f"Fusion Score at entry: {score}/100\n\n"
            f"Bot stats: {stats['total']} trades, {stats['wr']}% WR\n\n"
            f"Free signals: {CTA}\n"
            f"#{ticker} #crypto #trading"
        )
    else:
        x = (
            f"❌ ${ticker} {direction} stopped out {pnl_pct:+.1f}%\n\n"
            f"Risk managed. WR still {stats['wr']}% over {stats['total']} trades.\n\n"
            f"Transparency > hype.\n\n"
            f"Free signals: {CTA}\n"
            f"#{ticker} #crypto"
        )

    if is_win:
        li = (
            f"My system just closed a {direction} on ${ticker} at +{pnl_pct:.1f}%.\n\n"
            f"Entry {_fmt_px(entry)} → exit {_fmt_px(exit_px)} via {exit_label}.\n"
            f"Fusion Score was {score}/100 at entry — on-chain + technical + derivatives all aligned.\n\n"
            f"This wasn't a gut call. It was an algorithm following rules I defined months ago.\n\n"
            f"Running stats: {stats['total']} trades, {stats['wr']}% win rate, "
            f"${stats['pnl']:+.0f} cumulative PnL.\n\n"
            f"I share every signal — wins and losses → {CTA}\n\n"
            f"#CryptoTrading #AlgoTrading #SmartMoney"
        )
    else:
        li = (
            f"Not every trade wins — and that's the point of a system.\n\n"
            f"${ticker} {direction} was stopped out at {pnl_pct:+.1f}%. "
            f"Fusion Score was {score}/100 at entry. Exit: {exit_label}.\n\n"
            f"Overall record still holds: {stats['wr']}% win rate over "
            f"{stats['total']} trades, ${stats['pnl']:+.0f} total PnL.\n\n"
            f"I post the losses alongside the wins. "
            f"If someone only shows you winners — that's a red flag.\n\n"
            f"Full transparency → {CTA}\n\n"
            f"#CryptoTrading #Transparency #AlgoTrading"
        )

    gr = (
        f"📸 GRAPHIC: TVC Terminal ${ticker} chart with entry/exit + P&L History panel\n"
        f"  URL: tradingventureclub.com/terminal/?token={ticker}"
    )

    trade_id = f"{ticker}_{trade.get('closed_at', '')[:16]}"
    return [
        _post("linkedin", "trade_closed", li, gr, ticker, {"_trade_id": trade_id}),
        _post("x", "trade_closed", x, gr, ticker, {"_trade_id": trade_id}),
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — send posts to personal chat for copy-paste
# ═══════════════════════════════════════════════════════════════════════════════

def _tg_send_personal(text):
    """Send to personal Telegram for copy-paste convenience."""
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
        data = up.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = ur.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with ur.urlopen(req, timeout=10, context=ctx) as r:
            log(f"telegram personal: {r.status}")
    except Exception as e:
        log(f"telegram personal failed: {e}")


def _notify_new_posts(posts):
    """Send full post texts to personal Telegram — ready for copy-paste."""
    if not posts:
        return

    summary = f"📝 <b>Content Engine v3.0 — {len(posts)} nowych postów</b>\n\n"
    platforms = {}
    for p in posts:
        pl = p["platform"].upper()
        platforms[pl] = platforms.get(pl, 0) + 1
    summary += " · ".join(f"{k}: {v}" for k, v in platforms.items())
    _tg_send_personal(summary)

    for i, p in enumerate(posts, 1):
        platform = p["platform"].upper()
        ptype = p["type"].replace("_", " ").title()
        ticker = p.get("ticker") or ""

        header = f"━━━ <b>[{platform}] {ptype}</b>"
        if ticker:
            header += f" — {ticker}"
        header += f" ({i}/{len(posts)}) ━━━"

        text = p["text"].replace("<", "&lt;").replace(">", "&gt;")

        graphic = p.get("graphic", "")
        graphic_line = ""
        if graphic:
            graphic_clean = graphic.replace("📸 GRAPHIC: ", "").strip()
            graphic_line = f"\n\n📸 <b>Grafika:</b>\n{graphic_clean}"

        full_msg = f"{header}\n\n{text}{graphic_line}"

        if len(full_msg) > 4000:
            full_msg = full_msg[:3990] + "\n\n[...truncated]"

        _tg_send_personal(full_msg)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    weekday = _now().weekday()
    theme = DAY_THEMES.get(weekday, "Unknown")
    log(f"Content Engine v3.0 — {_now().isoformat()} — {theme}")

    q = _load_queue()
    new_posts = []

    x_today = _posts_today(q, "x")
    li_today = _posts_today(q, "linkedin")
    log(f"Posts today so far: X={x_today}, LinkedIn={li_today}")

    # ── 1. DAILY THEMED CONTENT (one per platform) ──────────────────────────

    day_type_map = {
        0: ("data_drop", gen_monday),
        1: ("behind_build", gen_tuesday),
        2: ("market_analysis", gen_wednesday),
        3: ("weekly_results", gen_thursday),
        4: ("education", gen_friday),
        # 5 = Saturday, no posts
        6: ("week_preview", gen_sunday),
    }

    if weekday in day_type_map:
        ptype, generator = day_type_map[weekday]
        if not _has_type_today(q, ptype):
            log(f"Generating daily themed posts: {ptype}")
            themed = generator()
            for p in themed:
                if p["platform"] == "x" and x_today >= MAX_X_PER_DAY:
                    log(f"  skip X {ptype} — daily limit")
                    continue
                if p["platform"] == "linkedin" and li_today >= MAX_LI_PER_DAY:
                    log(f"  skip LinkedIn {ptype} — daily limit")
                    continue
                new_posts.append(p)
                if p["platform"] == "x":
                    x_today += 1
                else:
                    li_today += 1
            log(f"  ✓ themed posts generated")
        else:
            log(f"Daily {ptype} already generated today — skipping")
    elif weekday == 5:
        log("Saturday — engagement-only day, no posts generated")

    # ── 2. BONUS: TRADE CLOSE POSTS (any day, if notable) ───────────────────

    closes = get_recent_closes(hours=6)
    if closes:
        log(f"Found {len(closes)} recent closed trade(s)")
        for trade in closes:
            trade_id = f"{trade['ticker']}_{trade.get('closed_at', '')[:16]}"
            if any(p.get("_trade_id") == trade_id for p in q["posts"]):
                log(f"  skip {trade['ticker']} — already in queue")
                continue

            posts = gen_trade_close(trade)
            for p in posts:
                if p["platform"] == "x" and x_today >= MAX_X_PER_DAY:
                    continue
                if p["platform"] == "linkedin" and li_today >= MAX_LI_PER_DAY:
                    continue
                new_posts.append(p)
                if p["platform"] == "x":
                    x_today += 1
                else:
                    li_today += 1
            log(f"  ✓ trade close posts for {trade['ticker']} "
                f"({trade.get('pnl_pct', 0):+.1f}%)")

    # ── 3. SAVE + NOTIFY ────────────────────────────────────────────────────

    if new_posts:
        q["posts"].extend(new_posts)
        q["meta"]["last_run"] = _now().isoformat()
        q["meta"]["version"] = "3.0"
        q["meta"]["total_generated"] = len(q["posts"])
        _save_queue(q)
        log(f"✓ Added {len(new_posts)} new posts to queue (total: {len(q['posts'])})")

        _notify_new_posts(new_posts)
    else:
        log("No new posts to generate this cycle.")
        q["meta"]["last_run"] = _now().isoformat()
        q["meta"]["version"] = "3.0"
        _save_queue(q)

    # Cleanup: keep only last 7 days
    cutoff = (_now() - timedelta(days=7)).strftime("%Y-%m-%d")
    before = len(q["posts"])
    q["posts"] = [p for p in q["posts"] if p.get("date", "") >= cutoff]
    if len(q["posts"]) < before:
        _save_queue(q)
        log(f"Cleaned up {before - len(q['posts'])} old posts (>7 days)")


if __name__ == "__main__":
    run()
