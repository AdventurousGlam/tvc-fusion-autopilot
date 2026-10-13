#!/usr/bin/env bash
# verify.sh — co musi przejść, zanim cokolwiek zostanie nazwane sprawdzonym.
#
# Powstało 13.10.2026 po dniu, w którym trzy razy pod rząd wyszedł ten sam błąd:
# kod przeszedł sprawdzenie składni, został ogłoszony zweryfikowanym i wywalił się
# przy wykonaniu. py_compile i `node --check` czytają składnię. Nie uruchamiają
# niczego i nie wiedzą, że nazwa nie istnieje w zasięgu ani że bash w CI ma set -e.
#
# Konkretnie złapałoby:
#   • `CONFLICTED=$(... | while ... grep -l ...)` w workflow — grep zwraca 1 przy
#     braku trafień, a GitHub Actions uruchamia bash z -eo pipefail → krok umiera.
#     Autopilot stał przez to 2 h 35 min (13.10, 12:58–15:33 UTC).
#   • `row + (sl_price,)` w paper_bot.py — sl_price nie jest zmienną w tym zasięgu,
#     tylko nazwą kolumny. NameError przy pierwszym otwarciu pozycji.
#
# Użycie:  ./verify.sh          — wszystko
#          ./verify.sh py       — tylko Python
#          ./verify.sh html     — tylko JS w HTML
#          ./verify.sh yml      — tylko workflow
set -uo pipefail
cd "$(dirname "$0")"
WHAT="${1:-all}"
FAIL=0

# Zależności: pyflakes i pyyaml. Na macOS `pip install` do systemowego Pythona
# zwykle się nie uda (externally-managed-environment), więc instalujemy do
# katalogu użytkownika. Brak którejkolwiek = sprawdzenie POMINIĘTE, a nie
# „przeszło" — dlatego skrypt kończy się wtedy błędem, nie zielonym.
_ensure() {  # $1 = moduł, $2 = nazwa paczki
  python3 -c "import $1" 2>/dev/null && return 0
  printf '   … brak %s, instaluję\n' "$2"
  pip3 install --user --quiet "$2" 2>/dev/null \
    || pip3 install --quiet --break-system-packages "$2" 2>/dev/null \
    || python3 -m pip install --user --quiet "$2" 2>/dev/null
  python3 -c "import $1" 2>/dev/null
}
step() { printf '\n\033[1m── %s ──\033[0m\n' "$1"; }
ok()   { printf '   ✅ %s\n' "$1"; }
bad()  { printf '   ❌ %s\n' "$1"; FAIL=1; }

# ── 1. PYTHON: składnia + nazwy nieistniejące w zasięgu ──────────────────────
if [ "$WHAT" = all ] || [ "$WHAT" = py ]; then
  step "Python — składnia"
  PYF=$(ls *.py 2>/dev/null | grep -v '^tvc_params\.py$' || true)
  for f in $PYF; do
    python3 -m py_compile "$f" 2>/dev/null && ok "$f" || bad "$f — py_compile"
  done

  step "Python — nazwy i martwy kod (pyflakes)"
  if _ensure pyflakes pyflakes; then
    # Interesują nas tylko błędy, które wywalają program przy wykonaniu.
    # "imported but unused" i f-stringi bez pól to styl, nie awaria.
    OUT=$(python3 -m pyflakes $PYF 2>&1 | grep -E "undefined name|local variable .* referenced before|redefinition of unused" || true)
    if [ -z "$OUT" ]; then
      ok "brak nieistniejących nazw"
    else
      echo "$OUT" | sed 's/^/   /'
      bad "nazwy, których nie ma w zasięgu — to NameError przy wykonaniu"
    fi
  else
    bad "pyflakes niedostępny — sprawdzenie nazw POMINIĘTE, nie mów że zweryfikowane"
  fi
fi

# ── 2. HTML: JavaScript wyciągnięty ze <script> ──────────────────────────────
if [ "$WHAT" = all ] || [ "$WHAT" = html ]; then
  step "HTML — składnia JavaScriptu"
  for f in tvc-terminal.html fusion-widget.html; do
    [ -f "$f" ] || continue
    python3 - "$f" > /tmp/_verify_js.js <<'PY'
import re, sys
h = open(sys.argv[1], encoding='utf-8', errors='replace').read()
print('\n'.join(re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>', h, re.S)))
PY
    if node --check /tmp/_verify_js.js 2>/tmp/_verify_js.err; then
      ok "$f"
    else
      sed 's/^/   /' /tmp/_verify_js.err
      bad "$f — błąd składni JS"
    fi
  done

  step "HTML — klucze t() bez definicji w I18N"
  python3 - <<'PY'
import re, sys, os
f = 'tvc-terminal.html'
if not os.path.exists(f):
    sys.exit(0)
h = open(f, encoding='utf-8', errors='replace').read()
defined = set(re.findall(r"^\s{2}(\w+):\s*\{\s*pl:", h, re.M))
# t('x') — odsiewamy trafienia z getContext('2d'), createElement('a') itp.
used = set(re.findall(r"[^a-zA-Z_]t\('([a-z][a-z0-9_]{3,})'\)", h))
miss = sorted(k for k in used if k not in defined)
print("   ✅ wszystkie klucze zdefiniowane" if not miss else "   ❌ brak w I18N: " + ", ".join(miss))
sys.exit(1 if miss else 0)
PY
  [ $? -ne 0 ] && FAIL=1
fi

# ── 3. WORKFLOW: pułapka set -e w przypisaniach ──────────────────────────────
if [ "$WHAT" = all ] || [ "$WHAT" = yml ]; then
  step "Workflow — składnia YAML"
  if ! _ensure yaml pyyaml; then
    bad "pyyaml niedostępny — sprawdzenia YAML POMINIĘTE, nie mów że zweryfikowane"
  else
  python3 - <<'PY'
import sys
try:
    import yaml
except ImportError:
    print("   ❌ brak pyyaml"); sys.exit(1)
try:
    d = yaml.safe_load(open('.github/workflows/tvc-autopilot.yml'))
    n = len(list(d['jobs'].values())[0]['steps'])
    print(f"   ✅ YAML poprawny, kroków: {n}")
except Exception as e:
    print(f"   ❌ {type(e).__name__}: {e}"); sys.exit(1)
PY
  [ $? -ne 0 ] && FAIL=1

  step "Workflow — przypisania, które mogą zabić krok przez set -e"
  python3 - <<'PY'
import re, sys
# GitHub Actions uruchamia `bash --noprofile --norc -eo pipefail`. Przypisanie
# NAME=$(cmd) przyjmuje kod wyjścia cmd. Jeśli cmd może zwrócić !=0 w normalnym
# przebiegu (grep bez trafień, test, find), krok pada — mimo że nic złego się
# nie stało. Ratuje `|| true` albo `|| echo` na końcu przypisania.
RYZYKOWNE = ('grep', 'test ', '[ ', 'find', 'diff', 'cmp', 'git diff')
try:
    L = open('.github/workflows/tvc-autopilot.yml', encoding='utf-8').read().split('\n')
except FileNotFoundError:
    sys.exit(0)
bad = []
for i, l in enumerate(L, 1):
    if not re.match(r'^\s+[A-Za-z_][A-Za-z_0-9]*=\$\(', l):
        continue
    blok, j = l, i
    while blok.count('(') > blok.count(')') and j < len(L):
        blok += '\n' + L[j]; j += 1
    if any(k in blok for k in RYZYKOWNE) and '|| true' not in blok and '|| echo' not in blok:
        bad.append((i, l.strip()[:72]))
if bad:
    for n, t in bad:
        print(f"   linia {n}: {t}")
    print("   ❌ przypisanie z polecenia, które zwraca !=0 w normalnym przebiegu — dopisz `|| true`")
    sys.exit(1)
print("   ✅ brak przypisań wrażliwych na set -e")
PY
  [ $? -ne 0 ] && FAIL=1

  fi
  step "Workflow — bloki run: uruchomione w bashu CI (-eo pipefail, dry-run)"
  if python3 -c "import yaml" 2>/dev/null; then
  python3 - <<'PY'
import yaml, subprocess, sys, tempfile, os
try:
    d = yaml.safe_load(open('.github/workflows/tvc-autopilot.yml'))
except Exception:
    sys.exit(0)
job = list(d['jobs'].values())[0]
bad = 0
for s in job['steps']:
    r = s.get('run')
    if not r:
        continue
    with tempfile.NamedTemporaryFile('w', suffix='.sh', delete=False) as f:
        f.write(r); p = f.name
    # -n = tylko parsowanie, nic się nie wykonuje; te same flagi co runner
    res = subprocess.run(['bash', '-n', '-eo', 'pipefail', p], capture_output=True, text=True)
    os.unlink(p)
    if res.returncode:
        print(f"   ❌ {s.get('name','(bez nazwy)')[:50]}: {res.stderr.strip()[:90]}")
        bad += 1
print("   ✅ wszystkie bloki run parsują się w bashu CI" if not bad else f"   ❌ bloków z błędem: {bad}")
sys.exit(1 if bad else 0)
PY
  [ $? -ne 0 ] && FAIL=1
  else
    bad "pyyaml niedostępny — bloki run NIESPRAWDZONE"
  fi
fi

printf '\n'
if [ "$FAIL" -eq 0 ]; then
  printf '\033[1;32m✅ WSZYSTKO PRZESZŁO\033[0m — dopiero teraz wolno powiedzieć "sprawdzone".\n'
else
  printf '\033[1;31m❌ SĄ BŁĘDY\033[0m — nie wgrywaj.\n'
fi
exit $FAIL
