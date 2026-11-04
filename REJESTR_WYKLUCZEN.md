# Rejestr wykluczeń — paper trading TVC Fusion

Zbiór publikowany od **28.08.2026** (pomiar wyjść po świecach, commit `b92888d0c`).
Wygrana = `pnl_usd > 0`. PnL jest netto: 0,05% opłaty + 0,02% poślizgu na stronę, 0,14% na obrót.
Źródło liczb: `paper_trades.db`, `status='closed' AND opened_at >= 2026-08-28 AND excluded=0`.

**55 pozycji · 14 wygranych / 41 stratnych**

EV −0,616% na pozycję · WR 25,5% · suma −39,99 $.

| kierunek | n | wygrane | stratne | suma $ |
| --- | --- | --- | --- | --- |
| long | 38 | 11 | 27 | +19,53 |
| short | 17 | 3 | 14 | −59,53 |

Pozycje zostają w bazie. `excluded=1` zdejmuje je tylko ze statystyki i z `results.json`.
Kryterium jest defekt reguły albo fillu, nie znak PnL.

## 2. flip_choch — reguła wycofana 9.09

32 pozycje z `hit_or_miss='flip_choch'`. Wyjście na odwróceniu struktury wycofane w v0.5 (commit `ace0b98`). Suma −11,17 $. Migracja `_migrate_flag_flip_choch` w `paper_bot.py init`.

## 2a. Era przed 28.08

Wejścia sprzed pomiaru po świecach. Ceny bywały z fusionu sprzed kilku dni.
Migracja w `_migrate_flag_out_of_range` oznacza `date < 2026-08-28`.
Wierszy z tą datą i flagą jest 9. Dwa z nich są też w klasie `sl_rescan_bug` (poniżej), więc osobno — żeby nie liczyć dwa razy — zostaje 7: #1, #2, #3, #4, #5, #7, #9.

## sl_rescan_bug

12 pozycji. Do 7.09 `cmd_check` skanował świece od otwarcia z już przesuniętym stopem i zamykał transakcję wstecz na breakeven. Migracja `_migrate_sl_rescan_bug`. Suma −0,26 $.

## 2b. Wyjście spoza zakresu — tylko #64

#64 SUI, `hit_sl` przy PnL +2,23%, stop powyżej wejścia. Zostaje wykluczona.
#14, #65 i #68 były w tej klasie przez zepsuty detektor (okno 1000 świec). v4.4.1 je przywróciła. Przywrócenie pogarsza wynik i dlatego jest w bazie, nie dlatego, że poprawia nagłówek.

## 2c. #100 ADA — entry off-market −4,7%

Long ADA, otwarta 2026-10-10 02:23 UTC (04:23 Warszawa), entry 0,24195. W tej samej chwili rynek (Binance perp, 5m) był 0,2540–0,2558. Teza sama pisze „Current $0.25”. Od otwarcia do przesunięcia stopu cena nie zeszła do entry.

Log przebiegu jest dostępny: GitHub Actions run `38016596933` (start 2026-10-10 02:20:23 UTC). Krok otwarcia:

- `[warn] ADA live price failed (HTTP Error 418: I'm a teapot) — fallback na środek strefy`
- `[open] LONG ADA @ 0.2419`

Czyli odczyt ceny padł i `_mid_entry()` zapisał środek strefy z fusionu. Fill po 0,24195 nie był możliwy. Wynik +6,47% / +38,82 $ nie wchodzi do publikacji.

Migracja `_migrate_flag_off_market`: `excluded=1`, `exclusion_reason='entry off-market -4.7%'`.
Od tej wersji brak odczytu ceny pomija transakcję. Nie ma powrotu do środka strefy. Wejście dalej niż ~0,5% od ceny rynkowej albo stop po złej stronie wejścia też jest odmową.

**Nie wykluczamy** longów, których stop przesunięto na breakeven albo trailing już po wejściu: #66, #67, #69, #70, #71, #72, #81, #82, #83, #84, #86, #87. `sl_price` trzyma stop bieżący. Oryginalny stop (sl0 z historii git) był 1,2–4,5% pod wejściem, a `sl_since` jest po `opened_at`.

## Etykiety breakeven

#66, #82, #83 i #84 były zapisane jako `hit_sl` przy netto −0,04% (brutto +0,10% minus 0,14% kosztów). To stop przesunięty na breakeven przed v3.1, nie trafienie stopa początkowego. Etykieta: `hit_breakeven`. PnL bez zmian. W statystyce zostają stratami, bo `pnl_usd <= 0`.

## 6. Publikacja

`STATS_PUBLIC_FROM = 2026-11-12` w `export_results.py` i `content_engine.py`.
Do tego dnia eksport tylko wypisuje liczby. Od 12.11.2026 zapisuje `results.json`, a posty mogą podawać WR i PnL.
W `results.json` pole `sl` to stop z chwili wejścia (`sl_initial`, a dla starszych wierszy sl0), nie stop po przesunięciu.

**Razem wykluczone: 53 pozycji**

Rozłączne klasy: flip_choch 32 + sl_rescan_bug 12 + era przed 28.08 poza tamtymi dwiema 7 + #64 + #100 = 53.
