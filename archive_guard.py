#!/usr/bin/env python3
"""Nienaruszalność archive/daily.

Dwa niezależne sprawdzenia:

1. MANIFEST.sha256 zgadza się z bajtami plików w katalogu dnia (podmiana treści
   bez poprawienia sumy odpada od razu).
2. W historii gita każdy plik dnia pojawia się tylko jako dodanie, wszystkie
   w tym samym commicie. Późniejsza zmiana, skasowanie albo dosypanie pliku
   do zamkniętego dnia wywala sprawdzenie — także wtedy, gdy manifest został
   przepisany tak, żeby suma znowu się zgadzała.

Dwa formaty dnia:
- do 2026-11-17: pełny snapshot (paper_trades.db, params.json, frozen_config.json,
  fusion.json) + MANIFEST.sha256 — stan bota był wtedy w repo;
- od 2026-11-18 (stan bota w sekretnym gist-cie): tylko MANIFEST.json z odciskami
  SHA-256 plików stanu w podanej rewizji gista, liczbami pozycji i wersją kodu.
  Bez wyników finansowych — klucze/teksty typu pnl/equity są odrzucane.
Oba formaty podlegają temu samemu sprawdzeniu historii (tylko dodanie, raz).

Płytki klon (shallow) nie wystarcza: stary commit z podmianą mógłby wypaść
poza okno. Sprawdzenie wtedy pada, zamiast udawać, że historii nie ruszono.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DAILY = ROOT / "archive" / "daily"
FREEZE_DATE = "2026-11-05"
ALLOWED = {
    "MANIFEST.json",
    "paper_trades.db",
    "fusion.json",
    "params.json",
    "frozen_config.json",
    "MANIFEST.sha256",
}
REQUIRED = ("paper_trades.db", "params.json", "frozen_config.json", "MANIFEST.sha256")
_LINE_RE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9._-]+)$")
_PATH_RE = re.compile(r"^archive/daily/(\d{4}-\d{2}-\d{2})/([^/]+)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Wariant A: od tego dnia stan bota jest w sekretnym gist-cie, a dzień archiwum
# to sam odcisk (MANIFEST.json). Wcześniejsze dni mają pełną bazę (MANIFEST.sha256).
GIST_FROM = "2026-11-18"
GIST_SCHEMA = "tvc.archive.day/2"
GIST_MODES = ("nightly", "backfill")
GIST_REQUIRED_FILES = ("paper_trades.db", "STATE_VERSION.json")
GIST_KEYS_REQUIRED = {
    "schema", "date", "mode", "freeze_date", "source", "selection_rule",
    "gist_revision", "gist_revision_committed_at", "revision_from_earlier_day",
    "state_written_at", "state_run_number", "sha256", "bytes", "positions",
    "code", "created_at", "workflow_run", "results_included", "notes",
}
GIST_KEYS_OPTIONAL = {"backfill_note"}
POSITIONS_KEYS = {"total", "open", "closed", "excluded", "max_id"}
CODE_KEYS_REQUIRED = {
    "commit", "strategy_version", "radar_version", "frozen_config_sha256",
    "frozen_config_from", "extractor_from", "code_defaults", "radar_test_track",
    "tvc_params_recorded", "fusion_file", "fusion_sha256",
}
RUN_KEYS = {"id", "number", "repository", "event"}
# Nic, co wygląda na wynik finansowy, nie ma prawa trafić do publicznego manifestu.
_LEAK_RE = re.compile(
    r"(pnl|p&l|equity|profit|balance|win_?rate|winrate|drawdown|realized|"
    r"r_multiple|return_pct|roi\b|gain|loss(es)?\b|wins\b|wygran|stratn|zysk|strat[ay]\b)",
    re.I,
)


def _git(root: Path, *args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def verify_day_dir(day_dir: Path, date: str | None = None) -> list[str]:
    errors = []
    date = date or day_dir.name
    if (day_dir / "MANIFEST.json").exists() or date >= GIST_FROM:
        return verify_gist_day_dir(day_dir, date)
    manifest_path = day_dir / "MANIFEST.sha256"
    if not manifest_path.is_file():
        return [f"{date}: brak MANIFEST.sha256"]
    listed: dict[str, str] = {}
    for lineno, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            errors.append(f"{date}: zła linia manifestu {lineno}")
            continue
        digest, name = match.group(1), match.group(2)
        if name in listed:
            errors.append(f"{date}: {name} dwa razy w manifeście")
        if name == "MANIFEST.sha256":
            errors.append(f"{date}: manifest nie może hashować sam siebie")
        listed[name] = digest

    if any(child.is_dir() for child in day_dir.iterdir()):
        errors.append(f"{date}: katalog dnia nie może mieć podkatalogów")
    present = {child.name for child in day_dir.iterdir() if child.is_file()}
    present.discard("MANIFEST.sha256")
    if present != set(listed):
        extra = sorted(present - set(listed))
        missing = sorted(set(listed) - present)
        errors.append(f"{date}: pliki vs manifest, ekstra={extra} brak={missing}")
    unknown = sorted(set(listed) - (ALLOWED - {"MANIFEST.sha256"}))
    if unknown:
        errors.append(f"{date}: niedozwolone pliki {unknown}")
    for name in REQUIRED:
        if name == "MANIFEST.sha256":
            continue
        if name not in listed:
            errors.append(f"{date}: manifest nie obejmuje {name}")
    for name, digest in listed.items():
        path = day_dir / name
        if not path.is_file():
            continue
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != digest:
            errors.append(f"{date}: {name} sha256 {got} != manifest {digest}")

    params_path = day_dir / "params.json"
    if params_path.is_file():
        errors.extend(_check_params(date, params_path, "fusion.json" in listed))
    frozen_path = day_dir / "frozen_config.json"
    if frozen_path.is_file():
        try:
            frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{date}: frozen_config.json nie jest JSON ({exc})")
        else:
            if not isinstance(frozen, dict) or frozen.get("freeze_date") != FREEZE_DATE:
                errors.append(f"{date}: frozen_config.json freeze_date != {FREEZE_DATE}")
    return errors


def _leaks(value, path: str = "") -> list[str]:
    """Klucze i teksty, które wyglądają na wyniki finansowe."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if _LEAK_RE.search(str(key)):
                found.append(f"{path}{key}")
            found.extend(_leaks(item, f"{path}{key}."))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(_leaks(item, f"{path}{idx}."))
    elif isinstance(value, str):
        if _LEAK_RE.search(value):
            found.append(f"{path.rstrip('.')}='{value[:40]}'")
    return found


def _day_end(date: str):
    from datetime import datetime, timedelta, timezone
    d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d + timedelta(days=1)


def _ts(text):
    from datetime import datetime, timezone
    if not isinstance(text, str) or not _TS_RE.match(text):
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def verify_gist_day_dir(day_dir: Path, date: str) -> list[str]:
    """Dzień wariantu A: jedyny plik MANIFEST.json, bez bazy i bez wyników."""
    from datetime import timedelta
    errors: list[str] = []
    if date < GIST_FROM:
        return [f"{date}: dzień sprzed {GIST_FROM} musi mieć pełną bazę (MANIFEST.sha256), nie sam odcisk"]
    if any(child.is_dir() for child in day_dir.iterdir()):
        errors.append(f"{date}: katalog dnia nie może mieć podkatalogów")
    present = sorted(child.name for child in day_dir.iterdir() if child.is_file())
    if present != ["MANIFEST.json"]:
        return errors + [
            f"{date}: od {GIST_FROM} dzień to tylko MANIFEST.json (baza zostaje w gist-cie), jest {present}"
        ]
    raw = (day_dir / "MANIFEST.json").read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return errors + [f"{date}: MANIFEST.json nie jest JSON ({exc})"]
    if not isinstance(doc, dict):
        return errors + [f"{date}: MANIFEST.json nie jest obiektem"]
    leaks = _leaks(doc)
    if leaks:
        errors.append(f"{date}: MANIFEST.json wygląda na wyniki finansowe: {leaks[:5]}")
    keys = set(doc)
    missing = sorted(GIST_KEYS_REQUIRED - keys)
    extra = sorted(keys - GIST_KEYS_REQUIRED - GIST_KEYS_OPTIONAL)
    if missing:
        errors.append(f"{date}: MANIFEST.json bez pól {missing}")
    if extra:
        errors.append(f"{date}: MANIFEST.json ma niedozwolone pola {extra}")
    if missing:
        return errors
    if doc["schema"] != GIST_SCHEMA:
        errors.append(f"{date}: schema {doc['schema']!r} != {GIST_SCHEMA}")
    if doc["date"] != date:
        errors.append(f"{date}: MANIFEST.date = {doc['date']!r}")
    if doc["freeze_date"] != FREEZE_DATE:
        errors.append(f"{date}: freeze_date != {FREEZE_DATE}")
    if doc["source"] != "gist":
        errors.append(f"{date}: source musi być 'gist'")
    if doc["results_included"] is not False:
        errors.append(f"{date}: results_included musi być false")
    if not _SHA_RE.match(str(doc["gist_revision"])):
        errors.append(f"{date}: gist_revision nie jest SHA commitu")
    if not isinstance(doc["revision_from_earlier_day"], bool):
        errors.append(f"{date}: revision_from_earlier_day nie jest bool")
    if not isinstance(doc["notes"], list) or not all(isinstance(n, str) for n in doc["notes"]):
        errors.append(f"{date}: notes to lista tekstów")
    end = _day_end(date)
    rev_at = _ts(doc["gist_revision_committed_at"])
    if rev_at is None:
        errors.append(f"{date}: gist_revision_committed_at nie jest czasem UTC (…Z)")
    elif rev_at >= end:
        errors.append(f"{date}: rewizja gista z {doc['gist_revision_committed_at']} jest po końcu dnia")
    elif isinstance(doc["revision_from_earlier_day"], bool) and \
            doc["revision_from_earlier_day"] != (rev_at < end - timedelta(days=1)):
        errors.append(f"{date}: revision_from_earlier_day nie zgadza się z czasem rewizji")
    created = _ts(doc["created_at"])
    mode = doc["mode"]
    if mode not in GIST_MODES:
        errors.append(f"{date}: mode {mode!r} (dozwolone {GIST_MODES})")
    if created is None:
        errors.append(f"{date}: created_at nie jest czasem UTC (…Z)")
    elif created < end:
        errors.append(f"{date}: manifest utworzony przed końcem dnia ({doc['created_at']})")
    elif mode == "nightly" and created >= end + timedelta(days=1):
        errors.append(f"{date}: 'nightly' utworzony dopiero {doc['created_at']} — to jest backfill")
    if mode == "backfill" and not str(doc.get("backfill_note") or "").strip():
        errors.append(f"{date}: backfill bez backfill_note")
    if mode == "nightly" and "backfill_note" in doc:
        errors.append(f"{date}: nightly z backfill_note")
    digests = doc["sha256"]
    sizes = doc["bytes"]
    if not isinstance(digests, dict) or not digests:
        errors.append(f"{date}: sha256 to niepusty obiekt plik -> suma")
    else:
        for name in GIST_REQUIRED_FILES:
            if name not in digests:
                errors.append(f"{date}: sha256 nie obejmuje {name}")
        for name, digest in digests.items():
            if not re.fullmatch(r"[A-Za-z0-9._-]+", str(name)) or name.startswith("."):
                errors.append(f"{date}: zła nazwa pliku {name!r}")
            if not _HEX64_RE.match(str(digest)):
                errors.append(f"{date}: {name} suma nie jest sha256")
        if not isinstance(sizes, dict) or set(sizes) != set(digests) or \
                not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in sizes.values()):
            errors.append(f"{date}: bytes musi mieć te same pliki co sha256 (liczby >= 0)")
    pos = doc["positions"]
    if not isinstance(pos, dict) or set(pos) != POSITIONS_KEYS or \
            not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in pos.values()):
        errors.append(f"{date}: positions musi mieć dokładnie {sorted(POSITIONS_KEYS)} (liczby >= 0)")
    elif pos["open"] + pos["closed"] > pos["total"] or pos["excluded"] > pos["total"] or pos["max_id"] < pos["total"]:
        errors.append(f"{date}: liczby pozycji się nie sumują")
    code = doc["code"]
    if not isinstance(code, dict) or set(code) != CODE_KEYS_REQUIRED:
        errors.append(f"{date}: code musi mieć dokładnie {sorted(CODE_KEYS_REQUIRED)}")
    else:
        if not _SHA_RE.match(str(code["commit"])):
            errors.append(f"{date}: code.commit nie jest SHA commitu")
        if not code["strategy_version"] or not code["radar_version"]:
            errors.append(f"{date}: code bez wersji strategii albo radaru")
        if code["tvc_params_recorded"] is not False:
            errors.append(f"{date}: archiwum nie może zapisywać sekretu TVC_PARAMS")
        if not _HEX64_RE.match(str(code["frozen_config_sha256"])):
            errors.append(f"{date}: code.frozen_config_sha256 nie jest sha256")
        defaults = code["code_defaults"]
        if not isinstance(defaults, dict) or "values" not in defaults:
            errors.append(f"{date}: code.code_defaults bez values")
        else:
            values = defaults.get("values") or {}
            if defaults.get("strategy_version") != code["strategy_version"]:
                errors.append(f"{date}: strategy_version rozjechana z code_defaults")
            if "TVC_PARAMS" in values:
                errors.append(f"{date}: code_defaults zawiera TVC_PARAMS")
        if (code["fusion_file"] is None) != (code["fusion_sha256"] is None):
            errors.append(f"{date}: fusion_file i fusion_sha256 razem albo wcale")
        elif code["fusion_sha256"] is not None and not _HEX64_RE.match(str(code["fusion_sha256"])):
            errors.append(f"{date}: code.fusion_sha256 nie jest sha256")
    run = doc["workflow_run"]
    if not isinstance(run, dict) or set(run) != RUN_KEYS:
        errors.append(f"{date}: workflow_run musi mieć {sorted(RUN_KEYS)}")
    return errors


def _day_counts(day_dir: Path) -> tuple[int, int] | None:
    """(liczba pozycji, max id) dnia — z manifestu albo z zarchiwizowanej bazy."""
    man = day_dir / "MANIFEST.json"
    if man.is_file():
        try:
            pos = json.loads(man.read_text(encoding="utf-8"))["positions"]
            return int(pos["total"]), int(pos["max_id"])
        except Exception:
            return None
    db = day_dir / "paper_trades.db"
    if not db.is_file():
        return None
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            row = con.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM positions").fetchone()
            return int(row[0]), int(row[1])
        finally:
            con.close()
    except Exception:
        return None


def continuity_errors(daily: Path) -> list[str]:
    """Pozycji nie ubywa z dnia na dzień (dotyczy też przejścia repo -> gist)."""
    errors = []
    prev = None
    for child in sorted(daily.iterdir()):
        if child.name.startswith(".") or not child.is_dir() or not _DATE_RE.match(child.name):
            continue
        counts = _day_counts(child)
        if counts is None:
            continue
        if prev is not None and (counts[0] < prev[1][0] or counts[1] < prev[1][1]):
            errors.append(
                f"{child.name}: pozycji {counts[0]} (max id {counts[1]}) < {prev[0]} "
                f"({prev[1][0]}, max id {prev[1][1]}) — stan się cofnął"
            )
        prev = (child.name, counts)
    return errors


def _check_params(date: str, path: Path, has_fusion: bool) -> list[str]:
    errors = []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{date}: params.json nie jest JSON ({exc})"]
    if not isinstance(doc, dict):
        return [f"{date}: params.json nie jest obiektem"]
    if doc.get("archive_date") != date:
        errors.append(f"{date}: params.archive_date = {doc.get('archive_date')!r}")
    if doc.get("freeze_date") != FREEZE_DATE:
        errors.append(f"{date}: params.freeze_date != {FREEZE_DATE}")
    if not _SHA_RE.match(str(doc.get("state_commit", ""))):
        errors.append(f"{date}: params.state_commit nie jest SHA commitu")
    if not doc.get("strategy_version") or not doc.get("radar_version"):
        errors.append(f"{date}: params bez wersji strategii albo radaru")
    if doc.get("tvc_params_recorded") is not False:
        errors.append(f"{date}: archiwum nie może zapisywać sekretu TVC_PARAMS")
    if doc.get("fusion_included") is not has_fusion:
        errors.append(f"{date}: fusion_included nie zgadza się z plikiem fusion.json")
    defaults = doc.get("code_defaults")
    if not isinstance(defaults, dict) or "values" not in defaults:
        errors.append(f"{date}: params.code_defaults bez values")
    else:
        values = defaults.get("values") or {}
        if defaults.get("strategy_version") != doc.get("strategy_version"):
            errors.append(f"{date}: strategy_version rozjechana z code_defaults")
        if values.get("paper_bot.RULES_VERSION") not in (None, doc.get("strategy_version")):
            errors.append(f"{date}: RULES_VERSION w code_defaults != strategy_version")
        if "TVC_PARAMS" in values:
            errors.append(f"{date}: code_defaults zawiera TVC_PARAMS")
    return errors


def _history_errors(root: Path) -> list[str]:
    shallow = _git(root, "rev-parse", "--is-shallow-repository", check=False)
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        return [
            "płytki klon albo nieznany stan historii — nie da się dowieść, że stare "
            "snapshoty nie były ruszane; pobierz pełną historię (fetch-depth: 0)"
        ]
    proc = _git(
        root,
        "log",
        "--reverse",
        "--no-renames",
        "--name-status",
        "--format=C %H",
        "--",
        "archive/daily",
        check=False,
    )
    if proc.returncode != 0:
        return [f"git log archive/daily: {proc.stderr.strip()}"]
    errors = []
    commit = None
    born: dict[str, str] = {}
    for raw in proc.stdout.splitlines():
        if raw.startswith("C "):
            commit = raw[2:].strip()
            continue
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) < 2 or commit is None:
            errors.append(f"nieczytelny wpis historii: {raw!r}")
            continue
        status, path = parts[0], parts[-1]
        match = _PATH_RE.match(path)
        if not match:
            errors.append(f"{path}: ścieżka poza archive/daily/YYYY-MM-DD/ ({status} {commit[:8]})")
            continue
        date, name = match.group(1), match.group(2)
        if name not in ALLOWED:
            errors.append(f"{path}: niedozwolona nazwa ({status} {commit[:8]})")
        if status != "A":
            errors.append(
                f"{path}: status {status} w {commit[:8]} — "
                "zamknięty snapshot nie może być zmieniony ani skasowany"
            )
            continue
        first = born.get(date)
        if first is None:
            born[date] = commit
        elif first != commit:
            errors.append(
                f"{path}: dodane w {commit[:8]}, a dzień {date} zamknięto w {first[:8]}"
            )
    return errors


def check(root: Path = ROOT) -> list[str]:
    daily = root / "archive" / "daily"
    errors = []
    if daily.exists():
        for child in sorted(daily.iterdir()):
            if child.name.startswith("."):
                continue
            if not child.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", child.name):
                errors.append(f"archive/daily/{child.name}: dozwolone są tylko katalogi YYYY-MM-DD")
                continue
            errors.extend(verify_day_dir(child))
        errors.extend(continuity_errors(daily))
    status = _git(root, "status", "--porcelain", "--untracked-files=all", "--", "archive/daily", check=False)
    if status.returncode != 0:
        errors.append(f"git status: {status.stderr.strip()}")
    else:
        for line in status.stdout.splitlines():
            if line.startswith("??"):
                continue
            errors.append(f"niezacommitowana zmiana archiwum: {line}")
    errors.extend(_history_errors(root))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sprawdź nienaruszalność archive/daily")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    errors = check(args.root.resolve())
    if errors:
        print("Archiwum dzienne:")
        for item in errors:
            print(f"   ❌ {item}")
        return 1
    daily = args.root.resolve() / "archive" / "daily"
    count = 0
    if daily.is_dir():
        count = sum(1 for child in daily.iterdir() if child.is_dir() and not child.name.startswith("."))
    print(f"   ✅ archiwum dzienne nienaruszone ({count} dni)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
