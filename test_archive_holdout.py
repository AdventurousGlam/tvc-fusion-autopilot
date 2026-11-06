#!/usr/bin/env python3
"""Archiwum dzienne jest dopisywane, a podmiana sumy albo pliku wywala strażnika.

Holdout: kod zgadza się z holdout/FROZEN.json, a zmiana progu bez wpisu
w unfreeze.json nie przechodzi. Nie dotyka paper_trades.db w tym repo.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import archive_daily
import archive_guard
import holdout_guard
import holdout_params

ROOT = Path(__file__).resolve().parent
FAILS: list[str] = []


def fail(msg: str):
    FAILS.append(msg)
    print(f"  ✗ {msg}")


def ok(msg: str):
    print(f"  ✓ {msg}")


def git_repo(path: Path):
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    for key, value in (
        ("user.email", "archive-test@example.com"),
        ("user.name", "archive-test"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(["git", "config", key, value], cwd=path, check=True)


def commit(path: Path, message: str, when: str = "2026-11-05T12:00:00Z"):
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = when
    env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-m", message], cwd=path, check=True, capture_output=True, env=env)


def day_files(day: str, db: bytes = b"db-v1", fusion: bytes | None = b'{"ok":true}\n'):
    params = {
        "archive_date": day,
        "captured_at": "2026-11-06T00:20:00Z",
        "state_commit": "a" * 40,
        "strategy_version": "v4.4",
        "radar_version": "v2.4",
        "freeze_date": "2026-11-05",
        "fusion_included": fusion is not None,
        "fusion_source": f"fusion_{day}.json" if fusion is not None else None,
        "frozen_config_from": "worktree",
        "extractor_from": "worktree",
        "tvc_params_recorded": False,
        "code_defaults": {
            "strategy_version": "v4.4",
            "radar_version": "v2.4",
            "values": {"paper_bot.RULES_VERSION": "v4.4"},
        },
    }
    frozen = {"freeze_date": "2026-11-05"}
    files = {
        "paper_trades.db": db,
        "params.json": (json.dumps(params, indent=2, sort_keys=True) + "\n").encode(),
        "frozen_config.json": (json.dumps(frozen, indent=2, sort_keys=True) + "\n").encode(),
    }
    if fusion is not None:
        files["fusion.json"] = fusion
    header = ["# test", f"# date: {day}", "# alg: sha256"]
    return files, header


def seal(repo: Path, day: str, **kwargs):
    files, header = day_files(day, **kwargs)
    archive_daily.write_day(repo / "archive" / "daily" / day, files, header)
    return files


def test_missing_days():
    assert archive_daily.missing_days(date(2026, 11, 5), set()) == []
    assert archive_daily.missing_days(date(2026, 11, 6), set()) == [date(2026, 11, 5)]
    got = archive_daily.missing_days(date(2026, 11, 8), {"2026-11-05"})
    assert got == [date(2026, 11, 6), date(2026, 11, 7)], got
    try:
        archive_daily.missing_days(date(2026, 11, 6), set(), only=date(2026, 11, 4))
    except RuntimeError:
        ok("dni do archiwum liczone od 2026-11-05, wcześniejszy odrzucony")
        return
    fail("dzień sprzed zamrożenia został przyjęty")


def test_manifest_and_refuse_overwrite():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        files = seal(repo, "2026-11-05")
        problems = archive_guard.verify_day_dir(repo / "archive" / "daily" / "2026-11-05")
        if problems:
            fail("świeży dzień nie przeszedł manifestu: " + "; ".join(problems))
            return
        try:
            archive_daily.write_day(repo / "archive" / "daily" / "2026-11-05", files, ["# x"])
        except FileExistsError:
            ok("istniejący dzień nie jest nadpisywany, manifest się zgadza")
            return
        fail("drugi zapis dnia się udał")


def test_history_tamper_detected():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        git_repo(repo)
        files = seal(repo, "2026-11-05")
        commit(repo, "seal 11-05")
        if archive_guard.check(repo):
            fail("czysty snapshot nie przeszedł: " + "; ".join(archive_guard.check(repo)))
            return
        db = repo / "archive" / "daily" / "2026-11-05" / "paper_trades.db"
        db.write_bytes(b"tampered")
        dirty = archive_guard.check(repo)
        if not dirty:
            fail("podmiana bajtów w drzewie nie wywaliła strażnika")
            return
        # Suma przepisana pod nową treść — historia i tak ma widzieć zmianę.
        files["paper_trades.db"] = b"tampered"
        manifest = archive_daily.render_manifest(files, ["# test", "# rewritten"])
        (repo / "archive" / "daily" / "2026-11-05" / "MANIFEST.sha256").write_text(manifest, encoding="utf-8")
        commit(repo, "rewrite snapshot", when="2026-11-06T01:00:00Z")
        rewritten = archive_guard.check(repo)
        if not any("status M" in item or "zmieniony" in item for item in rewritten):
            fail("przepisany manifest ukrył zmianę pliku: " + "; ".join(rewritten))
            return
        ok("podmiana pliku i przepisana suma są wykrywane")


def test_delete_and_late_add():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        git_repo(repo)
        seal(repo, "2026-11-05", fusion=None)
        commit(repo, "seal without fusion")
        day = repo / "archive" / "daily" / "2026-11-05"
        (day / "fusion.json").write_bytes(b'{"late":true}\n')
        # dopisz plik do już zamkniętego dnia i popraw manifest, jak przy podmianie
        files = {}
        for child in day.iterdir():
            if child.name != "MANIFEST.sha256":
                files[child.name] = child.read_bytes()
        (day / "MANIFEST.sha256").write_text(
            archive_daily.render_manifest(files, ["# late"]), encoding="utf-8"
        )
        # params mówi fusion_included false — strażnik struktury też ma paść,
        # ale nawet po poprawce params historia nie puszcza późniejszego dodania.
        params = json.loads((day / "params.json").read_text(encoding="utf-8"))
        params["fusion_included"] = True
        params["fusion_source"] = "fusion_2026-11-05.json"
        files["params.json"] = (json.dumps(params, indent=2, sort_keys=True) + "\n").encode()
        (day / "params.json").write_bytes(files["params.json"])
        (day / "MANIFEST.sha256").write_text(
            archive_daily.render_manifest(files, ["# late"]), encoding="utf-8"
        )
        commit(repo, "sneak fusion in", when="2026-11-06T02:00:00Z")
        late = archive_guard.check(repo)
        if not late:
            fail("dopisanie pliku do zamkniętego dnia przeszło")
            return
        ok("dopisanie pliku do zamkniętego dnia jest odrzucane")

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        git_repo(repo)
        seal(repo, "2026-11-05")
        commit(repo, "day")
        day = repo / "archive" / "daily" / "2026-11-05"
        (day / "fusion.json").unlink()
        commit(repo, "drop fusion", when="2026-11-06T03:00:00Z")
        deleted = archive_guard.check(repo)
        if not any("status D" in item or "skasow" in item for item in deleted):
            fail("skasowanie pliku archiwum nie wywaliło historii: " + "; ".join(deleted))
            return
        ok("skasowanie pliku archiwum jest odrzucane")


def test_second_day_and_shallow():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "origin"
        repo.mkdir()
        git_repo(repo)
        seal(repo, "2026-11-05")
        commit(repo, "day 1", when="2026-11-05T23:00:00Z")
        seal(repo, "2026-11-06")
        commit(repo, "day 2", when="2026-11-06T23:00:00Z")
        clean = archive_guard.check(repo)
        if clean:
            fail("dwa kolejne dni nie przeszły: " + "; ".join(clean))
            return
        shallow = Path(tmp) / "shallow"
        subprocess.run(
            ["git", "clone", "--no-local", "--depth", "1", str(repo), str(shallow)],
            check=True, capture_output=True,
        )
        shallow_err = archive_guard.check(shallow)
        if not any("płytki" in item for item in shallow_err):
            fail("płytki klon przeszedł strażnika archiwum")
            return
        ok("kolejny dzień wolno dopisać; płytki klon nie wystarcza")


def test_pipeline_writes_real_snapshot():
    """Małe repo z prawdziwym kodem strategii: snapshot dnia da się zbudować."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        git_repo(repo)
        for name in ("tvc_params.py", "paper_bot.py", "auto_fusion.py", "pump_radar.py", "holdout_params.py"):
            shutil.copy(ROOT / name, repo / name)
        holdout = repo / "holdout"
        holdout.mkdir()
        shutil.copy(ROOT / "holdout" / "FROZEN.json", holdout / "FROZEN.json")
        shutil.copy(ROOT / "holdout" / "unfreeze.json", holdout / "unfreeze.json")
        (repo / "paper_trades.db").write_bytes(b"fixture-register")
        (repo / "fusion_2026-11-05.json").write_bytes(b'{"date":"2026-11-05"}\n')
        commit(repo, "state", when="2026-11-05T22:00:00Z")
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            code = archive_daily.run(repo, dry_run=False, only=None, today=date(2026, 11, 6))
        if code != 0:
            fail(f"archive_daily zakończył się kodem {code}\n{log.getvalue()}")
            return
        day = repo / "archive" / "daily" / "2026-11-05"
        if not (day / "MANIFEST.sha256").is_file():
            fail("pipeline nie zapisał manifestu")
            return
        if (day / "paper_trades.db").read_bytes() != b"fixture-register":
            fail("snapshot rejestru nie jest bajtami z commitu dnia")
            return
        params = json.loads((day / "params.json").read_text(encoding="utf-8"))
        if params.get("tvc_params_recorded") is not False:
            fail("snapshot zapisał sekret")
            return
        if params["code_defaults"]["values"]["pump_radar.SCORE_WATCH"] != 60:
            fail("snapshot nie ma produkcyjnego WATCH 60")
            return
        if params["code_defaults"]["values"]["pump_radar.SCORE_HIGH"] != 70:
            fail("snapshot nie ma produkcyjnego HIGH 70")
            return
        if params["strategy_version"] != "v4.4" or params["radar_version"] != "v2.4":
            fail(f"wersje snapshotu: {params['strategy_version']} / {params['radar_version']}")
            return
        # Niezacommitowany nowy dzień jest OK (tak leci workflow przed git commit).
        pending = archive_guard.check(repo)
        if pending:
            fail("świeży niezacommitowany dzień nie przeszedł: " + "; ".join(pending))
            return
        commit(repo, "archive 2026-11-05", when="2026-11-06T00:20:00Z")
        sealed = archive_guard.check(repo)
        if sealed:
            fail("zacommitowany dzień nie przeszedł: " + "; ".join(sealed))
            return
        with contextlib.redirect_stdout(io.StringIO()):
            again = archive_daily.run(repo, dry_run=False, only=None, today=date(2026, 11, 6))
        if again != 0:
            fail("drugi bieg archiwum nie był no-opem")
            return
        ok("pipeline zapisuje dzień v4.4 / WATCH 60 / HIGH 70 i drugi raz go nie rusza")


def test_holdout_matches_code_and_unfreeze_rules():
    extracted = holdout_params.extract()
    doc = json.loads((ROOT / "holdout" / "FROZEN.json").read_text(encoding="utf-8"))
    live = holdout_guard.live_errors(doc, extracted)
    if live:
        fail("FROZEN.json != kod: " + "; ".join(live[:8]))
        return
    if extracted["values"]["pump_radar.SCORE_WATCH"] != 60 or extracted["values"]["pump_radar.SCORE_HIGH"] != 70:
        fail("produkcyjne progi radaru to nie 60/70")
        return
    if extracted["values"]["paper_bot.FEE_PCT"] != 0.05 or extracted["values"]["paper_bot.SLIPPAGE_PCT"] != 0.02:
        fail("domyślne koszty w kodzie zmienione")
        return
    if extracted["test_track"]["pump_radar.TEST_WATCH"] != 45:
        fail("ścieżka testowa WATCH 45 zniknęła z odczytu")
        return
    if "pump_radar.TEST_WATCH" in extracted["values"]:
        fail("TEST_WATCH wszedł do zamrożonych values")
        return
    tweaked = json.loads(json.dumps(doc))
    tweaked["values"]["paper_bot.MIN_LONG_SCORE"] = 99
    if not holdout_guard.live_errors(tweaked, extracted):
        fail("podniesiony próg nie wywalił porównania z kodem")
        return
    base_flat = holdout_guard.flat_view(doc)
    head_flat = holdout_guard.flat_view(tweaked)
    changed = {
        key: True
        for key in set(base_flat) | set(head_flat)
        if base_flat.get(key) != head_flat.get(key)
    }
    bare = holdout_guard._validate_new_entries([], changed, base_flat, head_flat)
    if not bare:
        fail("zmiana progu bez wpisu unfreeze przeszła")
        return
    entry = {
        "id": "2026-12-01-score",
        "date": "2026-12-01",
        "reason": "Przykład w teście: próg long zmieniony po zamknięciu oceny holdoutu, z zapisem old i new.",
        "keys": ["values.paper_bot.MIN_LONG_SCORE"],
        "changes": {
            "values.paper_bot.MIN_LONG_SCORE": {
                "old": base_flat["values.paper_bot.MIN_LONG_SCORE"],
                "new": head_flat["values.paper_bot.MIN_LONG_SCORE"],
            }
        },
    }
    covered = holdout_guard._validate_new_entries([entry], changed, base_flat, head_flat)
    if covered:
        fail("poprawny wpis unfreeze odrzucony: " + "; ".join(covered))
        return
    short = dict(entry, reason="za krótko")
    if not holdout_guard._validate_new_entries([short], changed, base_flat, head_flat):
        fail("krótkie uzasadnienie przeszło")
        return
    moved = json.loads(json.dumps(doc))
    moved["freeze_date"] = "2026-12-01"
    if not any("freeze_date" in item for item in holdout_guard.live_errors(moved, extracted)):
        fail("zmiana freeze_date przeszła")
        return
    ok("holdout v4.4 WATCH 60/HIGH 70 zgodny z kodem; unfreeze wymaga powodu i old/new")


def test_real_repo_guards_and_dry_run():
    if holdout_guard.main([]) != 0:
        fail("holdout_guard na tym repo nie przeszedł")
        return
    if archive_guard.main([]) != 0:
        fail("archive_guard na tym repo nie przeszedł")
        return
    proc = subprocess.run(
        [sys.executable, str(ROOT / "archive_daily.py"), "--dry-run", "--date", "2026-11-05"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        fail("dry-run 2026-11-05:\n" + proc.stdout + proc.stderr)
        return
    sealed = ROOT / "archive" / "daily" / "2026-11-05"
    if sealed.exists():
        fail("dry-run zapisał archive/daily/2026-11-05")
        return
    text = proc.stdout
    if "2026-11-05" not in text:
        fail("dry-run nie wspomina daty")
        return
    if "Już zamknięte" not in text and "paper_trades.db" not in text:
        fail("dry-run nie pokazał plików ani istniejącego zamknięcia:\n" + text)
        return
    ok("strażnicy tego repo przechodzą; dry-run 2026-11-05 nic nie zapisuje")


def main():
    test_missing_days()
    test_manifest_and_refuse_overwrite()
    test_history_tamper_detected()
    test_delete_and_late_add()
    test_second_day_and_shallow()
    test_holdout_matches_code_and_unfreeze_rules()
    test_pipeline_writes_real_snapshot()
    test_real_repo_guards_and_dry_run()
    print()
    if FAILS:
        print(f"✗ nie przeszło: {len(FAILS)}")
        return 1
    print("✓ archiwum i holdout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
