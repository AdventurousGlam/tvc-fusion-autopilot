#!/usr/bin/env python3
"""Strażnik holdoutu: kod musi zgadzać się z holdout/FROZEN.json.

Zmiana zamrożonej wartości przechodzi tylko z nowym wpisem w holdout/unfreeze.json
(data, powód, stara i nowa wartość). freeze_date 2026-11-05 nie podlega odblokowaniu.
Ścieżka testowa WATCH 45 nie jest w values — jej logowanie nie wywala tego testu.

Porównanie z bazą: merge-base z origin/main, albo --base <sha> z CI.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import holdout_params

ROOT = Path(__file__).resolve().parent
REASON_MIN = 60
_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,80}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _git(root: Path, *args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _ref_exists(root: Path, name: str) -> bool:
    return _git(root, "rev-parse", "--verify", "--quiet", name, check=False).returncode == 0


def resolve_base(root: Path, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if _ref_exists(root, "origin/main"):
        return _git(root, "merge-base", "HEAD", "origin/main").stdout.strip()
    if _ref_exists(root, "main"):
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        main = _git(root, "rev-parse", "main").stdout.strip()
        if head != main:
            return _git(root, "merge-base", "HEAD", "main").stdout.strip()
    return None


def _show(root: Path, rev: str, path: str) -> str | None:
    proc = _git(root, "show", f"{rev}:{path}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _load(text: str, label: str) -> tuple[dict | None, str | None]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{label}: nie jest JSON ({exc})"
    if not isinstance(doc, dict):
        return None, f"{label}: oczekiwany obiekt"
    return doc, None


def brief(value) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if len(text) > 180:
        return text[:177] + "..."
    return text


def via_json(value):
    """Ta sama postać, którą zapisuje json.dumps — porównanie kodu z plikiem."""
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))


def diff_keys(left: dict, right: dict, left_label: str, right_label: str) -> list[str]:
    lines = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            lines.append(
                f"{key}: {left_label}={brief(left.get(key))} {right_label}={brief(right.get(key))}"
            )
    return lines


def flat_view(doc: dict) -> dict:
    out = {
        "strategy_version": doc.get("strategy_version"),
        "radar_version": doc.get("radar_version"),
        "not_frozen": doc.get("not_frozen"),
    }
    production = doc.get("radar_production")
    if isinstance(production, dict):
        for key, value in production.items():
            out[f"radar_production.{key}"] = value
    else:
        out["radar_production"] = production
    values = doc.get("values")
    if isinstance(values, dict):
        for key, value in values.items():
            out[f"values.{key}"] = value
    else:
        out["values"] = values
    return out


def live_errors(doc: dict, extracted: dict) -> list[str]:
    errors = []
    if doc.get("freeze_date") != holdout_params.FREEZE_DATE:
        errors.append(
            f"freeze_date musi zostać {holdout_params.FREEZE_DATE} "
            f"(jest {doc.get('freeze_date')!r})"
        )
    if doc.get("strategy_version") != extracted["strategy_version"]:
        errors.append(
            f"strategy_version {doc.get('strategy_version')!r} "
            f"!= kod {extracted['strategy_version']!r}"
        )
    if doc.get("radar_version") != extracted["radar_version"]:
        errors.append(
            f"radar_version {doc.get('radar_version')!r} != kod {extracted['radar_version']!r}"
        )
    values = doc.get("values")
    if not isinstance(values, dict):
        errors.append("FROZEN.json: brak obiektu values")
        values = {}
    errors.extend(diff_keys(via_json(extracted["values"]), values, "kod", "FROZEN.json"))
    production = doc.get("radar_production") or {}
    if production.get("SCORE_WATCH") != extracted["values"]["pump_radar.SCORE_WATCH"]:
        errors.append("radar_production.SCORE_WATCH rozjechał się z values")
    if production.get("SCORE_HIGH") != extracted["values"]["pump_radar.SCORE_HIGH"]:
        errors.append("radar_production.SCORE_HIGH rozjechał się z values")
    if values.get("paper_bot.RULES_VERSION") != extracted["strategy_version"]:
        errors.append("values paper_bot.RULES_VERSION != strategy_version")
    if values.get("pump_radar.version") != extracted["radar_version"]:
        errors.append("values pump_radar.version != radar_version")
    notes = doc.get("notes")
    if not isinstance(notes, dict):
        errors.append("FROZEN.json: brak notes")
        notes = {}
    costs = str(notes.get("costs", ""))
    if "TVC_PARAMS" not in costs or "0.05" not in costs or "0.02" not in costs:
        errors.append(
            "notes.costs musi zostać przy domyślnych FEE 0.05 / slippage 0.02 "
            "i przy informacji, że sekret TVC_PARAMS może je nadpisać"
        )
    for key in ("holdout", "radar_test_track", "unfreeze"):
        if key not in notes:
            errors.append(f"notes.{key} brak")
    return errors


def _validate_new_entries(entries: list, changed: dict, base_flat: dict, head_flat: dict) -> list[str]:
    errors = []
    covered: dict = {}
    seen_ids = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("wpis unfreeze nie jest obiektem")
            continue
        ident = str(entry.get("id", ""))
        if not _ID_RE.fullmatch(ident):
            errors.append(f"unfreeze id {ident!r} — małe litery, cyfry, ._-")
        elif ident in seen_ids:
            errors.append(f"unfreeze id powtórzone: {ident}")
        seen_ids.add(ident)
        if not _DATE_RE.fullmatch(str(entry.get("date", ""))):
            errors.append(f"unfreeze {ident or '?'}: date musi być YYYY-MM-DD")
        reason = str(entry.get("reason", "")).strip()
        if len(reason) < REASON_MIN:
            errors.append(
                f"unfreeze {ident or '?'}: reason ma {len(reason)} znaków, "
                f"minimum {REASON_MIN}"
            )
        keys = entry.get("keys")
        changes = entry.get("changes")
        if not isinstance(keys, list) or not isinstance(changes, dict):
            errors.append(f"unfreeze {ident or '?'}: keys (lista) i changes (obiekt)")
            continue
        if set(keys) != set(changes):
            errors.append(f"unfreeze {ident}: keys i changes muszą nazywać te same pola")
        for key, pair in changes.items():
            if key in covered:
                errors.append(f"klucz {key} odblokowany dwa razy w tym PR")
                continue
            if not isinstance(pair, dict) or set(pair) != {"old", "new"}:
                errors.append(f"unfreeze {ident}: {key} potrzebuje old i new")
                continue
            covered[key] = pair
    missing = sorted(set(changed) - set(covered))
    extra = sorted(set(covered) - set(changed))
    if missing:
        errors.append("brak odblokowania dla: " + ", ".join(missing))
    if extra:
        errors.append("odblokowanie pól, które się nie zmieniły: " + ", ".join(extra))
    for key, pair in covered.items():
        if key not in changed:
            continue
        if pair["old"] != base_flat.get(key) or pair["new"] != head_flat.get(key):
            errors.append(
                f"{key}: old/new we wpisie nie jest diffem "
                f"({brief(base_flat.get(key))} → {brief(head_flat.get(key))})"
            )
    return errors


def check(root: Path = ROOT, base: str | None = None, extracted: dict | None = None) -> list[str]:
    errors = []
    frozen_path = root / "holdout" / "FROZEN.json"
    unfreeze_path = root / "holdout" / "unfreeze.json"
    if not frozen_path.is_file():
        return ["brak holdout/FROZEN.json"]
    if not unfreeze_path.is_file():
        return ["brak holdout/unfreeze.json"]
    head_doc, err = _load(frozen_path.read_text(encoding="utf-8"), "holdout/FROZEN.json")
    if err:
        return [err]
    unfreeze_doc, err = _load(unfreeze_path.read_text(encoding="utf-8"), "holdout/unfreeze.json")
    if err:
        return [err]
    assert head_doc is not None and unfreeze_doc is not None
    head_entries = unfreeze_doc.get("entries")
    if not isinstance(head_entries, list):
        errors.append("unfreeze.json: entries musi być listą")
        head_entries = []

    extracted = extracted if extracted is not None else holdout_params.extract()
    errors.extend(live_errors(head_doc, extracted))

    base_rev = resolve_base(root, base)
    base_text = _show(root, base_rev, "holdout/FROZEN.json") if base_rev else None
    base_unfreeze_text = _show(root, base_rev, "holdout/unfreeze.json") if base_rev else None

    if base_text is None:
        if head_entries:
            errors.append(
                "pierwsze zamrożenie nie może od razu zawierać wpisów unfreeze"
            )
        return errors

    base_doc, err = _load(base_text, f"{base_rev}:holdout/FROZEN.json")
    if err:
        return errors + [err]
    assert base_doc is not None
    if head_doc.get("freeze_date") != base_doc.get("freeze_date"):
        errors.append(
            "freeze_date jest datą startu holdoutu i nie podlega odblokowaniu"
        )

    if base_unfreeze_text is None:
        errors.append("baza ma FROZEN.json, ale nie ma holdout/unfreeze.json")
        base_entries: list = []
    else:
        base_unfreeze, err = _load(base_unfreeze_text, f"{base_rev}:holdout/unfreeze.json")
        if err:
            return errors + [err]
        assert base_unfreeze is not None
        base_entries = base_unfreeze.get("entries")
        if not isinstance(base_entries, list):
            errors.append("baza: unfreeze entries nie jest listą")
            base_entries = []

    if head_entries[: len(base_entries)] != base_entries:
        errors.append("historia holdout/unfreeze.json została przepisana albo skrócona")
    new_entries = head_entries[len(base_entries):]

    base_flat = flat_view(base_doc)
    head_flat = flat_view(head_doc)
    changed = {
        key: (base_flat.get(key), head_flat.get(key))
        for key in set(base_flat) | set(head_flat)
        if base_flat.get(key) != head_flat.get(key)
    }
    if changed and not new_entries:
        errors.append(
            "zamrożone wartości zmienione bez nowego wpisu w holdout/unfreeze.json: "
            + ", ".join(sorted(changed))
        )
    if new_entries or changed:
        errors.extend(_validate_new_entries(new_entries, changed, base_flat, head_flat))
    if not changed and new_entries:
        errors.append("nowy wpis unfreeze, ale żadna zamrożona wartość się nie zmieniła")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sprawdź zamrożenie progów holdoutu")
    parser.add_argument("--base", help="SHA albo ref bazy (domyślnie merge-base z origin/main)")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    errors = check(root, args.base)
    if errors:
        print("Holdout — zamrożone progi:")
        for item in errors:
            print(f"   ❌ {item}")
        return 1
    doc = json.loads((root / "holdout" / "FROZEN.json").read_text(encoding="utf-8"))
    print(
        "   ✅ holdout zamrożony "
        f"{doc['freeze_date']} · strategia {doc['strategy_version']} · "
        f"radar {doc['radar_version']} WATCH {doc['radar_production']['SCORE_WATCH']} / "
        f"HIGH {doc['radar_production']['SCORE_HIGH']} · "
        f"FEE {doc['values']['paper_bot.FEE_PCT']} / "
        f"slippage {doc['values']['paper_bot.SLIPPAGE_PCT']} "
        "(domyślne w kodzie; TVC_PARAMS może nadpisać)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
