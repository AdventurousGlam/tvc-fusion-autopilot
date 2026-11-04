# Prerejestracja: test progu WATCH 45 w Pump/Dump Radarze (4.11-2.12.2026)

Spisane 4.11.2026 przed zebraniem danych. Tylko edukacja, bez sygnałów.

## Co testujemy
Czy alerty z progiem WATCH 45 (ten sam scoring i ta sama bramka: funding + inny czynnik albo listing + wieloryby) mają lepsze zwroty niż losowy token z tego samego universe w tej samej chwili.
Zachowanie produkcyjne bez zmian: WATCH 60, HIGH 70, Telegram tylko HIGH na prywatny czat. Alerty testowe nie idą na Telegram ani do panelu.

## Log
`pump_radar_test45.json` (commitowany co cykl). Każdy wpis: ticker, wynik, pasmo (45-59 albo 60+), cena, składniki, fwd_1h/4h/24h oraz losowa kontrola (ctrl_ticker, ctrl_fwd_*). Cooldown 6h na token. Logowanie kończy się 3.12.2026 00:00 Warszawa.
Baseline: dotychczasowy log `pump_radar_alerts.json` (WATCH 60).

## Kryteria (ustalone z góry)
- Główna miara: odsetek alertów z fwd_4h ≥ +3% vs to samo dla losowej kontroli (sparowanej), test dwumianowy/McNemar, α = 0,05.
- Dodatkowo: mediana fwd_24h vs kontrola (Wilcoxon sparowany). Dwie miary, więc korekta Bonferroniego (α = 0,025 każda).
- Sukces = pasmo 45-59 bije kontrolę w głównej mierze przy n ≥ 30 i nie jest gorsze w medianie 24h. Inaczej zostaje 60.
- n < 30 alertów w paśmie 45-59 = brak wniosku, nie przedłużamy bez nowej decyzji.
- Koszty pomijamy (to miara zachowania ceny, nie strategia).
