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
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import holdout_params

ROOT = Path(__file__).resolve().parent
DAILY = ROOT / "archive" / "daily"
MODULES = ("tvc_params.py", "paper_bot.py", "auto_fusion.py", "pump_radar.py")


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


def run(root: Path, dry_run: bool, only: date | None, today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
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
    args = parser.parse_args(argv)
    only = _parse_day(args.date) if args.date else None
    return run(args.root.resolve(), args.dry_run, only)


if __name__ == "__main__":
    sys.exit(main())
