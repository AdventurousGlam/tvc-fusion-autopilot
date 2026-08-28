# TVC Fusion Terminal Integration — setup

Cel: fusion decisions widoczne w Twoim `tvcterminal.netlify.app` jako embedded widget obok chartu. Auto-refresh 5min. Update raz dziennie po *"odpal fusion"*.

**Total setup time: ~30 min pierwszy raz, potem 0 (auto).**

---

## Architektura

```
Twój Mac (rano)              GitHub Gist (public JSON)           Netlify tvcterminal (widget)
┌──────────────────┐         ┌────────────────────────┐         ┌────────────────────────┐
│ "odpal fusion"   │         │ fusion_latest.json     │         │ iframe: fusion-widget  │
│  → fusion JSON   │─── PATCH → (single file, always ← fetch ─── │  fetch every 5 min     │
│  → paper_bot     │  (API)  │  latest daily version) │  no-cache│  render decisions      │
│  → upload        │         └────────────────────────┘         └────────────────────────┘
└──────────────────┘                                                       ↑
                                                                     embedded w chart terminal
```

---

## Krok 1 — Utwórz GitHub Gist (5 min)

1. Otwórz [gist.github.com](https://gist.github.com) (zaloguj do GitHub jeśli trzeba)
2. **Gist description:** `TVC Fusion latest`
3. **Filename:** `fusion_latest.json`
4. **Content:** wklej minimalne bootstrap:
   ```json
   {"date": "2026-08-17", "regime": "RANGING", "decisions": [], "message": "Waiting for first upload"}
   ```
5. **Wybierz "Create public gist"** (Terminal fetchuje przez CORS — public jest prostszy). Alternatywa: private gist wymaga tokena w request, którego nie chcemy w frontend JS.
6. Skopiuj **Gist ID** z URL. Przykład URL: `https://gist.github.com/malgorzatamichalek/abc123def456` → Gist ID = `abc123def456`
7. Zapisz Gist ID gdzieś na chwilę.

**Alternatywnie w Terminalu:**
```bash
# szybko przez GitHub CLI jeśli masz
gh gist create ~/Claude/TVCFusion/fusion_2026-08-17.json --public
```

---

## Krok 2 — Wygeneruj GitHub Personal Access Token (5 min)

Potrzebny żeby paper_bot mógł update'ować gist automatycznie.

1. GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**
   
   Direct link: [github.com/settings/tokens](https://github.com/settings/tokens)
2. **Generate new token (classic)**
3. **Note:** `TVC Fusion bot`
4. **Expiration:** 90 days (albo custom — możesz później rotate)
5. **Scopes:** zaznacz TYLKO `gist` (nic więcej — minimal permissions)
6. **Generate token**
7. **SKOPIUJ token natychmiast** (GitHub pokaże go tylko raz). Format: `ghp_...`

---

## Krok 3 — Ustaw env vars na Macu (2 min)

Dodaj do swojego shell config file (`~/.zshrc` dla zsh, domyślne na Mac):

```bash
echo 'export GITHUB_TOKEN="ghp_YOUR_TOKEN_HERE"' >> ~/.zshrc
echo 'export TVC_GIST_ID="YOUR_GIST_ID_HERE"' >> ~/.zshrc
source ~/.zshrc
```

Verify:
```bash
echo $TVC_GIST_ID
echo $GITHUB_TOKEN | head -c 10
# powinno pokazać: ghp_XXXXXX
```

---

## Krok 4 — Test upload z dzisiejszymi fusion data (1 min)

```bash
python3 ~/Claude/TVCFusion/paper_bot.py upload
```

**Sukces:** zobaczysz coś w stylu:
```
[upload] gist updated at 2026-08-17T18:30:15Z
[upload] widget URL: https://gist.githubusercontent.com/malgorzatamichalek/abc123def456/raw/xyz789/fusion_latest.json
[upload] set FUSION_JSON_URL in fusion-widget.html to the above URL
```

**Skopiuj widget URL** — będzie potrzebny w Kroku 6.

**Verify** — otwórz URL w przeglądarce, powinieneś zobaczyć swój dzisiejszy JSON.

---

## Krok 5 — Deploy fusion-widget.html na Netlify (10 min)

Plik `fusion-widget.html` jest w `~/Claude/TVCFusion/fusion-widget.html`.

**Opcja A — dodaj do istniejącego tvcterminal repo (rekomendacja):**

1. Znajdź folder z kodem tvcterminal na Macu (jeśli używasz Git deploy)
2. Skopiuj widget:
   ```bash
   cp ~/Claude/TVCFusion/fusion-widget.html /path/to/tvcterminal-repo/
   ```
3. Edytuj `fusion-widget.html` — znajdź linię z `REPLACE_ME_WITH_YOUR_GIST_RAW_URL` i wklej Twój widget URL z Kroku 4.
4. Commit + push:
   ```bash
   cd /path/to/tvcterminal-repo/
   git add fusion-widget.html
   git commit -m "add fusion widget"
   git push
   ```
5. Netlify automatycznie zdeployuje. Widget będzie dostępny pod `https://tvcterminal.netlify.app/fusion-widget.html`

**Opcja B — drag & drop na Netlify dashboard:**

1. Edytuj widget: zastąp `REPLACE_ME_WITH_YOUR_GIST_RAW_URL` swoim URL
2. Netlify dashboard → wybierz stronę tvcterminal → Deploys → drag & drop widget file

---

## Krok 6 — Embed widget w tvcterminal (5 min)

Otwórz główny HTML tvcterminal (probably `index.html`). Znajdź miejsce w layoucie gdzie chcesz widget (prawy sidebar recommended, szerokość 380-420px).

Wklej jedną linię:

```html
<iframe
  src="fusion-widget.html"
  style="width: 400px; height: 100vh; border: none; background: #0a0e1a;"
  title="TVC Fusion">
</iframe>
```

Albo jako floating overlay (jeśli chart zajmuje cały ekran):

```html
<iframe
  src="fusion-widget.html"
  style="position: fixed; top: 60px; right: 12px; width: 400px; height: calc(100vh - 80px); border: 1px solid #1e2a44; border-radius: 8px; background: #0a0e1a; z-index: 100;"
  title="TVC Fusion">
</iframe>
```

**Opcjonalnie** — dodaj toggle button żeby móc chować/pokazywać widget:

```html
<button onclick="document.querySelector('#fusion-frame').style.display = document.querySelector('#fusion-frame').style.display === 'none' ? 'block' : 'none'">
  Toggle Fusion
</button>
<iframe id="fusion-frame" src="fusion-widget.html" style="..."></iframe>
```

Commit + push → Netlify deploy. **Widget się pojawi w Twoim terminal.**

---

## Krok 7 — Update dziennego rytmu

Dodaj `upload` do "odpal fusion" flow. Po każdym fusion (rano):

```bash
python3 ~/Claude/TVCFusion/paper_bot.py open      # otwiera paper pozycje
python3 ~/Claude/TVCFusion/paper_bot.py upload    # push do gist → widget się odświeży
```

Albo powiedz mi w Claude *"odpal fusion i upload"* — zrobię obie rzeczy w tej samej sesji.

---

## Weryfikacja end-to-end

Jutro rano po *"odpal fusion"*:

1. **Sprawdź gist:**
   ```bash
   curl -s "$YOUR_WIDGET_URL" | jq .date
   ```
   Powinno pokazać dzisiejszą datę.

2. **Sprawdź widget w Twoim tvcterminal.netlify.app** — otwórz w przeglądarce:
   - Header pokazuje "Fusion v0.2" + zielona kropka "Updated HH:MM"
   - Regime pill (RANGING / TRENDING_UP / etc.)
   - Lista decyzji z sub-scores bars
   - Kliknij dowolny wiersz → rozwija się z setup nums (entry/SL/TP)
   - Auto-refresh co 5 min (kropka mignie na zielono)

3. **Jeśli widget pokazuje "Config error"** → zapomniałaś zamienić `REPLACE_ME_WITH_YOUR_GIST_RAW_URL` w fusion-widget.html na prawdziwy URL. Otwórz plik, popraw, redeploy.

4. **Jeśli widget pokazuje "Fetch failed"** → sprawdź czy Gist jest public (albo url działa w Twojej przeglądarce). Może CORS block, wtedy raw.githubusercontent.com powinien działać (używamy tego endpointu).

---

## Troubleshooting

**"GitHub API 401"** przy upload → token expired albo nie ma scope `gist`. Wygeneruj nowy.

**"GitHub API 404"** przy upload → zły Gist ID w env var. Sprawdź URL swojego gista.

**Widget nie odświeża się** — sprawdź w Chrome DevTools → Network → sprawdź czy fetch call idzie do gista i czy dostaje 200. Cache-buster (`?t=timestamp`) powinien wymusić fresh, ale jeśli Gist ma jakiś edge cache, spróbuj Ctrl+Shift+R hard refresh.

**Widget wygląda źle w sidebar** — dostosuj `width` w iframe style (400px domyślnie). Wszystkie kolory już matchują tvcterminal dark theme.

**Chcesz sam customize widget** — plik `fusion-widget.html` jest samodzielny. Cały CSS + JS inline. Edytuj do woli.

---

## Auto-upload jako scheduled task (opcjonalnie)

Jeśli chcesz żeby upload robił się automatycznie zaraz po fusion (bez ręcznego "upload"):

Dodam nowy scheduled task `paper-bot-upload` który odpali się o 06:47 (5 min po fusion i paper-bot-open). Wymaga tylko żebym miała dostęp do env vars — powiedz mi *"dodaj scheduled task upload"* jak będziesz gotowa.

Ale tymczasowo — ręczne `paper_bot.py upload` po każdym fusion jest OK (5 sekund).
