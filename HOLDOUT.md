# Holdout i archiwum dzienne

Od **2026-11-05** (UTC) strategia **v4.4** i Pump/Dump Radar **v2.4** są zamrożone. Transakcje i sygnały od tego dnia to test forward: progów nie stroimy na tych danych. Wcześniejszy rejestr był kilka razy poprawiany wstecz, więc sam plik `paper_trades.db` na `main` nie jest już tym, co bot widział danego dnia.

To nie jest zmiana handlu. Koszty w kodzie zostają takie, jakie były. Sekret `TVC_PARAMS` (GitHub Actions) może je nadpisać w runtime i **nie jest** zapisywany.

## Archiwum

`archive/daily/YYYY-MM-DD/` powstaje raz, dla zakończonego dnia UTC, począwszy od 2026-11-05.

| Plik | Co to jest |
| --- | --- |
| `paper_trades.db` | rejestr z ostatniego commitu tego dnia (data committera przed północą UTC) |
| `fusion.json` | kopia `fusion_YYYY-MM-DD.json` z tego samego commitu |
| `params.json` | wersje (v4.4, radar v2.4) i progi odczytane z kodu tamtego commitu, bez sekretu |
| `frozen_config.json` | umowa zamrożenia obowiązująca w tym commicie (albo pierwsze zamrożenie, jeśli commit jest sprzed tego pliku) |
| `MANIFEST.sha256` | SHA-256 każdego pliku obok |

Workflow `.github/workflows/tvc-archive-daily.yml` odpala się o 00:20 UTC i dopisuje wczoraj oraz pominięte dni od daty zamrożenia. Istniejącego katalogu nie nadpisuje. Cykl co 5 minut (`tvc-autopilot.yml`) nie jest ruszany.

W git baza i fusion to te same obiekty, które i tak leżą w historii. Katalog dnia dokłada małe JSON-y i drugą nazwę na ten sam blob.

Podgląd bez zapisu (dzień, który jeszcze trwa, też — i nic nie zamyka):

```bash
python3 archive_daily.py --dry-run --date 2026-11-05
```

## Jak odtworzyć dzień

```bash
sqlite3 archive/daily/2026-11-05/paper_trades.db
# fusion: archive/daily/2026-11-05/fusion.json
# progi, jakie widział kod: archive/daily/2026-11-05/params.json
# commit źródłowy jest w nagłówku MANIFEST.sha256 (state_commit)
git show <state_commit>:paper_bot.py
```

Suma:

```bash
cd archive/daily/2026-11-05
sha256sum -c MANIFEST.sha256
```

`params.json` opisuje **domyślne wartości w kodzie** przy braku `TVC_PARAMS`. Jeśli sekret w Actions był inny, rejestr odzwierciedla handel na sekrecie, a plik progów — kod. Sam sekret do archiwum nie wchodzi. Domyślnie w kodzie: `FEE_PCT` 0,05% i `SLIPPAGE_PCT` 0,02% od nominału na stronę (`COST_PCT = 2 * (FEE_PCT + SLIPPAGE_PCT)`). Tego PR-a to nie zmienia.

## Jak widać ingerencję

`python3 archive_guard.py` (także `./verify.sh` i workflow `tvc-holdout-guard.yml`) robi dwie rzeczy:

1. Liczy SHA-256 plików dnia i porównuje z `MANIFEST.sha256`. Podmiana treści przy starej sumie odpada od razu.
2. Przechodzi historię `archive/daily/`. Każdy plik dnia może pojawić się tylko jako **dodanie**, wszystkie w tym samym commicie. Późniejsza zmiana, skasowanie albo dosypanie pliku do zamkniętego dnia wywala sprawdzenie — także wtedy, gdy manifest przepisano tak, żeby suma znowu się zgadzała.

Płytki klon nie przechodzi tego testu: stara podmiana mogłaby leżeć poza oknem historii.

## Zamrożenie

Źródło prawdy: `holdout/FROZEN.json`, data **2026-11-05**.

Zamrożone są progi i parametry decyzji v4.4 (w tym progi score, sizing, Fibonacci, scale-in, wagi fusion, pasma `score_to_action`, siatka `compute_size`) oraz produkcyjny radar v2.4: **WATCH 60 / HIGH 70**, próg obrotu, funding, cooldown, definicja trafienia, wykluczenia. Domyślne `FEE_PCT` i `SLIPPAGE_PCT` też są na tej liście, żeby koszt w kodzie nie zmienił się po cichu. Sekret i tak może je nadpisać — to jest zapisane w `notes.costs`, nie zmienione.

Poza zamrożeniem zostaje ścieżka testowa **WATCH 45** (`TEST_WATCH`, log `pump_radar_test45.json`, prerejestracja do 2.12.2026). Może dalej tylko logować. Nie wysyła Telegramu i nie rusza produkcyjnych 60/70.

Strażnik (`python3 holdout_guard.py`) przy wyłączonym `TVC_PARAMS` porównuje kod z `FROZEN.json`. Rozjazd wywala CI.

## Odblokowanie

Świadoma zmiana progu:

1. Zmień wartość w kodzie i w `holdout/FROZEN.json` tak, żeby znowu były zgodne.
2. Dopisz **na końcu** `holdout/unfreeze.json` → `entries` obiekt z `id`, `date`, `reason` (co najmniej 60 znaków: co i dlaczego), `keys` i `changes` (`old` / `new` dokładnie jak w diffie).
3. Nie edytuj starych wpisów. `freeze_date` zostaje 2026-11-05 — to data startu, nie parametr.

Bez takiego wpisu PR i `./verify.sh` nie przechodzą.
