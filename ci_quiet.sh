#!/usr/bin/env bash
# Uruchamia polecenie tak, żeby jego wyjście NIE trafiło do publicznego logu Actions
# (skrypty drukują m.in. equity, PnL i wyniki pozycji). Pełny log ląduje w
# $TVC_LOG_DIR/<nazwa>.log i jest zapisywany do prywatnego gista (last_cycle.log).
# Do publicznego logu idzie tylko: nazwa, kod wyjścia, liczba linii.
# Kod wyjścia polecenia jest zachowany (continue-on-error w workflow działa jak dotąd).
#
#   ci_quiet.sh <nazwa> <polecenie...>
set -u
NAME="$1"; shift
DIR="${TVC_LOG_DIR:-${RUNNER_TEMP:-/tmp}/tvc_logs}"
mkdir -p "$DIR"
LOG="$DIR/${NAME}.log"
"$@" >"$LOG" 2>&1
RC=$?
LINES=$(wc -l <"$LOG" | tr -d ' ')
echo "[quiet] ${NAME}: exit=${RC}, linii logu=${LINES} (pełny log: prywatny gist, last_cycle.log)"
exit "$RC"
