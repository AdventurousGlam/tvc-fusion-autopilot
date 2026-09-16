# TVC Fusion Terminal — Waitlist / Coming Soon Addition
# For: tradingventureclub.com (WordPress 7.1 / Astra theme)
# Created: 2026-09-15
# Purpose: Replace direct "Access Terminal" CTA with waitlist signup
# Use: Add this INSTEAD of the direct terminal access CTA on Home and Services pages

---

## ============================================================
## HOME PAGE — Replace CTA section at bottom of Terminal feature cards
## ============================================================

### ENGLISH VERSION

**Status badge (above heading, small pill/tag style):**
COMING SOON

**Heading:**
TVC Fusion Terminal is in private testing.

**Paragraph:**
We're running the system through rigorous tracked paper trading before opening access. Every signal, every trade, every result — verified and transparent. When it's ready, waitlist members get first access.

**What waitlist members get:**

- Early access before public launch
- Launch pricing locked in (lowest price, guaranteed)
- Weekly updates on system performance and development
- Direct input on features before release

**Email signup form:**

- Field placeholder: Your email address
- Button text: Join the Waitlist
- Small text below button: No spam. Only terminal updates. Unsubscribe anytime.

**Social proof line (optional, below form):**
Join [X] traders already on the list.
*(Update the number manually as signups grow. Start showing this after 20+ signups.)*

**Trust badges row (small icons + text, below form):**
- Transparent results — every trade logged publicly
- No black boxes — you see what the system sees
- Built on 10+ years of market experience

---

### POLISH VERSION

**Status badge:**
WKROTCE

**Heading:**
TVC Fusion Terminal jest w fazie prywatnych testow.

**Paragraph:**
Przeprowadzamy rygorystyczne testy paper tradingu z pelnym sledzeniem wynikow zanim otworzymy dostep. Kazdy sygnal, kazdy trade, kazdy wynik — zweryfikowany i przejrzysty. Kiedy bedzie gotowy, osoby z listy oczekujacych uzyskaja dostep jako pierwsze.

**What waitlist members get:**

- Wczesny dostep przed publicznym uruchomieniem
- Gwarantowana cena startowa (najnizsza cena, gwarancja)
- Cotygodniowe aktualizacje wynikow systemu i rozwoju
- Bezposredni wplyw na funkcje przed wydaniem

**Email signup form:**

- Field placeholder: Twoj adres e-mail
- Button text: Dolacz do listy oczekujacych
- Small text below button: Bez spamu. Tylko aktualizacje terminala. Wypisz sie w dowolnym momencie.

**Social proof line:**
Dolacz do [X] traderow juz na liscie.

**Trust badges row:**
- Przejrzyste wyniki — kazdy trade logowany publicznie
- Bez czarnych skrzynek — widzisz co widzi system
- Zbudowane na 10+ latach doswiadczenia rynkowego

---

## ============================================================
## SERVICES PAGE — Replace "Request Terminal Access" CTA
## ============================================================

### ENGLISH VERSION

**Replace the existing CTA section with:**

**Status line (before CTA area):**
Current status: Private paper trading phase | Target: verified 80%+ win rate before launch

**Live stats box (optional — update monthly):**
| Metric           | Value              |
|------------------|--------------------|
| Testing since    | September 2, 2026  |
| Trades logged    | [update manually]  |
| Current win rate | [update manually]  |
| Target win rate  | 80%+               |
| Status           | In development     |

*(This transparency table shows you're serious. Update the numbers every 1-2 weeks.)*

**Heading:**
Be the first to trade with Fusion.

**Paragraph:**
We're not launching until the system proves itself. Join the waitlist and you'll be the first to know when it does — plus lock in the lowest price we'll ever offer.

**Email signup form:**
- Field placeholder: Your email address
- Button text: Get Early Access
- Small text below: We'll notify you the moment the terminal opens. Early access members get priority onboarding and launch pricing.

---

### POLISH VERSION

**Status line:**
Aktualny status: Prywatna faza paper tradingu | Cel: zweryfikowane 80%+ win rate przed uruchomieniem

**Live stats box:**
| Metryka          | Wartosc            |
|------------------|--------------------|
| Testy od         | 2 wrzesnia 2026    |
| Zalogowane trade'y | [zaktualizuj]    |
| Obecny win rate  | [zaktualizuj]      |
| Docelowy win rate| 80%+               |
| Status           | W fazie rozwoju    |

**Heading:**
Badz pierwsza osoba tradujaca z Fusion.

**Paragraph:**
Nie uruchamiamy dopoki system sie nie udowodni. Dolacz do listy oczekujacych, a dowiesz sie jako pierwszy kiedy to nastapi — plus zablokujesz najnizsza cene jaka kiedykolwiek zaoferujemy.

**Email signup form:**
- Field placeholder: Twoj adres e-mail
- Button text: Uzyskaj wczesny dostep
- Small text below: Powiadomimy Cie w momencie otwarcia terminala. Osoby z wczesnym dostepem otrzymuja priorytetowy onboarding i cene startowa.

---

## ============================================================
## WORDPRESS SETUP — How to build the waitlist
## ============================================================

### Option A: WooCommerce (already installed on your site)

You already have WooCommerce. You can use it to collect emails:

1. **Create a free "product"** called "Terminal Waitlist" with price $0
2. Set it as a **virtual product** (no shipping)
3. On checkout, only require: email + first name
4. This gives you a customer list in WooCommerce → Customers
5. When the terminal launches, email them all with an upgrade offer

**Pros:** No new plugins. Already integrated.
**Cons:** Feels a bit clunky for a simple email signup.

### Option B: Simple email signup form (Recommended)

**Best free plugins for a waitlist form:**

1. **WPForms Lite** (free) — drag and drop form builder
   - Install from Plugins → Add New → search "WPForms"
   - Create a form with: Email field + Name field (optional)
   - Embed the form on your page with a shortcode
   - View submissions in WPForms → Entries

2. **MailerLite** (free up to 1,000 subscribers)
   - Sign up at mailerlite.com (free)
   - Install the MailerLite WordPress plugin
   - Create a signup form / embedded form
   - Subscribers go directly into your MailerLite list
   - When terminal launches → send a campaign email to the list
   - **Bonus:** Can set up automated welcome email sequence

3. **Mailchimp** (free up to 500 contacts)
   - Similar to MailerLite but lower free tier
   - More widely known, good integration ecosystem

**My recommendation: MailerLite**
- Free up to 1,000 subscribers (more than enough for start)
- Lets you send automated welcome emails
- Can segment the list later (beta testers vs regular waitlist)
- Has landing page builder if you want a standalone waitlist page
- Works well with WordPress

### Option C: SureCart (already installed on your site)

You already have SureCart. Check if it has a lead/waitlist feature:
1. Go to SureCart dashboard
2. Look for "Forms" or "Lead capture"
3. If available, create a waitlist form there — keeps everything in one system

### Setup steps (MailerLite route):

1. **Create MailerLite account** → mailerlite.com (free, takes 2 min)
2. **Create a Group** called "Terminal Waitlist"
3. **Create an Embedded Form** → choose inline style → customize colors to match your dark theme
4. **Set up Welcome Email** (automatic):
   Subject: "You're on the list — TVC Fusion Terminal"
   Body:
   ---
   Hi [name],

   You're on the TVC Fusion Terminal waitlist.

   Here's what happens next:
   - We're currently in private paper trading, testing every signal and trade
   - Our target: 80%+ verified win rate before we open access
   - You'll get updates on our progress
   - When we launch, you'll be first in line — with the best price we'll ever offer

   What is TVC Fusion Terminal?
   A professional crypto trading dashboard combining technical analysis, on-chain data, smart money tracking and sentiment analysis — backed by a 24/7 automated trading bot.

   More details: https://tradingventureclub.com/#terminal-section

   Questions? Reply to this email — I read every message.

   — Mal
   Trading Venture Club
   ---
5. **Install MailerLite plugin** in WordPress → Plugins → Add New
6. **Embed the form** on your Home page and Services page where the CTA sections are
7. **Test it** — sign up with your own email to verify the flow works

---

## ============================================================
## OPTIONAL: Standalone Waitlist Landing Page
## ============================================================

If you want a dedicated page at tradingventureclub.com/terminal-waitlist/

### ENGLISH

**Page title:** TVC Fusion Terminal — Join the Waitlist

**Hero heading:**
Crypto intelligence. Automated. Transparent.

**Hero subheading:**
TVC Fusion Terminal combines technical analysis, on-chain data, smart money tracking and market sentiment into one real-time dashboard — backed by a 24/7 automated trading bot with fully transparent results.

**We're not ready yet. And that's the point.**

Most trading tools launch with big promises and no proof. We're doing it differently:

- Building in public — every trade logged, every result visible
- Paper trading until we hit 80%+ verified win rate
- No access until the system proves itself

**When it's ready, waitlist members go first.**

[Email signup form]

**What you'll get as a waitlist member:**
1. First access when the terminal launches
2. Launch pricing — the lowest price we'll ever charge
3. Progress updates — see how the system improves week by week
4. Feature input — tell us what matters to you before we finalize

**What the terminal does (preview):**

[Include the 6 feature cards from the Home page content — Fusion Score, Automated Paper Trading, Smart Money Tracking, Pump Radar, Orderbook & Heatmap, ETF Flows & Macro]

**Current testing status:**
Testing since: September 2, 2026
Status: Private paper trading
Target: 80%+ win rate, verified

[Email signup form — repeated at bottom]

---

### POLISH

**Page title:** TVC Fusion Terminal — Dolacz do listy oczekujacych

**Hero heading:**
Inteligencja krypto. Zautomatyzowana. Przejrzysta.

**Hero subheading:**
TVC Fusion Terminal laczy analize techniczna, dane on-chain, sledzenie smart money i sentyment rynkowy w jednym dashboardzie w czasie rzeczywistym — wspierany przez zautomatyzowanego bota tradingowego 24/7 z w pelni przejrzystymi wynikami.

**Jeszcze nie jestesmy gotowi. I o to chodzi.**

Wiekszosc narzedzi tradingowych startuje z wielkimi obietnicami i zerowym dowodem. My robimy to inaczej:

- Budujemy publicznie — kazdy trade zalogowany, kazdy wynik widoczny
- Paper trading do osiagniecia 80%+ zweryfikowanego win rate
- Brak dostepu dopoki system sie nie udowodni

**Kiedy bedzie gotowy, osoby z listy oczekujacych wchodza pierwsze.**

[Formularz zapisu email]

**Co otrzymasz jako osoba na liscie oczekujacych:**
1. Pierwszy dostep po uruchomieniu terminala
2. Cena startowa — najnizsza cena jaka kiedykolwiek zaoferujemy
3. Aktualizacje postepu — sledz jak system poprawia sie tydzien po tygodniu
4. Wplyw na funkcje — powiedz nam co jest dla Ciebie wazne zanim sfinalizujemy

**Co robi terminal (podglad):**

[Umiesc 6 kart funkcji ze strony glownej]

**Aktualny status testow:**
Testy od: 2 wrzesnia 2026
Status: Prywatny paper trading
Cel: 80%+ win rate, zweryfikowane

[Formularz zapisu email — powtorzony na dole]

---

## ============================================================
## CHANGES TO EXISTING CONTENT (website-content-terminal.md)
## ============================================================

In the previously created website-content-terminal.md, make these replacements:

### Home page:
- REPLACE: "Access the Terminal →" button
- WITH: "Join the Waitlist →" button (links to waitlist form or #waitlist anchor)

- REPLACE: "Currently in tracked paper-trading phase. All results are transparent and verifiable."
- WITH: "Coming soon. Join the waitlist for first access and launch pricing."

### Services page:
- REPLACE: "Request Terminal Access →" button
- WITH: "Get Early Access →" button (links to waitlist form)

- ADD above CTA: The status/stats table showing testing progress

### Polish versions — same changes:
- "Wejdz do Terminala →" → "Dolacz do listy oczekujacych →"
- "Poproś o dostep do Terminala →" → "Uzyskaj wczesny dostep →"

---

*End of waitlist content package. Use together with website-content-terminal.md.*
