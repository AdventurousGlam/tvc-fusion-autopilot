#!/usr/bin/env python3
"""
TVC Fusion Content Engine v5.2 — lessons over data, interaction over volume

v5.2 (2026-10-06): reply brief ships 4 data-driven reply SKELETONS (bullish / bearish /
  "is this the top" / alt-season) + target accounts list. Skeletons, not replies:
  the first sentence must still refer to the post being answered.

WHY v5.1 (2026-10-05):
  Week of 29.09-05.10, measured:
    X        20 posts -> 64 impressions total (~3/post), 0 likes, 0 follows,
             then the account was LOCKED for suspected spam (7 posts on 1.10,
             every one with a cashtag + screenshot, same format, zero replies).
    LinkedIn 2 posts -> 182 impressions (-53%), 84 of 3,594 followers reached (2.3%),
             0 comments written by us. The two posts that worked were LESSONS
             (scams 74, PCE read 69). Terminal-data posts: 1-10 impressions.
  Conclusion: volume is negatively correlated with reach on both platforms, and the
  audience does not want terminal verdicts yet - it wants the lesson behind them.

  v5.1 changes:
    - X only on X_DAYS (Tue/Thu/Fri/Sun), 1 post, TEXT ONLY (no graphic), no hashtags
    - trade-close posts OFF everywhere (pure signal-bot pattern)
    - SHOW_STATS=False until STATS_PUBLIC_FROM: no WR/PnL in any post or brief
    - Monday LinkedIn = "what I'm watching and why" lesson, not a data dump
    - Thursday = "what the week taught the system" (no P&L table) while stats are off
    - reply brief: 4 X talking points + 3 LinkedIn comment angles, no stats
    - every Telegram batch ends with the DAILY ENGAGEMENT CHECKLIST (the actual lever)
  Targets by 2026-11-02: LinkedIn >500 weekly impressions and >=10 comments on our posts;
  X >30 impressions/post average. If missed, change strategy again - do not add posts.

WHY v5.0 (2026-10-02):
  X account reach collapsed (71 impressions/7d, posts at 0-1 impressions with 52 active
  followers) after a week of 4-7 posts/day with cashtags, Telegram links and
  "not financial advice" boilerplate — the exact pattern X's spam classifier flags.
  LinkedIn has 3,570 followers and rewards long-form. Priorities flip.

PLATFORM SPLIT:
  LinkedIn — PRIMARY. 3 original posts/week: Mon (data drop), Wed (market analysis),
             Fri (education / behind the build). Personal angle, max ~12 lines, no links in body.
  X        — SAFE MODE. 1 post/day max, no cashtags ($BTC → BTC), no URLs in body
             (link goes into a reply), no "not financial advice" line.
             Plus a daily REPLY BRIEF: 3 data-backed talking points to use as replies
             under large accounts — replies are where X still gives this account reach.
  Saturday — no posts anywhere.

CTA: "TVC Fusion PRO" by name only, every 3rd post. Never a raw t.me link on X.

v5.0 changes:
  - MAX_X_PER_DAY 3→1, MAX_LI_PER_DAY 2→1, LinkedIn only on Mon/Wed/Fri
  - X_SAFE_MODE post-processing in _post(): strips cashtags, URLs, NFA lines
  - gen_reply_brief(): daily "x_reply" item (not counted against X limit)
  - Evening X slot disabled; trade-close posts never on LinkedIn

Reads:  paper_trades.db, fusion_<today>.json, crypto_picks.json, pump_radar_alerts.json
Writes: content_queue.json + sends to personal Telegram for copy-paste
"""

from __future__ import annotations
import json
import re
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

MAX_X_PER_DAY = 1      # v5.0: 3→1 (reach restriction recovery)
MAX_LI_PER_DAY = 1     # v5.0: 2→1
LI_DAYS = {0, 2, 4}    # v5.0: LinkedIn only Mon/Wed/Fri
X_SAFE_MODE = True     # v5.0: strip cashtags/URLs/NFA from X posts
EVENING_X_SLOT = False # v5.0: disabled
X_DAYS = {1, 3, 4, 6}  # v5.1: X only Tue/Thu/Fri/Sun (4 posts/week, recovery mode)
X_TEXT_ONLY = True     # v5.1: no graphics on X - screenshots + cashtags = bot pattern
TRADE_CLOSE_POSTS = False  # v5.1: no "trade closed +x%" posts anywhere
STATS_PUBLIC_FROM = "2026-11-02"  # v5.1: v3.3 rule set 30-day window ends here
SHOW_STATS = datetime.now(timezone.utc).strftime("%Y-%m-%d") >= STATS_PUBLIC_FROM

DAILY_CHECKLIST = (
    "DAILY ENGAGEMENT - this is the lever, not the posts:\n"
    "X: 10 replies under big accounts (macro / on-chain / options). 2-4 sentences, "
    "one number they don't have, no links, no pitch.\n"
    "LinkedIn: 5 comments on posts from financial-services people (23% of your audience). "
    "Real comments, 2-3 sentences, disagree politely when you can.\n"
    "Your posts: answer every comment within 60 minutes. LinkedIn weighs the first hour.\n"
    "Never: cashtags on X, screenshots on X, more than 1 post/day, links in post body."
)

TG_PERSONAL_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CTA = "TVC Fusion PRO"  # v5.0: name only, no raw link on X
CTA_EVERY_N = 3        # v4.0: CTA every Nth post, not every post
BOOK_MENTION_EVERY_N = 9  # v4.1: mention books roughly every 9th post

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


def _should_add_cta(q):
    """v4.0: CTA rotation — returns True every Nth post."""
    total = len(q.get("posts", []))
    return (total + 1) % CTA_EVERY_N == 0


def _cta_line(q, prefix="Free signals"):
    """v4.0: Returns CTA line or empty string based on rotation."""
    if _should_add_cta(q):
        return f"{prefix}: {CTA}"
    return ""


# ── v4.1: BOOK MENTION ROTATION ──────────────────────────────────────────────

_BOOK_SNIPPETS_X = [
    "Before this, I wrote 6 books on Amazon. Now I build systems that trade 24/7.",
    "I published 6 books before switching to code. Same discipline, different medium.",
    "From 6 published books to 10,000+ lines of trading code. The builder never stops.",
    "Author of 6 books → solo crypto system builder. The common thread: shipping.",
    "6 books on Amazon taught me one thing: consistency compounds. Same with code.",
]

_BOOK_SNIPPETS_LI = [
    "Before I started building TVC Fusion, I published 6 books on Amazon. That taught me how to ship — how to sit down, create something from nothing, and put it in front of people. Building a trading system is the same muscle, different output.",
    "Fun fact most people don't know: before the terminal, before the trading bot, I wrote and published 6 books. The transition from author to builder was more natural than I expected — both require shipping consistently, even when the work isn't perfect yet.",
    "My path to building an automated trading system started in an unexpected place: Amazon KDP. I published 6 books there. Writing taught me how to structure complexity and ship on a deadline. Now I apply the same discipline to code.",
]


def _should_add_book_mention(q):
    """v4.1: Returns True every BOOK_MENTION_EVERY_N posts."""
    total = len(q.get("posts", []))
    return total > 0 and total % BOOK_MENTION_EVERY_N == 0


def _inject_book_mention(post, q):
    """v4.1: Add a book backstory line to a post if rotation says so."""
    if not _should_add_book_mention(q):
        return post
    platform = post.get("platform", "x")
    if platform == "x":
        idx = (int(_now().timestamp()) // 86400) % len(_BOOK_SNIPPETS_X)
        snippet = _BOOK_SNIPPETS_X[idx]
        # Insert before last line (signature ⚡ or CTA)
        lines = post["text"].strip().split("\n")
        if len(lines) >= 3:
            lines.insert(-1, f"\n{snippet}\n")
            post["text"] = "\n".join(lines)
    elif platform == "linkedin":
        idx = (int(_now().timestamp()) // 86400) % len(_BOOK_SNIPPETS_LI)
        snippet = _BOOK_SNIPPETS_LI[idx]
        # Insert after first paragraph (hook)
        parts = post["text"].split("\n\n", 1)
        if len(parts) == 2:
            post["text"] = f"{parts[0]}\n\n{snippet}\n\n{parts[1]}"
    return post


def _compact_li(text, max_lines=10):
    """v4.0: Trim LinkedIn post to max N lines. Keep hook + core + CTA."""
    lines = [l for l in text.strip().split("\n") if l.strip()]
    if len(lines) <= max_lines:
        return text
    # Keep first 2 lines (hook), last 2 lines (CTA + hashtags), fill middle
    hook = lines[:2]
    tail = lines[-2:]
    middle_budget = max_lines - len(hook) - len(tail)
    middle = lines[2:-2][:max(middle_budget, 1)]
    return "\n\n".join(hook + middle + tail)


def _post(platform, ptype, text, graphic, ticker=None, extra=None):
    """Build a post dict."""
    # v5.1: no performance numbers anywhere until STATS_PUBLIC_FROM
    if not SHOW_STATS:
        text = _scrub_stats(text)
    # v4.0: compact LinkedIn posts
    if platform == "linkedin":
        text = _compact_li(text)
    # v5.0: X safe mode
    links = []
    if platform == "x" and X_SAFE_MODE:
        text, links = _x_safe(text)
    if platform == "x" and X_TEXT_ONLY:
        graphic = ""  # v5.1: X posts go out as plain text
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
    if links:
        p["link_in_reply"] = links[0]
    return p


_URL_RE = re.compile(r"https?://\S+|\bt\.me/\S+|\b[a-z0-9.-]+\.(?:com|io|me|net|org)/\S*", re.I)
_CASHTAG_RE = re.compile(r"\$([A-Z]{2,6})\b")
_NFA_RE = re.compile(r"^\s*(not financial advice|nfa|dyor)\b.*$", re.I | re.M)
_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
_STATS_LINE_RE = re.compile(
    r"^.*(win rate|\bWR\b|\bP&?L\b|\bPnL\b|\d+\s*/\s*\d+\s*wins|\d+(\.\d+)?%\s*(WR|win)|"
    r"running stats|cumulative(\s+record)?:|net:\s*\$|total pnl).*$",
    re.I | re.M)


def _scrub_stats(text):
    """v5.1: drop any line that quotes win rate / PnL / trade counts, and fix
    the blank-line gaps it leaves. Applies to every platform while SHOW_STATS is False."""
    text = _STATS_LINE_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
_CTA_LINE_RE = re.compile(r"^\s*(free signals?|follow the signals?|want to see them live\??|see it live)\s*[:→-]?.*$",
                          re.I | re.M)


def _x_safe(text):
    """v5.0: make an X post read like a human wrote it, not a signal bot.
    cashtags -> plain tickers; URLs out of body (returned for a reply);
    'Not financial advice' lines removed."""
    links = _URL_RE.findall(text)
    text = _URL_RE.sub("", text)
    text = _CASHTAG_RE.sub(r"\1", text)
    text = _NFA_RE.sub("", text)
    text = _HASHTAG_RE.sub("", text)  # v5.1: no hashtags on X either
    text = _CTA_LINE_RE.sub("", text)  # v5.1: "Free signals: ..." lines read as spam
    out = []
    for ln in text.split("\n"):
        st = ln.strip()
        if st in ("→", "-", "•") or st.lower().rstrip(":") in ("free signals", "see it", "link", "follow"):
            continue
        # line that only pointed at a removed URL ("See more at", "Full record:", "→ link")
        if re.search(r"(?:\bat|:|→|here)\s*$", st, re.I) and len(st.split()) <= 5:
            continue
        out.append(ln.rstrip())
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, links


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

    # v5.1: Monday is a LESSON built on one observation, not a dump of the dashboard.
    # The two posts that reached anyone last week were the ones explaining the market
    # to a normal person. Terminal verdicts got 1-10 impressions.
    score = (btc or {}).get("fusion_score", 50)
    regime_raw = (btc or {}).get("regime", "RANGING")
    regime = regime_raw.replace("_", " ").lower()
    L = (btc or {}).get("layers", {}) or {}
    sm_verdict = (L.get("smart_money") or {}).get("verdict") or L.get("verdict") or ""
    cvd_read = (L.get("cvd") or {}).get("read") or ""
    oi_24h = (L.get("cvd") or {}).get("oi_24h")

    # Pick the one thing worth explaining this week, with the plain-language lesson.
    if "DOWN" in regime_raw or "CRASH" in regime_raw:
        observation = (f"Bitcoin is in a confirmed downtrend on my screen: lower highs, "
                       f"lower lows, momentum pointing down.")
        lesson = ("The mistake most people make here is buying 'the dip' before the structure "
                  "has changed. A dip in a downtrend is just a lower high waiting to happen. "
                  "I don't look for longs until the market prints a higher low and holds it.")
    elif "UP" in regime_raw and score >= 62:
        observation = (f"Bitcoin is trending up and the data behind it is reasonably clean: "
                       f"score {score}/100, smart money {sm_verdict or 'neutral'}.")
        lesson = ("The temptation in a trend is to add leverage because 'it's working'. "
                  "That's exactly when positioning gets crowded and the shake-outs get violent. "
                  "I keep size fixed in trends. The trend pays you; leverage charges you.")
    elif "UP" in regime_raw:
        observation = (f"Bitcoin is trending up on price, but the inputs underneath aren't "
                       f"confirming it yet: score {score}/100, smart money {sm_verdict or 'neutral'}"
                       + (f", spot flow '{cvd_read}'" if cvd_read else "") + ".")
        lesson = ("Price can lead and the data can lag, but when the gap stays open for days, "
                  "the move usually comes back to meet the data, not the other way round. "
                  "I treat an unconfirmed trend as a range with good PR.")
    else:
        observation = (f"Bitcoin is ranging. No clean trend either way, score {score}/100"
                       + (f", open interest {float(oi_24h):+.1f}% in 24h" if oi_24h is not None else "")
                       + ".")
        lesson = ("Ranges are where accounts die quietly: every breakout looks real, every "
                  "fade looks smart, and you pay for both. The only thing that has worked for me "
                  "in a range is waiting for the edge of it and sizing small. Boring on purpose.")

    li = (
        f"What I'm watching this week, and why.\n\n"
        f"{observation}\n\n"
        f"{lesson}\n\n"
        f"I spent ten years in business analysis before crypto. The habit that carried over: "
        f"decide what would prove you wrong before you decide what you want to be true.\n\n"
        f"What's the one level or data point you're watching this week?\n\n"
        f"#crypto #trading #riskmanagement"
    )

    x = (
        f"Monday read.\n\n"
        f"{observation}\n\n"
        f"{lesson.split('. ')[0]}.\n\n"
        f"What are you watching this week?"
    )

    gr = (
        "📸 GRAPHIC (LinkedIn only): BTC daily chart with the regime structure marked "
        "(higher lows / lower highs) — not a terminal screenshot"
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

        # v5.1: Wednesday is written as a read for a normal person, with the
        # bullets as evidence, not as the post. The 1.10 PCE post (69 impressions,
        # best of the week) was exactly this shape.
        if angle == "bullish":
            read = ("The inputs agree with the price for once. That's rarer than it sounds: most "
                    "of the time price runs ahead and the data catches up, or doesn't. When they "
                    "line up, the right move is usually to do nothing clever. Hold the plan, keep "
                    "the stop where it was, and stop checking the chart every ten minutes.")
        elif angle == "bearish":
            read = ("Price is being held up by people who haven't looked underneath it. The "
                    "positioning data is thinning out while the chart still looks fine, and that "
                    "combination has preceded every sharp drop I've sat through since 2017. It "
                    "doesn't tell you when. It tells you not to be the last one adding.")
        else:
            read = ("Nobody has an edge here, including the people who sound certain. A score in "
                    "the middle means the layers disagree with each other, and when they disagree "
                    "the market usually chops until one side gives up. My rule in this state: no "
                    "new positions, smaller size if I'm already in, and I write down what would "
                    "change my mind before it does.")

        li = (
            f"Mid-week read on Bitcoin, in plain language.\n\n"
            f"{contrarian}\n"
            f"{read}\n\n"
            f"I put these layers on one screen because I used to make the opposite mistake: "
            f"trading the chart and finding out about the positioning afterwards.\n\n"
            f"What's your read on the current structure?\n\n"
            f"#bitcoin #crypto #trading"
        )

        x = (
            f"Mid-week read.\n\n"
            f"BTC score {score}/100, regime {regime_label.lower()}, smart money {sm_verdict}.\n\n"
            f"{read.split('. ')[0]}.\n\n"
            f"What's your read?"
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

    if not SHOW_STATS:
        # v5.1: validation window - no WR / PnL / trade tables in public until
        # STATS_PUBLIC_FROM. Thursday becomes "what the week taught the system".
        n = len(weekly or [])
        wins = [t for t in (weekly or []) if (t.get("pnl_usd") or 0) > 0]
        losses = [t for t in (weekly or []) if (t.get("pnl_usd") or 0) <= 0]
        sl_hits = [t for t in losses if t.get("hit_or_miss") == "hit_sl"]
        if n == 0:
            week_line = ("The system took zero trades this week. Not a bug: nothing cleared the "
                         "entry line. Flat is a position too, and the cheapest one.")
            lesson = ("Most of my worst years came from trading when there was nothing to trade. "
                      "A rule that says 'no' most days is the rule that keeps the account alive "
                      "for the days that matter.")
        elif len(losses) > len(wins):
            week_line = (f"The system took {n} trade{'s' if n != 1 else ''} this week and more "
                         f"of them lost than won. I'm not going to dress that up.")
            lesson = ("What I check on a week like this isn't the P&L, it's whether the losses "
                      "were the kind the rules allow. "
                      + (f"{len(sl_hits)} stopped out at the planned level - that's the system working. "
                         if sl_hits else "")
                      + "The dangerous loss is the one that bypassed a rule. None did.")
        else:
            week_line = (f"The system took {n} trade{'s' if n != 1 else ''} this week and more "
                         f"won than lost. Good week. Also the most dangerous kind.")
            lesson = ("After a green week the urge is to add size, loosen a filter, trade one more. "
                      "I don't change a rule inside its 30-day window, green or red. "
                      "The record only means something if it describes one system.")
        li = (
            f"Thursday check-in on the paper system.\n\n"
            f"{week_line}\n\n"
            f"{lesson}\n\n"
            f"Every decision is logged before the outcome. Full numbers get published when the "
            f"current rule set completes a 30-day window without changes, so the stats describe "
            f"one system, not several.\n\n"
            f"How do you review a week - by the result, or by whether you followed the plan?\n\n"
            f"#trading #riskmanagement #crypto"
        )
        x = (
            f"{week_line}\n\n"
            f"{lesson.split('. ')[0]}.\n\n"
            f"Result or process - which one do you grade yourself on?"
        )
        gr = ""
        return [_post("linkedin", "weekly_results", li, gr),
                _post("x", "weekly_results", x, gr)]

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

    # v5.1: the macro calendar is the most useful thing in a week-ahead post and the
    # one thing chart accounts don't post. Tier-1 events in the next 7 days, in CEST.
    try:
        today = _now().date()
        for ev in fusion.get("macro_events") or []:
            if int(ev.get("tier") or 2) != 1:
                continue
            d_ev = datetime.strptime(ev["date"], "%Y-%m-%d").date()
            if 0 < (d_ev - today).days <= 7:
                hh, mm = ev.get("time_utc", "12:30").split(":")
                cest = f"{(int(hh) + 2) % 24:02d}:{mm}"
                name = ev["name"].split("—")[0].split("(")[0].strip()
                watch_items.append(f"{d_ev.strftime('%a')} {cest} CEST: {name}. "
                                   f"System goes flat 3h before, 1h after.")
    except Exception as e:
        log(f"sunday macro error: {e}")

    for d in decisions:
        if d.get("ticker") != "BTC":
            s = d.get("fusion_score")
            if not s:  # v5.1: 0/None = missing data, not "weakness"
                continue
            if s >= 60 or s <= 35:
                label = "closest to an entry" if s >= 60 else "weakest on the board"
                watch_items.append(f"${d['ticker']}: {label} (score {s}/100)")

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
        f"Next week, what I'm actually watching:\n\n"
        f"{x_list}\n\n"
        f"The calendar matters more than the chart on a week like this. "
        f"Most bad entries I've made were 20 minutes before a number I forgot was coming."
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
# v4.0 — ALERT POST (callable from pump_radar.py)
# ═══════════════════════════════════════════════════════════════════════════════

_ALERT_INSIGHTS = [
    "OI surging + price flat = someone's building a position.",
    "Funding extreme = the crowded side usually loses.",
    "CVD divergence = smart money positioning before price catches up.",
    "Volume spike + low OI = spot-driven, not leveraged. Healthier move.",
    "Liquidation cluster nearby = price magnet. Watch the sweep.",
]


def gen_alert_post(alert):
    """
    v4.0: Generate X post for a HIGH pump/flush alert.
    Callable from pump_radar.py:
        from content_engine import gen_alert_post
        gen_alert_post({"ticker": "SOL", "score": 82, "direction": "PUMP", ...})
    """
    ticker = alert.get("ticker", "?")
    score = alert.get("score", 0)
    direction = alert.get("direction", "PUMP")
    factors = alert.get("factors", [])

    # Pick a relevant insight
    insight_idx = hash(ticker + _today()) % len(_ALERT_INSIGHTS)
    insight = _ALERT_INSIGHTS[insight_idx]

    factor_lines = ""
    if factors:
        top = factors[:3]
        factor_lines = "\n".join(f"→ {f}" for f in top) + "\n\n"

    emoji = "🔴" if "FLUSH" in direction.upper() else "🟢"

    x = (
        f"{emoji} ${ticker} — {direction} alert (score {score})\n\n"
        f"{factor_lines}"
        f"{insight}\n\n"
        f"⚡"
    )

    gr = (
        f"📸 GRAPHIC: TVC Terminal Pump Radar — ${ticker} alert detail\n"
        f"  URL: tradingventureclub.com/terminal/"
    )

    post = _post("x", "alert", x, gr, ticker)

    # Save to queue + notify
    q = _load_queue()
    q["posts"].append(post)
    _save_queue(q)
    _notify_new_posts([post])
    log(f"✓ Alert post for ${ticker} ({direction}) saved + sent")
    return post


# ═══════════════════════════════════════════════════════════════════════════════
# v4.0 — TRADE RECAP (standalone callable from paper_bot.py)
# ═══════════════════════════════════════════════════════════════════════════════

def gen_trade_recap(trade):
    """
    v4.0: Generate and queue trade recap posts immediately after a close.
    Callable from paper_bot.py:
        from content_engine import gen_trade_recap
        gen_trade_recap({"ticker": "BTC", "direction": "long", "pnl_pct": 2.3, ...})
    """
    posts = gen_trade_close(trade)
    if not posts:
        return []

    q = _load_queue()
    x_today = _posts_today(q, "x")
    li_today = _posts_today(q, "linkedin")

    added = []
    for p in posts:
        if p["platform"] == "x" and x_today >= MAX_X_PER_DAY:
            continue
        if p["platform"] == "linkedin" and li_today >= MAX_LI_PER_DAY:
            continue
        q["posts"].append(p)
        added.append(p)
        if p["platform"] == "x":
            x_today += 1
        else:
            li_today += 1

    if added:
        _save_queue(q)
        _notify_new_posts(added)
        log(f"✓ Trade recap for {trade.get('ticker', '?')} "
            f"({trade.get('pnl_pct', 0):+.1f}%) — {len(added)} posts queued")

    return added


# ═══════════════════════════════════════════════════════════════════════════════
# v4.0 — EVENING X POST (3rd daily slot — education one-liner / insight)
# ═══════════════════════════════════════════════════════════════════════════════

_EVENING_INSIGHTS = [
    "The market doesn't owe you a setup every day.\n\nMost edge comes from the trades you DON'T take.",
    "Backtesting is lying to yourself professionally.\n\nForward testing is where conviction meets reality.",
    "Every signal channel that only shows wins is telling you exactly how much they respect your intelligence.",
    "Risk management isn't a chapter in a book.\n\nIt's the only chapter that matters.",
    "Your stop-loss isn't where you lose money.\n\nIt's where you stop losing money.",
    "The best traders I've studied all have one thing in common:\n\nThey're bored most of the time.",
    "\"I'll just move my stop a little.\"\n\nFamous last words of every blown account.",
    "Leverage doesn't make bad trades profitable.\n\nIt makes them fatal faster.",
    "Smart Money doesn't tweet their entries.\n\nThey show up in the data — if you know where to look.",
    "A 60% win rate with 2:1 R:R is a license to print money.\n\nMost traders chase 90% WR with 0.3 R:R instead.",
    "The difference between a trader and a gambler:\n\nOne has a system they trust. The other has hope.",
    "Volume precedes price.\n\nAlways has. Always will.",
    "If you can't explain your edge in one sentence,\nyou probably don't have one.",
    "The market doesn't care about your thesis.\n\nBut your stop-loss does.",
]


def gen_evening_x():
    """v4.0: Evening education/insight one-liner for X. 3rd daily slot."""
    stats = get_cumulative_stats()
    # Rotate daily
    idx = (int(_now().timestamp()) // 86400) % len(_EVENING_INSIGHTS)
    insight = _EVENING_INSIGHTS[idx]

    # Every 3rd evening post gets CTA
    q = _load_queue()
    cta_line = _cta_line(q, prefix="\nFree signals")

    x = f"{insight}{cta_line}\n\n⚡"

    gr = ""  # No graphic for evening insights — text-only

    return [_post("x", "evening_insight", x, gr)]


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

    summary = f"📝 <b>Content Engine v5.2 — {len(posts)} nowych postów</b>\n\n"
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

    # v5.1: the checklist goes out with every batch, even when there is no post today.
    if not any(p["type"] == "reply_brief" for p in posts):
        _tg_send_personal("━━━ <b>CHECKLIST</b> ━━━\n\n" + DAILY_CHECKLIST)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    weekday = _now().weekday()
    theme = DAY_THEMES.get(weekday, "Unknown")
    log(f"Content Engine v5.2 — {_now().isoformat()} — {theme}")

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
                if p["platform"] == "linkedin" and weekday not in LI_DAYS:
                    log(f"  skip LinkedIn {ptype} — not a LinkedIn day (v5.0)")
                    continue
                if p["platform"] == "x" and weekday not in X_DAYS:
                    log(f"  skip X {ptype} — not an X day (v5.1)")
                    continue
                # v4.0: CTA rotation — strip CTA from non-CTA posts
                if not _should_add_cta(q):
                    p["text"] = _strip_cta(p["text"])
                # v4.1: book mention on Tuesday/Friday posts
                if ptype in ("behind_build", "education"):
                    p = _inject_book_mention(p, q)
                if p["platform"] == "x" and x_today >= MAX_X_PER_DAY:
                    log(f"  skip X {ptype} — daily limit")
                    continue
                if p["platform"] == "linkedin" and li_today >= MAX_LI_PER_DAY:
                    log(f"  skip LinkedIn {ptype} — daily limit")
                    continue
                new_posts.append(p)
                q["posts"].append(p)  # v4.0: track for CTA counter
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

    closes = get_recent_closes(hours=6) if TRADE_CLOSE_POSTS else []
    if closes:
        log(f"Found {len(closes)} recent closed trade(s)")
        for trade in closes:
            trade_id = f"{trade['ticker']}_{trade.get('closed_at', '')[:16]}"
            if any(p.get("_trade_id") == trade_id for p in q["posts"]):
                log(f"  skip {trade['ticker']} — already in queue")
                continue

            posts = gen_trade_close(trade)
            for p in posts:
                if p["platform"] == "linkedin":
                    continue  # v5.0: no trade-close posts on LinkedIn
                if p["platform"] == "x" and x_today >= MAX_X_PER_DAY:
                    continue
                new_posts.append(p)
                q["posts"].append(p)
                if p["platform"] == "x":
                    x_today += 1
                else:
                    li_today += 1
            log(f"  ✓ trade close posts for {trade['ticker']} "
                f"({trade.get('pnl_pct', 0):+.1f}%)")

    # ── 3. v4.0: EVENING X POST (3rd daily slot) ───────────────────────────

    if EVENING_X_SLOT and weekday != 5 and not _has_type_today(q, "evening_insight"):
        if x_today < MAX_X_PER_DAY:
            log("Generating evening X insight post")
            evening = gen_evening_x()
            for p in evening:
                if p["platform"] == "x" and x_today < MAX_X_PER_DAY:
                    new_posts.append(p)
                    q["posts"].append(p)
                    x_today += 1
            log("  ✓ evening insight generated")
        else:
            log("Evening slot skipped — X daily limit reached")

    # ── 3b. v5.0: DAILY X REPLY BRIEF (not counted against X limit) ────────
    if weekday != 5 and not _has_type_today(q, "reply_brief"):
        brief = gen_reply_brief()
        if brief:
            new_posts.append(brief)
            q["posts"].append(brief)
            log("  ✓ reply brief generated")

    # ── 4. SAVE + NOTIFY ────────────────────────────────────────────────────

    # Remove temp tracking entries (already saved via q["posts"].append above)
    # Re-deduplicate: q["posts"] may have duplicates from tracking
    seen_ts = set()
    deduped = []
    for p in q["posts"]:
        ts = p.get("ts", "")
        if ts not in seen_ts:
            deduped.append(p)
            seen_ts.add(ts)
    q["posts"] = deduped

    if new_posts:
        q["meta"]["last_run"] = _now().isoformat()
        q["meta"]["version"] = "5.2"
        q["meta"]["total_generated"] = len(q["posts"])
        _save_queue(q)
        log(f"✓ Added {len(new_posts)} new posts to queue (total: {len(q['posts'])})")

        _notify_new_posts(new_posts)
    else:
        log("No new posts to generate this cycle.")
        q["meta"]["last_run"] = _now().isoformat()
        q["meta"]["version"] = "5.2"
        _save_queue(q)

    # Cleanup: keep only last 7 days
    cutoff = (_now() - timedelta(days=7)).strftime("%Y-%m-%d")
    before = len(q["posts"])
    q["posts"] = [p for p in q["posts"] if p.get("date", "") >= cutoff]
    if len(q["posts"]) < before:
        _save_queue(q)
        log(f"Cleaned up {before - len(q['posts'])} old posts (>7 days)")


def gen_reply_brief():
    """v5.0: 3 data-backed talking points to use as REPLIES under large crypto
    accounts today. Replies are the only surface where a reach-restricted account
    still gets impressions. A cheat sheet, not a post."""
    fusion = get_fusion_data() or {}
    decs = fusion.get("decisions") or []
    regime = fusion.get("regime", "?")
    pts = []
    try:
        btc = next((d for d in decs if d.get("ticker") == "BTC"), None)
        if btc:
            L = btc.get("layers") or {}
            sm = L.get("smart_money") or {}
            cvd = L.get("cvd") or {}
            fl = L.get("flush") or {}
            pts.append(
                f"BTC regime {regime}, score {btc.get('score')}/100. "
                f"Hyperliquid smart money net {float(sm.get('net') or 0):+.2f} ({sm.get('verdict', '?')}), "
                f"OI 24h {float(cvd.get('oi_24h') or 0):+.1f}%, spot CVD '{cvd.get('read', '?')}', "
                f"Flush Risk {fl.get('score', '?')}/100."
            )
        top = max(decs, key=lambda d: d.get("score") or 0) if decs else None
        if top and top.get("ticker") != "BTC":
            pts.append(f"Strongest score on the board is {top['ticker']} at {top.get('score')}/100, "
                       f"still below the 62 entry line, so the system sits flat.")
        # v5.1: ETF flows and options are the two numbers chart-only accounts never have
        etf = fusion.get("etf") or {}
        eb, ee = etf.get("btc") or {}, etf.get("eth") or {}
        if eb.get("d7") is not None or ee.get("d7") is not None:
            parts = []
            if eb.get("d7") is not None:
                s = int(eb.get("streak") or 0)
                parts.append(f"BTC {float(eb['d7']):+,.0f}M over 5 sessions"
                             + (f", {abs(s)}-day {'inflow' if s > 0 else 'outflow'} streak" if s else ""))
            if ee.get("d7") is not None:
                parts.append(f"ETH {float(ee['d7']):+,.0f}M")
            pts.append("ETF flows (Farside): " + "; ".join(parts)
                       + ". When price tests a level and the flows don't confirm it, the test usually fails.")
    except Exception as e:
        log(f"reply brief error (fusion): {e}")
    if SHOW_STATS:
        try:
            stats = get_cumulative_stats() or {}
            if stats.get("total"):
                pts.append(f"Public paper record: {stats['total']} trades, {stats.get('wr', 0)}% WR, "
                           f"{float(stats.get('pnl') or 0):+.0f} USD. Every one published, losers included.")
        except Exception as e:
            log(f"reply brief error (stats): {e}")
    if not pts:
        return None
    pts = pts[:4]

    # v5.2: reply SKELETONS built from today's numbers. Not ready replies - the first
    # sentence must still refer to the post you're answering. They cut the work in half.
    skel = _reply_skeletons(fusion)

    li_angles = [
        "Under any 'crypto is a casino' post: agree that leverage is, then explain the one data "
        "layer (ETF flows / positioning) that makes it readable. No pitch.",
        "Under macro posts (jobs, CPI, Fed): add how crypto priced the print - the number from "
        "point 1 - and what the options market expected. Two sentences.",
        "Under 'I lost money in crypto' posts: don't advise. Ask which rule they'd write now that "
        "they didn't have then. People remember the question, not the answer.",
    ]
    text = ("REPLY BRIEF (X) — 10 replies today under big accounts, 2-4 sentences, no links:\n\n"
            + "\n\n".join(f"{i + 1}. {p}" for i, p in enumerate(pts))
            + "\n\nSKELETONS (adapt the first sentence to the post, keep the number):\n\n"
            + "\n\n".join(f"• {k}:\n{v}" for k, v in skel.items())
            + "\n\nWhere: @CryptoHayes @LynAldenContact @KobeissiLetter @caprioleio @WClementeIII "
              "@Pentosh1 @glassnode @coinglass_com @Checkmatey @DylanLeClair_ @MacroScope17. "
              "Reply within 30 min of their post. Never pitch. Never link. Never a cashtag.\n\n"
              "COMMENT BRIEF (LinkedIn) — 5 comments today, financial-services people first:\n\n"
            + "\n\n".join(f"{i + 1}. {a}" for i, a in enumerate(li_angles))
            + "\n\n" + DAILY_CHECKLIST)
    return {"platform": "x_reply", "type": "reply_brief", "ticker": None,
            "date": _today(), "ts": _now().isoformat(), "text": text,
            "graphic": "", "posted": False}


def _reply_skeletons(fusion):
    """v5.2: four reply skeletons (bullish post / bearish post / 'is this the top' /
    alt-season post) written from today's BTC layers and the strongest alt score.
    Plain words, no cashtags, no links; the user prepends one sentence about the post."""
    decs = fusion.get("decisions") or []
    regime = str(fusion.get("regime") or "RANGING")
    btc = next((d for d in decs if d.get("ticker") == "BTC"), None) or {}
    L = btc.get("layers") or {}
    sm, cvd, fl = L.get("smart_money") or {}, L.get("cvd") or {}, L.get("flush") or {}
    score = int(btc.get("score") or btc.get("fusion_score") or 50)
    net = float(sm.get("net") or 0)
    oi = float(cvd.get("oi_24h") or 0)
    read = str(cvd.get("read") or "neutral").replace("_", " ")
    flush = int(fl.get("score") or 0)
    sm_word = "net long" if net > 0.15 else ("net short" if net < -0.15 else "flat")
    oi_word = (f"OI up {oi:.0f}% in 24h" if oi >= 3 else
               f"OI down {abs(oi):.0f}% in 24h" if oi <= -3 else "OI flat on the day")
    spot_word = ("spot is net buying" if "accum" in read else
                 "spot is net selling" if "distrib" in read else "spot flow is neutral")
    mid = 40 <= score < 62
    out = {}

    # 1. under a bullish call
    if score >= 62:
        out["Under a bullish post"] = (
            f"The inputs agree with you for once: score {score}/100, large HL wallets {sm_word}, "
            f"{oi_word} and {spot_word}. The risk now is everyone adding leverage because it's working.")
    elif oi >= 8:
        out["Under a bullish post"] = (
            f"Price agrees, the inputs don't yet. {oi_word[0].upper() + oi_word[1:]} while "
            f"{spot_word} — that's leverage doing the lifting, not demand, and leverage gets "
            f"shaken out first. "
            + ("Large wallets lean long, so maybe they're right; the setup is just fragile."
               if net > 0.15 else "Large wallets aren't leaning in either."))
    else:
        out["Under a bullish post"] = (
            f"Maybe, but the inputs aren't there yet. Score sits at {score}/100, {oi_word} and "
            f"{spot_word} — nobody is buying the breakout, they're waiting for it. "
            + ("Large wallets lean long, so the bias is right; the timing isn't confirmed."
               if net > 0.15 else "Large wallets aren't leaning in either."))

    # 2. under a bearish call
    if flush >= 50:
        out["Under a bearish post"] = (
            f"The flush setup is actually here: Flush Risk {flush}/100, {oi_word}, {spot_word}. "
            f"If this breaks, it breaks fast. Where's your invalidation?")
    else:
        out["Under a bearish post"] = (
            f"The flush setup isn't here. Flush Risk reads {flush}/100: {oi_word}, no funding extreme, "
            f"large wallets {sm_word}. Markets can still drop from here, but it'd be news-driven, "
            f"not positioning-driven — different trade.")

    # 3. under "is this the top?"
    if oi >= 8 and "distrib" in read:
        out["Under 'is this the top?'"] = (
            f"It has the signature: OI up {oi:.0f}% in 24h while spot sells into it. That's borrowed "
            f"buying, and borrowed buying gets repaid. Not a prediction, a positioning read.")
    else:
        out["Under 'is this the top?'"] = (
            f"Tops usually come with crowded leverage: OI spiking while spot sells. Right now "
            f"{oi_word} and {spot_word}. "
            + ("That's not a top signature, that's a range nobody wants to trade. Boring is the read."
               if mid else "Doesn't fit the pattern yet.")
        )

    # 4. under an alt-season post
    alts = [d for d in decs if d.get("ticker") != "BTC" and (d.get("score") or d.get("fusion_score"))]
    if alts:
        top = max(alts, key=lambda d: d.get("score") or d.get("fusion_score") or 0)
        ts = int(top.get("score") or top.get("fusion_score") or 0)
        out["Under an alt-season post"] = (
            f"On my board the strongest alt is {top['ticker']} at {ts}/100 and the entry line is 62. "
            + ("It cleared it — that's the first alt to do so in a while." if ts >= 62 else
               "Every alt is below it. Alt season starts when they clear that line on their own data, "
               "not when BTC pauses."))
    return out


def _strip_cta(text):
    """v4.0: Remove CTA line from post text for non-CTA rotation posts."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip().lower()
        if CTA.lower() in stripped and (
            "free signal" in stripped or "see it" in stripped
            or "follow" in stripped or "→ t.me" in stripped
            or stripped.startswith("free") or stripped.startswith("→ t.me")
            or stripped.startswith("daily signal") or "see the full" in stripped
        ):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI — standalone entry points for paper_bot / pump_radar integration
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """
    v4.0 CLI:
      python content_engine.py              — normal daily run
      python content_engine.py --recap JSON — trade recap (from paper_bot)
      python content_engine.py --alert JSON — alert post (from pump_radar)
    """
    if len(sys.argv) >= 3 and sys.argv[1] == "--recap":
        try:
            trade = json.loads(sys.argv[2])
            gen_trade_recap(trade)
        except json.JSONDecodeError as e:
            log(f"ERROR: invalid JSON for --recap: {e}")
            sys.exit(1)
    elif len(sys.argv) >= 3 and sys.argv[1] == "--alert":
        try:
            alert = json.loads(sys.argv[2])
            gen_alert_post(alert)
        except json.JSONDecodeError as e:
            log(f"ERROR: invalid JSON for --alert: {e}")
            sys.exit(1)
    else:
        run()


if __name__ == "__main__":
    main()
