#!/usr/bin/env python3
"""Nienaruszalność archive/daily.

Dwa niezależne sprawdzenia:

1. MANIFEST.sha256 zgadza się z bajtami plików w katalogu dnia (podmiana treści
   bez poprawienia sumy odpada od razu).
2. W historii gita każdy plik dnia pojawia się tylko jako dodanie, wszystkie
   w tym samym commicie. Późniejsza zmiana, skasowanie albo dosypanie pliku
   do zamkniętego dnia wywala sprawdzenie — także wtedy, gdy manifest został
   przepisany tak, żeby suma znowu się zgadzała.

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
