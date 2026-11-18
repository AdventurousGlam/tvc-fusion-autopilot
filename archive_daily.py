#!/usr/bin/env python3
"""Dzienny, dopisywany snapshot stanu, który bot już zacommitował.

Raz na zakończony dzień UTC (od 2026-11-05) powstaje archive/daily/YYYY-MM-DD/:
rejestr paper, fusion z tego dnia, progi odczytane z kodu tamtego commitu
i kopia umowy zamrożenia. Suma SHA-256 jest w MANIFEST.sha256.

Istniejącego katalogu nie nadpisujemy. Późniejsza migracja paper_trades.db
nie rusza raz zapisanego dnia — to pilnuje archive_guard.py.

Sekret TVC_PARAMS nie jest czytany ani zapisywany. W snapshotcie są domyślne
wartości z kodu commitu źródłowego; sekret w Actions może je w runtime nadpisać
(w szczególności FEE_PCT i SLIPPAGE_PCT).

  python3 archive_daily.py                 # dopisz brakujące zakończone dni
  python3 archive_daily.py --dry-run       # tylko pokaż
  python3 archive_daily.py --dry-run --date 2026-11-05

Od 2026-11-18 (wariant A) stan bota jest w sekretnym gist-cie. Dzień zamyka się
wtedy samym odciskiem: archive/daily/YYYY-MM-DD/MANIFEST.json z SHA-256 plików
stanu w rewizji gista = ostatni commit gista z datą przed północą UTC tego dnia.
Baza zostaje w gist-cie; w manifeście nie ma żadnych wyników finansowych.

  python3 archive_daily.py --source gist                 # CI: GITHUB_TOKEN=GIST_PAT, TVC_STATE_GIST_ID
  python3 archive_daily.py --source gist --backfill      # dni od 2026-11-18 do wczoraj, mode "backfill"
  python3 archive_daily.py --source gist --gist-dir DIR  # lokalny pełny klon gista (tylko odczyt)
  python3 archive_daily.py --verify-day 2026-11-18 --gist-dir DIR   # sprawdź odcisk z gistem
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
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import holdout_params

ROOT = Path(__file__).resolve().parent
DAILY = ROOT / "archive" / "daily"
MODULES = ("tvc_params.py", "paper_bot.py", "auto_fusion.py", "pump_radar.py")
# 18.11.2026 (krok 2) stan bota przeniesiono do sekretnego gista. paper_trades.db
# w repo zatrzymał się na commicie z 13:08 UTC tego dnia, więc historia gita main
# nie opisuje już dni od 18.11. Ostatni dzień, który wolno zamknąć z repo:
REPO_STATE_LAST_DAY = date(2026, 11, 17)
# Wariant A: od tego dnia dzień archiwum to odcisk stanu z historii gista.
GIST_STATE_FIRST_DAY = REPO_STATE_LAST_DAY + timedelta(days=1)
GIST_MANIFEST = "MANIFEST.json"
GIST_SCHEMA = "tvc.archive.day/2"
STATE_MANIFEST = "STATE_VERSION.json"     # manifest stanu pisany przez tvc_state.py push
STATE_SCHEMA = "tvc.state/1"
REPO_LAST_STATE_COMMIT = "d4f4dbea7"      # ostatni commit main z paper_trades.db (18.11 13:08 UTC)


def _git(root: Path, *args, text=True, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=text,
    )


def git_blob(root: Path, rev: str, path: str) -> bytes | None:
    spec = f"{rev}:{path}"
    probe = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", spec],
        capture_output=True,
    )
    if probe.returncode != 0:
        return None
    return subprocess.check_output(["git", "-C", str(root), "cat-file", "blob", spec])


def commit_as_of(root: Path, day: date) -> str:
    end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
    stamp = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    proc = _git(root, "rev-list", "-1", f"--before={stamp}", "HEAD")
    sha = proc.stdout.strip()
    if not sha:
        raise RuntimeError(f"brak commitu przed {stamp}")
    return sha


def _parse_day(text: str) -> date:
    return date.fromisoformat(text)


def freeze_day() -> date:
    return _parse_day(holdout_params.FREEZE_DATE)


def missing_days(today: date, existing: set[str], only: date | None = None) -> list[date]:
    """Zakończone dni UTC od zamrożenia, których katalog jeszcze nie istnieje."""
    if only is not None:
        if only < freeze_day():
            raise RuntimeError(
                f"{only.isoformat()} jest przed zamrożeniem {holdout_params.FREEZE_DATE}"
            )
        if only.isoformat() in existing:
            return []
        return [only]
    last = today - timedelta(days=1)
    if last < freeze_day():
        return []
    days = []
    cursor = freeze_day()
    while cursor <= last:
        if cursor.isoformat() not in existing:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _first_add(root: Path, path: str) -> str | None:
    proc = _git(
        root,
        "log",
        "--diff-filter=A",
        "--reverse",
        "--format=%H",
        "--",
        path,
        check=False,
    )
    if proc.returncode != 0:
        return None
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return lines[0] if lines else None


def frozen_bytes(root: Path, state_commit: str) -> tuple[bytes, str]:
    blob = git_blob(root, state_commit, "holdout/FROZEN.json")
    if blob is not None:
        return blob, "state_commit"
    intro = _first_add(root, "holdout/FROZEN.json")
    if intro:
        blob = git_blob(root, intro, "holdout/FROZEN.json")
        if blob is not None:
            return blob, "initial_freeze"
    path = root / "holdout" / "FROZEN.json"
    if path.is_file():
        return path.read_bytes(), "worktree"
    raise RuntimeError("brak holdout/FROZEN.json — nie ma czego włożyć do snapshotu")


def extract_at(root: Path, state_commit: str) -> tuple[dict, str]:
    """Progi z plików .py tamtego commitu. Ekstraktor: z commitu, a jak go nie ma — z drzewa."""
    extractor = git_blob(root, state_commit, "holdout_params.py")
    extractor_from = "state_commit"
    if extractor is None:
        extractor = (root / "holdout_params.py").read_bytes()
        extractor_from = "worktree"
    modules = {}
    for name in MODULES:
        blob = git_blob(root, state_commit, name)
        if blob is None:
            raise RuntimeError(f"{state_commit[:8]} nie ma {name}")
        modules[name] = blob
    with tempfile.TemporaryDirectory(prefix="tvc-holdout-") as tmp:
        folder = Path(tmp)
        for name, blob in modules.items():
            (folder / name).write_bytes(blob)
        script = folder / "holdout_params.py"
        script.write_bytes(extractor)
        out = folder / "extract.json"
        env = os.environ.copy()
        env.pop("TVC_PARAMS", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(folder)
        env["TVC_DB_PATH"] = str(folder / "unused.db")
        proc = subprocess.run(
            [sys.executable, str(script), "--json-out", str(out)],
            cwd=folder,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0 or not out.is_file():
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"odczyt progów z {state_commit[:8]} nie wyszedł: {detail[:500]}")
        return json.loads(out.read_text(encoding="utf-8")), extractor_from


def build_params(day: date, state_commit: str, extracted: dict, extractor_from: str,
                 frozen_from: str, has_fusion: bool, captured_at: str) -> bytes:
    doc = {
        "archive_date": day.isoformat(),
        "captured_at": captured_at,
        "state_commit": state_commit,
        "strategy_version": extracted["strategy_version"],
        "radar_version": extracted["radar_version"],
        "freeze_date": holdout_params.FREEZE_DATE,
        "fusion_included": has_fusion,
        "fusion_source": f"fusion_{day.isoformat()}.json" if has_fusion else None,
        "frozen_config_from": frozen_from,
        "extractor_from": extractor_from,
        "tvc_params_recorded": False,
        "tvc_params_note": (
            "Sekret TVC_PARAMS nie jest w tym pliku. Runtime (GitHub Actions) może "
            "nadpisać domyślne progi, w tym FEE_PCT i SLIPPAGE_PCT. code_defaults to "
            "wartości z kodu state_commit przy braku sekretu."
        ),
        "code_defaults": extracted,
        "radar_test_track": extracted["test_track"],
    }
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def render_manifest(files: dict[str, bytes], header: list[str]) -> str:
    lines = list(header)
    for name in sorted(files):
        digest = hashlib.sha256(files[name]).hexdigest()
        lines.append(f"{digest}  {name}")
    return "\n".join(lines) + "\n"


def write_day(dest: Path, files: dict[str, bytes], header: list[str]) -> None:
    """Atomowy zapis katalogu dnia. Istniejący dest zostaje nietknięty."""
    if dest.exists():
        raise FileExistsError(dest)
    if any("/" in name or name.startswith(".") or name == "MANIFEST.sha256" for name in files):
        raise ValueError("zła nazwa pliku snapshotu")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".tmp-{dest.name}-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    try:
        payload = dict(files)
        (tmp / "MANIFEST.sha256").write_text(render_manifest(payload, header), encoding="utf-8")
        for name, data in payload.items():
            (tmp / name).write_bytes(data)
        import archive_guard
        problems = archive_guard.verify_day_dir(tmp, dest.name)
        if problems:
            raise RuntimeError("; ".join(problems))
        os.rename(tmp, dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def assemble(root: Path, day: date, captured_at: str | None = None) -> tuple[dict[str, bytes], list[str], str]:
    if day > REPO_STATE_LAST_DAY:
        raise RuntimeError(
            f"od {(REPO_STATE_LAST_DAY + timedelta(days=1)).isoformat()} stan bota jest w gist, "
            "nie w repo — snapshot z historii gita byłby fałszywy; nic nie zapisuję "
            "(wariant A: --source gist)"
        )
    state = commit_as_of(root, day)
    db = git_blob(root, state, "paper_trades.db")
    if db is None:
        raise RuntimeError(f"{state[:8]} nie ma paper_trades.db")
    fusion_name = f"fusion_{day.isoformat()}.json"
    fusion = git_blob(root, state, fusion_name)
    frozen, frozen_from = frozen_bytes(root, state)
    extracted, extractor_from = extract_at(root, state)
    captured = captured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = {
        "paper_trades.db": db,
        "params.json": build_params(
            day, state, extracted, extractor_from, frozen_from, fusion is not None, captured
        ),
        "frozen_config.json": frozen,
    }
    if fusion is not None:
        files["fusion.json"] = fusion
    header = [
        "# TVC daily archive — append-only",
        f"# date: {day.isoformat()}",
        f"# state_commit: {state}",
        f"# captured_at: {captured}",
        f"# freeze_date: {holdout_params.FREEZE_DATE}",
        "# alg: sha256",
    ]
    return files, header, state


# ---------------------------------------------------------------- wariant A (gist)

_SECRETS: list[str] = []


def _register_secrets() -> None:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("TVC_STATE_TOKEN") or ""
    gid = os.environ.get("TVC_STATE_GIST_ID") or ""
    for item in (tok, gid, base64.b64encode(f"x-access-token:{tok}".encode()).decode() if tok else ""):
        if item and len(item) >= 6 and item not in _SECRETS:
            _SECRETS.append(item)


def scrub(text: str) -> str:
    """Nic z adresu gista ani tokenu nie trafia do publicznego logu Actions."""
    for item in _SECRETS:
        text = text.replace(item, "***")
    text = re.sub(r"https?://[^\s'\"]*gist\.github\.com/[^\s'\"]*", "https://gist.github.com/***", text)
    text = re.sub(r"file://[^\s'\"]+", "file://***", text)
    text = re.sub(r"(?i)authorization: *[^\n]*", "authorization: ***", text)
    return text


def _gist_env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("TVC_STATE_TOKEN") or ""
    if tok and not os.environ.get("TVC_STATE_GIST_URL"):
        b64 = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://gist.github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {b64}"
    return env


def _ggit(gdir: Path, *args, text=True, check=True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(gdir), *args], capture_output=True, text=text, env=_gist_env()
    )
    if check and proc.returncode != 0:
        err = proc.stderr if text else proc.stderr.decode("utf-8", "replace")
        raise RuntimeError("git (gist): " + scrub((err or "").strip())[:300])
    return proc


def _gist_url() -> str:
    url = os.environ.get("TVC_STATE_GIST_URL")
    if url:
        return url
    gid = (os.environ.get("TVC_STATE_GIST_ID") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8,64}", gid):
        raise RuntimeError("TVC_STATE_GIST_ID pusty lub w złym formacie (secret nie ustawiony?)")
    return f"https://gist.github.com/{gid}.git"


def open_gist(gist_dir: Path | None, workdir: Path) -> Path:
    """Pełna historia gista (bez checkoutu). gist_dir = gotowy lokalny klon (test/odczyt)."""
    if gist_dir is not None:
        gdir = gist_dir.resolve()
    else:
        _register_secrets()
        url = _gist_url()
        gdir = workdir / "gist"
        last = ""
        for attempt in range(1, 4):
            proc = subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", "--filter=blob:none", url, str(gdir)],
                capture_output=True, text=True, env=_gist_env(),
            )
            if proc.returncode == 0:
                break
            last = scrub((proc.stderr or "").strip())[:200]
            print(f"   klon gista nieudany (próba {attempt}/3): {last}")
            shutil.rmtree(gdir, ignore_errors=True)
        else:
            raise RuntimeError("gist niedostępny — nic nie zapisuję")
    shallow = _ggit(gdir, "rev-parse", "--is-shallow-repository", check=False)
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        raise RuntimeError("klon gista płytki albo nieczytelny — potrzebna pełna historia rewizji")
    return gdir


def gist_rev_as_of(gdir: Path, day: date) -> tuple[str, str] | None:
    """Ostatni commit gista z datą (committer) przed północą UTC kończącą dzień."""
    end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
    # git --before jest włącznie (<=), więc 23:59:59 = ściśle przed północą
    stamp = (end - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    proc = _ggit(gdir, "rev-list", "-1", "--format=%H %ct", f"--before={stamp}", "HEAD", check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln and not ln.startswith("commit ")]
    if proc.returncode != 0 or not lines:
        return None
    sha, ct = lines[0].split()
    when = datetime.fromtimestamp(int(ct), tz=timezone.utc)
    if when >= end:      # --before liczy w sekundach; pilnujemy granicy wprost
        return None
    return sha, when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _gist_blob(gdir: Path, rev: str, name: str) -> bytes:
    return _ggit(gdir, "cat-file", "blob", f"{rev}:{name}", text=False).stdout


def gist_state_at(gdir: Path, rev: str) -> tuple[dict[str, bytes], dict]:
    """Pliki stanu z rewizji gista, sprawdzone z jej STATE_VERSION.json."""
    names = set(_ggit(gdir, "ls-tree", "--name-only", rev).stdout.split())
    if STATE_MANIFEST not in names:
        raise RuntimeError(f"rewizja {rev[:10]} nie ma {STATE_MANIFEST} (bootstrap?) — nie zamykam z niej dnia")
    sv_raw = _gist_blob(gdir, rev, STATE_MANIFEST)
    try:
        sv = json.loads(sv_raw)
        listed = sv["files"]
    except Exception as exc:
        raise RuntimeError(f"{STATE_MANIFEST} w {rev[:10]} nieczytelny ({exc.__class__.__name__})")
    if sv.get("schema") != STATE_SCHEMA or not isinstance(listed, dict) or DB not in listed:
        raise RuntimeError(f"{STATE_MANIFEST} w {rev[:10]}: zły schemat albo brak {DB}")
    files = {STATE_MANIFEST: sv_raw}
    for name, want in sorted(listed.items()):
        if name not in names:
            raise RuntimeError(f"{rev[:10]}: {STATE_MANIFEST} wymienia {name}, a pliku nie ma")
        data = _gist_blob(gdir, rev, name)
        if hashlib.sha256(data).hexdigest() != want:
            raise RuntimeError(f"{rev[:10]}: suma {name} nie zgadza się z {STATE_MANIFEST}")
        files[name] = data
    return files, sv


DB = "paper_trades.db"


def db_counts(data: bytes) -> dict[str, int]:
    """Tylko liczby pozycji (bez cen i wyników). Baza musi przejść integrity_check."""
    with tempfile.TemporaryDirectory(prefix="tvc-archA-") as tmp:
        path = Path(tmp) / DB
        path.write_bytes(data)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError(f"{DB} nie przechodzi integrity_check")
            cols = {row[1] for row in con.execute("PRAGMA table_info(positions)")}
            row = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(status='open'),0), COALESCE(SUM(status='closed'),0), "
                + ("COALESCE(SUM(excluded=1),0), " if "excluded" in cols else "0, ")
                + "COALESCE(MAX(id),0) FROM positions"
            ).fetchone()
        finally:
            con.close()
    return {"total": int(row[0]), "open": int(row[1]), "closed": int(row[2]),
            "excluded": int(row[3]), "max_id": int(row[4])}


def _now() -> datetime:
    fake = os.environ.get("TVC_ARCHIVE_FAKE_NOW")
    if fake and os.environ.get("GITHUB_ACTIONS") != "true":   # tylko testy lokalne
        return datetime.strptime(fake, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def assemble_gist(root: Path, gdir: Path, day: date, mode: str, created_at: str) -> tuple[bytes, dict] | None:
    """Manifest dnia z historii gista. None = brak rewizji sprzed końca dnia (nie fabrykujemy)."""
    found = gist_rev_as_of(gdir, day)
    if found is None:
        return None
    rev, rev_at = found
    files, sv = gist_state_at(gdir, rev)
    counts = db_counts(files[DB])
    code_commit = commit_as_of(root, day)
    extracted, extractor_from = extract_at(root, code_commit)
    frozen, frozen_from = frozen_bytes(root, code_commit)
    fusion_name = f"fusion_{day.isoformat()}.json"
    fusion = git_blob(root, code_commit, fusion_name)
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    rev_dt = datetime.strptime(rev_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    notes = []
    if day == GIST_STATE_FIRST_DAY:
        notes.append(
            f"Dzień przejścia: do 13:08 UTC stan bota był w repo (ostatni commit z bazą "
            f"{REPO_LAST_STATE_COMMIT}), potem w sekretnym gist-cie. Odcisk dotyczy stanu z gista na koniec dnia."
        )
    if rev_dt < start:
        notes.append("Gist nie zmienił się w tym dniu — odcisk to ostatnia wcześniejsza rewizja.")
    run_id = os.environ.get("GITHUB_RUN_ID")
    doc = {
        "schema": GIST_SCHEMA,
        "date": day.isoformat(),
        "mode": mode,
        "freeze_date": holdout_params.FREEZE_DATE,
        "source": "gist",
        "selection_rule": (
            "ostatni commit gista stanu (git rev-list -1 --before=<dzień>T23:59:59Z HEAD), "
            f"data committera < {(start + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        ),
        "gist_revision": rev,
        "gist_revision_committed_at": rev_at,
        "revision_from_earlier_day": rev_dt < start,
        "state_written_at": str(sv.get("written_at", "")),
        "state_run_number": str(sv.get("run_number", "")),
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        "bytes": {name: len(data) for name, data in sorted(files.items())},
        "positions": counts,
        "code": {
            "commit": code_commit,
            "strategy_version": extracted["strategy_version"],
            "radar_version": extracted["radar_version"],
            "frozen_config_sha256": hashlib.sha256(frozen).hexdigest(),
            "frozen_config_from": frozen_from,
            "extractor_from": extractor_from,
            "code_defaults": extracted,
            "radar_test_track": extracted["test_track"],
            "tvc_params_recorded": False,
            "fusion_file": fusion_name if fusion is not None else None,
            "fusion_sha256": hashlib.sha256(fusion).hexdigest() if fusion is not None else None,
        },
        "created_at": created_at,
        "workflow_run": {
            "id": run_id or "local",
            "number": os.environ.get("GITHUB_RUN_NUMBER") or "local",
            "repository": os.environ.get("GITHUB_REPOSITORY") or "local",
            "event": os.environ.get("GITHUB_EVENT_NAME") or "local",
        },
        "results_included": False,
        "notes": notes,
    }
    if mode == "backfill":
        doc["backfill_note"] = (
            "Uzupełnione później z historii gista, nie tej samej nocy. Słabszy dowód: "
            "historię gista właściciel mógł przepisać przed datą tego commitu w repo."
        )
    data = (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    return data, doc


def write_gist_day(dest: Path, data: bytes) -> None:
    """Atomowy zapis dnia wariantu A (jeden plik). Istniejący dest zostaje nietknięty."""
    if dest.exists():
        raise FileExistsError(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".tmp-{dest.name}-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    try:
        (tmp / GIST_MANIFEST).write_bytes(data)
        import archive_guard
        problems = archive_guard.verify_day_dir(tmp, dest.name)
        if problems:
            raise RuntimeError("; ".join(problems))
        os.rename(tmp, dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def verify_gist_day(root: Path, gdir: Path, day: str) -> list[str]:
    """Niezależne sprawdzenie: odciski z MANIFEST.json == pliki z gista w gist_revision."""
    man = root / "archive" / "daily" / day / GIST_MANIFEST
    if not man.is_file():
        return [f"{day}: brak {man.relative_to(root)}"]
    doc = json.loads(man.read_text(encoding="utf-8"))
    rev = doc["gist_revision"]
    errors = []
    probe = _ggit(gdir, "cat-file", "-e", f"{rev}^{{commit}}", check=False)
    if probe.returncode != 0:
        return [f"{day}: rewizji {rev[:10]} nie ma w gist-cie (historia przepisana?)"]
    ct = _ggit(gdir, "show", "-s", "--format=%ct", rev).stdout.strip()
    got_at = datetime.fromtimestamp(int(ct), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if got_at != doc["gist_revision_committed_at"]:
        errors.append(f"{day}: data rewizji {got_at} != manifest {doc['gist_revision_committed_at']}")
    later = gist_rev_as_of(gdir, date.fromisoformat(day))
    if later is None or later[0] != rev:
        errors.append(f"{day}: w obecnej historii gista ostatnia rewizja dnia to "
                      f"{later[0][:10] if later else 'brak'}, nie {rev[:10]}")
    for name, want in sorted(doc["sha256"].items()):
        try:
            data = _gist_blob(gdir, rev, name)
        except RuntimeError:
            errors.append(f"{day}: {name} nie istnieje w rewizji {rev[:10]}")
            continue
        if hashlib.sha256(data).hexdigest() != want:
            errors.append(f"{day}: {name} sha256 różny od manifestu")
        if len(data) != doc["bytes"].get(name):
            errors.append(f"{day}: {name} rozmiar różny od manifestu")
    try:
        files, _ = gist_state_at(gdir, rev)
        if db_counts(files[DB]) != doc["positions"]:
            errors.append(f"{day}: liczby pozycji z bazy != manifest")
    except RuntimeError as exc:
        errors.append(f"{day}: {exc}")
    return errors


def _existing(daily: Path) -> set[str]:
    if not daily.is_dir():
        return set()
    return {
        child.name for child in daily.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    }


def _print_plan(day: date, files: dict[str, bytes], state: str, write: bool) -> None:
    mode = "ZAPIS" if write else "PODGLĄD (nic nie zapisuję)"
    print(f"{mode}  {day.isoformat()}  commit {state}")
    for name in sorted(files):
        digest = hashlib.sha256(files[name]).hexdigest()
        print(f"   {name:22} {len(files[name]):10} B  {digest}")


def run(root: Path, dry_run: bool, only: date | None, today: date | None = None,
        source: str = "repo", gist_dir: Path | None = None, backfill: bool = False) -> int:
    today = today or _now().date()
    daily = root / "archive" / "daily"
    existing = _existing(daily)
    if only is not None and only.isoformat() in existing:
        print(f"Już zamknięte: {only.isoformat()} — nie nadpisuję.")
        return 0
    try:
        days = missing_days(today, existing, only)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return 1
    if only is not None and only >= today and not dry_run:
        print(f"❌ {only.isoformat()} jeszcze trwa (UTC). Zamykam tylko zakończone dni.")
        return 1
    if not days:
        if existing:
            last = ", ".join(sorted(existing)[-3:])
            print(
                f"Archiwum aktualne ({len(existing)} dni, ostatnie: {last}). "
                f"Dziś UTC {today.isoformat()} jeszcze nie jest zamykane."
            )
        else:
            print(
                f"Brak dni do archiwum (zamrożenie {holdout_params.FREEZE_DATE}, "
                f"dziś UTC {today.isoformat()}). Pierwszy zamknięty dzień pojawi się po północy."
            )
        return 0
    gist_days = [d for d in days if d >= GIST_STATE_FIRST_DAY]
    if source == "gist" and gist_days:
        _register_secrets()
        with tempfile.TemporaryDirectory(prefix="tvc-gist-") as tmp:
            try:
                gdir = open_gist(gist_dir, Path(tmp))
            except RuntimeError as exc:
                print(f"❌ {scrub(str(exc))}")
                return 1
            code = _run_days(root, [d for d in days if d < GIST_STATE_FIRST_DAY], today, dry_run, daily)
            if code:
                return code
            return _run_gist_days(root, gdir, gist_days, today, dry_run, daily, backfill)
    return _run_days(root, days, today, dry_run, daily)


def _run_gist_days(root: Path, gdir: Path, days: list[date], today: date, dry_run: bool,
                   daily: Path, backfill: bool) -> int:
    now = _now()
    created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for day in days:
        if day >= today:
            print(f"❌ {day.isoformat()} jeszcze trwa (UTC) — dni wariantu A zamykam tylko po północy.")
            return 1
        day_end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
        # "nightly" tylko gdy manifest powstaje w ciągu doby po końcu dnia; inaczej uczciwie "backfill"
        nightly = not backfill and day == today - timedelta(days=1) and day_end <= now < day_end + timedelta(days=1)
        mode = "nightly" if nightly else "backfill"
        try:
            built = assemble_gist(root, gdir, day, mode, created_at)
        except (RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError, OSError, sqlite3.Error) as exc:
            print(f"❌ {day.isoformat()}: {scrub(str(exc))}")
            return 1
        if built is None:
            print(f"ℹ️  {day.isoformat()}: brak rewizji gista sprzed końca dnia — dnia nie zamykam "
                  "(nic nie fabrykuję, katalog nie powstaje).")
            continue
        data, doc = built
        what = "PODGLĄD (nic nie zapisuję)" if dry_run else "ZAPIS"
        print(f"{what}  {day.isoformat()}  {mode}  gist {doc['gist_revision'][:12]} "
              f"@ {doc['gist_revision_committed_at']}  plików {len(doc['sha256'])}  "
              f"pozycji {doc['positions']['total']}  sha256({DB})={doc['sha256'][DB][:16]}…")
        if dry_run:
            continue
        dest = daily / day.isoformat()
        if dest.exists():
            print(f"   już zamknięte: {dest.relative_to(root)}")
            continue
        try:
            write_gist_day(dest, data)
        except (RuntimeError, OSError) as exc:
            print(f"❌ {day.isoformat()}: manifest odrzucony: {scrub(str(exc))}")
            return 1
        print(f"   zapisane: {dest.relative_to(root)}/{GIST_MANIFEST}")
    return 0


def _run_days(root: Path, days: list[date], today: date, dry_run: bool, daily: Path) -> int:
    for day in days:
        preview = day >= today
        try:
            files, header, state = assemble(root, day)
        except (RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
            print(f"❌ {day.isoformat()}: {exc}")
            return 1
        if preview:
            print(
                f"Uwaga: {day.isoformat()} jeszcze trwa. To podgląd stanu z HEAD, "
                "nie zamknięcie dnia — nic nie zapisuję, więc nocny bieg "
                "nadal weźmie ostatni commit przed północą UTC."
            )
        _print_plan(day, files, state, write=not dry_run and not preview)
        if dry_run or preview:
            manifest = render_manifest(files, header)
            print("   --- MANIFEST.sha256 ---")
            print(manifest, end="" if manifest.endswith("\n") else "\n")
            continue
        dest = daily / day.isoformat()
        if dest.exists():
            print(f"   już zamknięte: {dest}")
            continue
        write_day(dest, files, header)
        print(f"   zapisane: {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dopisz dzienne archiwum paper/fusion/progów")
    parser.add_argument("--dry-run", action="store_true", help="policz sumy, nic nie zapisuj")
    parser.add_argument("--date", help="jeden dzień YYYY-MM-DD (dzisiejszy tylko z --dry-run)")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--source", choices=("repo", "gist"), default="repo",
                        help="gist = dni od 2026-11-18 z historii sekretnego gista (wariant A)")
    parser.add_argument("--gist-dir", type=Path, help="lokalny pełny klon gista zamiast klonowania")
    parser.add_argument("--backfill", action="store_true",
                        help="wszystkie brakujące dni od 2026-11-18 oznacz jako backfill")
    parser.add_argument("--verify-day", help="sprawdź MANIFEST.json dnia z gistem (wymaga --gist-dir albo sekretów)")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.verify_day:
        _register_secrets()
        with tempfile.TemporaryDirectory(prefix="tvc-gist-") as tmp:
            try:
                gdir = open_gist(args.gist_dir, Path(tmp))
                problems = verify_gist_day(root, gdir, args.verify_day)
            except (RuntimeError, OSError, KeyError, json.JSONDecodeError) as exc:
                problems = [scrub(str(exc))]
        if problems:
            for item in problems:
                print(f"   ❌ {item}")
            return 1
        print(f"   ✅ {args.verify_day}: odciski z MANIFEST.json zgadzają się z gistem")
        return 0
    only = _parse_day(args.date) if args.date else None
    return run(root, args.dry_run, only, source=args.source, gist_dir=args.gist_dir,
               backfill=args.backfill)


if __name__ == "__main__":
    sys.exit(main())
