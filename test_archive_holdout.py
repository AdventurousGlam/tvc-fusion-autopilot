#!/usr/bin/env python3
"""Archiwum dzienne jest dopisywane, a podmiana sumy albo pliku wywala strażnika.

Holdout: kod zgadza się z holdout/FROZEN.json, a zmiana progu bez wpisu
w unfreeze.json nie przechodzi. Nie dotyka paper_trades.db w tym repo.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
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


def _tree_digest(base: Path) -> dict[str, str]:
    """Ścieżka -> sha256 każdego pliku (także ukrytego, np. .tmp-*) pod base."""
    if not base.exists():
        return {}
    return {
        str(f.relative_to(base)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(base.rglob("*")) if f.is_file()
    } | {str(d.relative_to(base)) + "/": "dir" for d in base.rglob("*") if d.is_dir()}


def test_no_archive_from_repo_after_state_move():
    """Od 18.11.2026 baza bota jest w gist; repo ma zamrożoną kopię z 13:08 UTC.
    Dzień 18.11+ z historii repo byłby fałszywym snapshotem — ma być odmowa."""
    # Po wdrożeniu wariantu A dni od 18.11 pojawiają się w archiwum (z gista), więc
    # bierzemy pierwszy dzień od 18.11, którego jeszcze nie ma — inaczej run() kończy
    # się „Już zamknięte” i test robi się nieaktualny jak stary test dry-run.
    daily = ROOT / "archive" / "daily"
    probe = archive_daily.GIST_STATE_FIRST_DAY
    while (daily / probe.isoformat()).exists():
        probe += timedelta(days=1)
    before = _tree_digest(daily)
    for dry in (True, False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = archive_daily.run(ROOT, dry_run=dry, only=probe, today=probe + timedelta(days=2))
        if code == 0 or "stan bota jest w gist" not in out.getvalue():
            fail(f"archive_daily przyjął {probe.isoformat()} z repo (dry_run={dry})")
            return
    if _tree_digest(daily) != before:
        fail(f"odmowa {probe.isoformat()} mimo to coś zapisała")
        return
    if (daily / probe.isoformat()).exists():
        fail(f"powstał archive/daily/{probe.isoformat()}")
        return
    ok("dni od 2026-11-18 nie są archiwizowane z bazy w repo (stan jest w gist)")


def test_real_repo_guards_and_dry_run():
    if holdout_guard.main([]) != 0:
        fail("holdout_guard na tym repo nie przeszedł")
        return
    if archive_guard.main([]) != 0:
        fail("archive_guard na tym repo nie przeszedł")
        return
    # 2026-11-05 jest w repo od 07.11 (pierwszy nocny bieg). Sprawdzamy więc, że
    # dry-run nic w archive/daily nie zmienia, a nie że katalogu nie ma — stary
    # warunek był prawdziwy tylko do pierwszego zamknięcia dnia.
    before = _tree_digest(ROOT / "archive" / "daily")
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
    if _tree_digest(ROOT / "archive" / "daily") != before:
        fail("dry-run zmienił archive/daily")
        return
    text = proc.stdout
    if "2026-11-05" not in text:
        fail("dry-run nie wspomina daty")
        return
    if "Już zamknięte" not in text and "paper_trades.db" not in text:
        fail("dry-run nie pokazał plików ani istniejącego zamknięcia:\n" + text)
        return
    ok("strażnicy tego repo przechodzą; dry-run 2026-11-05 nic nie zapisuje")


# ------------------------------------------------------------ wariant A (gist)

def _mk_db(path: Path, n_closed: int, n_open: int = 0, n_excl: int = 0) -> bytes:
    import sqlite3
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, "
                "status TEXT, excluded INTEGER DEFAULT 0)")
    for i in range(n_closed + n_open):
        con.execute("INSERT INTO positions (ticker, status, excluded) VALUES (?,?,?)",
                    ("T", "closed" if i < n_closed else "open", 1 if i < n_excl else 0))
    con.commit()
    con.close()
    return path.read_bytes()


def _gist_commit(work: Path, when: str, n_closed: int, n_open: int = 0, bootstrap=False,
                 corrupt=False):
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = when
    env["GIT_COMMITTER_DATE"] = when
    if bootstrap:
        (work / "BOOTSTRAP_ALLOWED").write_text("x\n")
    else:
        (work / "BOOTSTRAP_ALLOWED").unlink(missing_ok=True)
        db = _mk_db(work / "paper_trades.db", n_closed, n_open, n_excl=1)
        (work / "results.json").write_text('{"n": %d}\n' % n_closed)
        files = {n: hashlib.sha256((work / n).read_bytes()).hexdigest()
                 for n in ("paper_trades.db", "results.json")}
        if corrupt:
            files["results.json"] = "0" * 64
        (work / "STATE_VERSION.json").write_text(json.dumps(
            {"schema": "tvc.state/1", "files": files, "positions": n_closed + n_open,
             "written_at": when, "run_number": "1", "run_id": "1"}, sort_keys=True) + "\n")
        (work / "last_cycle.log").write_text(f"log {when}\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", f"state {when}"], cwd=work, check=True, env=env)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()


def _fake_gist(base: Path, commits: list[tuple]) -> tuple[Path, list[str]]:
    work = base / "gist_work"
    work.mkdir()
    git_repo(work)
    shas = [_gist_commit(work, *c) for c in commits]
    return work, shas


def _code_repo(base: Path) -> Path:
    repo = base / "code"
    repo.mkdir()
    git_repo(repo)
    for name in ("tvc_params.py", "paper_bot.py", "auto_fusion.py", "pump_radar.py", "holdout_params.py"):
        shutil.copy(ROOT / name, repo / name)
    (repo / "holdout").mkdir()
    shutil.copy(ROOT / "holdout" / "FROZEN.json", repo / "holdout" / "FROZEN.json")
    shutil.copy(ROOT / "holdout" / "unfreeze.json", repo / "holdout" / "unfreeze.json")
    (repo / "fusion_2026-11-18.json").write_bytes(b'{"date":"2026-11-18"}\n')
    commit(repo, "code", when="2026-11-17T12:00:00Z")
    for n in range(5, 18):          # stare dni (pełne bazy) już są, jak w prawdziwym repo
        seal(repo, f"2026-11-{n:02d}")
    commit(repo, "old days", when="2026-11-18T00:20:00Z")
    return repo


def _run_quiet(*args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = archive_daily.run(*args, **kwargs)
    return code, out.getvalue()


def test_gist_pipeline_and_backfill():
    """Lokalny 'gist' (git) z wersjami w różnych dniach: wybór rewizji sprzed północy,
    nightly/backfill, idempotencja, weryfikacja odcisku i przepisana historia gista."""
    old_now = os.environ.get("TVC_ARCHIVE_FAKE_NOW")
    # sztuczny zegar działa tylko poza Actions (bezpiecznik w archive_daily._now);
    # na czas testu zdejmujemy GITHUB_ACTIONS i przywracamy w finally
    old_gha = os.environ.pop("GITHUB_ACTIONS", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _code_repo(base)
            gist, shas = _fake_gist(base, [
                ("2026-11-18T13:04:00Z", 0, 0, True),      # bootstrap, bez STATE_VERSION
                ("2026-11-18T13:17:00Z", 110),
                ("2026-11-18T23:59:59Z", 110, 1),          # ostatnia przed północą 18.11
                ("2026-11-19T00:00:00Z", 111),             # równo północ = już 19.11
                ("2026-11-20T09:00:00Z", 112),
            ])
            # 21.11 bez commitu -> rewizja z 20.11, revision_from_earlier_day
            os.environ["TVC_ARCHIVE_FAKE_NOW"] = "2026-11-22T00:20:00Z"
            code, out = _run_quiet(repo, dry_run=True, only=None, today=date(2026, 11, 22),
                                   source="gist", gist_dir=gist)
            if code != 0 or (repo / "archive" / "daily" / "2026-11-18").exists():
                fail("dry-run wariantu A: kod %s albo coś zapisał\n%s" % (code, out))
                return
            code, out = _run_quiet(repo, dry_run=False, only=None, today=date(2026, 11, 22),
                                   source="gist", gist_dir=gist)
            if code != 0:
                fail("zapis wariantu A: kod %s\n%s" % (code, out))
                return
            daily = repo / "archive" / "daily"
            docs = {d: json.loads((daily / d / "MANIFEST.json").read_text()) for d in
                    ("2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21")}
            want = {"2026-11-18": shas[2], "2026-11-19": shas[3], "2026-11-20": shas[4], "2026-11-21": shas[4]}
            for d, sha in want.items():
                if docs[d]["gist_revision"] != sha:
                    fail(f"{d}: rewizja {docs[d]['gist_revision'][:8]} != {sha[:8]}")
                    return
            modes = {d: docs[d]["mode"] for d in docs}
            if modes != {"2026-11-18": "backfill", "2026-11-19": "backfill",
                         "2026-11-20": "backfill", "2026-11-21": "nightly"}:
                fail(f"tryby dni: {modes}")
                return
            if not docs["2026-11-21"]["revision_from_earlier_day"] or docs["2026-11-20"]["revision_from_earlier_day"]:
                fail("revision_from_earlier_day źle policzone")
                return
            if docs["2026-11-18"]["positions"] != {"total": 111, "open": 1, "closed": 110, "excluded": 1, "max_id": 111}:
                fail(f"liczby pozycji 18.11: {docs['2026-11-18']['positions']}")
                return
            if "last_cycle.log" in docs["2026-11-18"]["sha256"]:
                fail("log cyklu trafił do odcisku")
                return
            for d in docs:
                if os.listdir(daily / d) != ["MANIFEST.json"]:
                    fail(f"{d}: w katalogu dnia jest coś poza MANIFEST.json")
                    return
            if archive_guard.check(repo):
                fail("świeże dni wariantu A nie przeszły strażnika: " + "; ".join(archive_guard.check(repo)))
                return
            commit(repo, "archive A", when="2026-11-22T00:21:00Z")
            if archive_guard.check(repo):
                fail("zacommitowane dni wariantu A nie przeszły: " + "; ".join(archive_guard.check(repo)))
                return
            before = _tree_digest(daily)
            code, out = _run_quiet(repo, dry_run=False, only=None, today=date(2026, 11, 22),
                                   source="gist", gist_dir=gist, backfill=True)
            if code != 0 or _tree_digest(daily) != before:
                fail("drugi przebieg wariantu A coś zmienił")
                return
            for d in docs:
                problems = archive_daily.verify_gist_day(repo, gist, d)
                if problems:
                    fail(f"weryfikacja {d} z gistem: {problems}")
                    return
            # właściciel przepisuje historię gista (force-push innej treści)
            env = os.environ.copy()
            subprocess.run(["git", "reset", "-q", "--hard", shas[1]], cwd=gist, check=True)
            _gist_commit(gist, "2026-11-18T23:59:59Z", 109)
            subprocess.run(["git", "reflog", "expire", "--expire=now", "--all"], cwd=gist, check=True)
            subprocess.run(["git", "gc", "-q", "--prune=now"], cwd=gist, check=True, env=env)
            rewritten = archive_daily.verify_gist_day(repo, gist, "2026-11-18")
            if not rewritten:
                fail("przepisana historia gista przeszła weryfikację odcisku")
                return
        ok("wariant A: rewizja sprzed północy, nightly/backfill, idempotencja, odcisk weryfikowalny, przepisany gist wykryty")
    finally:
        if old_now is None:
            os.environ.pop("TVC_ARCHIVE_FAKE_NOW", None)
        else:
            os.environ["TVC_ARCHIVE_FAKE_NOW"] = old_now
        if old_gha is not None:
            os.environ["GITHUB_ACTIONS"] = old_gha


def test_gist_edge_cases():
    old_now = os.environ.get("TVC_ARCHIVE_FAKE_NOW")
    # sztuczny zegar działa tylko poza Actions (bezpiecznik w archive_daily._now);
    # na czas testu zdejmujemy GITHUB_ACTIONS i przywracamy w finally
    old_gha = os.environ.pop("GITHUB_ACTIONS", None)
    try:
        os.environ["TVC_ARCHIVE_FAKE_NOW"] = "2026-11-20T00:20:00Z"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _code_repo(base)
            gist, _ = _fake_gist(base, [("2026-11-19T10:00:00Z", 5)])
            code, out = _run_quiet(repo, dry_run=False, only=None, today=date(2026, 11, 20),
                                   source="gist", gist_dir=gist)
            daily = repo / "archive" / "daily"
            if code != 0 or (daily / "2026-11-18").exists() or not (daily / "2026-11-19").is_dir():
                fail("dzień bez rewizji gista: kod %s\n%s" % (code, out))
                return
            if "brak rewizji gista" not in out:
                fail("dzień bez rewizji gista pominięty bez notatki")
                return
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _code_repo(base)
            gist, _ = _fake_gist(base, [("2026-11-18T10:00:00Z", 5, 0, False, True)])
            code, out = _run_quiet(repo, dry_run=False, only=None, today=date(2026, 11, 19),
                                   source="gist", gist_dir=gist)
            if code == 0 or (repo / "archive" / "daily" / "2026-11-18").exists():
                fail("rewizja z niezgodnym STATE_VERSION.json została zaarchiwizowana")
                return
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _code_repo(base)
            gist, _ = _fake_gist(base, [("2026-11-18T10:00:00Z", 5)])
            shallow = base / "shallow"
            subprocess.run(["git", "clone", "-q", "--no-local", "--depth", "1", str(gist), str(shallow)],
                           check=True, capture_output=True)
            code, out = _run_quiet(repo, dry_run=True, only=None, today=date(2026, 11, 19),
                                   source="gist", gist_dir=shallow)
            if code == 0 or "płytki" not in out:
                fail("płytki klon gista przyjęty")
                return
            # klonowanie przez URL (jak w CI), partial clone na lokalnym bare
            os.environ["TVC_ARCHIVE_FAKE_NOW"] = "2026-11-19T00:20:00Z"
            bare = base / "bare.git"
            subprocess.run(["git", "clone", "-q", "--bare", str(gist), str(bare)], check=True)
            subprocess.run(["git", "config", "uploadpack.allowFilter", "true"], cwd=bare, check=True)
            os.environ["TVC_STATE_GIST_URL"] = bare.as_uri()
            try:
                code, out = _run_quiet(repo, dry_run=False, only=None, today=date(2026, 11, 19),
                                       source="gist", gist_dir=None)
            finally:
                os.environ.pop("TVC_STATE_GIST_URL", None)
            if code != 0 or not (repo / "archive" / "daily" / "2026-11-18" / "MANIFEST.json").is_file():
                fail("klon gista po URL nie zadziałał: %s" % out)
                return
            if str(bare) in out:
                fail("adres gista trafił do logu")
                return
        ok("wariant A: dzień bez rewizji pominięty z notatką, zły STATE_VERSION odrzucony, płytki klon odrzucony, klon po URL bez adresu w logu")
    finally:
        if old_now is None:
            os.environ.pop("TVC_ARCHIVE_FAKE_NOW", None)
        else:
            os.environ["TVC_ARCHIVE_FAKE_NOW"] = old_now
        if old_gha is not None:
            os.environ["GITHUB_ACTIONS"] = old_gha


def _gist_doc(day: str = "2026-11-18", **over) -> dict:
    doc = {
        "schema": "tvc.archive.day/2", "date": day, "mode": "nightly",
        "freeze_date": "2026-11-05", "source": "gist", "selection_rule": "ostatni commit gista",
        "gist_revision": "b" * 40, "gist_revision_committed_at": f"{day}T23:55:00Z",
        "revision_from_earlier_day": False, "state_written_at": f"{day}T23:55:00+00:00",
        "state_run_number": "1",
        "sha256": {"paper_trades.db": "c" * 64, "STATE_VERSION.json": "d" * 64},
        "bytes": {"paper_trades.db": 10, "STATE_VERSION.json": 5},
        "positions": {"total": 110, "open": 0, "closed": 110, "excluded": 53, "max_id": 110},
        "code": {"commit": "e" * 40, "strategy_version": "v4.4", "radar_version": "v2.4",
                 "frozen_config_sha256": "f" * 64, "frozen_config_from": "state_commit",
                 "extractor_from": "state_commit",
                 "code_defaults": {"strategy_version": "v4.4", "radar_version": "v2.4", "values": {}},
                 "radar_test_track": {}, "tvc_params_recorded": False,
                 "fusion_file": None, "fusion_sha256": None},
        "created_at": "2026-11-19T00:21:00Z",
        "workflow_run": {"id": "1", "number": "1", "repository": "x/y", "event": "schedule"},
        "results_included": False, "notes": [],
    }
    doc.update(over)
    return doc


def _put_gist_day(repo: Path, doc: dict):
    day = repo / "archive" / "daily" / doc["date"]
    day.mkdir(parents=True, exist_ok=True)
    (day / "MANIFEST.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return day


def test_gist_manifest_guard():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        git_repo(repo)
        seal(repo, "2026-11-17")
        commit(repo, "old day", when="2026-11-18T00:20:00Z")
        _put_gist_day(repo, _gist_doc())
        commit(repo, "day A", when="2026-11-19T00:21:00Z")
        clean = archive_guard.check(repo)
        if clean:
            fail("poprawny dzień A nie przeszedł: " + "; ".join(clean))
            return
        bad_cases = {
            "pnl w manifeście": _gist_doc(positions={"total": 1, "open": 0, "closed": 1, "excluded": 0, "max_id": 1}, total_pnl_usd=12.5),
            "equity w notatce": _gist_doc(notes=["equity 10234"]),
            "results_included": _gist_doc(results_included=True),
            "nightly za późno": _gist_doc(created_at="2026-11-21T08:00:00Z"),
            "rewizja po końcu dnia": _gist_doc(gist_revision_committed_at="2026-11-19T00:00:00Z"),
            "manifest przed końcem dnia": _gist_doc(created_at="2026-11-18T22:00:00Z"),
            "backfill bez notatki": _gist_doc(mode="backfill"),
            "brak sumy bazy": _gist_doc(sha256={"STATE_VERSION.json": "d" * 64}, bytes={"STATE_VERSION.json": 5}),
            "zły tryb": _gist_doc(mode="later"),
        }
        for label, doc in bad_cases.items():
            with tempfile.TemporaryDirectory() as t2:
                day = _put_gist_day(Path(t2), doc)
                if not archive_guard.verify_day_dir(day):
                    fail(f"manifest przeszedł mimo: {label}")
                    return
        with tempfile.TemporaryDirectory() as t2:
            day = _put_gist_day(Path(t2), _gist_doc())
            (day / "paper_trades.db").write_bytes(b"full db")
            if not archive_guard.verify_day_dir(day):
                fail("dzień A z dorzuconą bazą przeszedł")
                return
        with tempfile.TemporaryDirectory() as t2:
            day = _put_gist_day(Path(t2), _gist_doc(day="2026-11-17"))
            if not archive_guard.verify_day_dir(day):
                fail("sam odcisk dla dnia sprzed 18.11 przeszedł")
                return
        with tempfile.TemporaryDirectory() as t2:
            files, header = day_files("2026-11-18")
            d = Path(t2) / "archive" / "daily" / "2026-11-18"
            d.mkdir(parents=True)
            (d / "MANIFEST.sha256").write_text(archive_daily.render_manifest(files, header))
            for n, b in files.items():
                (d / n).write_bytes(b)
            if not archive_guard.verify_day_dir(d):
                fail("pełna baza z repo dla dnia od 18.11 przeszła")
                return
        # podmiana manifestu zamkniętego dnia (nawet poprawnego formatu)
        day = repo / "archive" / "daily" / "2026-11-18"
        _put_gist_day(repo, _gist_doc(gist_revision="9" * 40))
        commit(repo, "swap manifest", when="2026-11-20T01:00:00Z")
        swapped = archive_guard.check(repo)
        if not any("status M" in item for item in swapped):
            fail("podmiana MANIFEST.json zamkniętego dnia przeszła: " + "; ".join(swapped))
            return
        subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=repo, check=True)
        shutil.rmtree(day)
        commit(repo, "drop day", when="2026-11-20T01:00:00Z")
        dropped = archive_guard.check(repo)
        if not any("status D" in item for item in dropped):
            fail("skasowanie dnia A przeszło: " + "; ".join(dropped))
            return
        subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=repo, check=True)
        # cofnięcie stanu (mniej pozycji niż dzień wcześniej)
        _put_gist_day(repo, _gist_doc(day="2026-11-19", created_at="2026-11-20T00:21:00Z",
                                      gist_revision_committed_at="2026-11-19T23:00:00Z",
                                      positions={"total": 100, "open": 0, "closed": 100, "excluded": 53, "max_id": 100}))
        back = archive_guard.check(repo)
        if not any("cofnął" in item for item in back):
            fail("cofnięcie liczby pozycji przeszło: " + "; ".join(back))
            return
        ok("dzień A: sam MANIFEST.json bez wyników; pnl/equity, zła data, dorzucona baza, podmiana, skasowanie i cofnięcie stanu odrzucane")


def main():
    test_missing_days()
    test_manifest_and_refuse_overwrite()
    test_history_tamper_detected()
    test_delete_and_late_add()
    test_second_day_and_shallow()
    test_holdout_matches_code_and_unfreeze_rules()
    test_pipeline_writes_real_snapshot()
    test_real_repo_guards_and_dry_run()
    test_no_archive_from_repo_after_state_move()
    test_gist_manifest_guard()
    test_gist_pipeline_and_backfill()
    test_gist_edge_cases()
    print()
    if FAILS:
        print(f"✗ nie przeszło: {len(FAILS)}")
        return 1
    print("✓ archiwum i holdout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
