#!/usr/bin/env python3
"""
TVC Fusion Content Engine v1.0 — auto-generates social media posts from bot data.

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
    log(f"Content Engine v1.0 — {_now().isoformat()}")

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
