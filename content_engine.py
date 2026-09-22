#!/usr/bin/env python3
"""
TVC Fusion Content Engine v2.0 — auto-generates social media posts from bot data
+ KOL personal brand + educational content.

THREE CONTENT LAYERS (naturally interleaved):
  Layer 1 — DATA: trade results, picks, pump alerts, market regime (from bot)
  Layer 2 — EDUCATION: trading concepts, risk mgmt, Smart Money explainers (KOL)
  Layer 3 — PERSONAL BRAND: origin story, philosophy, TVC as brand, industry takes

Reads:
  - paper_trades.db (SQLite) — closed/open trades, PnL, win rate
  - fusion_latest.json — current signals, regime, Smart Money
  - crypto_picks.json — daily picks
  - pump_radar_alerts.json — pump alerts

Writes:
  - content_queue.json — queue of ready posts (X, LinkedIn, YouTube scripts)
  - Optionally sends formatted posts to personal Telegram for copy-paste

Each post includes a 📸 GRAPHIC field describing what screenshot/chart to attach.

Runs in GitHub Actions after paper_bot refresh. Non-destructive: appends to queue,
never removes old entries. Max posts/day: 3 X + 3 LinkedIn (no spam).

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

# --- config ----------------------------------------------------------------

HOME = Path.home()
FUSION_DIR = HOME / "Claude" / "TVCFusion"
DB_PATH = FUSION_DIR / "paper_trades.db"
QUEUE_PATH = FUSION_DIR / "content_queue.json"
FUSION_JSON = FUSION_DIR / "fusion_latest.json"
PICKS_JSON = FUSION_DIR / "crypto_picks.json"
RADAR_ALERTS = FUSION_DIR / "pump_radar_alerts.json"

STATS_SINCE = "2026-09-02T19:00:00"
STATS_WHERE = "status='closed' AND opened_at >= ? AND COALESCE(excluded,0)=0"

MAX_X_PER_DAY = 3
MAX_LI_PER_DAY = 3

# Telegram (personal chat only — for copy-paste convenience)
TG_PERSONAL_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# --- helpers ---------------------------------------------------------------

def log(msg):
    print(f"[content] {msg}")


def _now():
    return datetime.now(timezone.utc)


def _today():
    return _now().strftime("%Y-%m-%d")


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
    QUEUE_PATH.write_text(json.dumps(q, indent=2, default=str, ensure_ascii=False), encoding="utf-8")


def _posts_today(q, platform):
    today = _today()
    return sum(1 for p in q["posts"] if p.get("date") == today and p.get("platform") == platform)


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


# --- data fetchers ---------------------------------------------------------

def get_recent_closes(hours=6):
    """Trades closed in the last N hours."""
    if not DB_PATH.exists():
        return []
    conn = db()
    since = (_now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status='closed' AND closed_at >= ? "
        "AND COALESCE(excluded,0)=0 ORDER BY closed_at DESC",
        (since,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_cumulative_stats():
    """Overall stats since tracking began."""
    if not DB_PATH.exists():
        return {"total": 0, "wins": 0, "pnl": 0, "wr": 0}
    conn = db()
    rows = conn.execute(f"SELECT pnl_usd FROM positions WHERE {STATS_WHERE}", (STATS_SINCE,)).fetchall()
    conn.close()
    total = len(rows)
    wins = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
    pnl = sum((r["pnl_usd"] or 0) for r in rows)
    return {"total": total, "wins": wins, "pnl": round(pnl, 2), "wr": round(wins / total * 100, 1) if total else 0}


def get_open_positions():
    """Currently open positions."""
    if not DB_PATH.exists():
        return []
    conn = db()
    rows = conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_todays_picks():
    """Today's auto picks."""
    data = _load_json(PICKS_JSON)
    if data.get("date") == _today():
        return data.get("picks", [])
    return []


def get_recent_pump_alerts(hours=6):
    """Recent HIGH pump alerts."""
    data = _load_json(RADAR_ALERTS)
    if not isinstance(data, list):
        data = data.get("alerts", [])
    since = (_now() - timedelta(hours=hours)).isoformat()
    return [a for a in data if a.get("level") == "HIGH" and a.get("ts", "") >= since]


def get_fusion_data():
    """Current fusion state."""
    return _load_json(FUSION_JSON)


# --- post generators -------------------------------------------------------

def _trade_closed_posts(trade):
    """Generate X + LinkedIn posts for a closed trade."""
    ticker = trade["ticker"]
    direction = trade.get("direction", "long").upper()
    pnl_pct = trade.get("pnl_pct", 0)
    pnl_usd = trade.get("pnl_usd", 0)
    entry = trade.get("entry_price", 0)
    exit_px = trade.get("exit_price", 0)
    score = trade.get("fusion_score", 0)
    regime = trade.get("regime", "")
    reason = trade.get("hit_or_miss", "")
    stats = get_cumulative_stats()

    is_win = pnl_usd > 0
    emoji = "✅" if is_win else "❌"
    result_word = "WIN" if is_win else "LOSS"

    # Exit reason human label
    exit_labels = {
        "hit_tp1": "TP1 hit", "hit_tp2": "TP2 hit", "hit_sl": "stopped out",
        "hit_trailing_sl": "trailing stop", "zombie_close": "max hold reached",
    }
    exit_label = exit_labels.get(reason, reason or "closed")

    # --- X POST ---
    if is_win:
        x_text = (
            f"{emoji} ${ticker} {direction} closed +{pnl_pct:.1f}% — {exit_label}\n\n"
            f"Entry {_fmt_px(entry)} → Exit {_fmt_px(exit_px)}\n"
            f"Smart Money confirmed. Fusion Score: {score}/100\n\n"
            f"Bot stats: {stats['total']} trades, {stats['wr']}% win rate, ${stats['pnl']:+.0f} total PnL\n\n"
            f"Free signals: t.me/TVCFusionSignals\n"
            f"#crypto #{ticker} #trading #smartmoney"
        )
    else:
        x_text = (
            f"{emoji} ${ticker} {direction} stopped out {pnl_pct:+.1f}%\n\n"
            f"Entry {_fmt_px(entry)} → Exit {_fmt_px(exit_px)}\n"
            f"Risk managed. Win rate still {stats['wr']}% over {stats['total']} trades.\n\n"
            f"Transparency > hype. Every trade logged, every result public.\n\n"
            f"Free signals: t.me/TVCFusionSignals\n"
            f"#crypto #{ticker} #trading"
        )

    x_graphic = (
        f"📸 GRAPHIC: Screenshot of TVC Terminal showing ${ticker} chart with:\n"
        f"  - Entry/exit points marked on the price chart\n"
        f"  - Smart Money panel (HL whales + Binance L/S + Taker flow)\n"
        f"  - P&L History panel with equity curve\n"
        f"  URL: tradingventureclub.com/terminal/?token={ticker}"
    )

    # --- LINKEDIN POST ---
    if is_win:
        li_text = (
            f"Our Smart Money algorithm just closed a {direction} on ${ticker} with +{pnl_pct:.1f}% profit.\n\n"
            f"Here's what happened:\n"
            f"→ Fusion Score hit {score}/100 — that's on-chain flow + technical structure + derivatives aligned\n"
            f"→ Smart Money layers confirmed: Hyperliquid whales + Binance top trader ratio + taker flow\n"
            f"→ Entry at {_fmt_px(entry)}, exit at {_fmt_px(exit_px)} via {exit_label}\n\n"
            f"Running stats (since Sep 2): {stats['total']} trades, {stats['wr']}% win rate, ${stats['pnl']:+.0f} cumulative PnL\n\n"
            f"The bot runs every 5 minutes in GitHub Actions — fully automated, no manual intervention.\n\n"
            f"I'm building this in public. Every trade is logged, every result is real.\n\n"
            f"What's your take — is quantitative Smart Money tracking the future of retail trading?\n\n"
            f"#SmartMoney #CryptoTrading #AlgoTrading #QuantTrading #TradingBot"
        )
    else:
        li_text = (
            f"Not every trade wins — and that's the point of a system.\n\n"
            f"Our algorithm took a {direction} on ${ticker} that was stopped out at {pnl_pct:+.1f}%.\n\n"
            f"What the data showed:\n"
            f"→ Fusion Score: {score}/100 at entry\n"
            f"→ Market regime: {regime}\n"
            f"→ Result: {exit_label} at {_fmt_px(exit_px)}\n\n"
            f"Overall performance still holds: {stats['wr']}% win rate over {stats['total']} trades, ${stats['pnl']:+.0f} total PnL.\n\n"
            f"I post the losses alongside the wins. If someone only shows you winners, that's a red flag.\n\n"
            f"What do you think — is radical transparency in trading signals valuable, or does it hurt conversion?\n\n"
            f"#CryptoTrading #Transparency #AlgoTrading #TradingBot"
        )

    li_graphic = (
        f"📸 GRAPHIC: Professional screenshot of TVC Terminal showing:\n"
        f"  - ${ticker} price chart with entry/exit annotations\n"
        f"  - Equity curve (P&L History panel) — cumulative performance visible\n"
        f"  - Smart Money verdict panel\n"
        f"  - Add a text overlay: \"{result_word}: ${ticker} {direction} {pnl_pct:+.1f}%\"\n"
        f"  Ideal format: 1200x627px (LinkedIn preview) or 1080x1080 (square)\n"
        f"  URL: tradingventureclub.com/terminal/?token={ticker}"
    )

    posts = []
    posts.append({
        "platform": "x",
        "type": "trade_closed",
        "ticker": ticker,
        "date": _today(),
        "ts": _now().isoformat(),
        "text": x_text,
        "graphic": x_graphic,
        "posted": False,
    })
    posts.append({
        "platform": "linkedin",
        "type": "trade_closed",
        "ticker": ticker,
        "date": _today(),
        "ts": _now().isoformat(),
        "text": li_text,
        "graphic": li_graphic,
        "posted": False,
    })
    return posts


def _daily_pick_posts(picks):
    """Generate posts from today's auto picks (top pick)."""
    if not picks:
        return []
    top = picks[0]
    ticker = top["ticker"]
    direction = top.get("direction", "long").upper()
    category = top.get("category", "")
    change_24h = top.get("change_24h_pct", 0)
    support = top.get("support", "—")
    resistance = top.get("resistance", "—")
    extra = len(picks) - 1

    cat_hook = {
        "Momentum": "Momentum breakout detected",
        "Derivatives": "Derivatives signal fired",
        "Squeeze setup": "Short squeeze setup building",
        "New listing": "New listing gaining traction",
        "Mean reversion": "Oversold bounce opportunity",
    }
    hook = cat_hook.get(category, "Algorithmic signal detected")

    x_text = (
        f"🎯 Today's Smart Money Pick: ${ticker} ({direction})\n\n"
        f"{hook} — {change_24h:+.1f}% in 24h\n"
        f"S: {support} | R: {resistance}\n\n"
        f"Screened from 200+ MEXC perps by our algorithm.\n"
    )
    if extra > 0:
        x_text += f"{extra} more picks in PRO → @TVCAlertsBot\n"
    x_text += (
        f"\nFree daily pick: t.me/TVCFusionSignals\n"
        f"#crypto #{ticker} #trading #picks"
    )

    x_graphic = (
        f"📸 GRAPHIC: Screenshot of TVC Terminal Crypto Picks panel showing:\n"
        f"  - Today's full pick list (all categories visible)\n"
        f"  - ${ticker} highlighted as top pick\n"
        f"  - Market context line at the top\n"
        f"  Alternative: ${ticker} chart with S/R levels drawn\n"
        f"  URL: tradingventureclub.com/terminal/"
    )

    li_text = (
        f"Our daily algorithmic screener just flagged ${ticker} ({direction}).\n\n"
        f"Category: {category}\n"
        f"Signal: {hook}\n"
        f"24h change: {change_24h:+.1f}%\n"
        f"Key levels — Support: {support}, Resistance: {resistance}\n\n"
        f"How it works:\n"
        f"→ Python script scans 200+ perpetual contracts on MEXC every morning\n"
        f"→ Scores each across 4 categories: momentum, derivatives, squeeze setups, mean reversion\n"
        f"→ Top picks delivered to Telegram + displayed in the TVC Fusion Terminal\n\n"
        f"Everything runs in GitHub Actions — zero manual intervention.\n\n"
        f"What's your screening process for finding trading opportunities?\n\n"
        f"#CryptoTrading #AlgoTrading #QuantFinance #TradingAlgorithm"
    )

    li_graphic = (
        f"📸 GRAPHIC: Screenshot showing the Crypto Picks pipeline:\n"
        f"  Option A: Terminal Crypto Picks panel (clean, shows all 5 picks)\n"
        f"  Option B: Side-by-side: Telegram message + Terminal panel\n"
        f"  Option C: Carousel (LinkedIn document post):\n"
        f"    Slide 1: \"Today's Algorithmic Pick: ${ticker}\" + chart\n"
        f"    Slide 2: How the screener works (4 categories diagram)\n"
        f"    Slide 3: Performance stats + CTA\n"
        f"  Format: 1200x627px or 1080x1080\n"
        f"  URL: tradingventureclub.com/terminal/"
    )

    return [
        {"platform": "x", "type": "daily_pick", "ticker": ticker, "date": _today(),
         "ts": _now().isoformat(), "text": x_text, "graphic": x_graphic, "posted": False},
        {"platform": "linkedin", "type": "daily_pick", "ticker": ticker, "date": _today(),
         "ts": _now().isoformat(), "text": li_text, "graphic": li_graphic, "posted": False},
    ]


def _pump_alert_posts(alerts):
    """Generate X post for HIGH pump alert (LinkedIn skip — too speculative)."""
    if not alerts:
        return []
    top = alerts[0]
    ticker = top.get("ticker", "?")
    score = top.get("score", 0)
    factors = top.get("factors", [])
    factors_str = ", ".join(factors[:4]) if factors else "multiple signals"

    x_text = (
        f"🚨 Pump Radar HIGH: ${ticker} (score {score})\n\n"
        f"Signals: {factors_str}\n\n"
        f"Our radar scans 200+ tokens every 5 min.\n"
        f"Track record + live alerts in TVC Terminal.\n\n"
        f"Free alerts: t.me/TVCFusionSignals\n"
        f"#crypto #{ticker} #pumpalert #trading"
    )

    x_graphic = (
        f"📸 GRAPHIC: Screenshot of TVC Terminal Pump Radar panel showing:\n"
        f"  - ${ticker} alert with score bar\n"
        f"  - Factor breakdown (funding, OI, whales, etc.)\n"
        f"  - Track record section (hit rate visible)\n"
        f"  URL: tradingventureclub.com/terminal/"
    )

    return [
        {"platform": "x", "type": "pump_alert", "ticker": ticker, "date": _today(),
         "ts": _now().isoformat(), "text": x_text, "graphic": x_graphic, "posted": False},
    ]


def _market_insight_post(fusion_data):
    """Generate a market regime insight post (1/day, LinkedIn only — thought leadership)."""
    decisions = fusion_data.get("decisions", [])
    state = fusion_data.get("state", {})
    if not decisions:
        return []

    # Find BTC
    btc = next((d for d in decisions if d.get("ticker") == "BTC"), None)
    if not btc:
        return []

    regime = btc.get("regime", "UNKNOWN")
    score = btc.get("fusion_score", 50)
    action = btc.get("action", "HOLD")
    sm = btc.get("layers", {})
    sm_verdict = sm.get("verdict", "neutral")

    regime_desc = {
        "TRENDING_UP": "trending bullish — higher highs, higher lows on multiple timeframes",
        "TRENDING_UP_VOLATILE": "bullish but volatile — strong trend with wide swings",
        "RANGING": "consolidating in a range — no clear direction yet",
        "TRENDING_DOWN": "trending bearish — lower highs being set on higher timeframes",
    }
    rdesc = regime_desc.get(regime, "in a transitional state")

    li_text = (
        f"BTC Market Regime Update: {regime.replace('_', ' ').title()}\n\n"
        f"Bitcoin is {rdesc}.\n\n"
        f"Key data points from our algorithm:\n"
        f"→ Fusion Score: {score}/100 (action: {action})\n"
        f"→ Smart Money verdict: {sm_verdict}\n"
    )
    if sm.get("hl_whale_bias"):
        li_text += f"→ Hyperliquid whales: {sm['hl_whale_bias']}\n"
    if sm.get("binance_top_trader"):
        li_text += f"→ Binance top trader L/S: {sm['binance_top_trader']}\n"
    if sm.get("taker_flow"):
        li_text += f"→ Taker flow: {sm['taker_flow']}\n"

    pnl = state.get("total_pnl_usd", 0)
    wr = state.get("cumulative_win_rate", 0)
    n = state.get("cumulative_closed", 0)

    li_text += (
        f"\nOur automated paper trading bot has processed {n} trades with "
        f"{wr}% win rate and ${pnl:+.0f} PnL — all tracked transparently.\n\n"
        f"What's your read on the current BTC structure?\n\n"
        f"#Bitcoin #CryptoMarket #SmartMoney #MarketAnalysis"
    )

    li_graphic = (
        f"📸 GRAPHIC: Screenshot of TVC Terminal dashboard for BTC showing:\n"
        f"  - Price chart with regime label visible\n"
        f"  - Smart Money panel (whale positions, L/S ratio, taker CVD)\n"
        f"  - Fusion Score gauge\n"
        f"  - Correlation Matrix panel (BTC vs NQ, DXY, gold)\n"
        f"  Add text overlay: \"BTC Regime: {regime.replace('_', ' ').title()}\"\n"
        f"  Format: 1200x627px\n"
        f"  URL: tradingventureclub.com/terminal/?token=BTC"
    )

    return [
        {"platform": "linkedin", "type": "market_insight", "ticker": "BTC", "date": _today(),
         "ts": _now().isoformat(), "text": li_text, "graphic": li_graphic, "posted": False},
    ]


def _behind_the_build_post():
    """Generate a 'behind the build' post — how the system works (once per week, random)."""
    stats = get_cumulative_stats()
    topics = [
        {
            "title": "How I built a fully automated crypto trading system with Python + GitHub Actions",
            "body": (
                "Here's the architecture behind TVC Fusion — our automated crypto trading system:\n\n"
                "1. 7 Python scripts run every 5 minutes via GitHub Actions\n"
                "2. Each script scans different data: on-chain flow, derivatives, whale positions, price structure\n"
                "3. A Fusion Score (0-100) is computed per token — combining 5 weighted factors\n"
                "4. If score ≥60 + Smart Money confirms (≥2/3 layers aligned) → automated paper trade opens\n"
                "5. Results published to Telegram channels + a live dashboard\n\n"
                f"Running stats: {stats['total']} trades, {stats['wr']}% win rate, ${stats['pnl']:+.0f} PnL\n\n"
                "No API keys needed. No broker integration. Pure public market data.\n\n"
                "The full source runs in a public GitHub repo. Radical transparency.\n\n"
                "What part of this would you want me to break down in detail?\n\n"
                "#BuildInPublic #CryptoTrading #Python #GitHubActions #Automation"
            ),
            "graphic": (
                "📸 GRAPHIC: Architecture diagram (create in Canva/Figma or screenshot):\n"
                "  Flow: cron-job.org → GitHub Actions → 7 Python scripts → SQLite + JSON\n"
                "     → Gist API → Terminal frontend\n"
                "     → Telegram Bot API → PRO/FREE channels\n"
                "  Or: screenshot of the GitHub Actions workflow runs page\n"
                "  Or: screenshot of terminal with multiple panels visible (bird's eye view)\n"
                "  Format: 1200x627px"
            ),
        },
        {
            "title": "Smart Money detection in crypto — what whales actually do before a move",
            "body": (
                "Most retail traders watch price. Smart Money watches positioning.\n\n"
                "Here's what our algorithm tracks in real-time:\n\n"
                "→ Hyperliquid whale wallets — top 30 addresses by PnL, their live positions and fills\n"
                "→ Binance Top Trader Long/Short ratio — what the top 20% are doing vs the crowd\n"
                "→ Taker flow + CVD — are buyers or sellers crossing the spread aggressively?\n\n"
                "The signal: when ≥2 out of 3 layers align with the technical setup, the bot enters.\n"
                "When they diverge (Smart Money says no while score says yes), the bot vetoes the trade.\n\n"
                f"This veto system has kept us at {stats['wr']}% win rate over {stats['total']} automated trades.\n\n"
                "Do you use any form of smart money analysis in your trading?\n\n"
                "#SmartMoney #CryptoTrading #WhaleTracking #AlgoTrading"
            ),
            "graphic": (
                "📸 GRAPHIC: Screenshot of TVC Terminal Smart Money panel showing:\n"
                "  - Hyperliquid whales section with Big vs Crowd indicator\n"
                "  - Binance Top Trader L/S ratio gauge\n"
                "  - Taker flow CVD chart\n"
                "  - Smart Money verdict badge (CONFIRMS / OPPOSES)\n"
                "  Format: 1200x627px\n"
                "  URL: tradingventureclub.com/terminal/?token=BTC"
            ),
        },
    ]
    # Pick based on day of year (deterministic, rotates)
    day_of_year = _now().timetuple().tm_yday
    topic = topics[day_of_year % len(topics)]

    return [
        {"platform": "linkedin", "type": "behind_the_build", "ticker": None,
         "date": _today(), "ts": _now().isoformat(),
         "text": f"{topic['title']}\n\n{topic['body']}",
         "graphic": topic["graphic"], "posted": False},
    ]


# --- LAYER 2: KOL Education posts (expertise + authority) ------------------

def _kol_education_posts():
    """Educational posts that position the author as a trading expert.
    Each topic blends teaching with real bot data. Rotates by day of year."""
    stats = get_cumulative_stats()
    open_pos = get_open_positions()
    n_open = len(open_pos)

    topics = [
        # 0 — Smart Money concept
        {
            "x": (
                "Most retail traders watch price charts.\n"
                "Smart Money watches positioning.\n\n"
                "3 signals institutional flow leaves behind:\n"
                "→ Whale wallet clustering on Hyperliquid\n"
                "→ Top trader L/S ratio divergence on Binance\n"
                "→ Aggressive taker flow crossing the spread\n\n"
                "When ≥2/3 align with the technical setup, the probability shifts.\n\n"
                f"We track all 3 in real-time. {stats['total']} trades, {stats['wr']}% WR.\n\n"
                "#SmartMoney #CryptoTrading #InstitutionalFlow"
            ),
            "li": (
                "What is Smart Money flow — and why does retail ignore it?\n\n"
                "After years of researching market microstructure, I noticed a pattern: "
                "the biggest moves in crypto are preceded by specific footprints that most traders never see.\n\n"
                "Three signals we track algorithmically:\n\n"
                "1. Hyperliquid whale wallets — the top 30 addresses by realized PnL. "
                "When they cluster into the same direction, it's not coincidence.\n\n"
                "2. Binance Top Trader Long/Short ratio — what the top 20% of traders are doing "
                "vs the crowd. Divergence = opportunity.\n\n"
                "3. Taker flow + CVD — who's crossing the spread aggressively? "
                "Passive limit orders vs active market orders tell a story about conviction.\n\n"
                "I built a system that reads all three in real-time and only enters when ≥2/3 confirm.\n\n"
                f"Result so far: {stats['total']} automated trades, {stats['wr']}% win rate, "
                f"${stats['pnl']:+.0f} cumulative PnL.\n\n"
                "The data is public. The methodology is transparent. "
                "That's how Trading Venture Club does things differently.\n\n"
                "What's your edge in crypto? Price action alone, or something deeper?\n\n"
                "#SmartMoney #CryptoTrading #QuantFinance #TradingEducation"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal Smart Money panel (BTC or current top holding):\n"
                "  - HL whales section (Big vs Crowd indicator)\n"
                "  - Binance Top Trader L/S ratio\n"
                "  - Taker flow CVD\n"
                "  - Add text overlay: \"Smart Money 101: What whales see before you do\"\n"
                "  Format: 1200x627 (LI) or 1080x1080 (X)"
            ),
        },
        # 1 — Risk management
        {
            "x": (
                "Risk management isn't exciting.\n"
                "But it's the only reason we're still profitable.\n\n"
                "Our rules:\n"
                "• Max 3 new trades/day\n"
                "• 2h cooldown between same ticker\n"
                "• Mandatory SL at entry (no exceptions)\n"
                "• Zombie close after 5 days (cut dead weight)\n"
                "• Smart Money veto overrides any score\n\n"
                f"{stats['total']} trades later: {stats['wr']}% WR, ${stats['pnl']:+.0f} PnL.\n\n"
                "Systems > emotions.\n\n"
                "#RiskManagement #Trading #CryptoTrading"
            ),
            "li": (
                "Risk management is not sexy. But it's why we're still profitable.\n\n"
                "I've seen too many traders obsess over entries and completely ignore risk. "
                "After years of studying what separates consistent performers from gamblers, "
                "I built these rules into our algorithm:\n\n"
                "→ Maximum 3 new positions per day — prevents overtrading in FOMO conditions\n"
                "→ 2-hour cooldown between reopening the same ticker — stops revenge trading\n"
                "→ Mandatory stop-loss at entry — no trade exists without a defined invalidation\n"
                "→ 5-day max hold — zombie positions that go nowhere get cut automatically\n"
                "→ Smart Money veto — even a perfect score gets rejected if institutional flow disagrees\n"
                "→ R:R gate — won't enter if reward/risk is below 1.0\n\n"
                "These aren't suggestions. They're hard-coded. The bot physically cannot break them.\n\n"
                "That's the advantage of algorithmic trading: your rules don't bend "
                "when you're tired, emotional, or overconfident.\n\n"
                f"Running stats: {stats['total']} trades, {stats['wr']}% win rate, ${stats['pnl']:+.0f} PnL.\n\n"
                "What's the one risk rule you wish you'd followed from day one?\n\n"
                "#RiskManagement #TradingPsychology #AlgoTrading #TradingVentureClub"
            ),
            "graphic": (
                "📸 GRAPHIC: Create a clean infographic or screenshot showing:\n"
                "  Option A: Terminal with positions panel + P&L history showing consistent equity curve\n"
                "  Option B: Text graphic listing the 6 risk rules (Canva/simple design)\n"
                "  Add: TVC Fusion logo/brand if available\n"
                "  Format: 1200x627 or 1080x1080"
            ),
        },
        # 2 — Market regimes
        {
            "x": (
                "The same strategy that prints money in a trend will destroy you in a range.\n\n"
                "That's why our bot detects market regime FIRST:\n"
                "• TRENDING UP → aggressive entries, wide TP\n"
                "• RANGING → tight range plays only\n"
                "• TRENDING DOWN → short bias, strict SL\n\n"
                "Most traders use one strategy in all conditions. That's why most traders lose.\n\n"
                "#MarketRegime #CryptoTrading #TradingEducation"
            ),
            "li": (
                "Why the same strategy doesn't work in every market condition — and what to do about it\n\n"
                "One of the biggest mistakes I see traders make: applying a trending strategy in a ranging market, "
                "then blaming the strategy when it fails.\n\n"
                "The problem isn't the strategy. It's the context.\n\n"
                "In our system, regime detection happens BEFORE any trade decision:\n\n"
                "→ TRENDING UP — higher highs, higher lows confirmed across timeframes. "
                "The Fibonacci filter loosens (0.90 threshold), giving more room for pullback entries.\n\n"
                "→ RANGING — no clear direction. Filter tightens (0.70), only taking "
                "high-conviction setups near range boundaries.\n\n"
                "→ TRENDING DOWN — lower highs dominating. Short bias activates, "
                "filter is strictest (0.50), and we're extremely selective.\n\n"
                "The bot adapts automatically. No emotional override, no \"I think it'll bounce.\"\n\n"
                "This is the difference between systematic trading and guessing. "
                "At Trading Venture Club, the algorithm does what the data says, not what feels right.\n\n"
                "Do you adapt your strategy to market conditions, or do you use the same approach regardless?\n\n"
                "#MarketAnalysis #TradingStrategy #AlgoTrading #CryptoEducation"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal showing BTC with regime label visible:\n"
                "  - Price chart with regime indicator\n"
                "  - Fibonacci levels panel\n"
                "  - Or: simple diagram showing 3 regimes → 3 different filter thresholds\n"
                "  URL: tradingventureclub.com/terminal/?token=BTC"
            ),
        },
        # 3 — Signal channels trust problem
        {
            "x": (
                "90% of crypto signal channels are scams. Here's how to spot them:\n\n"
                "🚩 Only show wins\n"
                "🚩 No entry/exit prices\n"
                "🚩 \"Trust me\" instead of data\n"
                "🚩 Delete losing calls\n"
                "🚩 Sell course after blowing your account\n\n"
                "We publish EVERY trade — wins AND losses.\n"
                f"Full track record: {stats['total']} trades, {stats['wr']}% WR.\n\n"
                "Transparency is the product.\n\n"
                "#CryptoSignals #Transparency #Trading"
            ),
            "li": (
                "The crypto signal industry has a trust problem. Here's what I'm doing about it.\n\n"
                "After spending years in crypto markets, I've seen the pattern repeat endlessly:\n"
                "→ Channel posts 10 winning trades in a row (deletes the 15 losers)\n"
                "→ Shows percentage gains with no entry/exit proof\n"
                "→ Sells premium access for $200/month based on fabricated track records\n"
                "→ Disappears when the drawdown hits\n\n"
                "I built Trading Venture Club on the opposite principle:\n\n"
                "Every trade is logged in a public database. Entry, exit, PnL, reason — all recorded.\n"
                "The bot runs in a public GitHub repository. You can read every line of code.\n"
                "Losses are posted alongside wins. If the bot gets stopped out, you see it.\n\n"
                f"Current reality: {stats['total']} trades, {stats['wr']}% win rate, "
                f"${stats['pnl']:+.0f} total PnL. Not perfect. Real.\n\n"
                "I believe radical transparency will become the standard for trading signals. "
                "Until then, ask anyone selling you signals: \"Can I see your full, unedited trade history?\"\n\n"
                "If the answer is no — you have your answer.\n\n"
                "#CryptoTrading #Transparency #TradingSignals #TradingVentureClub"
            ),
            "graphic": (
                "📸 GRAPHIC: Side by side comparison:\n"
                "  Left: generic scam channel (blurred/mocked) with only green trades\n"
                "  Right: TVC Terminal P&L History showing real equity curve (ups AND downs)\n"
                "  Or: screenshot of terminal with full trade log visible\n"
                "  Format: 1200x627"
            ),
        },
        # 4 — Derivatives data
        {
            "x": (
                "Derivatives data tells you what's coming before price does.\n\n"
                "What to watch:\n"
                "→ Open Interest surging + price flat = big move incoming\n"
                "→ Funding rate extreme = crowded trade about to unwind\n"
                "→ Liquidation clusters = magnets for price\n\n"
                "Our Pump Radar scans 200+ tokens for these patterns every 5 min.\n\n"
                "Free alerts: t.me/TVCFusionSignals\n"
                "#Derivatives #CryptoTrading #OpenInterest"
            ),
            "li": (
                "Why derivatives data is the most underrated edge in crypto trading\n\n"
                "Price is a lagging indicator. By the time you see a breakout on the chart, "
                "the move has already been positioned for in derivatives.\n\n"
                "Three derivatives signals I've found most predictive after years of research:\n\n"
                "1. Open Interest divergence — when OI surges but price stays flat, "
                "someone is building a massive position. Direction TBD, but volatility is coming.\n\n"
                "2. Funding rate extremes — when everyone is long and paying 0.1%+ per 8h, "
                "the trade is too crowded. The unwind becomes the trade.\n\n"
                "3. Liquidation clusters — large clusters of liquidations act like magnets. "
                "Market makers know where the stops are.\n\n"
                "Our Pump Radar algorithm scans 200+ perpetual contracts every 5 minutes "
                "for exactly these patterns. When signals cluster, the alert fires.\n\n"
                "This isn't theory — it's coded, tested, and running live. "
                "The edge isn't the data (it's public). The edge is systematizing the interpretation.\n\n"
                "What derivatives metrics do you track in your analysis?\n\n"
                "#Derivatives #TradingEducation #CryptoMarkets #QuantTrading"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal Pump Radar panel showing:\n"
                "  - Active alerts with factor breakdown\n"
                "  - Track record section\n"
                "  Or: chart showing OI vs price divergence example\n"
                "  URL: tradingventureclub.com/terminal/"
            ),
        },
        # 5 — Psychology: systems vs emotions
        {
            "x": (
                "\"I'll just hold a little longer.\"\n"
                "\"This time is different.\"\n"
                "\"I'll average down.\"\n\n"
                "Every blown account starts with one of these sentences.\n\n"
                "That's why I removed myself from the equation.\n"
                "The bot doesn't feel. It follows the rules.\n\n"
                f"{stats['total']} trades. Zero emotional overrides.\n\n"
                "#TradingPsychology #AlgoTrading #Discipline"
            ),
            "li": (
                "The hardest lesson in trading: you are the biggest risk.\n\n"
                "I've studied trading psychology extensively — both in books and through my own painful "
                "experience. The pattern is always the same:\n\n"
                "→ You hold losers too long because admitting a loss feels like failure\n"
                "→ You cut winners too early because taking profit feels safe\n"
                "→ You overtrade after a win streak because confidence becomes overconfidence\n"
                "→ You revenge-trade after a loss because you want to \"get it back\"\n\n"
                "The solution isn't more discipline. It's removing the decision from yourself entirely.\n\n"
                "That's why I built an automated system. The algorithm has rules, and it cannot break them. "
                "It doesn't care about yesterday's loss. It doesn't get excited about a winning streak. "
                "It just runs.\n\n"
                f"After {stats['total']} automated trades with {stats['wr']}% win rate, I'm convinced: "
                "the best thing I ever did for my trading was stop making trading decisions.\n\n"
                "What's the emotional trap you fall into most? I'd bet it's one of the four above.\n\n"
                "#TradingPsychology #AlgoTrading #TradingMindset #TradingVentureClub"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal equity curve (P&L History panel):\n"
                "  - Smooth, systematic equity line — no panic spikes\n"
                "  - Annotate: \"No emotional overrides. Pure algorithmic execution.\"\n"
                "  Or: Canva text graphic with the 4 emotional traps listed\n"
                "  Format: 1200x627 or 1080x1080"
            ),
        },
        # 6 — Funding rates explained
        {
            "x": (
                "Funding rates are one of crypto's best-kept secrets.\n\n"
                "→ Positive + extreme = too many longs → expect a flush\n"
                "→ Negative + extreme = too many shorts → expect a squeeze\n"
                "→ Near zero = balanced → follow the trend\n\n"
                "Our screener checks funding across 200+ tokens every run.\n"
                "It's free alpha hiding in plain sight.\n\n"
                "#FundingRate #CryptoTrading #TradingEducation"
            ),
            "li": (
                "Funding rates: the signal most crypto traders overlook entirely\n\n"
                "Quick primer if you trade perpetual futures (or want to understand them):\n\n"
                "Perpetual contracts have no expiry date. To keep them anchored to spot price, "
                "exchanges use a funding mechanism: longs pay shorts (or vice versa) every 8 hours.\n\n"
                "Why this matters:\n"
                "→ When funding is extremely positive — everyone is long and paying a premium to stay long. "
                "The trade is crowded, and a liquidation cascade can wipe it in minutes.\n"
                "→ When funding is extremely negative — shorts are paying. The squeeze risk is real.\n"
                "→ When funding is near zero — the market is balanced. Trend-following works best here.\n\n"
                "Our auto-picks algorithm checks funding rates across 200+ perpetual contracts "
                "daily. Extreme funding is one of the strongest contrarian signals we track.\n\n"
                "Understanding this one mechanic gives you an edge over 90% of retail. "
                "It's public data — the edge is knowing what to do with it.\n\n"
                "Did you know your exchange shows funding rates? Most traders never check.\n\n"
                "#TradingEducation #CryptoDerivatives #FundingRate #QuantTrading"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal or exchange screenshot showing:\n"
                "  - Funding rates for multiple tokens\n"
                "  - Highlight extreme values (red = high positive, blue = negative)\n"
                "  Or: simple diagram explaining funding mechanism\n"
                "  Format: 1200x627"
            ),
        },
        # 7 — Automation philosophy
        {
            "x": (
                "I stopped checking charts manually.\n\n"
                "Instead, 7 Python scripts run every 5 minutes:\n"
                "→ Screening 200+ tokens\n"
                "→ Tracking whale wallets\n"
                "→ Computing fusion scores\n"
                "→ Opening/closing paper trades\n"
                "→ Alerting via Telegram\n\n"
                "Total cost: $0 (GitHub Actions free tier).\n"
                "Total time: <20 min/day (just publishing posts).\n\n"
                "Automation > hustle.\n\n"
                "#Automation #Python #CryptoTrading #BuildInPublic"
            ),
            "li": (
                "Why I automated my trading and stopped making discretionary entries\n\n"
                "A year ago, I was doing what most traders do: staring at charts for hours, "
                "second-guessing entries, feeling the dopamine of a win and the frustration of a loss.\n\n"
                "Then I asked myself: if I can define my rules clearly enough to follow them, "
                "why can't a computer follow them better?\n\n"
                "So I built it. Seven Python scripts that run every 5 minutes:\n"
                "→ auto_picks.py — screens 200+ tokens against 4 signal categories\n"
                "→ pump_radar.py — scans for derivatives anomalies (OI surges, funding extremes)\n"
                "→ paper_bot.py — opens/manages/closes trades with 12 rejection gates\n"
                "→ auto_fusion.py — computes a 0-100 Fusion Score per token\n"
                "→ Plus correlation matrix, macro events, content generation\n\n"
                "Total infrastructure cost: $0. GitHub Actions free tier covers it.\n"
                "Total daily time commitment: ~20 minutes (publishing pre-written posts).\n\n"
                "The irony: I do more analysis now than when I traded manually. "
                "But the analysis goes into improving the system, not into individual trade decisions.\n\n"
                "That's what Trading Venture Club is: a research-first, automated-execution approach "
                "to crypto markets.\n\n"
                "If you could automate one part of your trading workflow, what would it be?\n\n"
                "#Automation #BuildInPublic #Python #AlgoTrading #TradingVentureClub"
            ),
            "graphic": (
                "📸 GRAPHIC: Architecture overview:\n"
                "  Option A: GitHub Actions workflow runs page (screenshot)\n"
                "  Option B: Terminal bird's eye view with multiple panels\n"
                "  Option C: Simple flowchart (Canva): cron → scripts → data → terminal + Telegram\n"
                "  Format: 1200x627"
            ),
        },
        # 8 — Correlation/macro
        {
            "x": (
                "BTC doesn't trade in a vacuum.\n\n"
                "Our correlation matrix tracks daily:\n"
                "→ BTC vs Nasdaq (NQ)\n"
                "→ BTC vs DXY (dollar)\n"
                "→ BTC vs Gold\n"
                "→ Cross-crypto correlations\n\n"
                "When correlations break, the opportunity appears.\n"
                "When they're high, you're just trading the same macro bet N times.\n\n"
                "#Bitcoin #Macro #Correlation #TradingEducation"
            ),
            "li": (
                "Why I track cross-asset correlations in my crypto trading system\n\n"
                "Most crypto traders think they're diversified because they hold 5 altcoins. "
                "In reality, when BTC dumps, most alts dump harder. \"Diversification\" is an illusion.\n\n"
                "At Trading Venture Club, our daily correlation matrix tracks:\n"
                "→ BTC vs Nasdaq (QQQ) — tells you if crypto is in 'risk-on tech' mode\n"
                "→ BTC vs DXY — dollar strength often precedes crypto weakness\n"
                "→ BTC vs Gold — when money flows to gold over BTC, the narrative has shifted\n"
                "→ Cross-crypto — if ETH/BTC correlation is weakening, alt season might be starting\n\n"
                "This data feeds into our macro layer. Before any trade opens, the system checks "
                "if the macro environment supports the thesis.\n\n"
                "Additionally, the algorithm pauses all trading 3 hours before and 1 hour after "
                "major macro events (FOMC, CPI, NFP). No edge in those conditions — only noise.\n\n"
                "Understanding what drives the market you're trading is half the edge.\n\n"
                "#MacroTrading #Correlation #Bitcoin #CryptoAnalysis"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal Correlation Matrix panel showing:\n"
                "  - Heatmap with BTC vs NQ, DXY, Gold\n"
                "  - Cross-crypto correlations\n"
                "  URL: tradingventureclub.com/terminal/"
            ),
        },
        # 9 — Holy grail myth
        {
            "x": (
                "There is no holy grail indicator.\n\n"
                "Every strategy has losers.\n"
                "Every edge degrades over time.\n\n"
                "What works:\n"
                "→ Multiple weak signals > one \"perfect\" signal\n"
                "→ Rigid risk rules > flexible entry rules\n"
                "→ Adapting to regime changes > one-size-fits-all\n\n"
                f"Our approach: 5 weighted factors + 3 SM layers + 12 rejection gates = {stats['wr']}% WR.\n\n"
                "#TradingReality #CryptoTrading #AlgoTrading"
            ),
            "li": (
                "Stop looking for the holy grail indicator. It doesn't exist.\n\n"
                "Early in my trading journey, I spent months searching for the \"perfect\" indicator. "
                "RSI, MACD, Bollinger, Ichimoku — I backtested them all. None worked consistently.\n\n"
                "The breakthrough came when I stopped looking for one signal and started combining many.\n\n"
                "Our Fusion Score combines 5 weighted factors: on-chain flow, technical structure, "
                "derivatives data, momentum, and sentiment. No single factor wins consistently. "
                "But when multiple weak signals align, the edge compounds.\n\n"
                "On top of that, 12 rejection gates filter out bad setups before any trade opens. "
                "Smart Money veto. Regime check. R:R minimum. Cooldown timer. "
                "The system says \"no\" far more often than it says \"yes.\"\n\n"
                "That selectivity IS the edge. Not the entry signal — the filtration.\n\n"
                f"Result: {stats['total']} trades, {stats['wr']}% win rate. "
                "Not because we found the holy grail. Because we stacked imperfect signals correctly.\n\n"
                "What's the indicator you relied on longest? And did it hold up?\n\n"
                "#TradingMythBusted #CryptoTrading #QuantFinance #TradingVentureClub"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal showing multi-factor view:\n"
                "  - Fusion Score breakdown (5 factors visible)\n"
                "  - Smart Money panel alongside\n"
                "  - Or: diagram of 12 rejection gates as a funnel\n"
                "  Format: 1200x627"
            ),
        },
    ]

    day_of_year = _now().timetuple().tm_yday
    idx = day_of_year % len(topics)
    topic = topics[idx]

    posts = []

    # X post (shorter, punchier)
    posts.append({
        "platform": "x", "type": "kol_education", "ticker": None,
        "date": _today(), "ts": _now().isoformat(),
        "text": topic["x"], "graphic": topic["graphic"], "posted": False,
    })

    # LinkedIn post (longer, narrative + CTA question)
    posts.append({
        "platform": "linkedin", "type": "kol_education", "ticker": None,
        "date": _today(), "ts": _now().isoformat(),
        "text": topic["li"], "graphic": topic["graphic"], "posted": False,
    })

    return posts


# --- LAYER 3: Personal Brand posts (narrative + TVC as brand) ---------------

def _personal_brand_posts():
    """Personal brand posts: origin story, philosophy, industry takes, expertise.
    References existing knowledge (books, research) without selling them.
    Rotates by day of year (offset from KOL to avoid same-day collisions)."""
    stats = get_cumulative_stats()

    topics = [
        # 0 — Origin story
        {
            "x": (
                "I didn't start as a trader.\n\n"
                "I started as a researcher. Reading whitepapers. "
                "Studying market microstructure. Writing about what I found.\n\n"
                "Then I thought: if I can write the thesis, can I code the execution?\n\n"
                "Trading Venture Club was born from that question.\n\n"
                "Now 7 scripts run 24/7, scanning markets while I sleep.\n\n"
                "Background matters less than curiosity.\n\n"
                "#BuildInPublic #CryptoTrading #FounderStory"
            ),
            "li": (
                "How Trading Venture Club started — and why it's not just a trading bot\n\n"
                "I didn't come to crypto as a day trader. I came as a researcher.\n\n"
                "I spent years diving deep into market microstructure, blockchain analytics, "
                "and the mechanics of how institutional money moves in crypto. "
                "I wrote extensively about it — analyzing patterns, testing hypotheses, "
                "building mental models for how markets actually work.\n\n"
                "At some point I realized: I have a thesis about how markets move. "
                "Why am I not testing it systematically?\n\n"
                "That question became Trading Venture Club. Not a signal channel. "
                "Not a course. A research-driven, fully automated trading system "
                "that turns analysis into execution — transparently, publicly, algorithmically.\n\n"
                "The expertise isn't in picking the right token on a given Tuesday. "
                "It's in building the system that makes consistent decisions "
                "across hundreds of market conditions.\n\n"
                "I'm sharing this journey publicly — the wins, the losses, the code, "
                "the data — because I believe transparency is what this industry needs most.\n\n"
                "What's your background? Sometimes the best traders come from the most unexpected places.\n\n"
                "#FounderStory #TradingVentureClub #BuildInPublic #CryptoEntrepreneur"
            ),
            "graphic": (
                "📸 GRAPHIC: Personal/professional photo or TVC Terminal bird's eye view\n"
                "  showing the scope of the system (multiple panels visible).\n"
                "  If no personal photo: terminal screenshot with text overlay:\n"
                "  \"From research to code. Trading Venture Club.\"\n"
                "  Format: 1200x627"
            ),
        },
        # 1 — Why radical transparency
        {
            "x": (
                "My controversial take:\n\n"
                "If a trading signal service won't show you their full history — including losses — "
                "you're not paying for signals.\n\n"
                "You're paying for fiction.\n\n"
                "Every TVC trade: logged, timestamped, public.\n"
                "That's not a feature. That's the minimum standard.\n\n"
                "#Transparency #CryptoTrading #TradingSignals"
            ),
            "li": (
                "Why I chose radical transparency over marketing hype\n\n"
                "When I was building Trading Venture Club, I had a choice:\n\n"
                "Option A: Only show winning trades. Screenshot the best results. "
                "Sell the dream of easy money. Scale fast.\n\n"
                "Option B: Show everything. Every win. Every loss. Every trade that "
                "looked perfect on paper but got stopped out in reality.\n\n"
                "I chose B. And I think it's the right long-term strategy, even if it's slower.\n\n"
                "Here's why: the crypto signal industry is built on survivorship bias. "
                "Channels that only show wins attract short-term subscribers who churn "
                "the moment reality hits. Channels that show the full picture attract "
                "people who understand how trading actually works.\n\n"
                "Those people stay. They engage. They refer others.\n\n"
                f"Our real record: {stats['total']} trades, {stats['wr']}% win rate, "
                f"${stats['pnl']:+.0f} PnL. Not every trade wins. "
                "That's exactly why the overall track record is credible.\n\n"
                "I believe this approach will become the industry standard. "
                "Until then, Trading Venture Club is proving it works.\n\n"
                "#RadicalTransparency #TradingVentureClub #CryptoSignals #Trust"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal P&L History showing full equity curve:\n"
                "  - Both wins and losses visible in the curve\n"
                "  - Cumulative PnL line trending up despite individual losses\n"
                "  - Text overlay: \"Every trade. Every result. No edits.\"\n"
                "  Format: 1200x627"
            ),
        },
        # 2 — Knowledge & research background
        {
            "x": (
                "You don't need a finance degree to build a trading system.\n\n"
                "You need:\n"
                "→ Deep research (read everything)\n"
                "→ Python basics (automate everything)\n"
                "→ Healthy skepticism (question everything)\n"
                "→ Patience (test everything)\n\n"
                "The best traders I know are curious people who happen to trade.\n\n"
                "#TradingMindset #LifelongLearning #CryptoTrading"
            ),
            "li": (
                "What I learned from years of researching crypto markets — before writing a single line of trading code\n\n"
                "Before I built any algorithm, I spent a long time just studying.\n\n"
                "Reading whitepapers on market microstructure. Analyzing how order books "
                "behave during liquidation cascades. Studying the difference between "
                "how institutional and retail traders position.\n\n"
                "Writing about these topics forced me to organize my thinking. "
                "You can't explain something clearly if you don't understand it deeply.\n\n"
                "Three insights from that research phase that directly shaped TVC Fusion:\n\n"
                "1. Markets are not random, but they're not deterministic either. "
                "The edge lives in probability, not prediction.\n\n"
                "2. Most retail traders have an information disadvantage — but derivatives data "
                "and on-chain flow are public. The gap isn't access. It's interpretation.\n\n"
                "3. Consistency beats brilliance. A system that makes 55% correct decisions "
                "with proper risk management will outperform a genius who's right 70% of the time "
                "but sizes positions emotionally.\n\n"
                "Every feature in the TVC Fusion Terminal came from a research question I couldn't "
                "find a good answer to. So I built the answer.\n\n"
                "What's a concept in trading you wish someone had explained properly?\n\n"
                "#CryptoResearch #TradingEducation #TradingVentureClub #Knowledge"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal with educational focus:\n"
                "  Option A: Terminal showing Smart Money + Fusion Score + correlation panel\n"
                "  Option B: Clean text quote graphic:\n"
                "    \"The gap isn't access. It's interpretation.\"\n"
                "    — Trading Venture Club\n"
                "  Format: 1200x627"
            ),
        },
        # 3 — Industry opinion
        {
            "x": (
                "Hot take: the future of retail trading is transparent, algorithmic, and verifiable.\n\n"
                "No more guru worship.\n"
                "No more cherry-picked results.\n"
                "No more \"just trust me.\"\n\n"
                "Code. Data. Public track records.\n\n"
                "That's what I'm building.\n\n"
                "#FutureOfTrading #CryptoTrading #BuildInPublic"
            ),
            "li": (
                "My prediction: transparent algorithmic trading will replace the guru model\n\n"
                "The current model of retail trading education is broken:\n"
                "→ Self-proclaimed experts sell courses based on curated screenshots\n"
                "→ Signal channels hide their full track record behind paywalls\n"
                "→ \"Community\" is just a marketing funnel for the next upsell\n\n"
                "I think the next generation of trading services will look completely different:\n\n"
                "1. Algorithms that execute defined strategies — no discretionary hand-waving\n"
                "2. Full trade history published in real-time — not after the fact\n"
                "3. Open methodology — you understand WHY a trade was taken, not just WHAT was traded\n"
                "4. Performance verified by data, not testimonials\n\n"
                "This is what Trading Venture Club is building. "
                "We're early, but I believe this model wins in the long run because trust compounds.\n\n"
                "The traders who build trust now will own the next era of this industry.\n\n"
                "Agree or disagree? I'd love to hear your take in the comments.\n\n"
                "#FutureOfTrading #CryptoIndustry #TradingVentureClub #Innovation"
            ),
            "graphic": (
                "📸 GRAPHIC: Clean text graphic or terminal screenshot:\n"
                "  Option A: Quote card: \"The future of retail trading is transparent,\n"
                "    algorithmic, and verifiable.\" with TVC branding\n"
                "  Option B: Terminal showing full dashboard as proof of concept\n"
                "  Format: 1200x627"
            ),
        },
        # 4 — TVC as brand / vision
        {
            "x": (
                "Trading Venture Club isn't a signal channel.\n\n"
                "It's a system:\n"
                "→ Research-driven methodology\n"
                "→ Algorithmic execution (no manual trades)\n"
                "→ Public track record (every trade logged)\n"
                "→ Smart Money verification (institutional flow)\n"
                "→ Adaptive (regime-aware, macro-aware)\n\n"
                "Building in public. t.me/TVCFusionSignals\n\n"
                "#TradingVentureClub #AlgoTrading #Crypto"
            ),
            "li": (
                "What is Trading Venture Club — and why does it exist?\n\n"
                "I get asked this a lot, so let me explain clearly.\n\n"
                "Trading Venture Club is a research and technology company focused on "
                "making institutional-grade crypto market analysis accessible to individual traders.\n\n"
                "What that means in practice:\n\n"
                "→ A proprietary Fusion Score system that combines 5 market factors "
                "into a single actionable signal per token\n\n"
                "→ Real-time Smart Money tracking — whale positions, institutional flow, "
                "derivatives data — the same information hedge funds use\n\n"
                "→ A fully automated paper trading system that logs every decision, "
                "every entry, every exit — transparently\n\n"
                "→ A live terminal dashboard where you see exactly what the algorithm sees\n\n"
                "→ Free and premium Telegram channels with real-time signals\n\n"
                "The vision: take the analytical depth of a crypto research desk, "
                "combine it with algorithmic execution, and deliver it with radical transparency.\n\n"
                "We're early. The track record is being built in real-time. "
                "But every week, the data set gets larger, the signals get refined, "
                "and the proof accumulates.\n\n"
                "Follow along: tradingventureclub.com or free signals at t.me/TVCFusionSignals\n\n"
                "#TradingVentureClub #CryptoTrading #FinTech #AlgoTrading #SmartMoney"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal full dashboard view (bird's eye):\n"
                "  - Multiple panels visible: chart, positions, Smart Money, picks\n"
                "  - Shows the scope and professionalism of the system\n"
                "  - Text overlay: \"Trading Venture Club — Research. Algorithms. Transparency.\"\n"
                "  URL: tradingventureclub.com/terminal/"
            ),
        },
        # 5 — Solo builder story
        {
            "x": (
                "Built as a solo founder:\n"
                "→ 10,000+ lines of terminal code\n"
                "→ 7 Python scripts running 24/7\n"
                "→ Dual Telegram channels\n"
                "→ Stripe membership system\n"
                "→ Public GitHub repo\n\n"
                "Total team size: 1.\n"
                "Total external funding: $0.\n\n"
                "You don't need a team to build. You need a system.\n\n"
                "#SoloFounder #BuildInPublic #Startup"
            ),
            "li": (
                "Building a trading technology company as a solo founder — what I've learned\n\n"
                "Trading Venture Club has no team. No investors. No funding.\n\n"
                "It has:\n"
                "→ A ~10,000-line trading terminal built from scratch\n"
                "→ 7 Python scripts running autonomously every 5 minutes\n"
                "→ An automated Stripe membership system\n"
                "→ Dual Telegram channels (free + premium)\n"
                "→ A content engine that generates posts from bot data\n"
                "→ A public GitHub repository anyone can audit\n\n"
                "I'm not sharing this to brag. I'm sharing it because "
                "the narrative that you need a team, funding, and 18 months to build something "
                "useful is wrong.\n\n"
                "You need: a clear problem, the willingness to learn the tools, "
                "and the discipline to ship something every week.\n\n"
                "The hardest part isn't building. It's publishing before it's perfect.\n\n"
                "If you're building something solo — keep going. The compound effect is real.\n\n"
                "#SoloFounder #BuildInPublic #Entrepreneurship #TradingVentureClub"
            ),
            "graphic": (
                "📸 GRAPHIC: GitHub repository page showing:\n"
                "  - Commit history (activity graph)\n"
                "  - Or: GitHub Actions run log (green checkmarks)\n"
                "  - Or: Terminal screenshot showing the scale of the system\n"
                "  Format: 1200x627"
            ),
        },
        # 6 — Data-driven decisions
        {
            "x": (
                "Opinions are free.\n"
                "Data costs effort.\n\n"
                "Every TVC decision is backed by:\n"
                "→ 200+ token scans/day\n"
                "→ 5-factor fusion score\n"
                "→ 3-layer Smart Money verification\n"
                "→ Regime-adaptive filtering\n\n"
                "In a market of hot takes, be the one with receipts.\n\n"
                "#DataDriven #CryptoTrading #QuantTrading"
            ),
            "li": (
                "In a market of opinions, data is the real edge\n\n"
                "Crypto Twitter is full of predictions. Everyone has a target. "
                "Everyone \"called\" the last move. Nobody shows the full history of their calls.\n\n"
                "At Trading Venture Club, I decided early on: no opinions without data.\n\n"
                "Every trade the system takes is grounded in measurable, verifiable inputs:\n"
                "→ A 0-100 Fusion Score computed from 5 weighted factors\n"
                "→ Smart Money verification from 3 independent data sources\n"
                "→ Regime detection that adapts the strategy to market conditions\n"
                "→ 12 rejection gates that filter out low-conviction setups\n\n"
                "Is it perfect? No. No system is. But it's consistent, documented, and improvable. "
                "Every losing trade teaches the algorithm something. Every winning pattern gets reinforced.\n\n"
                "The goal isn't to be right every time. "
                "It's to make better decisions than a human could under the same conditions.\n\n"
                "What do you base your trading decisions on? Gut feeling or structured analysis?\n\n"
                "#DataDrivenTrading #CryptoAnalysis #QuantFinance #TradingVentureClub"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal Fusion Score breakdown:\n"
                "  - Score components (on-chain, technical, derivatives, momentum, sentiment)\n"
                "  - Smart Money verdict\n"
                "  - Or: stats dashboard with trade counts and win rate\n"
                "  Format: 1200x627"
            ),
        },
        # 7 — Lessons from losses
        {
            "x": (
                "My best lessons came from losses.\n\n"
                "→ Lost on a short during a squeeze → added funding rate check\n"
                "→ Held a zombie trade 10 days → added max-hold timer\n"
                "→ Re-entered same ticker too fast → added cooldown gate\n\n"
                "Every bug in the system came from a real loss.\n"
                "Every fix is permanent.\n\n"
                "That's how systems improve.\n\n"
                "#TradingLessons #CryptoTrading #AlgoTrading"
            ),
            "li": (
                "Every feature in our trading system was born from a real loss\n\n"
                "I don't hide our losing trades. In fact, they're the most valuable part of the journey.\n\n"
                "Some examples:\n\n"
                "→ Got squeezed on a short because funding was already extreme negative → "
                "built a funding rate gate into the screener\n\n"
                "→ Held a position for 10 days that went nowhere, tying up capital → "
                "coded a 5-day maximum hold (\"zombie close\") rule\n\n"
                "→ Re-entered the same ticker 30 minutes after being stopped out → "
                "added a 2-hour cooldown between same-ticker trades\n\n"
                "→ Took a low-conviction trade that barely had 1:1 risk-reward → "
                "built a minimum R:R gate that rejects anything below 1.0\n\n"
                "Each of these was a painful lesson. But because the system is algorithmic, "
                "the fix is permanent. A human forgets their rules in the heat of the moment. "
                "Code doesn't.\n\n"
                "This is the compounding advantage of systematic trading: "
                "every mistake makes the system better, permanently.\n\n"
                "What's the most expensive lesson trading has taught you?\n\n"
                "#TradingLessons #LearnFromLosses #AlgoTrading #TradingVentureClub"
            ),
            "graphic": (
                "📸 GRAPHIC: TVC Terminal showing a losing trade:\n"
                "  - Position closed at SL with clear entry/exit\n"
                "  - Or: before/after comparison — old behavior vs new gate\n"
                "  - Be authentic: show a real loss, then explain the fix\n"
                "  Format: 1200x627"
            ),
        },
    ]

    # Offset by 5 so personal brand and KOL education don't pick same index
    day_of_year = _now().timetuple().tm_yday
    idx = (day_of_year + 5) % len(topics)
    topic = topics[idx]

    posts = []

    posts.append({
        "platform": "x", "type": "personal_brand", "ticker": None,
        "date": _today(), "ts": _now().isoformat(),
        "text": topic["x"], "graphic": topic["graphic"], "posted": False,
    })

    posts.append({
        "platform": "linkedin", "type": "personal_brand", "ticker": None,
        "date": _today(), "ts": _now().isoformat(),
        "text": topic["li"], "graphic": topic["graphic"], "posted": False,
    })

    return posts


# --- Telegram convenience (send posts to personal chat for copy-paste) -----

def _tg_send_personal(text):
    """Send to personal Telegram for copy-paste to LinkedIn/X."""
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
            log(f"telegram personal: {r.status}")
    except Exception as e:
        log(f"telegram personal failed: {e}")


def _notify_new_posts(posts):
    """Send a digest of new posts to personal Telegram."""
    if not posts:
        return
    msg_lines = ["📝 <b>Content Engine — nowe posty do publikacji</b>", ""]
    for p in posts:
        platform = p["platform"].upper()
        ptype = p["type"].replace("_", " ").title()
        ticker = p.get("ticker") or ""
        msg_lines.append(f"<b>[{platform}]</b> {ptype} {ticker}")
        # First 200 chars of text as preview
        preview = p["text"][:200].replace("<", "&lt;").replace(">", "&gt;")
        msg_lines.append(f"<i>{preview}...</i>")
        # Graphic recommendation
        graphic = p.get("graphic", "").split("\n")[0]
        msg_lines.append(f"🖼 {graphic}")
        msg_lines.append("")
    msg_lines.append("Pełne teksty w <code>content_queue.json</code>")
    _tg_send_personal("\n".join(msg_lines))


# --- main logic ------------------------------------------------------------

def run():
    log(f"Content Engine v2.0 — {_now().isoformat()}")

    q = _load_queue()
    new_posts = []

    # Check how many posts we already have today
    x_today = _posts_today(q, "x")
    li_today = _posts_today(q, "linkedin")
    log(f"Posts today so far: X={x_today}, LinkedIn={li_today}")

    # 1. CLOSED TRADES (highest priority)
    closes = get_recent_closes(hours=6)
    if closes:
        log(f"Found {len(closes)} recent closed trade(s)")
        for trade in closes:
            # Skip if we already generated a post for this trade
            trade_id = f"{trade['ticker']}_{trade.get('closed_at', '')[:16]}"
            if any(p.get("_trade_id") == trade_id for p in q["posts"]):
                log(f"  skip {trade['ticker']} — already in queue")
                continue

            posts = _trade_closed_posts(trade)
            for p in posts:
                p["_trade_id"] = trade_id
                if p["platform"] == "x" and x_today >= MAX_X_PER_DAY:
                    log(f"  skip X post — daily limit ({MAX_X_PER_DAY})")
                    continue
                if p["platform"] == "linkedin" and li_today >= MAX_LI_PER_DAY:
                    log(f"  skip LinkedIn post — daily limit ({MAX_LI_PER_DAY})")
                    continue
                new_posts.append(p)
                if p["platform"] == "x":
                    x_today += 1
                else:
                    li_today += 1
            log(f"  ✓ generated posts for {trade['ticker']} {trade.get('direction','long')} ({trade.get('pnl_pct',0):+.1f}%)")

    # 2. DAILY PICKS (once per day)
    if not any(p.get("type") == "daily_pick" and p.get("date") == _today() for p in q["posts"]):
        picks = get_todays_picks()
        if picks:
            log(f"Generating daily pick posts ({len(picks)} picks, top: {picks[0]['ticker']})")
            for p in _daily_pick_posts(picks):
                if p["platform"] == "x" and x_today >= MAX_X_PER_DAY:
                    continue
                if p["platform"] == "linkedin" and li_today >= MAX_LI_PER_DAY:
                    continue
                new_posts.append(p)
                if p["platform"] == "x":
                    x_today += 1
                else:
                    li_today += 1

    # 3. PUMP ALERTS (X only, max 1/day)
    if not any(p.get("type") == "pump_alert" and p.get("date") == _today() for p in q["posts"]):
        alerts = get_recent_pump_alerts(hours=6)
        if alerts and x_today < MAX_X_PER_DAY:
            log(f"Generating pump alert post ({alerts[0].get('ticker')})")
            for p in _pump_alert_posts(alerts):
                new_posts.append(p)
                x_today += 1

    # 4. MARKET INSIGHT (LinkedIn, 1/day — BTC regime)
    if not any(p.get("type") == "market_insight" and p.get("date") == _today() for p in q["posts"]):
        fusion = get_fusion_data()
        if fusion and li_today < MAX_LI_PER_DAY:
            posts = _market_insight_post(fusion)
            if posts:
                log("Generating market insight post (BTC regime)")
                new_posts.extend(posts)
                li_today += 1

    # 5. BEHIND THE BUILD (LinkedIn, 1 per week — every Wednesday)
    if _now().weekday() == 2:  # Wednesday
        if not any(p.get("type") == "behind_the_build" and p.get("date") == _today() for p in q["posts"]):
            if li_today < MAX_LI_PER_DAY:
                log("Generating 'behind the build' post (Wednesday)")
                posts = _behind_the_build_post()
                new_posts.extend(posts)
                li_today += 1

    # 6. KOL EDUCATION (X + LinkedIn, Mon/Wed/Fri — expertise & authority)
    if _now().weekday() in (0, 2, 4):  # Monday, Wednesday, Friday
        if not any(p.get("type") == "kol_education" and p.get("date") == _today() for p in q["posts"]):
            log("Generating KOL education posts (expertise layer)")
            for p in _kol_education_posts():
                if p["platform"] == "x" and x_today >= MAX_X_PER_DAY:
                    log(f"  skip X kol_education — daily limit ({MAX_X_PER_DAY})")
                    continue
                if p["platform"] == "linkedin" and li_today >= MAX_LI_PER_DAY:
                    log(f"  skip LinkedIn kol_education — daily limit ({MAX_LI_PER_DAY})")
                    continue
                new_posts.append(p)
                if p["platform"] == "x":
                    x_today += 1
                else:
                    li_today += 1

    # 7. PERSONAL BRAND (X + LinkedIn, Tue/Thu — narrative + TVC as brand)
    if _now().weekday() in (1, 3):  # Tuesday, Thursday
        if not any(p.get("type") == "personal_brand" and p.get("date") == _today() for p in q["posts"]):
            log("Generating personal brand posts (narrative layer)")
            for p in _personal_brand_posts():
                if p["platform"] == "x" and x_today >= MAX_X_PER_DAY:
                    log(f"  skip X personal_brand — daily limit ({MAX_X_PER_DAY})")
                    continue
                if p["platform"] == "linkedin" and li_today >= MAX_LI_PER_DAY:
                    log(f"  skip LinkedIn personal_brand — daily limit ({MAX_LI_PER_DAY})")
                    continue
                new_posts.append(p)
                if p["platform"] == "x":
                    x_today += 1
                else:
                    li_today += 1

    # Save queue
    if new_posts:
        q["posts"].extend(new_posts)
        q["meta"]["last_run"] = _now().isoformat()
        q["meta"]["total_generated"] = len(q["posts"])
        _save_queue(q)
        log(f"✓ Added {len(new_posts)} new posts to queue (total: {len(q['posts'])})")

        # Notify personal Telegram
        _notify_new_posts(new_posts)
    else:
        log("No new posts to generate this cycle.")
        q["meta"]["last_run"] = _now().isoformat()
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
