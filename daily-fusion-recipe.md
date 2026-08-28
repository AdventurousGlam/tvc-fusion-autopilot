# Daily Fusion Recipe (Wariant B — interactive)

## Twoje działanie rano (30 sekund)

Otwórz Claude i napisz:

> **odpal fusion**

Lub bardziej precyzyjnie:

> **odpal fusion na dziś i otwórz paper pozycje**

To wszystko. Reszta dzieje się w tle w jednej sesji interactive.

---

## Co Claude robi po tej komendzie

1. **Sprawdza dzisiejsze briefingi:**
   - Czyta `~/Claude/Projects/Daily notes/YYYY-MM-DD-btc-morning-note.md` (jedyny który zawsze zapisuje)
   - Sprawdza chat z scheduled task runs dla digest, position-prompts, morning-picks (jeśli nie zapisali plików, Claude ma dostęp do output przez chat history)
   - Jeśli któryś brakuje: WebSearch dla ETH/SOL/XRP/SUI news z ostatnich 24h + fetch cen z CoinGecko/Bybit

2. **Wykonuje fusion methodology:**
   - OnChain (30%): exchange netflows, whale accumulation, ETF flows
   - Technical (30%): position bias, invalidation, key levels
   - News (25%): catalysts, narrative strength
   - Momentum (15%): morning-picks setup type
   - Regime detection (TRENDING_UP/DOWN/RANGING/HIGH_VOL/CRASH)
   - Final score per token, action mapping, sizing, SL/TP

3. **Zapisuje output do dwóch miejsc:**
   - `~/Claude/TVCFusion/fusion_YYYY-MM-DD.json` (dla paper_bot.py — machine-readable)
   - `~/Claude/Projects/Daily notes/YYYY-MM-DD-fusion-decisions.md` (dla Ciebie — human-readable)

4. **Uruchamia paper bot:**
   - `python3 ~/Claude/TVCFusion/paper_bot.py open`
   - Otwiera paper pozycje w SQLite dla każdego BUY z fusion output
   - Raportuje w chacie: ile otworzył, entry/SL/TP każdej pozycji

5. **Twój raport:**
   - Tabela top-3 decyzji
   - Regime + dzisiejsze pozycje
   - Ostrzeżenia (jeśli są)

---

## Reszta jest auto

- **22:00 codziennie:** `paper-bot-eod` scheduled task odpala się sam, sprawdza SL/TP wszystkich open pozycji vs OHLC z Bybit, zamyka, produkuje EOD raport w Daily notes
- **Piątek 17:00:** `weekly-fusion-review` scheduled task czyta SQLite, produkuje analizę tygodnia i propozycję korekty wag

---

## Kryteria: kiedy przejdziesz na real broker (Warstwa 2)

- ≥ 3 tygodnie paper z pełnym flow (fusion + open + eod codziennie bez luk)
- ≥ 30 zamkniętych paper trades
- Win rate ≥ 45% na trades score ≥ 75
- Sharpe > 1.0
- Max DD ≤ 8% w każdym z 3 tygodni

Weekly review sam pokaże Ci te liczby. Jak spełnisz — mówisz *"Buduj Warstwę 2"* i dodaję ccxt z API keys, hard limits, kill switch, auto-execution na Bybit.

---

## Co jeśli zapomnisz odpalić fusion rano?

- `paper-bot-open` scheduled task próbuje o 06:45 — jeśli fusion JSON nie istnieje jeszcze, exit gracefully bez pozycji
- Odpalisz później? OK — fusion JSON dostaje freshness stamp; jeśli jest < 4h stary, `paper_bot.py open` go użyje. Starszy niż 4h → skip (chroni przed śmieciowymi trade'ami z wczorajszą ceną)
- Zapomniałaś na cały dzień? Nic strasznego. Jutro rano po prostu odpalisz. Weekly review odnotuje że tydzień miał X dni bez trade'ów (nie liczy się jako loss, ale wpływa na sample size).

---

## Alerty i nadgląd

**Sprawdź stan w każdej chwili:**

```bash
python3 ~/Claude/TVCFusion/paper_bot.py eod         # EOD raport bez czekania na 22:00
python3 ~/Claude/TVCFusion/paper_bot.py check       # tylko sprawdzenie open pozycji vs cen
python3 ~/Claude/TVCFusion/paper_bot.py week        # dump 7d jako JSON
```

Lub w Claude: *"Pokaż stan paper bota"* — odpalę i sformatuję.

---

## Kiedy Wariant A (API automation) staje się sensowny

Jak przez 2-3 tygodnie widzisz że codzienne "odpal fusion" jest tarciem — dodamy Anthropic API do paper_bot.py, wtedy całość leci bez Twojego zaangażowania. Ale najpierw sprawdź, czy Wariant B nie wystarczy — 30 sekund rano to nie problem.
