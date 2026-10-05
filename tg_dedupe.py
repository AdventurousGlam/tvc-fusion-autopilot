#!/usr/bin/env python3
"""
tg_dedupe — one guard for every Telegram channel send in the TVC Fusion pipeline.

WHY (2026-10-05): the same open/close/picks/radar message was landing 2-3x on the
PRO and FREE channels. Causes: a cycle re-running after a partial failure, a state
file that did not get committed before the next 5-minute run, and two scripts
formatting the same event independently. Rather than chase each path, every
channel send goes through should_send() first.

HOW: a fingerprint of (chat_id + normalized text) is stored in tg_sent.json with a
timestamp. The same fingerprint is refused for TTL_HOURS. Normalization strips
HTML tags, whitespace, clock times and ISO dates, so a re-render of the same
event with a new "12:35" or a reformatted price does not slip through.
Callers can also pass an explicit `key` (e.g. "open:BTC:long:2026-10-05") when the
semantic identity of the message matters more than its exact text.

tg_sent.json is committed by the autopilot workflow (see tvc-autopilot.yml) so the
memory survives between GitHub Actions runs. If the file is missing or corrupt the
guard fails OPEN (sends) — a duplicate is better than a lost signal.
"""

from __future__ import annotations
import hashlib
import json
import os
import re
import time
from pathlib import Path

TTL_HOURS = 48
STATE_PATH = Path(os.environ.get("TG_DEDUPE_PATH") or Path(__file__).resolve().parent / "tg_sent.json")

_TAG_RE = re.compile(r"<[^>]+>")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(T[\d:.+Z-]*)?\b")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    t = _TAG_RE.sub("", text or "")
    t = _TIME_RE.sub("", t)
    t = _DATE_RE.sub("", t)
    t = _WS_RE.sub(" ", t).strip().lower()
    return t


def _fingerprint(chat_id: str, text: str, key: str | None) -> str:
    base = f"{chat_id}|{key}" if key else f"{chat_id}|{_normalize(text)}"
    return hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()


def _load() -> dict:
    try:
        if STATE_PATH.exists():
            d = json.loads(STATE_PATH.read_text() or "{}")
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _save(d: dict) -> None:
    try:
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=0, sort_keys=True))
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _prune(d: dict, now: float) -> dict:
    cutoff = now - TTL_HOURS * 3600
    return {k: v for k, v in d.items() if isinstance(v, (int, float)) and v >= cutoff}


def should_send(chat_id: str, text: str, key: str | None = None) -> bool:
    """True if this message has NOT been sent to this chat within TTL_HOURS.
    Records the fingerprint when it returns True, so call it right before sending."""
    if not chat_id:
        return True
    now = time.time()
    d = _prune(_load(), now)
    fp = _fingerprint(str(chat_id), text, key)
    if fp in d:
        return False
    d[fp] = now
    _save(d)
    return True


def forget(chat_id: str, text: str, key: str | None = None) -> None:
    """Undo a should_send() reservation (e.g. the HTTP send failed and you want a retry
    on the next cycle to go through)."""
    d = _load()
    d.pop(_fingerprint(str(chat_id), text, key), None)
    _save(d)


if __name__ == "__main__":
    import sys
    chat = sys.argv[1] if len(sys.argv) > 1 else "test"
    msg = sys.argv[2] if len(sys.argv) > 2 else "hello 12:30"
    print("send" if should_send(chat, msg) else "DUPLICATE")
