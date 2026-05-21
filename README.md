# stocks-calendar

Generate a rolling 30-day iCalendar feed for watchlist earnings and key US market events.

The feed is built for iOS Calendar subscriptions through GitHub Pages. Update `watchlist.yaml`, optionally set an FMP API key in GitHub Secrets, and GitHub Actions publishes `public/earnings.ics` once per day.

## Calendar URL

After GitHub Pages is enabled and the workflow succeeds, subscribe to:

```text
https://<github-user>.github.io/stocks-calendar/earnings.ics
```

Generation status is published at:

```text
https://<github-user>.github.io/stocks-calendar/status.json
```

The local branch also writes a static dashboard at:

```text
public/dashboard.html
```

On iPhone:

1. Open Settings.
2. Go to Calendar.
3. Open Accounts.
4. Tap Add Account.
5. Tap Other.
6. Tap Add Subscribed Calendar.
7. Paste the calendar URL.

## Data Source

This project uses free data sources by default. Financial Modeling Prep is only used for earnings when an API key is available; macro events and exchange holidays do not require a paid provider.

```text
https://financialmodelingprep.com/stable/earnings-calendar
https://api.nasdaq.com/api/calendar/earnings
https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
https://www.bls.gov/schedule/news_release/cpi.htm
https://www.bls.gov/schedule/news_release/empsit.htm
https://www.bea.gov/news/schedule
data/official_macro_releases.yaml
Calculated NYSE/Nasdaq/KRX/HKEX holiday rules with official exchange links
Company IR press release RSS feeds and IR pages from `watchlist.yaml`
```

To improve earnings coverage, create a free API key at Financial Modeling Prep, then add it to the GitHub repository:

```text
Settings -> Secrets and variables -> Actions -> New repository secret
Name: FMP_API_KEY
Value: <your-api-key>
```

Local `.env` files are ignored. Use `.env.example` as a reference only.

If FMP returns an authorization, payment, quota, or transient provider error, the workflow continues. It records the error in `public/status.json`, keeps any official IR/manual/free scheduled events it can still generate, and reuses the previously published `earnings.ics` when the degraded run would otherwise publish an empty calendar.

`public/status.json` also includes `earnings_coverage`, which lists symbols with detected earnings events, symbols without events in the current 30-day window, official-IR-confirmed rows, Nasdaq session-enriched rows, timed rows, low-confidence rows, source quality, and event confidence. This is intended to make future integration with a local investment database straightforward.

Official IR scanning is deliberately capped. Per-symbol `preferred_ir_url_patterns` and `skip_ir_url_patterns` in `watchlist.yaml` can move likely earnings announcement pages first and suppress noisy historical result pages. The scan cache records URL failures, skipped URLs, truncation, and next refresh timing in `official_ir_cache_audit`.

## Watchlist

Edit `watchlist.yaml`:

```yaml
symbols:
  - symbol: AAPL
    name: Apple
    tradingview: NASDAQ:AAPL
```

`tradingview` is used to build links like:

```text
https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL
```

This is an HTTPS TradingView chart link, so iOS can handle it as a universal link when the TradingView app is installed.

The generated description also includes a best-effort Apple Stocks URL scheme:

```text
stocks://?symbol=AAPL
```

Apple does not document this as a stable public integration, so TradingView HTTPS links are the reliable fallback.

Non-US earnings can use a per-symbol exchange timezone:

```yaml
symbols:
  - symbol: 005930.KS
    name: Samsung Electronics
    tradingview: KRX:005930
    timezone: Asia/Seoul
```

When `timezone` is present, earnings events for that symbol are written with that local exchange timezone, for example `DTSTART;TZID=Asia/Seoul`. US macro events remain controlled by `config.yaml` and are still limited to the configured US economic calendar countries.

ADR entries can keep the ADR as the displayed ticker while querying the original listing for earnings:

```yaml
symbols:
  - symbol: HSBC
    name: HSBC
    tradingview: NYSE:HSBC
    earnings_symbols:
      - HSBC
      - HSBA
    earnings_timezone: Europe/London
    ir_url: https://www.hsbc.com/investors/results-and-announcements
```

`symbol` and `tradingview` stay tied to the subscribed ticker you care about. `earnings_symbols` are the data-source symbols accepted for the company's earnings record. `earnings_timezone` is used for the generated event time when the earnings row comes from the underlying listing.

## Portfolio Context

The local branch reads PersonalHub Postgres directly and falls back to the read-only Dexter export if the database is unavailable:

```yaml
portfolio:
  enabled: true
  source: personalhub_postgres
  project_root: /Users/azir/PersonalHub
  fallback_path: /Users/azir/PersonalHub/exports/dexter/investment_context.json
  symbol_map_file: data/symbol_mappings.yaml
  include_holdings_in_watchlist: true
  infer_market_holidays: true
  instrument_types:
    - equity
```

When enabled, equity holdings are merged into the generated watchlist for earnings lookup, and their inferred exchanges are added to the holiday calendar. For example, `000660.KS` adds KRX holidays, while USD-listed holdings add US market holidays. Symbol mappings in `data/symbol_mappings.yaml` handle ADRs and ETF-like products; for example `XIACY` maps to Xiaomi's Hong Kong ordinary shares for issuer events, while ETF holdings such as `DRAM` and `SNXX` are used for exchange holidays and macro exposure but not corporate earnings.

The generated `status.json` and `public/dashboard.html` include:

- `portfolio_context`: holding symbols, added watchlist symbols, inferred exchanges, and holdings grouped by exchange
- `portfolio_event_impact`: per-holding related calendar events, match reasons, impact scores, and market-value weighted impact scores when PersonalHub has market values
- `macro_audit`: official source, reference period, URL, and whether the date was estimated
- `earnings_coverage`: detected earnings rows, source quality, timed rows, and missing symbols
- `confidence_counts` and `event_summary`: confidence levels, event categories, impact scores, and URLs
- `url_validation`: a capped, role-aware HTTP validation pass for `primary_event_url`, `official_source_url`, `tradingview_url`, `apple_stocks_scheme`, and source URLs
- holding themes such as semiconductor, memory, bank/rates-sensitive, auto, ADR, ETF, and local-market exposure

## Timing Rules

- The calendar window is today through the next 30 days.
- If the data source provides a precise time, the event is timed in the symbol's configured exchange timezone, or `America/New_York` when the symbol has no override.
- If a company IR press release/page provides an official release, webcast, or conference-call time, that official timing is used ahead of FMP and Nasdaq timing.
- Official IR pages and RSS links are scanned as a free fallback and cached in `.cache/official_ir_earnings.json`; the cache is ignored by git.
- If FMP does not provide before/after timing, Nasdaq earnings calendar is used automatically to enrich `盘前` / `盘后`.
- If the data source only provides before-market or after-market status, the event is timed with the symbol's exchange timezone so iOS converts it correctly for local time zones: `盘前` -> `08:00`, `盘后` -> `16:05`, `盘中` -> `12:00`.
- If timing is unknown, the title says `时间待定`.
- If an official conference-call or webcast time is found, the calendar creates a separate `财报电话会` event. When enabled, it also creates a `财报后观察` rule-based observation window.
- The observation window uses the next trading day for the symbol's exchange timezone, so a Friday after-market release is not placed on Saturday.
- `calendar.ics_filter` controls what reaches the subscribed `.ics`; dashboard and status keep the full audit trail.
- Earnings descriptions include `时间精度` so inferred session times are not confused with official minute-level release times.
- Event descriptions include `事件分类` and `可信度`.
- Each event includes a one-day-before `VALARM`.

Example title:

```text
Apple (AAPL) 财报 - 盘后
```

## Financial Events

The calendar includes:

- FOMC rate decisions
- FOMC meeting minutes
- CPI
- Nonfarm payrolls and related labor data
- PCE/Core PCE
- US market holidays
- KRX market holidays
- HKEX market holidays
- TSE, TWSE, LSE, and Euronext public-holiday based market-holiday reminders when matching holdings imply those exchanges
- US quarterly witching days
- Manual company/technology events

Only high-impact macro events are included by default. Macro release dates are loaded from official BLS/BEA schedules first and fall back to `data/official_macro_releases.yaml` when official sites are temporarily unavailable. Rule-estimated dates are used only when both official sources and the local snapshot do not cover a category. US and KRX exchange holidays are included as all-day events and are calculated locally so they do not depend on an API quota.

Each event description includes affected assets, expected direction when previous/estimate values are available, high-vs-low surprise logic, and watchlist tickers most likely to react.

Calendar event URLs point to official source pages for macro events and meetings. Earnings event URLs point to TradingView, with the Apple Stocks URL scheme placed at the top of the event description. Manual company/technology events must include their official event page URL.

Manual non-earnings events live in `events/manual_events.yaml`.

Example:

```yaml
events:
  - id: nvidia-gtc-2026
    title: NVIDIA GTC
    category: tech_event
    date: 2026-03-16
    timezone: America/New_York
    description: NVIDIA annual AI and developer conference.
    url: https://www.nvidia.com/gtc/
```

Supported manual categories include `tech_event`, `company_event`, and internal economic categories such as `opec`, `fed_speech`, `treasury_auction`, or `fomc_rate`.

These events are included in the same `earnings.ics` feed.

## Local Commands

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run tests:

```bash
python -m unittest discover -s tests
```

Generate with a fixture:

```bash
python scripts/generate_calendar.py --fixture tests/fixtures/fmp_earnings.json --skip-auto-financial-events
```

Generate with the real provider:

```bash
FMP_API_KEY=<your-api-key> python scripts/generate_calendar.py
```

The first real run may spend extra time building the official IR cache. Later runs reuse the cache until `earnings.official_ir_cache.ttl_hours` expires.

The generator also has stage-level caps:

```yaml
earnings:
  official_ir_cache:
    max_elapsed_seconds: 45
url_validation:
  max_elapsed_seconds: 20
```

If a stage hits its cap, the run degrades and records the truncation instead of blocking the workflow.

Preview a local Apple Calendar sync on macOS:

```bash
python scripts/sync_apple_calendar.py
```

This is disabled by default and only prints the events that would be written. To actually write the current `public/earnings.ics` events into a local Apple Calendar named `Stocks Calendar`, run:

```bash
python scripts/sync_apple_calendar.py --apply
```

The sync script creates/uses a separate Apple Calendar and marks events with an internal `stocks-calendar` UID in the event notes. On each apply run it replaces only previously synced events in the generated feed window, so it does not delete unrelated personal calendar items.

A disabled-by-default launchd template is available at:

```text
configs/launchd/com.azir.stocks-calendar.local.plist.example
```

It shows the intended future macOS local flow: generate the calendar, then optionally sync it into Apple Calendar. Installing or loading that plist is a manual step and is not done by this project.

The helper installer writes a LaunchAgent plist but does not load it unless `--load` is passed:

```bash
python scripts/install_local_launchd.py
```

By default the installed job only regenerates the local files. To include Apple Calendar writing in that plist, pass `--sync-apple-calendar`; to actually enable launchd, pass `--load` explicitly.

## GitHub Pages

The included workflow deploys the `public` directory to GitHub Pages.

If Pages is not already active, set:

```text
Settings -> Pages -> Source -> GitHub Actions
```

## Publish

If the repository has not been created yet:

```bash
gh auth login
gh repo create stocks-calendar --public --source=. --remote=origin --push
```

If the repository already exists:

```bash
git remote add origin git@github.com:<github-user>/stocks-calendar.git
git push -u origin main
```
