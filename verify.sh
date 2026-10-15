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

  step "HTML — CSP a adresy, z ktorych terminal naprawde korzysta"
  python3 - <<'PY2'
import os, re, sys
# 15.10.2026: do CSP trafil `wss://stream.binance.com`, a kod laczy sie z
# `wss://stream.binance.com:9443`. W CSP zrodlo BEZ portu dopasowuje tylko port
# domyslny (443), wiec przeszly WSZYSTKIE strumienie orderbooka — na zywej
# stronie, bo `node --check` widzi skladnie, a nie polityke przegladarki.
f = 'tvc-terminal.html'
if not os.path.exists(f):
    sys.exit(0)
h = open(f, encoding='utf-8', errors='replace').read()
m = re.search(r'Content-Security-Policy"\s+content="([^"]+)', h)
if not m:
    print("   ⚠️  brak CSP w pliku — pomijam"); sys.exit(0)
csp = m.group(1)
cs = re.search(r'connect-src([^;]*)', csp)
allowed = set(cs.group(1).split()) if cs else set()

def covered(origin):
    if origin in allowed:
        return True
    scheme, host = origin.split('://', 1)
    # zrodlo bez portu w CSP == tylko port domyslny schematu
    if ':' in host:
        bare, port = host.rsplit(':', 1)
        default = {'https': '443', 'wss': '443', 'http': '80', 'ws': '80'}.get(scheme)
        if port == default and f"{scheme}://{bare}" in allowed:
            return True
        return f"{scheme}://{bare}:*" in allowed
    return False

used = set()
# wss:// zawsze idzie przez connect-src — bierzemy wszystkie
used |= set(re.findall(r'wss://[A-Za-z0-9.\-]+(?::\d+)?', h))
# https:// tylko tam, gdzie w tej samej linii jest fetch() albo new WebSocket()
for line in h.split('\n'):
    if 'fetch(' in line or 'new WebSocket(' in line or 'EventSource(' in line:
        used |= set(re.findall(r'https://[A-Za-z0-9.\-]+(?::\d+)?', line))
used = {u for u in used if u not in ('https://cdn.jsdelivr.net',)}

missing = sorted(u for u in used if not covered(u))
if missing:
    for u in missing:
        print(f"   {u}  — kod sie laczy, CSP NIE dopuszcza")
    print("   ❌ CSP zablokuje te polaczenia w przegladarce (uwaga na porty!)")
    sys.exit(1)
print(f"   ✅ wszystkie {len(used)} adresow z fetch/WebSocket dopuszczone w connect-src")
PY2
  [ $? -ne 0 ] && FAIL=1

  step "HTML — nazwy nieistniejace w zasiegu (acorn, odpowiednik no-undef)"
  # 15.10.2026: na zywej stronie wyszlo `i18n is not defined` w dwoch panelach
  # (obiekt nazywa sie `I18N`). `node --check` tego nie widzi, bo skladnia jest
  # poprawna — blad wychodzi przy wykonaniu funkcji renderujacej.
  # Dwie proby zlapania tego wyrazeniami regularnymi daly 11 i 15 falszywych
  # alarmow; regex nie odroznia kodu od tekstu w stringach i komentarzach.
  # Acorn parsuje JS na drzewo i daje zero falszywych trafien.
  if [ ! -d node_modules/acorn-globals ]; then
    printf '   … brak acorn-globals, instaluję\n'
    npm install --silent --no-audit --no-fund acorn acorn-globals >/dev/null 2>&1 || true
  fi
  if [ -d node_modules/acorn-globals ]; then
    node check_js_globals.js tvc-terminal.html fusion-widget.html || FAIL=1
  else
    bad "acorn-globals niedostępny — sprawdzenie nazw w JS POMINIĘTE, nie mów że zweryfikowane"
  fi

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
    import glob
    bad = 0
    for f in sorted(glob.glob('.github/workflows/*.yml')):
        try:
            d = yaml.safe_load(open(f))
            n = sum(len(j.get('steps', [])) for j in d['jobs'].values())
            print(f"   ✅ {f.split('/')[-1]} — kroków: {n}")
        except Exception as e:
            print(f"   ❌ {f.split('/')[-1]}: {type(e).__name__}: {e}"); bad = 1
    sys.exit(bad)
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
import glob
bad = []
for _f in sorted(glob.glob('.github/workflows/*.yml')):
  L = open(_f, encoding='utf-8').read().split('\n')
  for i, l in enumerate(L, 1):
    if not re.match(r'^\s+[A-Za-z_][A-Za-z_0-9]*=\$\(', l):
        continue
    blok, j = l, i
    while blok.count('(') > blok.count(')') and j < len(L):
        blok += '\n' + L[j]; j += 1
    if any(k in blok for k in RYZYKOWNE) and '|| true' not in blok and '|| echo' not in blok and '|| OGON' not in blok:
        bad.append((f"{_f.split('/')[-1]}:{i}", l.strip()[:72]))
if bad:
    for n, t in bad:
        print(f"   {n}: {t}")
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
import glob
bad = 0
_steps = []
for _f in sorted(glob.glob('.github/workflows/*.yml')):
    try:
        d = yaml.safe_load(open(_f))
    except Exception:
        continue
    for job in d['jobs'].values():
        for st in job.get('steps', []):
            _steps.append((_f.split('/')[-1], st))
for _fn, s in _steps:
    r = s.get('run')
    if not r:
        continue
    with tempfile.NamedTemporaryFile('w', suffix='.sh', delete=False) as f:
        f.write(r); p = f.name
    # -n = tylko parsowanie, nic się nie wykonuje; te same flagi co runner
    res = subprocess.run(['bash', '-n', '-eo', 'pipefail', p], capture_output=True, text=True)
    os.unlink(p)
    if res.returncode:
        print(f"   ❌ {_fn} / {s.get('name','(bez nazwy)')[:40]}: {res.stderr.strip()[:80]}")
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
