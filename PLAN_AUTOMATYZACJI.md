# TVC Fusion — Plan Automatyzacji Content Pipeline

**Data:** 22 września 2026  
**Status:** Plan do akceptacji  
**Cel:** Maksymalna automatyzacja strategii z STRATEGIA_POZYSKIWANIA_KLIENTOW.md v1.2

---

## Co już masz (działa automatycznie)

| Komponent | Co robi | Trigger |
|-----------|---------|---------|
| `auto_fusion.py` | Algorytmiczne sygnały BTC/ETH/SOL/XRP/SUI | Co 5 min (GH Actions) |
| `paper_bot.py` | Paper trading: open/refresh/eod + Telegram PRO/FREE | Co 5 min + EOD 22:00 |
| `auto_picks.py` | Screener MEXC — daily crypto picks + Telegram | Raz dziennie ~05:00 UTC |
| `pump_radar.py` | Skan rynku pod pumpy + Telegram alerts | Co 5 min |
| `correlation_matrix.py` | Korelacja BTC vs makro | Raz dziennie |
| `polymarket_predictions.py` | Predykcje rynku | Raz dziennie |
| `tg_membership.py` | Stripe → Telegram PRO invite/kick | Webhook (Render.com) |
| **Gist sync** | Dane → terminal frontend | Każdy cykl |
| **GitHub Actions** | Orkiestrator wszystkiego | cron-job.org co 5 min |

**Podsumowanie:** Backend jest w pełni zautomatyzowany. Dane się generują, Telegram dostaje posty. **Brakuje:** automatyczne tworzenie i publikacja treści na X, LinkedIn, YouTube.

---

## Co można zautomatyzować — 4 warstwy

### WARSTWA 1: Auto-generowanie treści (content_engine.py)
**Koszt: $0 | Trudność: Niska | Czas: 2–3h kodowania**

Nowy skrypt Python, wklejony do GitHub Actions workflow. Czyta dane z istniejących plików i generuje gotowe posty.

**Dane wejściowe (już istnieją):**
- `paper_trades.db` → zamknięte trade'y, P&L, win rate
- `fusion_latest.json` → aktualne sygnały, regime, Smart Money
- `crypto_picks.json` → daily picks
- `pump_radar_alerts.json` → alerty pumpów

**Dane wyjściowe (nowy plik):**
- `content_queue.json` → kolejka postów w formatach:
  - `x_post` — max 280 znaków, hashtagi, CTA
  - `linkedin_post` — 800–1500 znaków, profesjonalny ton
  - `youtube_script` — scenariusz do odczytania (Shorts 60s)
  - `telegram_teaser` — (backup gdyby Telegram się zepsul)

**Typy automatycznie generowanych postów:**

| Trigger | Post X | Post LinkedIn | YT Script |
|---------|--------|---------------|-----------|
| Paper bot zamyka trade z zyskiem | "🐋 $BTC LONG closed +4.2% — Smart Money confirmed. My bot saw it 15 min early. Free signals: t.me/..." | "Our Smart Money algorithm detected BTC accumulation at $63,200. Entry confirmed by whale flow + taker CVD. Result: +4.2% in 18h..." | "This whale signal made 4.2% in 18 hours — here's exactly how" |
| Paper bot zamyka trade ze stratą | "❌ $ETH SHORT stopped out -1.8%. Risk managed. Win rate still 58% over 45 trades. Transparency > hype." | "Not every trade wins. Our $ETH short was stopped at -1.8%. Here's what the data showed and why we took it..." | (pomiń — straty bez video) |
| Weekly recap (niedziela) | "📊 Week 38 results: 5 trades, 3 wins, +6.1% net. BTC whale accumulation at ATH. Full breakdown 👇🧵" (thread) | "Weekly Smart Money Report: 5 automated trades, 60% hit rate, +6.1% net PnL. Key insight: whale positioning..." | "My AI bot's Week 38 report — every trade explained" |
| Pump radar HIGH alert | "🚨 $TOKEN OI +35% in 2h, funding -0.08%, taker flip buy. Squeeze setup building. Watching..." | (pomiń — zbyt spekulacyjne na LI) | (pomiń) |
| Auto picks — top pick | "Today's Smart Money pick: $TOKEN — breakout setup, whale accumulation detected..." | "Our daily algorithmic screen flagged $TOKEN..." | (pomiń) |

**Jak to wpiąć w GitHub Actions:**
```yaml
- name: Content Engine (generuj posty z danych)
  working-directory: /home/runner/home-clone/Claude/TVCFusion
  run: python3 content_engine.py
```
Uruchamia się po paper_bot refresh — generuje posty z najnowszych danych, zapisuje do `content_queue.json` i commituje do repo.

---

### WARSTWA 2: Auto-posting na X/Twitter (API v2)
**Koszt: $0 (Free tier) lub $100/mies. (Basic) | Trudność: Średnia | Czas: 3–4h**

| Plan X API | Cena | Limity | Co daje |
|------------|------|--------|---------|
| **Free** | $0 | 1 500 postów/mies. (≈50/dzień) | Tylko pisanie (post tweets) |
| **Basic** | $100/mies. | 3 000 postów/mies. + odczyt | Pisanie + odczyt + analytics |
| **Pro** | $5 000/mies. | Full API | Overkill — nie potrzebujesz |

**Rekomendacja: Zacznij z Free tier** — 50 postów/dzień to więcej niż 3/dzień z strategii.

**Wymagania:**
1. Konto X Developer → developer.x.com (darmowe, wymaga opisu projektu)
2. Utworzenie "Project" + "App" → dostaniesz API Key + Secret
3. OAuth 2.0 User Authentication (żeby postować jako Twoje konto)
4. Sekrety do GitHub Actions: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`

**Co robi skrypt (x_poster.py):**
```
1. Czyta content_queue.json
2. Filtruje posty z flagą "x_ready" = true, "x_posted" = false
3. Postuje na X przez API v2
4. Oznacza jako "x_posted" = true
5. Max 3 posty/dzień (nie spamuj)
```

**Ograniczenia Free tier:**
- Nie możesz czytać cudzych tweetów (nie zautomatyzujesz komentowania)
- Nie masz analytics API (musisz sprawdzać ręcznie w apce)
- Nie możesz postować zdjęć/mediów (tylko tekst) — **to ważne!** Do screenshotów potrzebujesz Basic ($100/mies.) lub ręcznego postowania

**Wniosek: Free tier = auto-posty tekstowe. Screenshoty terminala = ręcznie LUB $100/mies.**

---

### WARSTWA 3: LinkedIn — semi-automatyzacja
**Koszt: $0–$6/mies. | Trudność: Średnia | Czas: 1–2h setup**

LinkedIn API jest **restrykcyjne** — w przeciwieństwie do X, nie pozwala łatwo na auto-posting:

| Opcja | Koszt | Wymagania | Ograniczenia |
|-------|-------|-----------|-------------|
| **LinkedIn API (Share)** | $0 | Weryfikacja firmy/organizacji w LinkedIn Developer | Wymaga review procesu — tygodnie/miesiące, mogą odmówić |
| **Buffer (scheduling)** | $0 (free: 10 postów/kanał) / $6/mies. | Konto Buffer + połączenie z LinkedIn | Free: max 10 zaplanowanych postów jednocześnie |
| **Typefully** | $0 (free) / $12/mies. | Konto Typefully | Free: ograniczone, ale X + LinkedIn |
| **Publer** | $0 (free: 3 kanały) | Konto Publer | Free: basic scheduling |
| **Ręcznie z wygenerowanego tekstu** | $0 | Kopiuj-wklej z content_queue.json | 2–3 min/dzień |

**Rekomendacja: Buffer Free ($0) + upgrade do $6/mies. gdy potrzebujesz więcej**

**Workflow:**
```
content_engine.py generuje linkedin_post
  → zapisuje do content_queue.json
  → Ty otwierasz plik / email z treścią
  → kopiujesz do Buffer / wklejasz na LinkedIn
  → 2-3 minuty dziennie
```

**LinkedIn Newsletter — nie da się zautomatyzować.** Trzeba pisać ręcznie w LinkedIn interface. ALE: `weekly_recap.py` może generować gotowy tekst do skopiowania.

---

### WARSTWA 4: YouTube Shorts — automatyzacja produkcji
**Koszt: $0 | Trudność: Wysoka | Czas: 8–16h kodowania**

YouTube nie ma API do tworzenia filmów. Ale możesz zautomatyzować **produkcję** (generowanie wideo z danych):

| Opcja | Koszt | Co robi | Trudność |
|-------|-------|---------|----------|
| **Remotion** (React → MP4) | $0 (open source) | Kod React generuje animowane wideo z danych (wykresy, tekst, przejścia) | Wysoka — trzeba napisać template |
| **FFmpeg + Python (Pillow)** | $0 | Generuje slideshow: obrazki z danych → połączone w MP4 z muzyką | Średnia |
| **Ręcznie OBS + CapCut** | $0 | Screen recording + edycja | 15–30 min/film |

**Remotion — jak to działa:**
1. Template React: animacja wykresu ceny + overlay danych Smart Money + tekst
2. `content_engine.py` generuje dane → plik JSON
3. Remotion renderuje MP4 z tych danych (w GitHub Actions z headless Chrome)
4. Gotowy Short do ręcznego uploadu na YouTube

**Rekomendacja: Zostaw na FAZĘ 2.** Na start OBS + CapCut (15 min/film) jest wystarczające. Remotion dopiero gdy masz rhythm i wiesz jakie formaty działają.

---

### WARSTWA 5: Screenshot terminala (automatyczny)
**Koszt: $0 | Trudność: Niska | Czas: 1–2h**

Automatyczny screenshot terminala do postów — Puppeteer w GitHub Actions:

```yaml
- name: Terminal screenshot (Puppeteer)
  run: |
    npx puppeteer-cli screenshot \
      --url "https://tradingventureclub.com/terminal/?token=BTC&auto=1" \
      --viewport 1920x1080 \
      --output terminal_screenshot.png
```

Problem: terminal jest za password gate. Rozwiązania:
1. Dodaj parametr URL `?bypass=SEKRET` (niewidoczny publicznie, tylko w Actions)
2. Lub: użyj Puppeteer z automatycznym logowaniem (wpisz hasło)
3. Lub: specjalny endpoint bez gate'a (tylko dla screenshotów, np. `/terminal/screenshot-mode.html`)

**Screenshot pójdzie do:** `content_queue.json` jako base64 lub path do pliku, gotowy do załączenia w poście.

---

## Podsumowanie kosztów

| Komponent | Koszt mies. | Priorytet | Kiedy |
|-----------|-------------|-----------|-------|
| content_engine.py (generowanie postów) | **$0** | 🔴 WYSOKI | Tydzień 1 |
| weekly_recap.py (tygodniowy raport) | **$0** | 🔴 WYSOKI | Tydzień 1 |
| Terminal screenshot (Puppeteer) | **$0** | 🟡 ŚREDNI | Tydzień 2 |
| X API Free (auto-post tekst) | **$0** | 🟡 ŚREDNI | Tydzień 2 |
| Buffer Free (LinkedIn scheduling) | **$0** | 🟡 ŚREDNI | Tydzień 2 |
| X API Basic (auto-post z obrazkami) | **$100/mies.** | 🟢 NISKI | Gdy przychód > $500/mies. |
| Buffer paid (więcej postów) | **$6/mies.** | 🟢 NISKI | Gdy 10 postów/tydzień nie starcza |
| Remotion (auto-generowane Shorts) | **$0** | 🟢 NISKI | Miesiąc 2–3 |

**ŁĄCZNY KOSZT NA START: $0**  
**ŁĄCZNY KOSZT PO SKALOWANIU: $6–$106/mies.**

---

## Proponowane fazy wdrożenia

### FAZA A — Natychmiast (ten tydzień, $0)

**Nowe skrypty:**
1. `content_engine.py` — generuje gotowe posty z danych bota
2. `weekly_recap.py` — niedzielna kompilacja tygodnia

**Zmiana w workflow:**
```yaml
# Dodaj po paper_bot refresh:
- name: Content Engine
  run: python3 content_engine.py

# Dodaj z warunkiem (niedziela 18:00-18:14 UTC):
- name: Weekly Recap (niedziela)
  run: |
    DOW=$(date -u +%u)
    HOUR=$(date -u +%H)
    MIN=$(date -u +%M)
    if [ "$DOW" = "7" ] && [ "$HOUR" = "18" ] && [ "$MIN" -lt "15" ]; then
      python3 weekly_recap.py
    fi
```

**Output:** `content_queue.json` commitowany do repo. Ty otwierasz plik i kopiujesz posty na platformy. **Oszczędność: ~80% czasu** (nie musisz wymyślać treści, tylko kopiuj-wklej).

**Dodatkowa opcja:** content_engine.py może TAKŻE wysyłać wygenerowane posty na Twój **prywatny Telegram** (chat ID: 1556849998) — dostajesz powiadomienie na telefon z gotowym tekstem, kopiujesz go na LinkedIn/X w 30 sekund.

### FAZA B — Tydzień 2–3 ($0)

1. **X Developer Account** — aplikujesz (darmowe, 1–3 dni review)
2. `x_poster.py` — auto-posting na X (tekstowe posty)
3. **Buffer konto** — łączysz LinkedIn → scheduler
4. **Terminal screenshot** — Puppeteer w Actions

**Workflow teraz wygląda tak:**
```
GitHub Actions co 5 min:
  → auto_fusion.py → paper_bot.py → pump_radar.py
  → content_engine.py → content_queue.json
  → x_poster.py → automatyczny post na X (max 3/dzień)
  → Telegram posty na prywatny chat z treścią do LinkedIn
```

**Twoja codzienna praca:** ~5 min
- Rano: otwórz Telegram, skopiuj LinkedIn post → wklej na LinkedIn (lub Buffer)
- Wieczorem: sprawdź engagement, odpowiedz na komentarze

### FAZA C — Miesiąc 2+ ($0–$106/mies.)

1. **X Basic tier** ($100/mies.) — jeśli przychody to uzasadniają → auto-posting ze screenshotami
2. **Remotion template** — auto-generowane YouTube Shorts z danych
3. **Email newsletter** (Buttondown/Substack, $0) — `weekly_recap.py` wysyła auto draft
4. **LinkedIn API** — jeśli uda się przejść weryfikację, pełne auto-posting

---

## Architektura po wdrożeniu

```
cron-job.org (co 5 min)
  │
  ▼
GitHub Actions workflow
  │
  ├── auto_fusion.py ──────── fusion_latest.json ──┐
  ├── paper_bot.py ────────── paper_trades.db ──────┤
  ├── auto_picks.py ───────── crypto_picks.json ────┤
  ├── pump_radar.py ───────── pump_radar.json ──────┤
  ├── correlation_matrix.py ── correlation_data.json │
  ├── polymarket_predictions.py                     │
  │                                                 │
  │   ┌─────────────────────────────────────────────┘
  │   ▼
  ├── content_engine.py ───── content_queue.json
  │     │
  │     ├── → X post (via x_poster.py / API v2)  [AUTO]
  │     ├── → LinkedIn post (via Telegram DM)     [SEMI — kopiuj-wklej 30s]
  │     ├── → YouTube script (w pliku)            [MANUAL — nagraj i upload]
  │     └── → Telegram PRO/FREE                   [JUŻ JEST AUTO]
  │
  ├── weekly_recap.py (niedziela) ── weekly_report.json
  │     ├── → X thread                            [AUTO]
  │     ├── → LinkedIn Newsletter draft            [SEMI]
  │     └── → YouTube "Weekly Results" script      [MANUAL]
  │
  └── Gist sync ──────────── Terminal frontend
```

---

## Ile czasu oszczędzasz

| Zadanie | BEZ automatyzacji | PO automatyzacji |
|---------|-------------------|------------------|
| Wymyślanie treści postów | 30–60 min/dzień | 0 min (skrypt generuje) |
| Pisanie postów X | 15–20 min/dzień | 0 min (auto-post) |
| Pisanie postów LinkedIn | 15–20 min/dzień | 2 min (kopiuj-wklej) |
| Weekly recap | 1–2h/tydzień | 5 min (przejrzyj wygenerowany) |
| Telegram posty | 0 min (już auto) | 0 min |
| Screenshoty terminala | 5–10 min/dzień | 0 min (Puppeteer) |
| **ŁĄCZNIE** | **~1.5–2.5h/dzień** | **~5–10 min/dzień** |

**Oszczędność: ~90% czasu na content.**

Pozostaje na Tobie: odpowiadanie na komentarze, nagrywanie YT Shorts, strategiczne decyzje.

---

## Ryzyka automatyzacji

| Ryzyko | Mitygacja |
|--------|-----------|
| X blokuje konto za "bot behavior" | Max 3 posty/dzień, różnorodna treść, nie spamuj linków w każdym poście |
| LinkedIn oznacza jako spam | Nie auto-postuj — kopiuj ręcznie. LinkedIn nie lubi botów. |
| Generowane posty brzmią "robotycznie" | Template'y z ludzkim tonem, dodaj losowe variacje, edytuj top posty ręcznie |
| API rate limits | content_engine.py ma wbudowane limity (max N postów/dzień) |
| Wyciek sekretów API | Wszystkie klucze w GitHub Secrets, nigdy w kodzie |

---

## Następny krok

Kiedy zaakceptujesz plan — zaczynam od **Fazy A**:
1. Piszę `content_engine.py` (generowanie postów z danych bota)
2. Piszę `weekly_recap.py` (niedzielna kompilacja)
3. Update `tvc-autopilot.yml` (nowy krok w workflow)
4. Opcja: Telegram DM z gotowymi postami do kopiowania

**Czas realizacji Fazy A: ~2–3h**
