"""
tvc_params.py — ładowanie strojonych parametrów spoza repozytorium.

Po co: repozytorium jest publiczne (portfolio, backlink, wiarygodność), ale
wartości progów i wag to jedyna rzecz, której nie da się odtworzyć bez danych
z kilku miesięcy pracy. Kod zostaje jawny, liczby wyjeżdżają do GitHub Secrets.

Jak działa: skrypty czytają jedną zmienną środowiskową TVC_PARAMS zawierającą
JSON. Jeśli jej nie ma, używane są DOMYŚLNE wartości neutralne — celowo
zachowawcze, NIE te produkcyjne — a `LOADED` zostaje False.

WAŻNE: paper_bot sprawdza `LOADED` przed otwarciem jakiejkolwiek pozycji.
Brak sekretu = bot nie handluje, zamiast handlować na przypadkowych progach.

Format TVC_PARAMS (JSON):
  {
    "MIN_LONG_SCORE": 58,
    "MIN_LONG_SCORE_BY_REGIME": {"TRENDING_UP": 62, ...},
    "TIERED_LONG_SIZES": {"62-68": 4.0, "68-72": 6.0, ...}
  }

Klucze zakresowe ("62-68") są zamieniane na krotki (62, 68) przez `ranges()`.
"""

import json
import os

_raw = os.environ.get("TVC_PARAMS", "").strip()

LOADED = False
_P = {}

if _raw:
    try:
        _P = json.loads(_raw)
        LOADED = isinstance(_P, dict) and bool(_P)
    except json.JSONDecodeError as e:
        print(f"[params] ⛔ TVC_PARAMS nie jest poprawnym JSON-em: {e}")
        print("[params]    Używam wartości domyślnych. Bot NIE będzie otwierał pozycji.")
        _P = {}

if not LOADED:
    print("[params] ⚠️  Brak TVC_PARAMS — działam na domyślnych, zachowawczych wartościach.")
    print("[params]    Odczyt i raportowanie działają normalnie; otwieranie pozycji jest zablokowane.")


def get(key, default):
    """Pojedyncza wartość (liczba, string, bool)."""
    return _P.get(key, default)


def mapping(key, default):
    """Słownik o kluczach tekstowych, np. progi per reżim."""
    v = _P.get(key)
    return dict(v) if isinstance(v, dict) else dict(default)


def ranges(key, default):
    """
    Słownik o kluczach-zakresach. W JSON zapisany jako {"62-68": 4.0},
    w Pythonie używany jako {(62, 68): 4.0}.
    """
    v = _P.get(key)
    if not isinstance(v, dict):
        return dict(default)
    out = {}
    for k, val in v.items():
        try:
            lo, hi = str(k).split("-", 1)
            out[(int(lo), int(hi))] = float(val)
        except (ValueError, TypeError):
            print(f"[params] pomijam niepoprawny zakres: {k!r}")
    return out or dict(default)


def require_loaded(action="otworzyć pozycję"):
    """
    Wywoływane przed akcjami, które zmieniają stan portfela.
    Zwraca True, gdy parametry pochodzą z sekretu.
    """
    if LOADED:
        return True
    print(f"[params] ⛔ Odmawiam {action} — brak TVC_PARAMS.")
    print("[params]    Ustaw sekret w Settings → Secrets and variables → Actions.")
    return False
