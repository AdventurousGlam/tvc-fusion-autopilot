#!/usr/bin/env python3
"""Stan i wyniki bota w PRYWATNYM gist-cie zamiast w commitach publicznego repo.

Komendy (uruchamiane z katalogu repo, tam gdzie leży paper_trades.db):

  tvc_state.py pull  --state-dir DIR [--no-skip-worktree]
      Klonuje gist (depth 1), sprawdza sumy, kopiuje pliki stanu do katalogu roboczego.
      Wyjście 0 = stan wczytany (status=ok) albo gist oznaczony do pierwszego zasilenia
      plikiem BOOTSTRAP_ALLOWED (status=bootstrap: bierze stan z checkoutu repo).
      Każdy inny przypadek (brak sekretu, brak sieci, 404, rozjazd sum, pusty gist
      bez BOOTSTRAP_ALLOWED) = kod != 0 i NICZEGO nie zapisujemy.

  tvc_state.py push  --state-dir DIR [--logs-dir DIR]
      Sprawdza integralność (SQLite integrity_check, liczba pozycji nie spada, JSON-y
      parsują się, brak znaczników konfliktu), potem dopisuje JEDEN commit do gista.
      Bez --force. Jeśli gist ruszył się w trakcie przebiegu (inny pisarz) — kod 6,
      nic nie nadpisujemy.

Sekrety: URL, ID gista i token nigdy nie trafiają do stdout/stderr (publiczne logi
Actions). Wszystko, co drukujemy z gita, przechodzi przez _scrub().
Zmienne: GITHUB_TOKEN (w workflow = secrets.GIST_PAT), TVC_STATE_GIST_ID,
(test) TVC_STATE_GIST_URL — lokalne bare repo zamiast GitHuba.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "tvc.state/1"
MANIFEST = "STATE_VERSION.json"
BOOTSTRAP_MARK = "BOOTSTRAP_ALLOWED"
BASE_FILE = "BASE_SHA"
DB = "paper_trades.db"

# Pliki stanu i wyników, które dotąd commitował workflow (git add ... w kroku "Commit").
STATE_FILES = [
    "paper_trades.db",
    "results.json",
    "crypto_picks.json",
    "crypto_picks_history.json",
    "layers_cache.json",
    "pump_radar.json",
    "pump_radar_alerts.json",
    "pump_radar_test45.json",
    "correlation_data.json",
    "market_predictions.json",
    "content_queue.json",
    "weekly_recap.json",
    "om_cache.json",
    "tg_sent.json",
    "autopilot_health.json",
]
MAX_FILE_BYTES = 90 * 1024 * 1024      # twardy limit gista przy git push = 100 MB / plik
LOG_MAX_BYTES = 400 * 1024
CONFLICT = re.compile(rb"^(<<<<<<< |>>>>>>> |=======$)", re.M)

_SECRETS: list[str] = []


def _register_secrets() -> None:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("TVC_STATE_TOKEN") or ""
    gid = os.environ.get("TVC_STATE_GIST_ID") or ""
    for s in (tok, gid, base64.b64encode(f"x-access-token:{tok}".encode()).decode() if tok else ""):
        if s and len(s) >= 6:
            _SECRETS.append(s)


def _scrub(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    text = re.sub(r"https://[^\s'\"]*gist\.github\.com/[^\s'\"]*", "https://gist.github.com/***", text)
    text = re.sub(r"(?i)authorization: *[^\n]*", "authorization: ***", text)
    return text


def say(msg: str) -> None:
    print(_scrub(msg), flush=True)


def die(code: int, msg: str) -> "None":
    say(f"[tvc_state] BŁĄD({code}): {msg}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"state_status=error{code}\n")
    sys.exit(code)


def set_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"{key}={value}\n")


def git(cwd: Path, *args: str, check: bool = True, auth: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    cmd = ["git"]
    if auth:
        tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("TVC_STATE_TOKEN") or ""
        if tok and not os.environ.get("TVC_STATE_GIST_URL"):
            b64 = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.https://gist.github.com/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {b64}"
    cmd += list(args)
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(_scrub((proc.stderr or proc.stdout or "git error").strip())[:300])
    return proc


def gist_url() -> str:
    url = os.environ.get("TVC_STATE_GIST_URL")
    if url:
        return url
    gid = (os.environ.get("TVC_STATE_GIST_ID") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8,64}", gid):
        die(2, "TVC_STATE_GIST_ID pusty lub w złym formacie (secret nie ustawiony?)")
    if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("TVC_STATE_TOKEN")):
        die(2, "brak GITHUB_TOKEN (GIST_PAT)")
    return f"https://gist.github.com/{gid}.git"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def positions_count(db: Path) -> int | None:
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return int(c.execute("SELECT COUNT(*) FROM positions").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return None


def db_ok(db: Path) -> bool:
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            c.close()
    except Exception:
        return False


def skip_worktree(root: Path, files: list[str]) -> None:
    """Pliki stanu w checkoucie są śledzone w repo (stare wersje). Oznaczamy je, żeby
    git status ich nie pokazywał i żaden `git add`/`commit -a` ich nie zabrał."""
    if not (root / ".git").exists():
        return
    tracked = git(root, "ls-files", "--", *files, check=False).stdout.split()
    if tracked:
        git(root, "update-index", "--skip-worktree", "--", *tracked, check=False)


def cmd_pull(a: argparse.Namespace) -> None:
    root = Path.cwd()
    url = gist_url()
    sd = Path(a.state_dir)
    if sd.exists():
        shutil.rmtree(sd)
    last = ""
    for i in range(1, 4):
        try:
            git(root, "clone", "--quiet", "--depth", "1", url, str(sd), auth=True)
            break
        except RuntimeError as e:
            last = str(e)
            say(f"[tvc_state] clone nieudany (próba {i}/3): {last[:120]}")
            if sd.exists():
                shutil.rmtree(sd, ignore_errors=True)
            time.sleep(3 * i)
    else:
        die(3, "gist niedostępny — przebieg NIE zapisuje stanu i NIE commituje do repo")
    sha = git(sd, "rev-parse", "HEAD").stdout.strip()
    (sd / ".git" / BASE_FILE).write_text(sha)
    man_path = sd / MANIFEST
    if man_path.exists():
        try:
            man = json.loads(man_path.read_text())
            files = man["files"]
        except Exception as e:
            die(4, f"{MANIFEST} nieczytelny: {e}")
        if DB not in files:
            die(4, f"w manifeście brak {DB}")
        for name, want in files.items():
            p = sd / name
            if not p.is_file():
                die(4, f"manifest wymienia {name}, a pliku nie ma w gist-cie")
            if sha256(p) != want:
                die(4, f"suma {name} nie zgadza się z manifestem (uszkodzony gist?)")
        if not db_ok(sd / DB):
            die(4, f"{DB} z gista nie przechodzi integrity_check")
        for name in files:
            shutil.copy2(sd / name, root / name)
        if not a.no_skip_worktree:
            skip_worktree(root, list(files))
        n = positions_count(root / DB)
        say(f"[tvc_state] stan wczytany z gista: {len(files)} plików, pozycji w bazie: {n}, base={sha[:8]}")
        set_output("state_status", "ok")
        return
    if (sd / BOOTSTRAP_MARK).exists():
        if not (root / DB).is_file() or not db_ok(root / DB):
            die(5, f"bootstrap: brak poprawnego {DB} w checkoucie")
        if not a.no_skip_worktree:
            skip_worktree(root, STATE_FILES)
        say(f"[tvc_state] BOOTSTRAP: gist pusty z {BOOTSTRAP_MARK} — stan startowy z checkoutu repo "
            f"(pozycji: {positions_count(root / DB)}), base={sha[:8]}")
        set_output("state_status", "bootstrap")
        return
    die(5, f"gist nie ma {MANIFEST} ani {BOOTSTRAP_MARK} — nie wiem, czy to właściwy gist; nic nie zapisuję")


def cmd_push(a: argparse.Namespace) -> None:
    root = Path.cwd()
    sd = Path(a.state_dir)
    if not (sd / ".git").exists():
        die(6, "brak sklonowanego gista (krok pull nie przeszedł) — nic nie zapisuję")
    base = (sd / ".git" / BASE_FILE).read_text().strip()
    # --- walidacja przed zapisem ---
    present = [f for f in STATE_FILES if (root / f).is_file()]
    if DB not in present:
        die(7, f"brak {DB} — nie zapisuję")
    if (root / (DB + "-wal")).exists() or (root / (DB + "-journal")).exists():
        die(7, "baza ma niezamknięty -wal/-journal — nie zapisuję")
    if not db_ok(root / DB):
        die(7, f"{DB} nie przechodzi integrity_check — nie zapisuję")
    new_n = positions_count(root / DB)
    old_n = positions_count(sd / DB) if (sd / DB).is_file() else None
    if new_n is None:
        die(7, "nie umiem policzyć pozycji w bazie")
    if old_n is not None and new_n < old_n and os.environ.get("TVC_STATE_ALLOW_SHRINK") != "1":
        die(7, f"liczba pozycji spadła {old_n} -> {new_n} — nie nadpisuję (TVC_STATE_ALLOW_SHRINK=1 omija)")
    for f in present:
        p = root / f
        if p.stat().st_size > MAX_FILE_BYTES:
            die(7, f"{f} ma {p.stat().st_size} B > limit gista")
        if f.endswith(".json"):
            raw = p.read_bytes()
            if CONFLICT.search(raw):
                die(7, f"{f} ma znaczniki konfliktu — nie zapisuję")
            try:
                json.loads(raw)
            except Exception as e:
                die(7, f"{f} nie jest poprawnym JSON-em ({e.__class__.__name__}) — nie zapisuję")
    # --- gist nie mógł się ruszyć w trakcie przebiegu ---
    try:
        git(sd, "fetch", "--quiet", "--depth", "1", "origin", auth=True)
        remote = git(sd, "rev-parse", "FETCH_HEAD").stdout.strip()
    except RuntimeError as e:
        die(6, f"nie mogę sprawdzić gista przed zapisem: {e}")
    if remote != base:
        die(6, f"gist zmienił się w trakcie przebiegu (base {base[:8]} -> {remote[:8]}) — NIE nadpisuję")
    # --- zapis ---
    for f in present:
        shutil.copy2(root / f, sd / f)
    logs_note = None
    if a.logs_dir and Path(a.logs_dir).is_dir():
        parts = []
        for lp in sorted(Path(a.logs_dir).glob("*.log")):
            parts.append(f"===== {lp.stem} =====\n" + lp.read_text(errors="replace"))
        blob = "\n".join(parts).encode()[-LOG_MAX_BYTES:]
        (sd / "last_cycle.log").write_bytes(blob)
        logs_note = "last_cycle.log"
    manifest = {
        "schema": SCHEMA,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "run_number": os.environ.get("GITHUB_RUN_NUMBER", "local"),
        "positions": new_n,
        "files": {f: sha256(sd / f) for f in present},
    }
    (sd / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    if (sd / BOOTSTRAP_MARK).exists():
        git(sd, "rm", "-q", "-f", BOOTSTRAP_MARK)
    git(sd, "config", "user.name", "tvc-autopilot")
    git(sd, "config", "user.email", "autopilot@tvcfusion.local")
    add = present + [MANIFEST] + ([logs_note] if logs_note else [])
    git(sd, "add", "--", *add)
    if git(sd, "diff", "--cached", "--quiet", check=False).returncode == 0:
        say("[tvc_state] brak zmian w stanie.")
        set_output("state_saved", "nochange")
        return
    git(sd, "commit", "--quiet", "-m", f"state {manifest['written_at']} run {manifest['run_number']}")
    branch = git(sd, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    for i in range(1, 4):
        proc = git(sd, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", check=False, auth=True)
        if proc.returncode == 0:
            say(f"[tvc_state] stan zapisany do gista: {len(present)} plików, pozycji {new_n}.")
            set_output("state_saved", "yes")
            return
        err = _scrub((proc.stderr or "").strip())[:200]
        if "non-fast-forward" in err or "rejected" in err or "fetch first" in err:
            die(6, "push odrzucony (gist ruszył się w trakcie) — NIE nadpisuję")
        say(f"[tvc_state] push nieudany (próba {i}/3): {err}")
        time.sleep(3 * i)
    die(6, "push do gista nie przeszedł 3 razy — stan z tego przebiegu nie został zapisany")


def main() -> None:
    _register_secrets()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--no-skip-worktree", action="store_true")
    p.set_defaults(fn=cmd_pull)
    q = sub.add_parser("push")
    q.add_argument("--state-dir", required=True)
    q.add_argument("--logs-dir")
    q.set_defaults(fn=cmd_push)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
