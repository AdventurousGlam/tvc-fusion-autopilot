#!/usr/bin/env python3
"""Odczyt progów, które zamrażamy na holdout (kod, nie sekret TVC_PARAMS).

Nie zmienia handlu. Importuje paper_bot / auto_fusion / pump_radar i spisuje
wartości, na których kod naprawdę stoi, gdy zmiennej TVC_PARAMS nie ma.
Sonda score_to_action i compute_size łapie progi zaszyte w funkcjach, nie tylko
stałe na górze pliku.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from datetime import timezone
from pathlib import Path

FREEZE_DATE = "2026-11-05"
STRATEGY_VERSION = "v4.4"
RADAR_VERSION = "v2.4"

# Dzień sondy jest sztywny, żeby wynik nie zależał od dnia uruchomienia CI.
_PROBE_WEEKDAY = (2026, 11, 4)   # środa
_PROBE_WEEKEND = (2026, 11, 7)   # sobota
_PROBE_REGIMES = (
    "TRENDING_UP",
    "TRENDING_UP_VOLATILE",
    "RANGING",
    "TRENDING_DOWN",
    "TRENDING_DOWN_VOLATILE",
    "CRASH",
    "OTHER",
)
_PROBE_SCORES = (0, 20, 39, 40, 50, 59, 60, 74, 75, 79, 80, 100)
_PROBE_TICKERS = ("BTC", "SOL")

NOTES = {
    "holdout": (
        "Dane od 2026-11-05 UTC włącznie są testem forward. "
        "Wartości z 'values' nie są strojone na tych danych."
    ),
    "costs": (
        "Domyślne w kodzie, nie ruszane w tym zamrożeniu: FEE_PCT 0.05 i "
        "SLIPPAGE_PCT 0.02 (procent od nominału, każda strona). "
        "COST_PCT = 2*(FEE_PCT+SLIPPAGE_PCT), czyli pełny obrót. "
        "Sekret GitHub Actions TVC_PARAMS może nadpisać te i inne klucze w runtime; "
        "sekretu nie ma w repo i archiwum go nie zapisuje."
    ),
    "radar_test_track": (
        "TEST_WATCH 45 to osobny log pump_radar_test45.json (prerejestracja "
        "4.11–2.12.2026), bez Telegramu i bez zmiany produkcyjnych WATCH 60 / HIGH 70. "
        "Nie wchodzi do values i wolno mu dalej tylko logować."
    ),
    "unfreeze": (
        "Zmiana values wymaga nowego wpisu w holdout/unfreeze.json. "
        "freeze_date zostaje 2026-11-05."
    ),
}


def _silence_imports():
    """tvc_params wypisuje ostrzeżenie na stdout, gdy nie ma sekretu. To nie błąd."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        os.environ.pop("TVC_PARAMS", None)
        import auto_fusion
        import paper_bot
        import pump_radar
    text = buf.getvalue()
    if "⛔" in text or "sumują" in text:
        print(text, file=sys.stderr, end="" if text.endswith("\n") else "\n")
    return paper_bot, auto_fusion, pump_radar


def _ranges(mapping):
    out = {}
    for key, val in mapping.items():
        lo, hi = key
        out[f"{int(lo)}-{int(hi)}"] = val
    return out


def _bands(auto_fusion, regime):
    rows = []
    start = 0
    prev = auto_fusion.score_to_action(0, regime)
    for score in range(1, 101):
        action = auto_fusion.score_to_action(score, regime)
        if action != prev:
            rows.append({"from": start, "to": score - 1, "action": prev})
            start = score
            prev = action
    rows.append({"from": start, "to": 100, "action": prev})
    return rows


def _patch_datetime(auto_fusion, ymd):
    real = auto_fusion.datetime
    year, month, day = ymd

    class _Fixed(real):
        @classmethod
        def now(cls, tz=None):
            return real(year, month, day, 12, 0, tzinfo=timezone.utc)

    return real, _Fixed


def _size_grid(auto_fusion):
    real = auto_fusion.datetime
    grid = {}
    try:
        for label, ymd in (("weekday", _PROBE_WEEKDAY), ("weekend", _PROBE_WEEKEND)):
            _, fixed = _patch_datetime(auto_fusion, ymd)
            auto_fusion.datetime = fixed
            for regime in _PROBE_REGIMES:
                for ticker in _PROBE_TICKERS:
                    for score in _PROBE_SCORES:
                        key = f"{label}|{regime}|{ticker}|{score}"
                        grid[key] = auto_fusion.compute_size(score, regime, ticker)
    finally:
        auto_fusion.datetime = real
    weekday = grid["weekday|TRENDING_UP|BTC|60"]
    weekend = grid["weekend|TRENDING_UP|BTC|60"]
    if not (weekday > weekend > 0):
        raise RuntimeError(
            f"sonda wielkości nie widzi weekendu: weekday={weekday} weekend={weekend}"
        )
    return grid


def _radar_version(pump_radar):
    match = re.search(r"Pump Radar (v\d+\.\d+)", pump_radar.__doc__ or "")
    if not match:
        raise RuntimeError("brak wersji Pump Radar w docstringu pump_radar.py")
    return match.group(1)


def extract():
    """Wartości kodu przy wyłączonym TVC_PARAMS. Bez sieci i bez zapisu."""
    paper_bot, auto_fusion, pump_radar = _silence_imports()
    radar_version = _radar_version(pump_radar)

    values = {
        "paper_bot.RULES_VERSION": paper_bot.RULES_VERSION,
        "paper_bot.MIN_LONG_SCORE": paper_bot.MIN_LONG_SCORE,
        "paper_bot.MIN_LONG_SCORE_FLOOR": paper_bot.MIN_LONG_SCORE_FLOOR,
        "paper_bot.MAX_SHORT_SCORE": paper_bot.MAX_SHORT_SCORE,
        "paper_bot.MIN_RR_AT_ENTRY": paper_bot.MIN_RR_AT_ENTRY,
        "paper_bot.REOPEN_COOLDOWN_MINUTES": paper_bot.REOPEN_COOLDOWN_MINUTES,
        "paper_bot.MAX_NEW_TRADES_PER_DAY": paper_bot.MAX_NEW_TRADES_PER_DAY,
        "paper_bot.MAX_HOLD_DAYS": paper_bot.MAX_HOLD_DAYS,
        "paper_bot.MIN_LONG_SCORE_BY_REGIME": dict(paper_bot.MIN_LONG_SCORE_BY_REGIME),
        "paper_bot.MAX_SHORT_SCORE_BY_REGIME": dict(paper_bot.MAX_SHORT_SCORE_BY_REGIME),
        "paper_bot.FIB_THRESHOLD_BY_REGIME": dict(paper_bot.FIB_THRESHOLD_BY_REGIME),
        "paper_bot.FIB_THRESHOLD_DEFAULT": paper_bot.FIB_THRESHOLD_DEFAULT,
        "paper_bot.SCALE_IN_ENABLED": paper_bot.SCALE_IN_ENABLED,
        "paper_bot.SCALE_IN_SIZE_FACTOR": paper_bot.SCALE_IN_SIZE_FACTOR,
        "paper_bot.SCALE_IN_MIN_LAYERS": paper_bot.SCALE_IN_MIN_LAYERS,
        "paper_bot.SCALE_IN_LONG_MIN_SCORE": paper_bot.SCALE_IN_LONG_MIN_SCORE,
        "paper_bot.SCALE_IN_SHORT_MAX_SCORE": paper_bot.SCALE_IN_SHORT_MAX_SCORE,
        "paper_bot.SCALE_IN_MIN_RR": paper_bot.SCALE_IN_MIN_RR,
        "paper_bot.TIERED_LONG_SIZES": _ranges(paper_bot.TIERED_LONG_SIZES),
        "paper_bot.TIERED_SHORT_SIZES": _ranges(paper_bot.TIERED_SHORT_SIZES),
        "paper_bot.SHORT_SIZE_CAP_PCT": paper_bot.SHORT_SIZE_CAP_PCT,
        "paper_bot.BREAKEVEN_TRIGGER": paper_bot.BREAKEVEN_TRIGGER,
        "paper_bot.TRAILING_TRIGGER": paper_bot.TRAILING_TRIGGER,
        "paper_bot.TRAILING_DISTANCE": paper_bot.TRAILING_DISTANCE,
        "paper_bot.BREAKEVEN_OFFSET": paper_bot.BREAKEVEN_OFFSET,
        "paper_bot.TRAILING_TRIGGER_R": paper_bot.TRAILING_TRIGGER_R,
        "paper_bot.TRAILING_DISTANCE_R": paper_bot.TRAILING_DISTANCE_R,
        "paper_bot.TP1_CLOSE_FRAC": paper_bot.TP1_CLOSE_FRAC,
        "paper_bot.RISK_PCT": paper_bot.RISK_PCT,
        "paper_bot.ATR_SL_MULTIPLIER": paper_bot.ATR_SL_MULTIPLIER,
        "paper_bot.ATR_SL_MULTIPLIER_SHORT": paper_bot.ATR_SL_MULTIPLIER_SHORT,
        "paper_bot.STATS_SINCE": paper_bot.STATS_SINCE,
        "paper_bot.FEE_PCT": paper_bot.FEE_PCT,
        "paper_bot.SLIPPAGE_PCT": paper_bot.SLIPPAGE_PCT,
        "paper_bot.COST_PCT": paper_bot.COST_PCT,
        "auto_fusion.TICKERS": list(auto_fusion.TICKERS),
        "auto_fusion.W_ONCHAIN": auto_fusion.W_ONCHAIN,
        "auto_fusion.W_TA": auto_fusion.W_TA,
        "auto_fusion.W_SENTIMENT": auto_fusion.W_SENTIMENT,
        "auto_fusion.W_NEWS": auto_fusion.W_NEWS,
        "auto_fusion.FNG_GREED_COEF": auto_fusion.FNG_GREED_COEF,
        "auto_fusion.FNG_FEAR_COEF": auto_fusion.FNG_FEAR_COEF,
        "auto_fusion.EXTENDED_ATR_MULT": auto_fusion.EXTENDED_ATR_MULT,
        "auto_fusion.EXTENDED_CHG24_PCT": auto_fusion.EXTENDED_CHG24_PCT,
        "auto_fusion.MIN_RR": auto_fusion.MIN_RR,
        "auto_fusion.MAX_SL_PCT": auto_fusion.MAX_SL_PCT,
        "auto_fusion.MIN_SL_PCT": auto_fusion.MIN_SL_PCT,
        "auto_fusion.LEVEL_CLUSTER_PCT": auto_fusion.LEVEL_CLUSTER_PCT,
        "auto_fusion.action_bands": {
            regime: _bands(auto_fusion, regime) for regime in _PROBE_REGIMES
        },
        "auto_fusion.size_grid": _size_grid(auto_fusion),
        "pump_radar.version": radar_version,
        "pump_radar.SCORE_WATCH": pump_radar.SCORE_WATCH,
        "pump_radar.SCORE_HIGH": pump_radar.SCORE_HIGH,
        "pump_radar.MIN_TURNOVER_USD": pump_radar.MIN_TURNOVER_USD,
        "pump_radar.MIN_FUNDING_BPS": pump_radar.MIN_FUNDING_BPS,
        "pump_radar.ALERT_COOLDOWN_H": pump_radar.ALERT_COOLDOWN_H,
        "pump_radar.HIT_1H_PCT": pump_radar.HIT_1H_PCT,
        "pump_radar.HIT_4H_PCT": pump_radar.HIT_4H_PCT,
        "pump_radar.EXCLUDE": sorted(pump_radar.EXCLUDE),
        "pump_radar.EXCLUDE_SUBSTR": list(pump_radar.EXCLUDE_SUBSTR),
    }
    return {
        "strategy_version": paper_bot.RULES_VERSION,
        "radar_version": radar_version,
        "values": values,
        "test_track": {
            "pump_radar.TEST_WATCH": pump_radar.TEST_WATCH,
            "pump_radar.TEST_END_TS": pump_radar.TEST_END_TS,
            "log": "pump_radar_test45.json",
        },
    }


def frozen_document(extracted=None):
    ex = extracted or extract()
    return {
        "freeze_date": FREEZE_DATE,
        "strategy_version": ex["strategy_version"],
        "radar_version": ex["radar_version"],
        "radar_production": {
            "SCORE_WATCH": ex["values"]["pump_radar.SCORE_WATCH"],
            "SCORE_HIGH": ex["values"]["pump_radar.SCORE_HIGH"],
        },
        "values": ex["values"],
        "not_frozen": ex["test_track"],
        "notes": NOTES,
    }


def dumps(doc) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Wypisz zamrożone progi jako JSON")
    parser.add_argument("--json-out", type=Path, help="zapisz extract() do pliku")
    parser.add_argument("--frozen-out", type=Path, help="zapisz pełny dokument zamrożenia")
    args = parser.parse_args()
    if args.json_out:
        args.json_out.write_text(dumps(extract()), encoding="utf-8")
        return
    doc = frozen_document()
    if doc["strategy_version"] != STRATEGY_VERSION or doc["radar_version"] != RADAR_VERSION:
        raise SystemExit(
            f"odmowa zapisu: kod to {doc['strategy_version']} / {doc['radar_version']}, "
            f"zamrożenie ma opisać {STRATEGY_VERSION} / {RADAR_VERSION}"
        )
    if args.frozen_out:
        args.frozen_out.write_text(dumps(doc), encoding="utf-8")
        return
    sys.stdout.write(dumps(doc))


if __name__ == "__main__":
    main()
