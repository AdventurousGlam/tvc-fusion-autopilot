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
# Na końcu (zawsze): archiwum dzienne nie było ruszane i progi holdoutu
# zgadzają się z kodem. Patrz HOLDOUT.md.
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

# ── Rejestr wykluczeń vs baza ────────────────────────────────────────────────
# Rejestr i kod rozjechały się dwa razy: 18.10 poprawiliśmy dane w bazie, a kod
# je nadpisywał przy każdym starcie; 19.10 rejestr miał już n=50 w nagłówku, ale
# metryki pod spodem dalej pochodziły z 47 pozycji. Oba błędy były niewidoczne,
# bo nikt nie porównywał tych dwóch źródeł. Od teraz porównuje to ten test.
if [ -f REJESTR_WYKLUCZEN.md ] && [ -f paper_trades.db ]; then
  step "Rejestr wykluczeń — zgodność z bazą"
  python3 - <<'PY'
import re, sqlite3, sys
md = open("REJESTR_WYKLUCZEN.md", encoding="utf-8").read()
c = sqlite3.connect("paper_trades.db")
q = lambda s: c.execute(s).fetchone()[0]
baza_n = q("SELECT COUNT(*) FROM positions WHERE status='closed' "
           "AND COALESCE(excluded,0)=0 AND opened_at >= '2026-08-28T00:00:00'")
baza_x = q("SELECT COUNT(*) FROM positions WHERE COALESCE(excluded,0)=1")
bad = 0
m = re.search(r"\*\*(\d+) pozycji · (\d+) wygranych / (\d+) stratnych", md)
if not m:
    print("   ❌ nie znalazłem nagłówka z liczbą pozycji"); bad = 1
else:
    rej_n, w, l = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if rej_n != baza_n:
        print(f"   ❌ pozycji: rejestr {rej_n}, baza {baza_n}"); bad = 1
    if w + l != rej_n:
        print(f"   ❌ {w} wygranych + {l} stratnych != {rej_n}"); bad = 1
m2 = re.search(r"\*\*Razem wykluczone: (\d+) pozycj", md)
if not m2:
    print("   ❌ nie znalazłem sumy wykluczonych"); bad = 1
elif int(m2.group(1)) != baza_x:
    print(f"   ❌ wykluczonych: rejestr {m2.group(1)}, baza {baza_x}"); bad = 1
kier = re.findall(r"^\| (long|short) \| (\d+) \|", md, re.M)
if len(kier) == 2:
    suma = sum(int(n) for _, n in kier)
    if suma != baza_n:
        print(f"   ❌ podział na kierunki sumuje się do {suma}, a pozycji jest {baza_n}"); bad = 1
if not bad:
    print(f"   ✅ rejestr zgodny z bazą ({baza_n} pozycji, {baza_x} wykluczonych, kierunki się sumują)")
sys.exit(bad)
PY
  [ $? -ne 0 ] && FAIL=1
fi

# ── Stopy: oryginał po właściwej stronie, przesunięty tylko po otwarciu ─────
# 4.11.2026: audyt porównał entry z sl_price i uznał 13 longów za otwarte ze
# stopem nad wejściem. sl_price to stop BIEŻĄCY (breakeven/trailing). Oryginał
# jest w sl_initial albo w r_multiple_reconstructed.csv (sl0).
if [ -f paper_trades.db ]; then
  step "Stopy — strona względem wejścia"
  python3 - <<'PY'
import csv, sqlite3, sys
from datetime import datetime, timezone
import paper_bot

bad = 0
def fail(msg):
    global bad
    bad = 1
    print(f"   ❌ {msg}")

# Reguły odmowy przy otwarciu (bez sieci).
cases = [
    ("long", 100, 100, 110, True),
    ("short", 100, 100, 90, True),
    ("long", 100, 100, 100, True),
    ("short", 100, 100, 100, True),
    ("long", 100, 100, 90, False),
    ("short", 100, 100, 110, False),
    ("long", 95.0, 100.0, 90, True),    # 5% od rynku — jak #100
    ("long", 100.4, 100.0, 90, False),  # 0,4% mieści się w ~0,5%
    ("long", 100.6, 100.0, 90, True),   # 0,6% już nie
]
for direction, entry, live, sl, rejected in cases:
    reason = paper_bot._reject_entry(direction, entry, live, sl)
    if rejected and not reason:
        fail(f"_reject_entry({direction}, {entry}, live={live}, sl={sl}) przepuściło")
    if not rejected and reason:
        fail(f"_reject_entry({direction}, {entry}, live={live}, sl={sl}) odrzuciło: {reason}")
real_fetch = paper_bot._fetch_current_price
def _boom(_ticker):
    raise RuntimeError("HTTP 418")
paper_bot._fetch_current_price = _boom
if paper_bot._live_entry_or_skip("ADA", "long") is not None:
    fail("brak ceny rynkowej nie pomija transakcji (wróciłby fallback na środek strefy)")
paper_bot._fetch_current_price = real_fetch

c = sqlite3.connect("paper_trades.db")
c.row_factory = sqlite3.Row

def opened_ms(iso):
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000

def wrong(direction, entry, sl):
    return paper_bot._stop_wrong_side(direction, entry, sl)

# (a) żaden stop początkowy po złej stronie — kolumna i odtworzenie z gita.
sl0 = {}
try:
    with open("r_multiple_reconstructed.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(line for line in fh if not line.startswith("#")):
            sl0[int(r["id"])] = float(r["sl0"])
except FileNotFoundError:
    pass
for r in c.execute("SELECT id, direction, entry_price, sl_initial FROM positions"):
    if r["sl_initial"] is not None and wrong(r["direction"], r["entry_price"], r["sl_initial"]):
        fail(f"#{r['id']} sl_initial {r['sl_initial']} po złej stronie entry {r['entry_price']}")
    if r["id"] in sl0 and wrong(r["direction"], r["entry_price"], sl0[r["id"]]):
        fail(f"#{r['id']} sl0 {sl0[r['id']]} po złej stronie entry {r['entry_price']}")

# (b) sl_price po złej stronie tylko wtedy, gdy stop zmieniono PO otwarciu.
known_rescan = 0
for r in c.execute("""SELECT id, direction, entry_price, sl_price, opened_at, sl_since,
                             COALESCE(excluded,0) excluded, hit_or_miss
                      FROM positions
                      WHERE sl_price IS NOT NULL AND entry_price IS NOT NULL"""):
    if not wrong(r["direction"], r["entry_price"], r["sl_price"]):
        continue
    moved = False
    if r["sl_since"] and r["opened_at"]:
        try:
            moved = float(r["sl_since"]) > opened_ms(r["opened_at"])
        except (TypeError, ValueError):
            moved = False
    if moved:
        continue
    if r["excluded"] and r["hit_or_miss"] == "sl_rescan_bug":
        known_rescan += 1
        continue
    fail(f"#{r['id']} sl_price po złej stronie entry, a sl_since nie jest po opened_at")
if known_rescan:
    print(f"   · {known_rescan} wykluczonych sl_rescan_bug ma stop po złej stronie bez sl_since "
          f"(kolumna doszła później; klasa jest w rejestrze)")

# Rejestr tej poprawki: #100 poza zbiorem, cztery breakeveny bez zmiany PnL,
# reszta „13 longów" zostaje.
row = c.execute("SELECT COALESCE(excluded,0), exclusion_reason FROM positions WHERE id=100").fetchone()
if row is None or row[0] != 1 or row[1] != "entry off-market -4.7%":
    fail(f"#100 nie jest wykluczona z powodem 'entry off-market -4.7%' ({None if row is None else tuple(row)})")
for pid in (66, 82, 83, 84):
    r = c.execute("SELECT hit_or_miss, pnl_pct, pnl_gross_pct, pnl_usd, COALESCE(excluded,0) ex FROM positions WHERE id=?", (pid,)).fetchone()
    if r is None or r["hit_or_miss"] != "hit_breakeven" or r["ex"]:
        fail(f"#{pid} nie jest hit_breakeven w zbiorze ({None if r is None else dict(r)})")
    elif r["pnl_gross_pct"] is None or abs(r["pnl_gross_pct"] - 0.1) > 0.005 or abs((r["pnl_pct"] or 0) + 0.04) > 0.005:
        fail(f"#{pid} PnL ruszone przy zmianie etykiety: {r['pnl_pct']} / {r['pnl_gross_pct']}")
for pid in (66, 67, 69, 70, 71, 72, 81, 82, 83, 84, 86, 87):
    ex = c.execute("SELECT COALESCE(excluded,0) FROM positions WHERE id=?", (pid,)).fetchone()
    if ex is None or ex[0]:
        fail(f"#{pid} wykluczona — stop przesunięty po wejściu zostaje w zbiorze")

if not bad:
    print("   ✅ stop początkowy po właściwej stronie; przesunięty sl_price tylko po otwarciu")
sys.exit(bad)
PY
  [ $? -ne 0 ] && FAIL=1

  step "Wejścia — opcjonalnie poza świecą otwarcia (5m)"
  python3 - <<'PY'
import json, sqlite3, sys, urllib.request
from datetime import datetime, timezone
# Flaga, nie twardy warunek sieci: brak odpowiedzi Binance nie wywala verify.sh.
# Rozjazd >0,5% na pozycji NIEWYKLUCZONEJ (jak #100) — już tak.
c = sqlite3.connect("paper_trades.db")
c.row_factory = sqlite3.Row
rows = c.execute("""SELECT id, ticker, entry_price, opened_at FROM positions
                    WHERE status='closed' AND COALESCE(excluded,0)=0
                      AND opened_at >= '2026-08-28T00:00:00'
                      AND entry_price IS NOT NULL""").fetchall()

def candle(ticker, opened_iso):
    dt = datetime.fromisoformat(str(opened_iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    start = ms - (ms % (5 * 60 * 1000))
    url = (f"https://data-api.binance.vision/api/v3/klines?symbol={ticker.upper()}USDT"
           f"&interval=5m&startTime={start}&limit=1")
    req = urllib.request.Request(url, headers={"User-Agent": "tvc-verify/1"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read())
    if not data:
        return None
    k = data[0]
    return float(k[3]), float(k[2])  # low, high

bad = 0
flagged = 0
try:
    for i, r in enumerate(rows):
        try:
            span = candle(r["ticker"], r["opened_at"])
        except Exception as e:
            print(f"   · pominięte (sieć albo Binance): {type(e).__name__}: {e}")
            sys.exit(bad)
        if not span:
            continue
        lo, hi = span
        px = float(r["entry_price"])
        if lo <= px <= hi:
            continue
        gap = (lo - px) / px if px < lo else (px - hi) / px
        flagged += 1
        line = f"#{r['id']} {r['ticker']} entry {px} poza świecą {lo}–{hi} (luka {gap*100:.2f}%)"
        if gap > 0.005:
            print(f"   ❌ {line}")
            bad = 1
        else:
            print(f"   · {line}")
except Exception as e:
    print(f"   · pominięte: {type(e).__name__}: {e}")
    sys.exit(0)
if bad:
    sys.exit(1)
print(f"   ✅ sprawdzono {len(rows)} wejść względem świecy 5m"
      + (f", flag poza zakresem poniżej 0,5%: {flagged}" if flagged else ""))
PY
  [ $? -ne 0 ] && FAIL=1
fi

# ── Archiwum dzienne i zamrożony holdout (od 2026-11-05) ─────────────────────
# Podmiana albo skasowanie snapshotu z archive/daily oraz zmiana zamrożonego
# progu bez wpisu w holdout/unfreeze.json ma wywalić się tu, nie na produkcji.
step "Archiwum dzienne — sumy i historia"
python3 archive_guard.py || FAIL=1

step "Holdout — zamrożone progi v4.4 / radar v2.4"
python3 holdout_guard.py || FAIL=1

step "Archiwum i holdout — testy (w tym suche zamknięcie dnia)"
python3 test_archive_holdout.py || FAIL=1

printf '\n'
if [ "$FAIL" -eq 0 ]; then
  printf '\033[1;32m✅ WSZYSTKO PRZESZŁO\033[0m — dopiero teraz wolno powiedzieć "sprawdzone".\n'
else
  printf '\033[1;31m❌ SĄ BŁĘDY\033[0m — nie wgrywaj.\n'
fi
exit $FAIL
