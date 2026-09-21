#!/usr/bin/env python3
"""
TVC Fusion PRO — Telegram membership bot (Stripe integration).

Flow:
  1. Klient płaci przez Stripe Checkout (link na stronie / w FREE kanale)
  2. Stripe webhook → ten skrypt (Flask) → bot generuje jednorazowy invite link
  3. Link wysyłany do klienta (email via Stripe receipt + DM jeśli znamy TG username)
  4. Stripe webhook cancel/unpaid → bot kickuje usera z kanału PRO
  5. Codzienne sprawdzenie: kto ma wygasłą subskrypcję → kick

Deployment: GitHub Actions (codzienne sprawdzenie) + mały serwer (webhook listener).
Dane: SQLite (members.db).

Env vars (GitHub Secrets):
  TELEGRAM_BOT_TOKEN      — ten sam bot co alerty (TVC Alerts)
  STRIPE_SECRET_KEY        — sk_live_... z dashboardu Stripe
  STRIPE_WEBHOOK_SECRET    — whsec_... z Stripe webhook settings
  STRIPE_PRICE_ID          — price_... dla planu $29/mo
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Config ────────────────────────────────────────────────────────────────
TG_PRO_CHANNEL = "-1004436927192"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "").strip()

DB_PATH = Path(__file__).parent / "members.db"

# ─── Database ──────────────────────────────────────────────────────────────
def db_init():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stripe_customer_id TEXT UNIQUE,
        stripe_subscription_id TEXT,
        email TEXT,
        telegram_user_id TEXT,
        telegram_username TEXT,
        status TEXT DEFAULT 'active',        -- active / cancelled / kicked
        invite_link TEXT,
        joined_channel INTEGER DEFAULT 0,    -- 1 = dołączył do kanału
        created_at TEXT,
        expires_at TEXT,                      -- NULL = recurring, data = kiedy kończy się
        kicked_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        event_type TEXT,
        stripe_event_id TEXT,
        details TEXT
    )""")
    conn.commit()
    return conn


def log_event(conn, event_type, stripe_event_id="", details=""):
    conn.execute("INSERT INTO events_log (ts, event_type, stripe_event_id, details) VALUES (?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(), event_type, stripe_event_id, details))
    conn.commit()


# ─── Telegram helpers ──────────────────────────────────────────────────────
import urllib.request as ur
import urllib.parse as up
import urllib.error as ue
import ssl

def _tg_ssl():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()

SSL_CTX = _tg_ssl()

def tg_api(method, params=None):
    """Wywołanie Telegram Bot API. Zwraca (ok, result)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = None
    if params:
        data = up.urlencode(params).encode()
    try:
        req = ur.Request(url, data=data) if data else ur.Request(url)
        with ur.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            body = json.loads(resp.read())
            return body.get("ok", False), body.get("result")
    except ue.HTTPError as e:
        err = e.read().decode(errors="replace")[:300]
        print(f"[tg] {method} HTTP {e.code}: {err}")
        return False, err
    except Exception as e:
        print(f"[tg] {method} failed: {e}")
        return False, str(e)


def create_invite_link(name=""):
    """Tworzy jednorazowy invite link do kanału PRO. Ważny 48h."""
    ok, result = tg_api("createChatInviteLink", {
        "chat_id": TG_PRO_CHANNEL,
        "member_limit": 1,
        "expire_date": int(time.time()) + 48 * 3600,
        "name": name[:32] if name else "PRO member"
    })
    if ok and result:
        return result.get("invite_link")
    print(f"[tg] createChatInviteLink failed: {result}")
    return None


def kick_member(telegram_user_id):
    """Usuwa członka z kanału PRO."""
    ok, result = tg_api("banChatMember", {
        "chat_id": TG_PRO_CHANNEL,
        "user_id": telegram_user_id,
        "revoke_messages": "false"
    })
    if ok:
        # Odbanuj żeby mógł wrócić po odnowieniu
        tg_api("unbanChatMember", {
            "chat_id": TG_PRO_CHANNEL,
            "user_id": telegram_user_id,
            "only_if_banned": "true"
        })
    return ok


def send_dm(telegram_user_id, text):
    """Wyślij DM do usera (musi wcześniej /start do bota)."""
    return tg_api("sendMessage", {
        "chat_id": telegram_user_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"
    })


# ─── Stripe helpers ───────────────────────────────────────────────────────
def stripe_api(method, endpoint, params=None):
    """Proste wywołanie Stripe API (bez SDK — zero zależności)."""
    url = f"https://api.stripe.com/v1/{endpoint}"
    headers = {
        "Authorization": f"Bearer {STRIPE_SECRET}",
    }
    data = None
    if params:
        data = up.urlencode(params).encode()
    try:
        req = ur.Request(url, data=data, method=method, headers=headers)
        with ur.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            return json.loads(resp.read())
    except ue.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(f"[stripe] {endpoint} HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"[stripe] {endpoint} failed: {e}")
        return None


def create_checkout_session(success_url="https://tradingventureclub.com/terminal/?welcome=pro",
                            cancel_url="https://tvc-membership.onrender.com/join"):
    """Tworzy Stripe Checkout Session dla planu PRO."""
    result = stripe_api("POST", "checkout/sessions", {
        "mode": "subscription",
        "line_items[0][price]": STRIPE_PRICE_ID,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "subscription_data[metadata][source]": "tvc_fusion_pro",
    })
    if result:
        return result.get("url")
    return None


# ─── Webhook handler (standalone Flask/Bottle) ────────────────────────────
def handle_stripe_webhook(payload_body: bytes, sig_header: str) -> dict:
    """
    Przetwarza Stripe webhook event. Zwraca dict z akcją.

    Wywoływane z serwera HTTP — Flask/Bottle/lambda.
    Weryfikacja podpisu: wymaga stripe lib LUB ręczna HMAC (poniżej).
    """
    import hmac
    import hashlib

    # Weryfikacja podpisu webhook
    if STRIPE_WEBHOOK_SECRET:
        parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
        timestamp = parts.get("t", "")
        expected_sig = parts.get("v1", "")
        signed_payload = f"{timestamp}.{payload_body.decode()}"
        computed = hmac.new(
            STRIPE_WEBHOOK_SECRET.encode(),
            signed_payload.encode(),
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(computed, expected_sig):
            return {"error": "invalid signature"}

    event = json.loads(payload_body)
    event_type = event.get("type", "")
    event_id = event.get("id", "")
    data = event.get("data", {}).get("object", {})

    conn = db_init()
    log_event(conn, event_type, event_id, json.dumps(data)[:500])

    result = {"event": event_type, "action": "none"}

    if event_type == "checkout.session.completed":
        # Nowa subskrypcja — generuj invite link
        customer_id = data.get("customer", "")
        email = data.get("customer_email") or data.get("customer_details", {}).get("email", "")
        sub_id = data.get("subscription", "")

        link = create_invite_link(email[:32] if email else customer_id[-8:])

        conn.execute("""INSERT OR REPLACE INTO members
            (stripe_customer_id, stripe_subscription_id, email, status, invite_link, created_at)
            VALUES (?, ?, ?, 'active', ?, ?)""",
            (customer_id, sub_id, email, link, datetime.now(timezone.utc).isoformat()))
        conn.commit()

        result = {"event": event_type, "action": "invite_created", "email": email, "link": link}
        print(f"[membership] NEW: {email} → invite link created")

    elif event_type in ("customer.subscription.deleted",
                        "customer.subscription.paused",
                        "invoice.payment_failed"):
        # Subskrypcja anulowana / pauza / nieudana płatność
        customer_id = data.get("customer", "")
        row = conn.execute("SELECT * FROM members WHERE stripe_customer_id=?", (customer_id,)).fetchone()
        if row and row["telegram_user_id"] and row["status"] == "active":
            kicked = kick_member(row["telegram_user_id"])
            conn.execute("UPDATE members SET status='kicked', kicked_at=? WHERE stripe_customer_id=?",
                         (datetime.now(timezone.utc).isoformat(), customer_id))
            conn.commit()
            # Powiadom usera
            send_dm(row["telegram_user_id"],
                     "⚠️ Twoja subskrypcja TVC Fusion PRO wygasła.\n"
                     "Dostęp do kanału PRO został usunięty.\n"
                     "Odnów subskrypcję: https://tradingventureclub.com/#pricing")
            result = {"event": event_type, "action": "kicked", "customer": customer_id, "kicked": kicked}
            print(f"[membership] KICKED: {row['email']} (sub deleted/failed)")
        else:
            conn.execute("UPDATE members SET status='cancelled' WHERE stripe_customer_id=?", (customer_id,))
            conn.commit()
            result = {"event": event_type, "action": "marked_cancelled", "customer": customer_id}

    elif event_type == "customer.subscription.updated":
        # Odnowienie — jeśli był kicked, wygeneruj nowy link
        customer_id = data.get("customer", "")
        status = data.get("status", "")
        if status == "active":
            row = conn.execute("SELECT * FROM members WHERE stripe_customer_id=?", (customer_id,)).fetchone()
            if row and row["status"] in ("cancelled", "kicked"):
                link = create_invite_link(row["email"][:32] if row["email"] else "renewed")
                conn.execute("UPDATE members SET status='active', invite_link=?, kicked_at=NULL WHERE stripe_customer_id=?",
                             (link, customer_id))
                conn.commit()
                if row["telegram_user_id"]:
                    send_dm(row["telegram_user_id"],
                             f"✅ Twoja subskrypcja TVC Fusion PRO została odnowiona!\n"
                             f"Dołącz ponownie: {link}")
                result = {"event": event_type, "action": "reactivated", "customer": customer_id, "link": link}
                print(f"[membership] REACTIVATED: {row['email']}")

    conn.close()
    return result


# ─── Bot command: /start z deep link ──────────────────────────────────────
def handle_bot_start(telegram_user_id, telegram_username, text):
    """
    Obsługuje /start <stripe_customer_id> — linkuje TG user z Stripe customer.
    Wywoływane z webhook bota (setWebhook) lub polling (getUpdates).
    """
    parts = text.strip().split()
    if len(parts) < 2:
        send_dm(telegram_user_id,
                "👋 Witaj w TVC Fusion!\n\n"
                "🆓 Darmowy kanał: @TVCFusionSignals\n"
                "💎 PRO ($29/mo): https://tvc-membership.onrender.com/join\n\n"
                "Już zapłaciłeś? Kliknij link z emaila lub napisz swój email.")
        return

    identifier = parts[1]  # stripe customer ID lub email
    conn = db_init()
    row = conn.execute("SELECT * FROM members WHERE stripe_customer_id=? OR email=?",
                       (identifier, identifier)).fetchone()
    if row and row["status"] == "active":
        # Link TG user z kontem Stripe
        conn.execute("UPDATE members SET telegram_user_id=?, telegram_username=? WHERE id=?",
                     (str(telegram_user_id), telegram_username or "", row["id"]))
        conn.commit()
        link = row["invite_link"]
        if link:
            send_dm(telegram_user_id,
                     f"✅ Konto połączone!\n\nDołącz do kanału PRO:\n{link}\n\n"
                     f"Link ważny 48h, jednorazowy.")
        else:
            # Wygeneruj nowy link
            new_link = create_invite_link(telegram_username or str(telegram_user_id))
            conn.execute("UPDATE members SET invite_link=? WHERE id=?", (new_link, row["id"]))
            conn.commit()
            send_dm(telegram_user_id,
                     f"✅ Konto połączone!\n\nDołącz do kanału PRO:\n{new_link}\n\n"
                     f"Link ważny 48h, jednorazowy.")
    else:
        send_dm(telegram_user_id,
                "❌ Nie znalazłem aktywnej subskrypcji dla tego ID.\n"
                "Sprawdź email z Stripe lub kup subskrypcję:\n"
                "https://tvc-membership.onrender.com/join")
    conn.close()


# ─── Codzienne sprawdzenie (GitHub Actions cron) ──────────────────────────
def daily_check():
    """
    Uruchamiane raz dziennie. Sprawdza:
    1. Członkowie z wygasłymi subskrypcjami → kick
    2. Invite linki starsze niż 48h bez dołączenia → regeneruj
    3. Raport do osobistego chata Gosi
    """
    conn = db_init()
    now = datetime.now(timezone.utc)
    stats = {"active": 0, "kicked_today": 0, "total": 0}

    members = [dict(r) for r in conn.execute("SELECT * FROM members").fetchall()]
    stats["total"] = len(members)

    for m in members:
        if m["status"] == "active":
            stats["active"] += 1
            # Sprawdź w Stripe czy sub nadal aktywna
            if m["stripe_subscription_id"] and STRIPE_SECRET:
                sub = stripe_api("GET", f"subscriptions/{m['stripe_subscription_id']}")
                if sub and sub.get("status") not in ("active", "trialing"):
                    if m["telegram_user_id"]:
                        kick_member(m["telegram_user_id"])
                        send_dm(m["telegram_user_id"],
                                "⚠️ Twoja subskrypcja TVC Fusion PRO wygasła.\n"
                                "Odnów: https://tradingventureclub.com/#pricing")
                    conn.execute("UPDATE members SET status='kicked', kicked_at=? WHERE id=?",
                                 (now.isoformat(), m["id"]))
                    stats["kicked_today"] += 1
                    print(f"[daily] kicked {m['email']} (sub status: {sub.get('status')})")

    conn.commit()

    # Raport do osobistego chata
    personal_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if personal_chat and BOT_TOKEN:
        report = (f"📊 <b>TVC PRO — raport członkostwa</b>\n"
                  f"Aktywnych: {stats['active']}\n"
                  f"Kicked dziś: {stats['kicked_today']}\n"
                  f"Łącznie: {stats['total']}")
        tg_api("sendMessage", {"chat_id": personal_chat, "text": report,
                                "parse_mode": "HTML", "disable_web_page_preview": "true"})

    conn.close()
    print(f"[daily] done: {stats}")
    return stats


# ─── Flask webhook server (opcjonalny — do uruchomienia na serwerze) ──────
def run_webhook_server(host="0.0.0.0", port=8080):
    """Mini serwer HTTP do odbierania webhooków Stripe + Telegram bot updates."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            if self.path == "/stripe/webhook":
                sig = self.headers.get("Stripe-Signature", "")
                result = handle_stripe_webhook(body, sig)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

            elif self.path == "/telegram/webhook":
                update = json.loads(body)
                msg = update.get("message", {})
                text = msg.get("text", "")
                user = msg.get("from", {})
                if text.startswith("/start"):
                    handle_bot_start(user.get("id"), user.get("username"), text)
                self.send_response(200)
                self.end_headers()

            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                conn = db_init()
                active = conn.execute("SELECT COUNT(*) FROM members WHERE status='active'").fetchone()[0]
                conn.close()
                self.wfile.write(json.dumps({"ok": True, "active_members": active}).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                conn = db_init()
                active = conn.execute("SELECT COUNT(*) FROM members WHERE status='active'").fetchone()[0]
                conn.close()
                self.wfile.write(json.dumps({"ok": True, "active_members": active}).encode())
            elif self.path.startswith("/join"):
                # Redirect do Stripe Checkout
                url = create_checkout_session()
                if url:
                    self.send_response(302)
                    self.send_header("Location", url)
                    self.end_headers()
                else:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"Stripe checkout error")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            print(f"[http] {args[0]}" if args else "")

    server = HTTPServer((host, port), Handler)
    print(f"[membership] webhook server on {host}:{port}")
    print(f"  Stripe webhook: POST /stripe/webhook")
    print(f"  Telegram bot:   POST /telegram/webhook")
    print(f"  Health:         GET  /health")
    print(f"  Join (redirect): GET /join")
    server.serve_forever()


# ─── CLI ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="TVC Fusion PRO — membership management")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("server", help="Start webhook HTTP server")
    sub.add_parser("daily", help="Daily membership check (cron)")
    sub.add_parser("stats", help="Show membership stats")

    checkout_p = sub.add_parser("checkout", help="Create a Stripe Checkout URL")
    link_p = sub.add_parser("invite", help="Create an invite link manually")
    link_p.add_argument("--name", default="manual", help="Name tag for the link")

    kick_p = sub.add_parser("kick", help="Kick a member by TG user ID")
    kick_p.add_argument("user_id", help="Telegram user ID to kick")

    args = ap.parse_args()

    if args.cmd == "server":
        port = int(os.environ.get("PORT", 8080))
        run_webhook_server(port=port)

    elif args.cmd == "daily":
        daily_check()

    elif args.cmd == "stats":
        conn = db_init()
        active = conn.execute("SELECT COUNT(*) FROM members WHERE status='active'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        kicked = conn.execute("SELECT COUNT(*) FROM members WHERE status='kicked'").fetchone()[0]
        print(f"Active: {active} | Kicked: {kicked} | Total: {total}")
        recent = conn.execute("SELECT email, status, created_at FROM members ORDER BY id DESC LIMIT 5").fetchall()
        for r in recent:
            print(f"  {r['email']} — {r['status']} ({r['created_at'][:10]})")
        conn.close()

    elif args.cmd == "checkout":
        if not STRIPE_SECRET or not STRIPE_PRICE_ID:
            print("ERROR: STRIPE_SECRET_KEY and STRIPE_PRICE_ID required")
            sys.exit(1)
        url = create_checkout_session()
        print(f"Checkout URL: {url}")

    elif args.cmd == "invite":
        if not BOT_TOKEN:
            print("ERROR: TELEGRAM_BOT_TOKEN required")
            sys.exit(1)
        link = create_invite_link(args.name)
        print(f"Invite link: {link}")

    elif args.cmd == "kick":
        if not BOT_TOKEN:
            print("ERROR: TELEGRAM_BOT_TOKEN required")
            sys.exit(1)
        ok = kick_member(args.user_id)
        print(f"Kicked: {ok}")

    else:
        ap.print_help()
